import secrets
import re
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db import get_conn, log_event, now_utc, transaction
from config import config

NAME_RE = re.compile(r'^[^\d\W][\w\s\.\-\']{1,59}$', re.UNICODE)

def normalize_name(s: str) -> str:
    return " ".join(s.strip().split())

def validate_display_name(name: str) -> bool:
    n = normalize_name(name)
    if len(n) < 2 or len(n) > 60:
        return False
    if re.match(r'^\d+$', n):
        return False
    if re.search(r'[<>\'\";\\/]', n):
        return False
    return True

def login(name: str, roll_number: str, email: str, session_token_in: str = None):
    roll = roll_number.strip().upper()
    em = email.strip().lower()
    nm = normalize_name(name)

    conn = get_conn()
    p = conn.execute("SELECT * FROM participants WHERE roll_number=?", (roll,)).fetchone()

    if not p:
        return {"error": "ACCESS_DENIED", "message": "Invalid credentials. Please check your details."}

    if not p['eligibility']:
        return {"error": "ACCESS_DENIED", "message": "Invalid credentials. Please check your details."}

    if p['email'].strip().lower() != em:
        return {"error": "ACCESS_DENIED", "message": "Invalid credentials. Please check your details."}

    # ---- PLACEHOLDER path ----
    if p['is_placeholder']:
        if p['claimed']:
            # Allow resume if token matches
            if session_token_in and p['session_token'] == session_token_in:
                return _build_session_response(p)
            return {"error": "ALREADY_CLAIMED",
                    "message": "This registration has already been claimed. Please contact the event administrator."}
        # First claim
        if not validate_display_name(nm):
            return {"error": "INVALID_NAME",
                    "message": "Please enter a valid name (2-60 chars, not purely numeric)."}
        token = secrets.token_hex(32)
        with transaction() as conn:
            rows = conn.execute(
                """UPDATE participants SET
                   name=?, claimed=1, claimed_at=?, logged_in=1, login_at=?,
                   session_token=?, status='LOGGED_IN', last_seen=?
                   WHERE roll_number=? AND claimed=0""",
                (nm, now_utc(), now_utc(), token, now_utc(), roll)
            ).rowcount
            if rows == 0:
                return {"error": "ALREADY_CLAIMED",
                        "message": "This registration has already been claimed. Please contact the event administrator."}
        log_event("PLACEHOLDER_CLAIMED", f"{nm} claimed placeholder {roll}", participant_id=p['id'])
        p2 = conn.execute("SELECT * FROM participants WHERE roll_number=?", (roll,)).fetchone()
        return _build_session_response(p2)

    # ---- EXISTING participant path ----
    stored_name = normalize_name(p['name'])
    if stored_name.lower() != nm.lower():
        return {"error": "ACCESS_DENIED", "message": "Invalid credentials. Please check your details."}

    if p['logged_in']:
        if session_token_in and p['session_token'] == session_token_in:
            return _build_session_response(p)
        return {"error": "ALREADY_REGISTERED",
                "message": "This participant is already registered in the current event. Please contact the event administrator."}

    token = secrets.token_hex(32)
    conn.execute(
        "UPDATE participants SET logged_in=1, login_at=?, session_token=?, status='LOGGED_IN', last_seen=? WHERE roll_number=?",
        (now_utc(), token, now_utc(), roll)
    )
    conn.commit()
    log_event("PARTICIPANT_LOGGED_IN", f"{nm} logged in", participant_id=p['id'])
    p2 = conn.execute("SELECT * FROM participants WHERE roll_number=?", (roll,)).fetchone()
    return _build_session_response(p2)

def _build_session_response(p):
    return {
        "ok": True,
        "session_token": p['session_token'],
        "participant": {
            "id": p['id'],
            "name": p['name'],
            "roll_number": p['roll_number'],
            "is_placeholder": bool(p['is_placeholder']),
            "claimed": bool(p['claimed']),
            "team_id": p['team_id'],
            "status": p['status'],
        }
    }

def get_by_token(token: str):
    if not token:
        return None
    conn = get_conn()
    p = conn.execute("SELECT * FROM participants WHERE session_token=?", (token,)).fetchone()
    if p:
        conn.execute("UPDATE participants SET last_seen=? WHERE id=?", (now_utc(), p['id']))
        conn.commit()
    return p

def reset_session(participant_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE participants SET session_token=NULL, logged_in=0, login_at=NULL, status='REGISTERED', last_seen=NULL WHERE id=?",
        (participant_id,)
    )
    conn.commit()
    log_event("SESSION_RESET", f"Session reset for participant {participant_id}", participant_id=participant_id)

def unclaim(participant_id: int):
    conn = get_conn()
    p = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
    if not p or not p['is_placeholder']:
        return False
    # Only if not in an active game
    conn.execute(
        """UPDATE participants SET
           name=original_name, claimed=0, claimed_at=NULL, session_token=NULL,
           logged_in=0, login_at=NULL, status='REGISTERED', last_seen=NULL, team_id=NULL
           WHERE id=?""",
        (participant_id,)
    )
    conn.commit()
    log_event("PARTICIPANT_UNCLAIMED", f"Placeholder {participant_id} unclaimed", participant_id=participant_id)
    return True

def is_online(p) -> bool:
    if not p or not p['last_seen']:
        return False
    from datetime import datetime, timezone
    try:
        ls = datetime.fromisoformat(p['last_seen'].replace('Z', '+00:00'))
        if ls.tzinfo is None:
            ls = ls.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - ls).total_seconds() < config.ONLINE_THRESHOLD_SECONDS
    except Exception:
        return False
