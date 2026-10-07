import re
import openpyxl
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn, log_event, now_utc
from config import config

PLACEHOLDER_RE = re.compile(config.PLACEHOLDER_NAME_PATTERN)

def normalize_header(h: str) -> str:
    if h is None:
        return ""
    return re.sub(r"[\s_]+", "_", str(h).strip().lower())

def normalize_roll(r) -> str:
    if r is None:
        return ""
    return str(r).strip().upper()

def normalize_email(e) -> str:
    if e is None:
        return ""
    return str(e).strip().lower()

def normalize_name(n) -> str:
    if n is None:
        return ""
    return " ".join(str(n).strip().split())

def parse_eligibility(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ('true', '1', 'yes', 'y')

def is_placeholder_name(name: str) -> bool:
    return not name or bool(PLACEHOLDER_RE.match(name.strip()))

def load_participants() -> dict:
    path = os.path.abspath(config.PARTICIPANT_CSV_PATH)
    if not os.path.exists(path):
        return {"error": f"File not found: {path}"}

    rows = []
    if path.endswith('.xlsx') or path.endswith('.xls'):
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        raw = list(ws.iter_rows(values_only=True))
        if not raw:
            return {"error": "Empty workbook"}
        headers = [normalize_header(h) for h in raw[0]]
        for row in raw[1:]:
            rows.append(dict(zip(headers, row)))
    else:
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({normalize_header(k): v for k, v in row.items()})

    # Map headers
    def find_col(d, *candidates):
        for c in candidates:
            if c in d:
                return d[c]
        return None

    conn = get_conn()
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}

    for row in rows:
        raw_name = normalize_name(find_col(row, 'name') or "")
        raw_roll = normalize_roll(find_col(row, 'roll_number', 'roll number', 'rollnumber') or "")
        raw_email = normalize_email(find_col(row, 'email') or "")
        raw_elig = find_col(row, 'eligibility', 'eligible')

        if not raw_roll:
            stats["skipped"] += 1
            continue

        eligibility = parse_eligibility(raw_elig)
        is_ph = is_placeholder_name(raw_name)
        original_name = raw_name

        existing = conn.execute(
            "SELECT id, claimed, logged_in FROM participants WHERE roll_number=?", (raw_roll,)
        ).fetchone()

        if existing:
            # Never overwrite claimed/logged_in records
            if existing['claimed'] or existing['logged_in']:
                stats["skipped"] += 1
                continue
            conn.execute(
                "UPDATE participants SET email=?, eligibility=?, is_placeholder=?, original_name=? WHERE roll_number=?",
                (raw_email, int(eligibility), int(is_ph), original_name, raw_roll)
            )
            stats["updated"] += 1
        else:
            display_name = raw_name if not is_ph else f"Participant-{raw_roll}"
            conn.execute(
                """INSERT INTO participants
                   (roll_number, name, original_name, email, eligibility, is_placeholder, claimed, status)
                   VALUES (?,?,?,?,?,?,0,'REGISTERED')""",
                (raw_roll, display_name, original_name, raw_email, int(eligibility), int(is_ph))
            )
            stats["inserted"] += 1

    conn.commit()
    log_event("PARTICIPANTS_LOADED", f"Import: {stats['inserted']} inserted, {stats['updated']} updated, {stats['skipped']} skipped")
    return stats
