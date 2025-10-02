from flask import Flask, request, jsonify, render_template
import os
import json
import pandas as pd
from werkzeug.utils import secure_filename
import re

# Assuming your scripts are named exceltojson.py and scheduler.py
from exceltojson import schedule_from_file
from scheduler import schedule, SLOT_LABELS

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# --- Main Routes (largely unchanged) ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'No selected file'}), 400
    try:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        processed_data = schedule_from_file(filepath)
        summary = {
            "Programs": len(processed_data.get("divisions", [])),
            "Faculty": len(get_unique_faculty(processed_data)),
            "Courses": len(processed_data.get("whole_class_sessions", [])) + sum(len(v) for v in processed_data.get("labs", {}).values())
        }
        json_output_path = os.path.join(app.config['UPLOAD_FOLDER'], 'processed_schedule.json')
        with open(json_output_path, 'w') as f:
            json.dump(processed_data.get('divisions', []), f, indent=2)
        return jsonify({'message': 'File processed successfully', 'summary': summary, 'processed_file': 'processed_schedule.json'})
    except Exception as e:
        return jsonify({'error': f'An error occurred: {str(e)}'}), 500

# --- NEW: The `/generate` endpoint is updated to find all output files ---
@app.route('/generate', methods=['POST'])
def generate_timetable():
    try:
        data = request.get_json()
        json_input_path = os.path.join(UPLOAD_FOLDER, data.get('processed_file'))
        if not os.path.exists(json_input_path):
            return jsonify({'error': 'Processed file not found.'}), 404

        with open(json_input_path, 'r') as f:
            divisions_data = json.load(f)

        # Clean output directory
        for f in os.listdir(OUTPUT_FOLDER):
            os.remove(os.path.join(OUTPUT_FOLDER, f))

        schedule(divisions_data, export=True, out_dir=OUTPUT_FOLDER)

        # --- Data Aggregation ---
        all_timetables = {}
        all_teacher_subjects = {}
        all_unassigned = {}

        # First, read the consolidated files if they exist
        teacher_pairs_path = os.path.join(OUTPUT_FOLDER, "teacher_subject_pairs.json")
        if os.path.exists(teacher_pairs_path):
            with open(teacher_pairs_path, 'r', encoding='utf-8') as f:
                all_teacher_subjects = json.load(f)

        unassigned_path = os.path.join(OUTPUT_FOLDER, "unassigned_sessions.json")
        if os.path.exists(unassigned_path):
            with open(unassigned_path, 'r', encoding='utf-8') as f:
                all_unassigned = json.load(f)

        # Then, process the timetable CSVs
        for fname in os.listdir(OUTPUT_FOLDER):
            if fname.startswith('timetable_') and fname.endswith('.csv'):
                full_path = os.path.join(OUTPUT_FOLDER, fname)
                base_name = os.path.splitext(fname)[0]
                division_name = normalize_division_key(base_name.replace("timetable_", ""))
                df = pd.read_csv(full_path, index_col=0)
                all_timetables[division_name] = parse_df_to_user_json_format(df)

# Normalize keys in teacherSubjects + unassignedLectures too
        all_teacher_subjects = {
            normalize_division_key(k): v for k, v in (all_teacher_subjects or {}).items()
        }
        all_unassigned = {
            normalize_division_key(k): v for k, v in (all_unassigned or {}).items()
        }
        if not all_timetables:
            return jsonify({'error': 'Scheduler ran, but no output timetables were found.'}), 500

        faculty_load = calculate_faculty_load(all_timetables)

        return jsonify({
            'message': f'{len(all_timetables)} timetables generated!',
            'timetables': all_timetables,
            'teacherSubjects': all_teacher_subjects,
            'unassignedLectures': all_unassigned,
            'facultyLoad': faculty_load
        })

    except Exception as e:
        print(f"Error in /generate: {e}")
        return jsonify({'error': f'An error occurred during generation: {str(e)}'}), 500

# --- Helper functions (largely unchanged) ---
def get_unique_faculty(data):
    faculty = set()
    for session in data.get("whole_class_sessions", []):
        if 'faculty' in session: faculty.add(session['faculty'])
    for lab_list in data.get("labs", {}).values():
        for lab_session in lab_list:
            if 'faculty' in lab_session:
                if isinstance(lab_session['faculty'], list):
                    for f in lab_session['faculty']: faculty.add(f)
                else: faculty.add(lab_session['faculty'])
    return list(faculty)
    
def parse_df_to_user_json_format(df):
    division_schedule = {}
    for day in df.columns:
        day_sessions = [
            parse_cell(df.loc[slot_label, day]) for slot_label in df.index
        ]
        division_schedule[day] = day_sessions
    return division_schedule

def parse_cell(cell):
    if pd.isna(cell) or not str(cell).strip():
        return None
    parts = str(cell).strip().split('\n')
    session_type = "theory"
    if any(kw in parts[0] for kw in ["Lab", "(P)", "Practical"]): session_type = "practical"
    if "Tut" in parts[0]: session_type = "tutorial"
    return {
        "subject": parts[0].strip() if len(parts) > 0 else "",
        "faculty": parts[1].strip() if len(parts) > 1 else "",
        "room": parts[2].strip() if len(parts) > 2 else "",
        "type": session_type,
        "batch": parts[3].strip() if len(parts) > 3 else None
    }

def calculate_faculty_load(all_timetables):
    load = {}
    for timetable in all_timetables.values():
        for sessions in timetable.values():
            for session in sessions:
                if session and (faculty_name := session.get("faculty")):
                    load.setdefault(faculty_name, {'hours': 0, 'courses': set()})
                    load[faculty_name]['hours'] += 1
                    load[faculty_name]['courses'].add(session.get("subject"))
    return {f: {**d, 'courses': list(d['courses'])} for f, d in load.items()}
def normalize_division_key(name: str) -> str:
    return name.strip().replace("_", " ").replace("-", " ").title()

if __name__ == '__main__':
    app.run(debug=True)