"""
Timetable Scheduler (v4.1 - Name-based Keys)

Implements a deterministic, priority-based, subject-by-subject scheduling algorithm.
Uses division names directly as keys, removing the need for UIDs. Schedules electives,
then Visiting Faculty, then Core Subjects with multi-pass relaxation.
"""

import logging
from collections import defaultdict
import pandas as pd
import os
import json
import argparse
from datetime import datetime
import uuid

# ---------- CONFIG ----------
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
TIME_SLOTS = [(8 + i, 9 + i) for i in range(10)] # 8 AM to 6 PM (18:00)
SLOT_LABELS = [f"{s[0]:02d}:00-{s[1]:02d}:00" for s in TIME_SLOTS]
N_SLOTS = len(TIME_SLOTS)

# --- Room Pools ---
CLASSROOMS = ["CR-302", "CR-303", "CR-304", "CR-305", "CR-306", "CR-401", "CR-402"]
LABS = ["CL-404", "CL-405", "CL-406", "CL-407", "CL-403", "CL-402"]
SINGLE_LABS_POOL = ["CC-101", "CC-102", "CC-103"]
ALL_ROOMS = CLASSROOMS + LABS + SINGLE_LABS_POOL

# --- UPDATED Pre-defined STRICT (IDEAL) Constraints as per request ---
MAX_SUBJECT_HOURS_PER_DAY = 3
MAX_FACULTY_CONTINUOUS_THEORY = 3   # Max 3 hours of continuous theory
MAX_FACULTY_CONTINUOUS_TOTAL = 5  # Max 4 hours of continuous work (theory + lab)
MAX_STUDENT_CONTINUOUS_HOURS = 5    # Max 4 continuous hours for students
FACULTY_MAX_WORKDAY_SPAN = 8
MAX_STUDENT_DAILY_HOURS = 8

# --- Logging ---
LOG_FILE = "scheduler_v4.log"
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE, mode='w'),
                              logging.StreamHandler()])

# ---------- Data Structures and Availability Trackers ----------

def get_all_faculty(divisions):
    """Initial parse to get a set of all unique faculty names."""
    all_faculty = set()
    for d in divisions:
        for s in d.get("subjects", []):
            if s.get("faculty"): all_faculty.add(s["faculty"])
            for fac in s.get("batch_faculty", []):
                if fac: all_faculty.add(fac)
    return sorted(list(all_faculty))

def init_availability(divisions):
    """Creates availability dictionaries for faculty, divisions, and rooms."""
    all_faculty_names = get_all_faculty(divisions)
    faculty_availability = {fac: {day: [None] * N_SLOTS for day in WEEKDAYS} for fac in all_faculty_names}
    # MODIFIED: Use division name as the key directly
    division_timetables = {d["division"]: {day: [None] * N_SLOTS for day in WEEKDAYS} for d in divisions}
    room_availability = {room: {day: [None] * N_SLOTS for day in WEEKDAYS} for room in ALL_ROOMS}
    return faculty_availability, division_timetables, room_availability

# ---------- Constraint Checking Engine ----------

def check_all_constraints(subject, division, faculty_name, day, slot_idx, duration, timetables, faculty_availability, max_subject_hours, max_faculty_total_continuous):
    """
    Checks a potential assignment against all predefined constraints for a SINGLE division.
    """
    # MODIFIED: Use division name to get the timetable
    div_tt_day = timetables[division['division']][day]
    fac_schedule_day = faculty_availability[faculty_name][day]
    is_lab = duration == 2

    # Student Daily Hour Limit
    if sum(1 for s in div_tt_day if s is not None) + duration > MAX_STUDENT_DAILY_HOURS:
        return False, f"Exceeds max daily hours ({MAX_STUDENT_DAILY_HOURS}h) for students"

    # Subject Daily Hour Limit
    hours_today = sum(1 for slot in div_tt_day if slot and slot['subject'] == subject['name'])
    if hours_today + duration > max_subject_hours:
        return False, f"Exceeds max subject hours ({max_subject_hours}h) for '{subject['name']}'"

    # Student Continuous Hours
    temp_student_schedule = list(div_tt_day)
    for i in range(duration): temp_student_schedule[slot_idx + i] = 'occupied'
    max_cont = 0
    current_cont = 0
    for slot in temp_student_schedule + [None]:
        if slot is not None: current_cont += 1
        else:
            max_cont = max(max_cont, current_cont)
            current_cont = 0
    if max_cont > MAX_STUDENT_CONTINUOUS_HOURS:
        return False, f"Creates >{MAX_STUDENT_CONTINUOUS_HOURS} continuous hours for students"

    # Faculty Constraints (ignore for visiting faculty as per rule)
    if not subject.get("visiting", False):
        temp_faculty_schedule = list(fac_schedule_day)
        for i in range(duration): temp_faculty_schedule[slot_idx + i] = {'type': 'Practical' if is_lab else 'Theory'}

        # Faculty Continuous Hours (Theory and Total)
        max_total_cont, current_total_cont = 0, 0
        max_theory_cont, current_theory_cont = 0, 0
        for slot in temp_faculty_schedule + [None]:
            if slot is not None:
                current_total_cont += 1
                if slot.get('type') == 'Theory':
                    current_theory_cont += 1
                else: # Reset theory counter on lab
                    max_theory_cont = max(max_theory_cont, current_theory_cont)
                    current_theory_cont = 0
            else:
                max_total_cont = max(max_total_cont, current_total_cont)
                max_theory_cont = max(max_theory_cont, current_theory_cont)
                current_total_cont, current_theory_cont = 0, 0
        
        if max_theory_cont > MAX_FACULTY_CONTINUOUS_THEORY:
            return False, f"Creates >{MAX_FACULTY_CONTINUOUS_THEORY} continuous theory hours for faculty"
        if max_total_cont > max_faculty_total_continuous:
            return False, f"Creates >{max_faculty_total_continuous} continuous total hours for faculty"

        # Faculty Workday Span
        first_lec_idx, last_lec_idx = -1, -1
        for i, slot in enumerate(temp_faculty_schedule):
            if slot is not None:
                if first_lec_idx == -1: first_lec_idx = i
                last_lec_idx = i
        if first_lec_idx != -1:
            workday_span = (last_lec_idx - first_lec_idx) + 1
            if workday_span > FACULTY_MAX_WORKDAY_SPAN:
                return False, f"Exceeds faculty workday span ({FACULTY_MAX_WORKDAY_SPAN}h)"

    return True, ""

# ---------- Scheduler Core Logic ----------

def get_floor_from_room(room_name):
    try:
        return int(room_name.split('-')[1][0])
    except (IndexError, ValueError):
        return None

def find_and_assign_slot(subject, division, hours_to_schedule, is_lab, timetables, faculty_availability, room_availability, max_subject_hours, max_faculty_continuous, is_final_pass=False):
    """Schedules a session for a SINGLE division."""
    # MODIFIED: Use division name as the key
    division_name = division['division']
    duration = 2 if is_lab else 1
    
    batch_faculties = subject.get("batch_faculty", []) if is_lab else [subject.get("faculty")]
    if not batch_faculties or not all(batch_faculties):
        logging.warning(f"Faculty not defined for {'lab' if is_lab else 'theory'} of {subject['name']} in {division['division']}. Skipping.")
        return False, [f"Faculty not defined for {'lab' if is_lab else 'theory'}"]

    room_pool = SINGLE_LABS_POOL if is_lab and len(batch_faculties) == 1 else (LABS if is_lab else CLASSROOMS)
    sessions_to_schedule_per_batch = hours_to_schedule // duration
    unassigned_for_this_subject = []

    for batch_index, faculty_name in enumerate(batch_faculties):
        for _ in range(sessions_to_schedule_per_batch):
            slot_found = False
            failure_reasons = defaultdict(list)
            pref = division.get("morning_or_evening", "morning")
            preferred_range = range(N_SLOTS - duration + 1) if pref == "morning" else range(N_SLOTS - duration, -1, -1)

            for slot_idx in preferred_range:
                for day in WEEKDAYS:
                    slot_label = f"{day}, {SLOT_LABELS[slot_idx]}"
                    
                    if any(faculty_availability[faculty_name][day][slot_idx + j] is not None for j in range(duration)):
                        if is_final_pass: failure_reasons[slot_label].append("Faculty busy")
                        continue
                    # MODIFIED: Use division name to check timetable
                    if any(timetables[division_name][day][slot_idx + j] is not None for j in range(duration)):
                        if is_final_pass: failure_reasons[slot_label].append("Division busy")
                        continue

                    dept_floors = division.get("dept_floors", [])
                    available_room = None
                    for room in room_pool:
                        room_floor = get_floor_from_room(room)
                        if dept_floors and room_floor not in dept_floors: continue
                        if all(room_availability[room][day][slot_idx + j] is None for j in range(duration)):
                            available_room = room
                            break
                    
                    if not available_room:
                        if is_final_pass: failure_reasons[slot_label].append(f"No free {'lab' if is_lab else 'classroom'}")
                        continue
                    
                    is_valid, reason = check_all_constraints(subject, division, faculty_name, day, slot_idx, duration, timetables, faculty_availability, max_subject_hours, max_faculty_total_continuous=max_faculty_continuous)
                    if not is_valid:
                        if is_final_pass: failure_reasons[slot_label].append(reason)
                        continue

                    # If all checks pass, assign the slot
                    start_label = SLOT_LABELS[slot_idx]
                    end_time = TIME_SLOTS[slot_idx + duration - 1][1]
                    slot_label_full = f"{day}, {start_label.split('-')[0]}-{end_time:02d}:00"
                    
                    assignment_info = {"subject": subject["name"], "faculty": faculty_name, "room": available_room, "type": "Practical" if is_lab else "Theory"}
                    if is_lab: assignment_info['batch'] = f"Batch {batch_index + 1}"

                    logging.info(f"Assigning {subject['name']} ({assignment_info['type']}) for {division['division']} to {slot_label_full} in {available_room}")
                    for j in range(duration):
                        # MODIFIED: Use division name for assignment
                        timetables[division_name][day][slot_idx + j] = assignment_info
                        faculty_availability[faculty_name][day][slot_idx + j] = {'division': division['division'], 'subject': subject['name'], 'room': available_room, 'type': assignment_info['type']}
                        room_availability[available_room][day][slot_idx + j] = (division['division'], subject['name'])
                    
                    slot_found = True
                    break
                if slot_found: break
            
            if not slot_found:
                logging.error(f"FAILED to schedule {'Lab' if is_lab else 'Theory'} for {subject['name']} ({faculty_name}) in {division['division']}")
                if is_final_pass:
                    logging.warning(f"--- Diagnostic Report for Unscheduled Session ---")
                    for slot, reasons in sorted(failure_reasons.items()):
                        logging.warning(f"  - Slot {slot}: {', '.join(sorted(list(set(reasons))))}")
                    logging.warning(f"--- End of Report ---")
                unassigned_for_this_subject.append(f"{'Lab' if is_lab else 'Theory/Tut'} for {subject['name']} (Faculty: {faculty_name})")
    
    return not unassigned_for_this_subject, unassigned_for_this_subject

def find_and_assign_elective_slot(subject, divisions, hours_to_schedule, timetables, faculty_availability, room_availability, is_final_pass=False):
    """Schedules a combined elective session for MULTIPLE divisions at once."""
    duration = 1 # Electives are assumed to be 1-hour theory sessions
    faculty_name = subject['faculty']
    unassigned_for_this_subject = []
    
    div_names_str = ", ".join([d['division'] for d in divisions])
    logging.info(f"Attempting to schedule elective '{subject['name']}' for: {div_names_str}")

    for _ in range(hours_to_schedule):
        slot_found = False
        failure_reasons = defaultdict(list)
        preferred_range = range(N_SLOTS - duration + 1)

        for slot_idx in preferred_range:
            for day in WEEKDAYS:
                slot_label = f"{day}, {SLOT_LABELS[slot_idx]}"
                
                if any(faculty_availability[faculty_name][day][slot_idx + j] is not None for j in range(duration)):
                    if is_final_pass: failure_reasons[slot_label].append("Faculty busy")
                    continue

                all_divs_free = True
                for d in divisions:
                    # MODIFIED: Use division name to check timetable
                    if any(timetables[d['division']][day][slot_idx + j] is not None for j in range(duration)):
                        if is_final_pass: failure_reasons[slot_label].append(f"Division {d['division']} busy")
                        all_divs_free = False
                        break
                if not all_divs_free:
                    continue

                available_room = None
                for room in CLASSROOMS:
                    if all(room_availability[room][day][slot_idx + j] is None for j in range(duration)):
                        available_room = room
                        break
                if not available_room:
                    if is_final_pass: failure_reasons[slot_label].append("No free classroom")
                    continue
                
                all_constraints_met = True
                final_reason = ""
                for d in divisions:
                    is_valid, reason = check_all_constraints(subject, d, faculty_name, day, slot_idx, duration, timetables, faculty_availability, MAX_SUBJECT_HOURS_PER_DAY, MAX_FACULTY_CONTINUOUS_TOTAL)
                    if not is_valid:
                        all_constraints_met = False
                        final_reason = f"Constraint fail for {d['division']}: {reason}"
                        break
                
                if not all_constraints_met:
                    if is_final_pass: failure_reasons[slot_label].append(final_reason)
                    continue

                start_label = SLOT_LABELS[slot_idx]
                slot_label_full = f"{day}, {start_label}"
                assignment_info = {"subject": subject["name"], "faculty": faculty_name, "room": available_room, "type": "Theory (Elective)"}
                
                logging.info(f"Assigning ELECTIVE {subject['name']} for {div_names_str} to {slot_label_full} in {available_room}")
                
                for d in divisions:
                    # MODIFIED: Use division name for assignment
                    timetables[d['division']][day][slot_idx] = assignment_info
                
                faculty_availability[faculty_name][day][slot_idx] = {'division': 'ELECTIVE', 'subject': subject['name'], 'room': available_room, 'type': 'Theory (Elective)'}
                room_availability[available_room][day][slot_idx] = ('ELECTIVE', subject['name'])
                
                slot_found = True
                break
            if slot_found: break
        
        if not slot_found:
            logging.error(f"FAILED to schedule ELECTIVE for {subject['name']} for divisions: {div_names_str}")
            if is_final_pass:
                logging.warning(f"--- Diagnostic Report for Unscheduled Elective Session ---")
                for slot, reasons in sorted(failure_reasons.items()):
                    logging.warning(f"  - Slot {slot}: {', '.join(sorted(list(set(reasons))))}")
                logging.warning(f"--- End of Report ---")
            unassigned_for_this_subject.append(f"Elective Theory for {subject['name']} (Faculty: {faculty_name})")

    return not unassigned_for_this_subject, unassigned_for_this_subject

def group_sessions(divisions):
    """Groups all sessions into electives, visiting faculty, and regular."""
    elective_groups = defaultdict(lambda: {'subject': None, 'divisions': []})
    vf_sessions = []
    regular_sessions = []

    for d in divisions:
        for s in d['subjects']:
            elective_id = s.get('elective')
            is_visiting = s.get('visiting', False)

            if elective_id and isinstance(elective_id, int) and elective_id > 0:
                group_key = (elective_id, s['faculty'])
                if elective_groups[group_key]['subject'] is None:
                    elective_groups[group_key]['subject'] = s
                elective_groups[group_key]['divisions'].append(d)
            elif is_visiting:
                theory_hours = s.get("Theory", 0) + s.get("Tut", 0)
                if theory_hours > 0: vf_sessions.append({'subject': s, 'division': d, 'hours': theory_hours, 'is_lab': False})
                practical_hours = s.get("Pra", 0)
                if practical_hours > 0: vf_sessions.append({'subject': s, 'division': d, 'hours': practical_hours, 'is_lab': True})
            else:
                theory_hours = s.get("Theory", 0) + s.get("Tut", 0)
                if theory_hours > 0: regular_sessions.append({'subject': s, 'division': d, 'hours': theory_hours, 'is_lab': False})
                practical_hours = s.get("Pra", 0)
                if practical_hours > 0: regular_sessions.append({'subject': s, 'division': d, 'hours': practical_hours, 'is_lab': True})
                
    return elective_groups, vf_sessions, regular_sessions

def schedule(divisions, export=True, out_dir="outputs"):
    # REMOVED: UID assignment is no longer necessary
    unassigned_per_division = defaultdict(list)

    faculty_availability, timetables, room_availability = init_availability(divisions)
    os.makedirs(out_dir, exist_ok=True)
    
    elective_groups, vf_sessions, regular_sessions = group_sessions(divisions)
    
    # --- Phase 1: Scheduling Electives ---
    logging.info("\n--- Phase 1: Scheduling Electives (Highest Priority) ---")
    for group_key, group_data in elective_groups.items():
        subject = group_data['subject']
        divs_involved = group_data['divisions']
        hours = subject.get("Theory", 0) + subject.get("Tut", 0)
        if hours > 0:
            success, failures = find_and_assign_elective_slot(subject, divs_involved, hours, timetables, faculty_availability, room_availability, is_final_pass=True)
            if not success:
                for d in divs_involved:
                    unassigned_per_division[d['division']].extend(failures)

    logging.info("\n--- Phase 1.5: Scheduling Elective LABS ---")
    for group_key, group_data in elective_groups.items():
        subject = group_data['subject']
        practical_hours = subject.get("Pra", 0)
        if practical_hours > 0:
            for division in group_data['divisions']:
                logging.info(f"Scheduling lab for elective '{subject['name']}' for division {division['division']}")
                success, failures = find_and_assign_slot(subject, division, practical_hours, True, timetables, faculty_availability, room_availability, MAX_SUBJECT_HOURS_PER_DAY, MAX_FACULTY_CONTINUOUS_TOTAL, is_final_pass=True)
                if not success:
                    unassigned_per_division[division['division']].extend(failures)

    # --- Phase 2: Scheduling Visiting Faculty ---
    logging.info("\n--- Phase 2: Scheduling Visiting Faculty ---")
    for item in vf_sessions:
        success, failures = find_and_assign_slot(item['subject'], item['division'], item['hours'], item['is_lab'], timetables, faculty_availability, room_availability, MAX_SUBJECT_HOURS_PER_DAY, 99)
        if not success:
            unassigned_per_division[item['division']['division']].extend(failures)

    # --- Phase 3: Regular Scheduling (Strict Pass) ---
    logging.info(f"\n--- Pass 3: Strict Scheduling (Subj/Day <= {MAX_SUBJECT_HOURS_PER_DAY}, FacCont <= {MAX_FACULTY_CONTINUOUS_TOTAL}) ---")
    failed_pass3 = []
    for item in regular_sessions:
        success, _ = find_and_assign_slot(item['subject'], item['division'], item['hours'], item['is_lab'], timetables, faculty_availability, room_availability, MAX_SUBJECT_HOURS_PER_DAY, MAX_FACULTY_CONTINUOUS_TOTAL)
        if not success: failed_pass3.append(item)

    # --- Phase 4: Regular Scheduling (Relaxed Faculty Pass) ---
    failed_pass4 = []
    if failed_pass3:
        logging.warning(f"\n--- Pass 4: Faculty Relaxation (FacCont <= {MAX_FACULTY_CONTINUOUS_TOTAL + 1}) ---")
        for item in failed_pass3:
            success, _ = find_and_assign_slot(item['subject'], item['division'], item['hours'], item['is_lab'], timetables, faculty_availability, room_availability, MAX_SUBJECT_HOURS_PER_DAY, max_faculty_continuous=MAX_FACULTY_CONTINUOUS_TOTAL + 1)
            if not success: failed_pass4.append(item)
    
    # --- Phase 5: Regular Scheduling (Relaxed Subject Pass + Diagnostics) ---
    if failed_pass4:
        logging.warning(f"\n--- Pass 5: Subject Relaxation (Subj/Day <= {MAX_SUBJECT_HOURS_PER_DAY + 1}) ---")
        for item in failed_pass4:
            success, failures = find_and_assign_slot(item['subject'], item['division'], item['hours'], item['is_lab'], timetables, faculty_availability, room_availability, max_subject_hours=MAX_SUBJECT_HOURS_PER_DAY + 1, max_faculty_continuous=MAX_FACULTY_CONTINUOUS_TOTAL + 1, is_final_pass=True)
            if not success:
                unassigned_per_division[item['division']['division']].extend(failures)

    # --- Final Data Export ---
    # MODIFIED: Simplified loop for teacher-subject pairs
    teacher_subject_pairs = defaultdict(set)
    for div_name, tt in timetables.items():
        for day in WEEKDAYS:
            for slot in tt[day]:
                if slot:
                    teacher_subject_pairs[div_name].add((slot['faculty'], slot['subject']))
    teacher_subject_pairs = {div: sorted(list(pairs)) for div, pairs in teacher_subject_pairs.items()}

    if export:
        export_data(timetables, faculty_availability, unassigned_per_division, teacher_subject_pairs, divisions, out_dir)
        
    return timetables, unassigned_per_division

# ---------- Export and Helper Functions ----------
def export_data(timetables, faculty_availability, unassigned, teacher_pairs, divisions, out_dir):
    # MODIFIED: The UID map is no longer needed.
    
    # Loop directly by division name
    for div_name, tt_data in timetables.items():
        generate_excel_for_timetable(tt_data, div_name, "timetable", out_dir, is_faculty=False)

    for fac_name, tt_data in faculty_availability.items():
        generate_excel_for_timetable(tt_data, fac_name, "faculty", out_dir, is_faculty=True)
    
    with open(os.path.join(out_dir, "division_timetables.json"), "w") as f: json.dump(timetables, f, indent=2)
    with open(os.path.join(out_dir, "faculty_timetables.json"), "w") as f: json.dump(faculty_availability, f, indent=2)
    with open(os.path.join(out_dir, "unassigned_sessions.json"), "w") as f: json.dump(unassigned, f, indent=2)
    # This is the teacher-to-subject map file you requested
    with open(os.path.join(out_dir, "teacher_subject_pairs.json"), "w") as f: json.dump(teacher_pairs, f, indent=2)

def generate_excel_for_timetable(timetable_data, name, basename, out_dir, is_faculty=False):
    df = pd.DataFrame(index=SLOT_LABELS, columns=WEEKDAYS)
    for day in WEEKDAYS:
        for i, slot in enumerate(timetable_data.get(day, [])):
            if isinstance(slot, dict):
                if is_faculty:
                    df.at[SLOT_LABELS[i], day] = f"{slot['division']}\n{slot['subject']}\n[{slot['room']}]"
                else:
                    batch_info = f"\n({slot['batch']})" if 'batch' in slot else ""
                    df.at[SLOT_LABELS[i], day] = f"{slot['subject']}{batch_info}\n({slot['faculty']})\n[{slot['room']}]"
            else:
                df.at[SLOT_LABELS[i], day] = ""
    
    safe_name = name.replace(" ", "_").replace("/", "-").replace("(", "").replace(")", "")
    fname = os.path.join(out_dir, f"{basename}_{safe_name}.xlsx")
    
    try:
        import xlsxwriter
        with pd.ExcelWriter(fname, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name='Timetable')
            workbook  = writer.book
            worksheet = writer.sheets['Timetable']
            worksheet.set_column('A:H', 25)
            cell_format = workbook.add_format({'valign': 'vcenter', 'align': 'center', 'text_wrap': True, 'border': 1})
            header_format = workbook.add_format({'bold': True, 'valign': 'vcenter', 'align': 'center', 'border': 1, 'bg_color': '#DDEBF7'})
            worksheet.conditional_format('A1:H12', {'type': 'no_blanks', 'format': cell_format})
            worksheet.conditional_format('B1:H1', {'type': 'no_blanks', 'format': header_format})
            worksheet.conditional_format('A2:A12', {'type': 'no_blanks', 'format': header_format})
        logging.info(f"Exported styled timetable for {name} -> {fname}")
    except ImportError:
        csv_fname = os.path.join(out_dir, f"{basename}_{safe_name}.csv")
        logging.warning(f"Module 'xlsxwriter' not found. Exporting as plain CSV to {csv_fname}")
        df.to_csv(csv_fname)

# ---------- Main Execution Block ----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Timetable Scheduler v4.1 (Name-based Keys)")
    parser.add_argument("--input", "-i", required=True, help="Path to the input JSON file with division data.")
    parser.add_argument("--out", "-o", default="outputs", help="Directory to save the output files.")
    args = parser.parse_args()

    try:
        with open(args.input, 'r') as f:
            divisions_data = json.load(f)
        
        # Add a placeholder 'uid' if not present, for compatibility with check_all_constraints
        for d in divisions_data:
            d['uid'] = d.get('uid', str(uuid.uuid4())[:8])

        start_time = datetime.now()
        schedule(divisions_data, export=True, out_dir=args.out)
        end_time = datetime.now()
        logging.info(f"Scheduling process finished in {end_time - start_time}.")

    except FileNotFoundError:
        logging.error(f"Error: Input file not found at {args.input}")
    except json.JSONDecodeError:
        logging.error(f"Error: Could not decode JSON from the input file. Please check its format.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)