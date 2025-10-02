"""
emailchecker_scheduler.py

Scheduler that generates timetable JSON from Excel/CSV input,
parses Remarks/Availability for visiting faculty, handles electives,
and creates sessions for theory, tutorial, and practicals.

Requirements:
  pip install pandas python-dotenv
"""

from collections import defaultdict
import pandas as pd
import os
import json
import argparse
import sys
import re
from typing import List, Dict, Optional

# ---------------- Logging ----------------
def log(msg: str, level: str = "info"):
    prefix = {"info": "INFO", "warn": "WARN", "error": "ERROR"}.get(level, "INFO")
    print(f"[{prefix}] {msg}")

# ---------------- Availability parsing ----------------
DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_SLOT_START = 8
DEFAULT_SLOT_END = 18
NUM_SLOTS = DEFAULT_SLOT_END - DEFAULT_SLOT_START

_time_range_re = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)?\s*[-–to]{1,3}\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)?")
_day_token_re = re.compile(r"\b(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b", re.I)
_combined_re = re.compile(r"(?:combined|combine|combined\s+class)\s*(?:with|:)?\s*(.+)", re.I)

def _parse_time_part(hour_str: str, minute_str: Optional[str], ampm: Optional[str]) -> int:
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if ampm:
        ampm = ampm.lower()
        if ampm.endswith('pm') and hour != 12:
            hour += 12
        if ampm.endswith('am') and hour == 12:
            hour = 0
    return hour * 60 + minute

def parse_availability(remarks: Optional[str]) -> List[Dict]:
    if not remarks or not str(remarks).strip():
        return []
    text = str(remarks)
    out = []
    parts = re.split(r",\s*(?![^()]*\))", text)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        raw = p
        if re.search(r"\b(full day|fullday|all day)\b", p, re.I):
            day_tokens = _day_token_re.findall(p)
            if day_tokens:
                for d in day_tokens:
                    out.append({"day": d[:3].lower(), "start_min": None, "end_min": None, "full_day": True, "raw": raw})
            else:
                out.append({"day": None, "start_min": None, "end_min": None, "full_day": True, "raw": raw})
            continue
        days = _day_token_re.findall(p)
        m = _time_range_re.search(p)
        if m:
            s_hour, s_min, s_amp, e_hour, e_min, e_amp = m.groups()
            start_min = _parse_time_part(s_hour, s_min, s_amp)
            end_min = _parse_time_part(e_hour, e_min, e_amp)
            if days:
                for d in days:
                    out.append({"day": d[:3].lower(), "start_min": start_min, "end_min": end_min, "full_day": False, "raw": raw})
            else:
                out.append({"day": None, "start_min": start_min, "end_min": end_min, "full_day": False, "raw": raw})
            continue
        if days:
            for d in days:
                out.append({"day": d[:3].lower(), "start_min": None, "end_min": None, "full_day": False, "raw": raw})
    return out

def availability_to_vf_timing(avail_list: List[Dict], slot_start: int = DEFAULT_SLOT_START, slot_end: int = DEFAULT_SLOT_END) -> Dict[str, List[int]]:
    mapping = {d: [0] * (slot_end - slot_start) for d in DAY_ORDER}
    if not avail_list:
        return mapping
    for entry in avail_list:
        days = [entry.get('day')] if entry.get('day') else DAY_ORDER
        start_min = entry.get('start_min')
        end_min = entry.get('end_min')
        full_day = entry.get('full_day', False)
        for d in days:
            if d is None:
                continue
            d = d.lower()
            if d not in mapping:
                continue
            if full_day or (start_min is None and end_min is None):
                mapping[d] = [1] * (slot_end - slot_start)
                continue
            for slot_idx in range(slot_end - slot_start):
                slot_hour_start = (slot_start + slot_idx) * 60
                slot_hour_end = (slot_start + slot_idx + 1) * 60
                if start_min is None or end_min is None:
                    mapping[d][slot_idx] = 1
                else:
                    if not (end_min <= slot_hour_start or start_min >= slot_hour_end):
                        mapping[d][slot_idx] = 1
    return mapping

def parse_combined_classes(remarks: Optional[str]) -> List[str]:
    if not remarks:
        return []
    m = _combined_re.search(str(remarks))
    if not m:
        return []
    tail = m.group(1).strip()
    parts = re.split(r",| and |/|;", tail)
    return [p.strip() for p in parts if p.strip()]

# ---------------- Normalizer ----------------
def normalize_divisions_from_dataframe(df: pd.DataFrame):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    col_map = {c.lower(): c for c in df.columns}
    def col(keys):
        for k in keys:
            k_lower = k.lower()
            if k_lower in col_map:
                return col_map[k_lower]
        return None

    # ---- Column mapping ----
    prog_col = col(["Program"])
    sem_col = col(["Semester"])
    div_col = col(["Division"])
    course_col = col(["Name of the Course"])
    theory_col = col(["Theory"])
    pra_col = col(["PRA"])
    tut_col = col(["Tutorial"])
    credits_col = col(["Credits"])
    batches_col = col(["No. of Batches for Practical/ Tutorial"])
    dept_col = col(["Department to which Services Requested"])
    cvf_col = col(["C/VF"])
    faculty_theory_col = col(["Name of Faculty (Theory)"])
    batch_faculty_cols = [
        "Name of Faculty Batch 1 (Lab/Tut)",
        "Name of Faculty Batch 2 (Lab/Tut)",
        "Name of Faculty Batch 3 (Lab/Tut)"
    ]
    elective_col = col(["Elective"])
    combined_col = col(["Combined Lectures"])

    grouped = {}
    raw_rows = []

    def to_int_safe(val, default=0):
        try:
            if pd.isna(val):
                return default
            return int(float(val))
        except:
            return default

    for _, r in df.iterrows():
        prog = str(r[prog_col]).strip() if prog_col and prog_col in r and not pd.isna(r[prog_col]) else ""
        sem = str(r[sem_col]).strip() if sem_col and sem_col in r and not pd.isna(r[sem_col]) else ""
        div = str(r[div_col]).strip() if div_col and div_col in r and not pd.isna(r[div_col]) else ""
        division_name = f"{prog}-{sem}{('-'+div) if div else ''}".strip("-").strip()
        if not division_name:
            division_name = "UnknownDivision"

        subject = {}
        subject["name"] = str(r[course_col]).strip() if course_col and course_col in r and not pd.isna(r[course_col]) else "Unknown"
        subject["Theory"] = to_int_safe(r[theory_col]) if theory_col and theory_col in r else 0
        subject["Pra"] = to_int_safe(r[pra_col]) if pra_col and pra_col in r else 0
        subject["Tut"] = to_int_safe(r[tut_col]) if tut_col and tut_col in r else 0
        subject["Credits"] = to_int_safe(r[credits_col]) if credits_col and credits_col in r else 0

        batch_faculty_names = [str(r[bc]).strip() for bc in batch_faculty_cols if bc in r and not pd.isna(r[bc])]
        subject["Batches"] = to_int_safe(r[batches_col], default=max(1, len(batch_faculty_names))) if batches_col and batches_col in r else max(1, len(batch_faculty_names))
        subject["batch_faculty"] = batch_faculty_names

        visiting_flag = False
        if cvf_col and cvf_col in r and not pd.isna(r[cvf_col]):
            visiting_flag = "vf" in str(r[cvf_col]).lower()
        subject["visiting"] = visiting_flag

        fac = None
        if faculty_theory_col and faculty_theory_col in r and not pd.isna(r[faculty_theory_col]):
            fac = str(r[faculty_theory_col]).strip()
        elif batch_faculty_names:
            fac = batch_faculty_names[0]
        else:
            fac = f"FAC_{subject['name']}"
        subject["faculty"] = fac

        dept = str(r[dept_col]).strip() if dept_col and dept_col in r and not pd.isna(r[dept_col]) else "Unknown"

        elective_flag = to_int_safe(r[elective_col]) if elective_col and elective_col in r else 0
        subject["elective"] = elective_flag

        combined = []
        if elective_flag == 1 and combined_col and combined_col in r and not pd.isna(r[combined_col]):
            combined = [c.strip() for c in str(r[combined_col]).split(",") if c.strip()]
        subject["combined_class"] = combined

        remarks = None
        for possible in ["vf hours", "Remarks", "Availability"]:
            if possible in r.index and not pd.isna(r[possible]):
                remarks = str(r[possible]).strip()
                break

        availability = parse_availability(remarks)
        raw_vf_timing = availability_to_vf_timing(availability)
        vf_timing = {d: slots for d, slots in raw_vf_timing.items() if any(slots)} if visiting_flag else {}
        subject["vf_timing"] = vf_timing

        key = (division_name, dept)
        if key not in grouped:
            grouped[key] = {"division": division_name, "department": dept, "morning_or_evening": "morning", "subjects": []}
        grouped[key]["subjects"].append(subject)

        raw_rows.append({
            "division": division_name,
            "department": dept,
            "subject": subject.copy(),
            "remarks": remarks,
            "availability": availability,
            "vf_timing": vf_timing,
            "combined_class": combined,
            "raw_row": {k: (None if pd.isna(v) else str(v)) for k, v in r.items()}
        })

    return list(grouped.values()), raw_rows

# ---------------- Session creation ----------------
def create_sessions(divisions: List[Dict]):
    whole_class_sessions = []
    divisional_lab_pools = defaultdict(list)

    for d in divisions:
        divname = d["division"]
        for subj in d["subjects"]:
            name = subj["name"]
            visiting = bool(subj.get("visiting", False))
            batches = int(subj.get("Batches", 1))
            theory_fac = subj.get("faculty", f"FAC_{name}")
            batch_fac_list = subj.get("batch_faculty", []) or []

            for _ in range(int(subj.get("Theory", 0))):
                whole_class_sessions.append({
                    "division": divname, "subject": name, "faculty": theory_fac, "duration": 1,
                    "is_lab": False, "is_tut": False, "visiting": visiting, "type": "theory"
                })

            for _ in range(int(subj.get("Tut", 0))):
                whole_class_sessions.append({
                    "division": divname, "subject": name, "faculty": theory_fac, "duration": 1,
                    "is_lab": False, "is_tut": True, "visiting": visiting, "type": "tutorial"
                })

            pra = int(subj.get("Pra", 0))
            if pra > 0:
                num_2h_blocks = pra // 2
                num_1h_blocks = pra % 2

                for batch_idx in range(batches):
                    fac_for_batch = batch_fac_list[batch_idx] if batch_idx < len(batch_fac_list) else theory_fac
                    for _ in range(num_2h_blocks):
                        divisional_lab_pools[divname].append({
                            "subject": name, "faculty": fac_for_batch, "duration": 2, "visiting": visiting
                        })
                    if num_1h_blocks > 0:
                        log(f"Warning: {divname} - subject {name} has odd practical hours ({pra}) -> creating {num_1h_blocks}x 1-hour practical session(s)", "warn")
                        for _ in range(num_1h_blocks):
                            divisional_lab_pools[divname].append({
                                "subject": name, "faculty": fac_for_batch, "duration": 1, "visiting": visiting
                            })

    return whole_class_sessions, divisional_lab_pools

# ---------------- Top-level functions ----------------
def schedule_from_df(df: pd.DataFrame, out_dir: str = "outputs") -> Dict:
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    divisions, raw_rows = normalize_divisions_from_dataframe(df)
    whole_class_sessions, divisional_lab_pools = create_sessions(divisions)

    with open(os.path.join(out_dir, "divisions.json"), "w", encoding="utf-8") as f:
        json.dump(divisions, f, indent=2, ensure_ascii=False)

    with open(os.path.join(out_dir, "whole_class_sessions.json"), "w", encoding="utf-8") as f:
        json.dump(whole_class_sessions, f, indent=2, ensure_ascii=False)

    lab_out = {}
    for div, labs in divisional_lab_pools.items():
        lab_out[div] = labs
        fname = os.path.join(out_dir, f"labs_{div.replace(' ', '_').replace('/', '_')}.json")
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(labs, f, indent=2, ensure_ascii=False)

    log(f"Saved outputs to {os.path.abspath(out_dir)}")
    return {"divisions": divisions, "whole_class_sessions": whole_class_sessions, "labs": lab_out}

def schedule_from_file(path: str, out_dir: str = "outputs") -> Dict:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xls", ".xlsx"):
        df = pd.read_excel(path, sheet_name=0)
    elif ext in (".csv",):
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported file extension: {ext}")
    return schedule_from_df(df, out_dir=out_dir)

def schedule_from_list(list_of_dicts: List[Dict], out_dir: str = "outputs") -> Dict:
    df = pd.DataFrame(list_of_dicts)
    return schedule_from_df(df, out_dir=out_dir)

# ---------------- CLI ----------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Create schedule sessions from an Excel/CSV sheet")
    p.add_argument("input", help="Path to input Excel/CSV file")
    p.add_argument("--out", default="outputs", help="Output directory to save JSON summaries")
    args = p.parse_args(argv)

    try:
        res = schedule_from_file(args.input, out_dir=args.out)
        log(f"Done. Divisions: {len(res.get('divisions', []))}, Whole-class sessions: {len(res.get('whole_class_sessions', []))}, Lab groups: {len(res.get('labs', {}))}")
    except Exception as e:
        log(str(e), "error")
        sys.exit(1)

if __name__ == "__main__":
    main()
