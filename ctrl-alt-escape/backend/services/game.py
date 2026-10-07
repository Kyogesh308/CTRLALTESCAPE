import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db import get_conn, log_event, now_utc, transaction, get_setting
from config import config

def load_challenges():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'data', 'challenges.json')
    with open(path) as f:
        return json.load(f)

CHALLENGES = None

def get_challenges():
    global CHALLENGES
    if CHALLENGES is None:
        CHALLENGES = {c['level']: c for c in load_challenges()}
    return CHALLENGES

def public_challenge(level: int):
    c = get_challenges().get(level)
    if not c:
        return None
    return {
        "level": level,
        "title": c['title'],
        "narrative": c['narrative'],
        "objective": c['objective'],
        "body": c['body'],
        "difficulty": c['difficulty'],
        "base_seconds": c['base_seconds'],
        "topics": c.get('topics', []),
    }

def admin_challenge(level: int):
    c = get_challenges().get(level)
    if not c:
        return None
    return {**c}

def get_game_state(current_participant):
    conn = get_conn()
    pid = current_participant['id']
    team_id = current_participant['team_id']
    phase = get_setting('event_phase', 'REGISTRATION')
    event_paused = get_setting('event_paused', '0') == '1'

    if not team_id:
        return {"phase": phase, "has_team": False}

    team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return {"phase": phase, "has_team": False}

    # Get all level states for team
    levels = conn.execute(
        "SELECT * FROM level_states WHERE team_id=? ORDER BY level_number", (team_id,)
    ).fetchall()

    level_states = []
    active_level = None

    for ls in levels:
        remaining = compute_remaining(ls, event_paused or bool(team['paused']))
        ls_data = {
            "level_number": ls['level_number'],
            "status": ls['status'],
            "allowed_seconds": ls['allowed_seconds'],
            "remaining_seconds": remaining,
            "started_at": ls['started_at'],
            "frozen_remaining": ls['frozen_remaining'],
        }
        if ls['status'] == 'ACTIVE':
            active_level = ls['level_number']
            # Add challenge content
            ch = public_challenge(ls['level_number'])
            if ch:
                ls_data['challenge'] = ch
            # Add hints
            hints = conn.execute(
                "SELECT hint_number FROM hint_events WHERE team_id=? AND level_number=?",
                (team_id, ls['level_number'])
            ).fetchall()
            c = get_challenges().get(ls['level_number'])
            ls_data['hints_used'] = [h['hint_number'] for h in hints]
            ls_data['hints'] = {}
            if c:
                for hn in ls_data['hints_used']:
                    if hn == 1:
                        ls_data['hints'][1] = c['hint1']
                    elif hn == 2:
                        ls_data['hints'][2] = c['hint2']
        elif ls['status'] == 'UNDER_REVIEW':
            ls_data['frozen_remaining'] = ls['frozen_remaining']
            # Add last submission
            sub = conn.execute(
                "SELECT * FROM submissions WHERE team_id=? AND level_number=? ORDER BY submitted_at DESC LIMIT 1",
                (team_id, ls['level_number'])
            ).fetchone()
            if sub:
                ls_data['submission'] = {
                    "answer": sub['answer'],
                    "explanation": sub['explanation'],
                    "submitted_at": sub['submitted_at'],
                    "evaluation_status": sub['evaluation_status'],
                    "admin_note": sub['admin_note'],
                }
        level_states.append(ls_data)

    # Violations count for team
    v_count = conn.execute("SELECT COUNT(*) FROM violations WHERE team_id=?", (team_id,)).fetchone()[0]

    # Check for latest review decision
    latest_decision = None
    if active_level or team['current_level']:
        lvl = active_level or team['current_level']
        sub = conn.execute(
            "SELECT evaluation_status, admin_note FROM submissions WHERE team_id=? AND level_number=? ORDER BY submitted_at DESC LIMIT 1",
            (team_id, lvl)
        ).fetchone()
        if sub and sub['evaluation_status'] in ('APPROVED', 'REJECTED', 'CLARIFY'):
            latest_decision = {"status": sub['evaluation_status'], "note": sub['admin_note']}

    return {
        "phase": phase,
        "has_team": True,
        "team": {
            "id": team['id'],
            "name": team['name'],
            "status": team['status'],
            "current_level": team['current_level'],
            "paused": bool(team['paused']) or event_paused,
            "carryover_seconds": team['carryover_seconds'],
        },
        "levels": level_states,
        "active_level": active_level,
        "violations": v_count,
        "latest_decision": latest_decision,
        "server_time": now_utc(),
        "event_paused": event_paused,
    }

def start_level(current_participant, level_number: int):
    conn = get_conn()
    team_id = current_participant['team_id']
    if not team_id:
        return {"error": "NO_TEAM", "message": "You are not part of a team."}

    team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return {"error": "NO_TEAM", "message": "Team not found."}
    if team['status'] == 'ELIMINATED':
        return {"error": "ELIMINATED", "message": "Your team has been eliminated."}
    if team['paused'] or get_setting('event_paused', '0') == '1':
        return {"error": "PAUSED", "message": "The event is currently paused."}

    ls = conn.execute(
        "SELECT * FROM level_states WHERE team_id=? AND level_number=?", (team_id, level_number)
    ).fetchone()
    if not ls:
        return {"error": "LOCKED", "message": "This level is locked."}
    if ls['status'] == 'ACTIVE':
        return {"ok": True, "message": "Level already started.", "already_active": True}
    if ls['status'] != 'UNLOCKED':
        return {"error": "NOT_UNLOCKED", "message": f"Level is {ls['status']}."}

    # Check no other level is active
    other_active = conn.execute(
        "SELECT level_number FROM level_states WHERE team_id=? AND status='ACTIVE' AND level_number != ?",
        (team_id, level_number)
    ).fetchone()
    if other_active:
        return {"error": "ANOTHER_ACTIVE", "message": "Another level is already active."}

    base = config.LEVEL_TIMES.get(level_number, 900)
    carryover = min(int(team['carryover_seconds'] or 0), config.MAX_CARRYOVER_SECONDS)
    allowed = base + carryover

    with transaction() as conn:
        conn.execute(
            "UPDATE level_states SET status='ACTIVE', started_at=?, allowed_seconds=?, carryover_seconds=?, paused_seconds=0, frozen_remaining=NULL WHERE team_id=? AND level_number=?",
            (now_utc(), allowed, carryover, team_id, level_number)
        )
        conn.execute("UPDATE teams SET current_level=? WHERE id=?", (level_number, team_id))

    log_event("LEVEL_STARTED", f"Team {team['name']} started Level {level_number}", team_id=team_id, level_number=level_number)
    return {"ok": True, "allowed_seconds": allowed, "carryover_seconds": carryover}

def request_hint(current_participant, level_number: int, hint_number: int):
    conn = get_conn()
    team_id = current_participant['team_id']
    if not team_id:
        return {"error": "NO_TEAM", "message": "Not in a team."}

    ls = conn.execute(
        "SELECT * FROM level_states WHERE team_id=? AND level_number=?", (team_id, level_number)
    ).fetchone()
    if not ls or ls['status'] != 'ACTIVE':
        return {"error": "NOT_ACTIVE", "message": "Level is not active."}
    if hint_number not in (1, 2):
        return {"error": "INVALID", "message": "Hint number must be 1 or 2."}

    c = get_challenges().get(level_number)
    if not c:
        return {"error": "NOT_FOUND", "message": "Challenge not found."}

    hint_text = c['hint1'] if hint_number == 1 else c['hint2']

    try:
        conn.execute(
            "INSERT INTO hint_events (team_id, level_number, hint_number, requested_by) VALUES (?,?,?,?)",
            (team_id, level_number, hint_number, current_participant['id'])
        )
        conn.commit()
        log_event("HINT_REQUESTED", f"Team hint {hint_number} for L{level_number}", team_id=team_id, level_number=level_number)
    except Exception:
        pass  # Already requested (UNIQUE constraint)

    return {"ok": True, "hint_text": hint_text}

def submit_answer(current_participant, level_number: int, answer: str, explanation: str):
    conn = get_conn()
    team_id = current_participant['team_id']
    if not team_id:
        return {"error": "NO_TEAM", "message": "Not in a team."}

    team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if team['paused'] or get_setting('event_paused', '0') == '1':
        return {"error": "PAUSED", "message": "Event is paused."}

    ls = conn.execute("SELECT * FROM level_states WHERE team_id=? AND level_number=?", (team_id, level_number)).fetchone()
    if not ls or ls['status'] != 'ACTIVE':
        return {"error": "NOT_ACTIVE", "message": "This level is not active or already submitted."}

    remaining = compute_remaining(ls, False)
    if remaining <= 0:
        return {"error": "TIMEOUT", "message": "Time has expired for this level."}

    if not answer.strip():
        return {"error": "EMPTY", "message": "Answer cannot be empty."}
    if not explanation.strip():
        return {"error": "EMPTY", "message": "Explanation cannot be empty."}

    elapsed = ls['allowed_seconds'] - remaining
    hints_used = conn.execute(
        "SELECT COUNT(*) FROM hint_events WHERE team_id=? AND level_number=?", (team_id, level_number)
    ).fetchone()[0]

    # Check against expected answer
    c = get_challenges().get(level_number)
    matches = 0
    if c:
        ans_lower = answer.strip().lower()
        expected_lower = c['expected_answer'].strip().lower()
        variants_lower = [v.strip().lower() for v in c.get('accepted_variants', [])]
        if ans_lower == expected_lower or ans_lower in variants_lower:
            matches = 1

    with transaction() as conn:
        # Check no pending sub
        pending = conn.execute(
            "SELECT id FROM submissions WHERE team_id=? AND level_number=? AND evaluation_status='PENDING'",
            (team_id, level_number)
        ).fetchone()
        if pending:
            return {"error": "ALREADY_SUBMITTED", "message": "Already submitted, awaiting review."}

        conn.execute(
            """INSERT INTO submissions
               (team_id, level_number, answer, explanation, submitted_by, elapsed, remaining, hints_used, evaluation_status, matches_expected)
               VALUES (?,?,?,?,?,?,?,?,'PENDING',?)""",
            (team_id, level_number, answer.strip(), explanation.strip(),
             current_participant['id'], elapsed, remaining, hints_used, matches)
        )
        # Freeze timer
        conn.execute(
            "UPDATE level_states SET status='UNDER_REVIEW', submitted_at=?, frozen_remaining=? WHERE team_id=? AND level_number=?",
            (now_utc(), remaining, team_id, level_number)
        )

    log_event("ANSWER_SUBMITTED", f"Team {team['name']} submitted L{level_number}", team_id=team_id, level_number=level_number)
    return {"ok": True, "message": "Answer transmitted. Awaiting control center evaluation."}

def approve_submission(submission_id: int, admin_note: str = None):
    conn = get_conn()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        return {"error": "NOT_FOUND"}

    team_id = sub['team_id']
    level_number = sub['level_number']
    remaining = sub['remaining'] or 0
    carryover = min(remaining, config.MAX_CARRYOVER_SECONDS)

    with transaction() as conn:
        conn.execute(
            "UPDATE submissions SET evaluation_status='APPROVED', reviewed_at=?, admin_note=? WHERE id=?",
            (now_utc(), admin_note, submission_id)
        )
        conn.execute(
            "UPDATE level_states SET status='COMPLETED', completed_at=? WHERE team_id=? AND level_number=?",
            (now_utc(), team_id, level_number)
        )
        conn.execute("UPDATE teams SET carryover_seconds=? WHERE id=?", (carryover, team_id))

        if level_number < 6:
            # Unlock next
            conn.execute(
                "UPDATE level_states SET status='UNLOCKED' WHERE team_id=? AND level_number=?",
                (team_id, level_number + 1)
            )
            log_event("LEVEL_UNLOCKED", f"L{level_number + 1} unlocked for team {team_id}", team_id=team_id, level_number=level_number + 1)
        else:
            # Team completed!
            conn.execute("UPDATE teams SET status='COMPLETED', completed_at=? WHERE id=?", (now_utc(), team_id))
            conn.execute("UPDATE participants SET status='COMPLETED' WHERE team_id=?", (team_id,))
            log_event("TEAM_COMPLETED", f"Team {team_id} completed all levels!", team_id=team_id)

    log_event("LEVEL_APPROVED", f"L{level_number} approved for team {team_id}, carry-over={carryover}s", team_id=team_id, level_number=level_number)
    return {"ok": True, "carryover": carryover}

def reject_submission(submission_id: int, admin_note: str = None):
    conn = get_conn()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        return {"error": "NOT_FOUND"}

    team_id = sub['team_id']
    level_number = sub['level_number']
    frozen = sub['remaining'] or 0

    # Shift started_at to account for review time
    ls = conn.execute("SELECT * FROM level_states WHERE team_id=? AND level_number=?", (team_id, level_number)).fetchone()
    if ls and ls['started_at']:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        sub_time = datetime.fromisoformat(sub['submitted_at'].replace('Z', '+00:00'))
        if sub_time.tzinfo is None:
            sub_time = sub_time.replace(tzinfo=timezone.utc)
        review_duration = (now - sub_time).total_seconds()

        started = datetime.fromisoformat(ls['started_at'].replace('Z', '+00:00'))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        new_started = started + timedelta(seconds=review_duration)

        with transaction() as conn:
            conn.execute(
                "UPDATE submissions SET evaluation_status='REJECTED', reviewed_at=?, admin_note=? WHERE id=?",
                (now_utc(), admin_note, submission_id)
            )
            conn.execute(
                "UPDATE level_states SET status='ACTIVE', submitted_at=NULL, frozen_remaining=NULL, started_at=? WHERE team_id=? AND level_number=?",
                (new_started.isoformat(), team_id, level_number)
            )
    else:
        with transaction() as conn:
            conn.execute(
                "UPDATE submissions SET evaluation_status='REJECTED', reviewed_at=?, admin_note=? WHERE id=?",
                (now_utc(), admin_note, submission_id)
            )
            conn.execute(
                "UPDATE level_states SET status='ACTIVE', submitted_at=NULL, frozen_remaining=NULL WHERE team_id=? AND level_number=?",
                (team_id, level_number)
            )

    log_event("LEVEL_REJECTED", f"L{level_number} rejected for team {team_id}", team_id=team_id, level_number=level_number)
    return {"ok": True}

def record_violation(current_participant, level_number: int, type: str):
    conn = get_conn()
    team_id = current_participant['team_id']
    if not team_id:
        return {"error": "NO_TEAM"}

    # Only count if level is ACTIVE
    ls = conn.execute("SELECT status FROM level_states WHERE team_id=? AND level_number=?", (team_id, level_number)).fetchone()
    if not ls or ls['status'] != 'ACTIVE':
        return {"ok": True, "ignored": True}
    if get_setting('event_paused', '0') == '1':
        return {"ok": True, "ignored": True}

    count = conn.execute("SELECT COUNT(*) FROM violations WHERE team_id=?", (team_id,)).fetchone()[0]
    warning_number = count + 1

    with transaction() as conn:
        conn.execute(
            "INSERT INTO violations (team_id, participant_id, level_number, type, warning_number) VALUES (?,?,?,?,?)",
            (team_id, current_participant['id'], level_number, type, warning_number)
        )
        if warning_number >= config.MAX_WARNINGS:
            conn.execute("UPDATE teams SET status='ELIMINATED' WHERE id=?", (team_id,))
            conn.execute("UPDATE participants SET status='ELIMINATED' WHERE team_id=?", (team_id,))
            log_event("TEAM_ELIMINATED", f"Team {team_id} eliminated after {warning_number} violations", team_id=team_id)

    log_event("TAB_SWITCH_DETECTED", f"Violation {warning_number}/{config.MAX_WARNINGS} for team {team_id}", team_id=team_id, level_number=level_number)
    return {"ok": True, "warning_number": warning_number, "max_warnings": config.MAX_WARNINGS, "eliminated": warning_number >= config.MAX_WARNINGS}

def compute_remaining(ls, is_paused: bool = False) -> int:
    """Compute remaining seconds server-side. Never store a ticking counter."""
    if ls['status'] in ('COMPLETED', 'LOCKED', 'UNLOCKED', 'TIMEOUT'):
        return ls['allowed_seconds'] or 0
    if ls['status'] == 'UNDER_REVIEW':
        return ls['frozen_remaining'] or 0
    if not ls['started_at']:
        return ls['allowed_seconds'] or 0

    from datetime import datetime, timezone
    try:
        started = datetime.fromisoformat(ls['started_at'].replace('Z', '+00:00'))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed = (now - started).total_seconds() - (ls['paused_seconds'] or 0)
        if is_paused:
            # Don't count current pause duration
            pass
        remaining = (ls['allowed_seconds'] or 0) - elapsed
        return max(0, int(remaining))
    except Exception:
        return 0

def check_timeouts():
    """Background task: mark ACTIVE levels that have timed out."""
    conn = get_conn()
    active = conn.execute(
        "SELECT ls.*, t.paused FROM level_states ls JOIN teams t ON ls.team_id = t.id WHERE ls.status='ACTIVE'"
    ).fetchall()

    event_paused = get_setting('event_paused', '0') == '1'
    for ls in active:
        is_paused = bool(ls['paused']) or event_paused
        remaining = compute_remaining(ls, is_paused)
        if remaining <= 0 and not is_paused:
            conn.execute(
                "UPDATE level_states SET status='TIMEOUT', frozen_remaining=0 WHERE team_id=? AND level_number=?",
                (ls['team_id'], ls['level_number'])
            )
            conn.commit()
            log_event("LEVEL_TIMEOUT", f"Level {ls['level_number']} timed out for team {ls['team_id']}",
                      team_id=ls['team_id'], level_number=ls['level_number'])
