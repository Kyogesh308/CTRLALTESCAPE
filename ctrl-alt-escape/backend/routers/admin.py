import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import secrets

from db import get_conn, get_setting, set_setting, log_event, now_utc, transaction
from config import config
from services.game import approve_submission, reject_submission, compute_remaining, check_timeouts
from services.importer import load_participants
from services.participants import is_online, reset_session, unclaim

router = APIRouter()

# In-memory admin sessions
_admin_sessions: dict = {}

def require_admin(x_admin: Optional[str] = Header(None, alias="X-Admin-Token")):
    if not x_admin or _admin_sessions.get(x_admin) != "admin":
        raise HTTPException(401, detail={"error": "UNAUTHORIZED", "message": "Admin access required."})
    return x_admin

# ---- ADMIN AUTH ----

class AdminLoginBody(BaseModel):
    username: str
    password: str

@router.post("/admin/login")
async def admin_login(body: AdminLoginBody):
    if not (secrets.compare_digest(body.username.strip(), config.ADMIN_USERNAME) and
            secrets.compare_digest(body.password.strip(), config.ADMIN_PASSWORD)):
        raise HTTPException(401, detail={"error": "UNAUTHORIZED", "message": "Invalid admin credentials."})
    token = secrets.token_hex(32)
    _admin_sessions[token] = "admin"
    return {"ok": True, "admin_token": token}

@router.post("/admin/logout")
async def admin_logout(tok: str = Depends(require_admin)):
    _admin_sessions.pop(tok, None)
    return {"ok": True}

# ---- OVERVIEW ----

@router.get("/admin/overview")
async def admin_overview(_: str = Depends(require_admin)):
    conn = get_conn()
    phase = get_setting('event_phase', 'REGISTRATION')
    event_paused = get_setting('event_paused', '0') == '1'

    total_p = conn.execute("SELECT COUNT(*) FROM participants WHERE eligibility=1").fetchone()[0]
    claimed = conn.execute("SELECT COUNT(*) FROM participants WHERE is_placeholder=1 AND claimed=1 AND eligibility=1").fetchone()[0]
    unclaimed_ph = conn.execute("SELECT COUNT(*) FROM participants WHERE is_placeholder=1 AND claimed=0 AND eligibility=1").fetchone()[0]
    logged_in = conn.execute("SELECT COUNT(*) FROM participants WHERE logged_in=1 AND eligibility=1").fetchone()[0]
    no_team = conn.execute("SELECT COUNT(*) FROM participants WHERE logged_in=1 AND team_id IS NULL AND eligibility=1").fetchone()[0]

    total_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    forming = conn.execute("SELECT COUNT(*) FROM teams WHERE status='FORMING'").fetchone()[0]
    ready = conn.execute("SELECT COUNT(*) FROM teams WHERE status='READY'").fetchone()[0]
    playing = conn.execute("SELECT COUNT(*) FROM teams WHERE status='PLAYING'").fetchone()[0]
    completed = conn.execute("SELECT COUNT(*) FROM teams WHERE status='COMPLETED'").fetchone()[0]
    eliminated = conn.execute("SELECT COUNT(*) FROM teams WHERE status='ELIMINATED'").fetchone()[0]

    pending_subs = conn.execute("SELECT COUNT(*) FROM submissions WHERE evaluation_status='PENDING'").fetchone()[0]
    pending_reqs = conn.execute("SELECT COUNT(*) FROM team_requests WHERE status='PENDING'").fetchone()[0]

    # Alerts
    alerts = []
    if pending_subs > 0:
        alerts.append({"type": "PENDING_SUBMISSION", "count": pending_subs, "message": f"{pending_subs} submission(s) awaiting review"})
    timeouts = conn.execute("SELECT COUNT(*) FROM level_states WHERE status='TIMEOUT'").fetchone()[0]
    if timeouts > 0:
        alerts.append({"type": "TIMEOUT", "count": timeouts, "message": f"{timeouts} level(s) timed out"})
    viol_teams = conn.execute("SELECT DISTINCT team_id FROM violations WHERE warning_number >= 2").fetchall()
    if viol_teams:
        alerts.append({"type": "VIOLATIONS", "count": len(viol_teams), "message": f"{len(viol_teams)} team(s) have multiple violations"})

    return {
        "phase": phase,
        "event_paused": event_paused,
        "participants": {
            "total": total_p, "claimed": claimed, "unclaimed_placeholders": unclaimed_ph,
            "logged_in": logged_in, "no_team": no_team,
        },
        "teams": {
            "total": total_teams, "forming": forming, "ready": ready,
            "playing": playing, "completed": completed, "eliminated": eliminated,
        },
        "pending_submissions": pending_subs,
        "pending_requests": pending_reqs,
        "alerts": alerts,
    }

# ---- PARTICIPANTS ----

@router.get("/admin/participants")
async def list_participants(search: str = "", _: str = Depends(require_admin)):
    conn = get_conn()
    q = f"%{search}%"
    rows = conn.execute("""
        SELECT p.*, t.name as team_name
        FROM participants p
        LEFT JOIN teams t ON p.team_id = t.id
        WHERE (p.name LIKE ? OR p.roll_number LIKE ? OR p.email LIKE ?)
        ORDER BY p.is_placeholder, p.name
    """, (q, q, q)).fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r['id'],
            "name": r['name'],
            "roll_number": r['roll_number'],
            "email": r['email'],
            "is_placeholder": bool(r['is_placeholder']),
            "claimed": bool(r['claimed']),
            "claimed_at": r['claimed_at'],
            "logged_in": bool(r['logged_in']),
            "login_at": r['login_at'],
            "team_id": r['team_id'],
            "team_name": r['team_name'],
            "status": r['status'],
            "online": is_online(r),
            "eligibility": bool(r['eligibility']),
        })
    return {"participants": result}

@router.post("/admin/participants/{pid}/reset-session")
async def admin_reset_session(pid: int, _: str = Depends(require_admin)):
    reset_session(pid)
    return {"ok": True}

@router.post("/admin/participants/{pid}/unclaim")
async def admin_unclaim(pid: int, _: str = Depends(require_admin)):
    ok = unclaim(pid)
    if not ok:
        raise HTTPException(400, detail={"error": "FAILED", "message": "Cannot unclaim this participant."})
    return {"ok": True}

# ---- TEAMS ----

@router.get("/admin/teams")
async def list_teams(_: str = Depends(require_admin)):
    check_timeouts()
    conn = get_conn()
    teams = conn.execute("SELECT * FROM teams ORDER BY created_at").fetchall()
    result = []
    for t in teams:
        members = conn.execute(
            "SELECT id, name, roll_number, last_seen, status FROM participants WHERE team_id=?", (t['id'],)
        ).fetchall()
        levels = conn.execute(
            "SELECT * FROM level_states WHERE team_id=? ORDER BY level_number", (t['id'],)
        ).fetchall()
        violations = conn.execute("SELECT COUNT(*) FROM violations WHERE team_id=?", (t['id'],)).fetchone()[0]
        pending_sub = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE team_id=? AND evaluation_status='PENDING'", (t['id'],)
        ).fetchone()[0]

        level_info = []
        for ls in levels:
            remaining = compute_remaining(ls, get_setting('event_paused','0')=='1' or bool(t['paused']))
            level_info.append({
                "level_number": ls['level_number'],
                "status": ls['status'],
                "remaining_seconds": remaining,
                "allowed_seconds": ls['allowed_seconds'],
            })

        result.append({
            "id": t['id'],
            "name": t['name'],
            "leader_id": t['leader_id'],
            "status": t['status'],
            "roster_confirmed": bool(t['roster_confirmed']),
            "current_level": t['current_level'],
            "paused": bool(t['paused']),
            "members": [{"id": m['id'], "name": m['name'], "roll_number": m['roll_number'],
                         "online": is_online(m), "status": m['status']} for m in members],
            "size": len(members),
            "violations": violations,
            "pending_submission": pending_sub > 0,
            "levels": level_info,
            "created_at": t['created_at'],
        })
    return {"teams": result}

@router.get("/admin/teams/{team_id}")
async def get_team(team_id: str, _: str = Depends(require_admin)):
    conn = get_conn()
    t = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not t:
        raise HTTPException(404)
    members = conn.execute("SELECT * FROM participants WHERE team_id=?", (team_id,)).fetchall()
    levels = conn.execute("SELECT * FROM level_states WHERE team_id=? ORDER BY level_number", (team_id,)).fetchall()
    subs = conn.execute("SELECT * FROM submissions WHERE team_id=? ORDER BY submitted_at DESC", (team_id,)).fetchall()
    viols = conn.execute("SELECT * FROM violations WHERE team_id=? ORDER BY timestamp DESC", (team_id,)).fetchall()
    actions = conn.execute("SELECT * FROM admin_actions WHERE team_id=? ORDER BY timestamp DESC LIMIT 20", (team_id,)).fetchall()

    return {
        "team": dict(t),
        "members": [dict(m) for m in members],
        "levels": [dict(ls) for ls in levels],
        "submissions": [dict(s) for s in subs],
        "violations": [dict(v) for v in viols],
        "admin_actions": [dict(a) for a in actions],
    }

# ---- TEAM REQUESTS ----

@router.get("/admin/team-requests")
async def list_requests(status: str = "", _: str = Depends(require_admin)):
    conn = get_conn()
    if status:
        rows = conn.execute(
            """SELECT tr.*, ps.name as sender_name, ps.roll_number as sender_roll,
                      pr.name as receiver_name, pr.roll_number as receiver_roll
               FROM team_requests tr
               JOIN participants ps ON tr.sender_id = ps.id
               JOIN participants pr ON tr.receiver_id = pr.id
               WHERE tr.status=? ORDER BY tr.created_at DESC""", (status.upper(),)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT tr.*, ps.name as sender_name, ps.roll_number as sender_roll,
                      pr.name as receiver_name, pr.roll_number as receiver_roll
               FROM team_requests tr
               JOIN participants ps ON tr.sender_id = ps.id
               JOIN participants pr ON tr.receiver_id = pr.id
               ORDER BY tr.created_at DESC LIMIT 100"""
        ).fetchall()
    return {"requests": [dict(r) for r in rows]}

@router.post("/admin/team-requests/{req_id}/cancel")
async def admin_cancel_request(req_id: int, _: str = Depends(require_admin)):
    conn = get_conn()
    conn.execute(
        "UPDATE team_requests SET status='CANCELLED', cancel_reason='admin_cancelled', responded_at=? WHERE id=? AND status='PENDING'",
        (now_utc(), req_id)
    )
    conn.commit()
    return {"ok": True}

# ---- SUBMISSIONS ----

@router.get("/admin/submissions")
async def list_submissions(status: str = "PENDING", _: str = Depends(require_admin)):
    conn = get_conn()
    q = status.upper()
    rows = conn.execute(
        """SELECT s.*, t.name as team_name, p.name as submitter_name
           FROM submissions s
           JOIN teams t ON s.team_id = t.id
           LEFT JOIN participants p ON s.submitted_by = p.id
           WHERE s.evaluation_status=?
           ORDER BY s.submitted_at DESC""", (q,)
    ).fetchall()
    # Include expected answer for admin
    from services.game import admin_challenge
    result = []
    for r in rows:
        ch = admin_challenge(r['level_number'])
        hints = conn.execute(
            "SELECT hint_number FROM hint_events WHERE team_id=? AND level_number=?",
            (r['team_id'], r['level_number'])
        ).fetchall()
        result.append({
            **dict(r),
            "expected_answer": ch['expected_answer'] if ch else "N/A",
            "hints_used_list": [h['hint_number'] for h in hints],
        })
    return {"submissions": result}

class ReviewBody(BaseModel):
    admin_note: Optional[str] = None

@router.post("/admin/submissions/{sub_id}/approve")
async def approve(sub_id: int, body: ReviewBody, _: str = Depends(require_admin)):
    result = approve_submission(sub_id, body.admin_note)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

@router.post("/admin/submissions/{sub_id}/reject")
async def reject(sub_id: int, body: ReviewBody, _: str = Depends(require_admin)):
    result = reject_submission(sub_id, body.admin_note)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

# ---- EVENT PHASE CONTROLS ----

@router.post("/admin/event/open-formation")
async def open_formation(_: str = Depends(require_admin)):
    set_setting('event_phase', 'TEAM_FORMATION')
    log_event("FORMATION_OPENED", "Admin opened team formation")
    return {"ok": True, "phase": "TEAM_FORMATION"}

@router.post("/admin/event/lock-formation")
async def lock_formation(_: str = Depends(require_admin)):
    set_setting('event_phase', 'FORMATION_LOCKED')
    log_event("FORMATION_LOCKED", "Admin locked team formation")
    return {"ok": True, "phase": "FORMATION_LOCKED"}

@router.post("/admin/event/unlock-formation")
async def unlock_formation(_: str = Depends(require_admin)):
    set_setting('event_phase', 'TEAM_FORMATION')
    return {"ok": True, "phase": "TEAM_FORMATION"}

@router.post("/admin/event/start")
async def start_event(_: str = Depends(require_admin)):
    conn = get_conn()
    set_setting('event_phase', 'GAME_STARTED')
    set_setting('event_started_at', now_utc())

    # Initialize level states for all ready teams
    teams = conn.execute(
        "SELECT id FROM teams WHERE status IN ('READY','FORMING') AND id IN (SELECT team_id FROM participants GROUP BY team_id HAVING COUNT(*) >= ?)",
        (config.MIN_TEAM_SIZE,)
    ).fetchall()

    warnings = []
    for team in teams:
        tid = team['id']
        conn.execute("UPDATE teams SET status='PLAYING', game_started_at=? WHERE id=?", (now_utc(), tid))
        conn.execute("UPDATE participants SET status='PLAYING' WHERE team_id=?", (tid,))
        # Create level states
        for level in range(1, 7):
            status = 'UNLOCKED' if level == 1 else 'LOCKED'
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO level_states (team_id, level_number, status, allowed_seconds) VALUES (?,?,?,?)",
                    (tid, level, status, config.LEVEL_TIMES.get(level, 900))
                )
            except Exception:
                pass
        conn.commit()

    # Warn about teams below minimum
    small = conn.execute(
        "SELECT t.id, t.name, COUNT(p.id) as size FROM teams t LEFT JOIN participants p ON p.team_id=t.id GROUP BY t.id HAVING size < ?",
        (config.MIN_TEAM_SIZE,)
    ).fetchall()
    for s in small:
        warnings.append(f"Team {s['name']} has only {s['size']} members (min {config.MIN_TEAM_SIZE})")

    no_team = conn.execute("SELECT COUNT(*) FROM participants WHERE logged_in=1 AND team_id IS NULL").fetchone()[0]
    if no_team > 0:
        warnings.append(f"{no_team} participant(s) are not in any team")

    log_event("EVENT_STARTED", f"Event started. {len(teams)} teams playing.")
    return {"ok": True, "teams_started": len(teams), "warnings": warnings}

@router.post("/admin/event/pause")
async def pause_event(_: str = Depends(require_admin)):
    conn = get_conn()
    set_setting('event_paused', '1')
    conn.execute("UPDATE teams SET paused=1, paused_at=? WHERE status='PLAYING'", (now_utc(),))
    conn.commit()
    log_event("EVENT_PAUSED", "Admin paused the event")
    return {"ok": True}

@router.post("/admin/event/resume")
async def resume_event(_: str = Depends(require_admin)):
    conn = get_conn()
    set_setting('event_paused', '0')
    # Add pause gap to paused_seconds for all active levels
    from datetime import datetime, timezone
    teams = conn.execute("SELECT id, paused_at FROM teams WHERE paused=1").fetchall()
    for team in teams:
        if team['paused_at']:
            try:
                paused_at = datetime.fromisoformat(team['paused_at'].replace('Z','+00:00'))
                if paused_at.tzinfo is None:
                    paused_at = paused_at.replace(tzinfo=timezone.utc)
                gap = (datetime.now(timezone.utc) - paused_at).total_seconds()
                conn.execute(
                    "UPDATE level_states SET paused_seconds = paused_seconds + ? WHERE team_id=? AND status='ACTIVE'",
                    (int(gap), team['id'])
                )
            except Exception:
                pass
        conn.execute("UPDATE teams SET paused=0, paused_at=NULL WHERE id=?", (team['id'],))
    conn.commit()
    log_event("EVENT_RESUMED", "Admin resumed the event")
    return {"ok": True}

# ---- TEAM CONTROLS ----

class TeamControlBody(BaseModel):
    reason: Optional[str] = None
    seconds: Optional[int] = None
    level_number: Optional[int] = None

@router.post("/admin/teams/{team_id}/pause")
async def pause_team(team_id: str, body: TeamControlBody, _: str = Depends(require_admin)):
    conn = get_conn()
    conn.execute("UPDATE teams SET paused=1, paused_at=? WHERE id=?", (now_utc(), team_id))
    conn.commit()
    log_event("TEAM_PAUSED", f"Admin paused team {team_id}. Reason: {body.reason}", team_id=team_id)
    return {"ok": True}

@router.post("/admin/teams/{team_id}/resume")
async def resume_team(team_id: str, body: TeamControlBody, _: str = Depends(require_admin)):
    conn = get_conn()
    team = conn.execute("SELECT paused_at FROM teams WHERE id=?", (team_id,)).fetchone()
    if team and team['paused_at']:
        from datetime import datetime, timezone
        try:
            paused_at = datetime.fromisoformat(team['paused_at'].replace('Z','+00:00'))
            if paused_at.tzinfo is None:
                paused_at = paused_at.replace(tzinfo=timezone.utc)
            gap = (datetime.now(timezone.utc) - paused_at).total_seconds()
            conn.execute(
                "UPDATE level_states SET paused_seconds = paused_seconds + ? WHERE team_id=? AND status='ACTIVE'",
                (int(gap), team_id)
            )
        except Exception:
            pass
    conn.execute("UPDATE teams SET paused=0, paused_at=NULL WHERE id=?", (team_id,))
    conn.commit()
    log_event("TEAM_RESUMED", f"Admin resumed team {team_id}", team_id=team_id)
    return {"ok": True}

@router.post("/admin/teams/{team_id}/add-time")
async def add_time(team_id: str, body: TeamControlBody, _: str = Depends(require_admin)):
    conn = get_conn()
    secs = body.seconds or 300
    conn.execute(
        "UPDATE level_states SET adjustment_seconds = adjustment_seconds + ?, allowed_seconds = allowed_seconds + ? WHERE team_id=? AND status='ACTIVE'",
        (secs, secs, team_id)
    )
    conn.commit()
    log_event("ADMIN_TIME_ADJUSTED", f"Added {secs}s to team {team_id}. Reason: {body.reason}", team_id=team_id)
    return {"ok": True}

@router.post("/admin/teams/{team_id}/eliminate")
async def eliminate_team(team_id: str, body: TeamControlBody, _: str = Depends(require_admin)):
    conn = get_conn()
    conn.execute("UPDATE teams SET status='ELIMINATED' WHERE id=?", (team_id,))
    conn.execute("UPDATE participants SET status='ELIMINATED' WHERE team_id=?", (team_id,))
    conn.commit()
    log_event("TEAM_ELIMINATED", f"Admin eliminated team {team_id}. Reason: {body.reason}", team_id=team_id)
    return {"ok": True}

@router.post("/admin/teams/{team_id}/restore")
async def restore_team(team_id: str, body: TeamControlBody, _: str = Depends(require_admin)):
    conn = get_conn()
    conn.execute("UPDATE teams SET status='PLAYING' WHERE id=?", (team_id,))
    conn.execute("UPDATE participants SET status='PLAYING' WHERE team_id=?", (team_id,))
    # Reset violations
    conn.execute("DELETE FROM violations WHERE team_id=?", (team_id,))
    conn.commit()
    log_event("TEAM_RESTORED", f"Admin restored team {team_id}. Reason: {body.reason}", team_id=team_id)
    return {"ok": True}

@router.post("/admin/teams/{team_id}/unlock-level")
async def unlock_level(team_id: str, body: TeamControlBody, _: str = Depends(require_admin)):
    level = body.level_number
    conn = get_conn()
    existing = conn.execute("SELECT id FROM level_states WHERE team_id=? AND level_number=?", (team_id, level)).fetchone()
    if existing:
        conn.execute("UPDATE level_states SET status='UNLOCKED' WHERE team_id=? AND level_number=?", (team_id, level))
    else:
        conn.execute(
            "INSERT INTO level_states (team_id, level_number, status, allowed_seconds) VALUES (?,?,'UNLOCKED',?)",
            (team_id, level, config.LEVEL_TIMES.get(level, 900))
        )
    conn.commit()
    log_event("LEVEL_UNLOCKED", f"Admin unlocked L{level} for team {team_id}", team_id=team_id, level_number=level)
    return {"ok": True}

@router.post("/admin/event/unlock-all")
async def unlock_all(body: TeamControlBody, _: str = Depends(require_admin)):
    level = body.level_number
    conn = get_conn()
    teams = conn.execute("SELECT id FROM teams WHERE status='PLAYING'").fetchall()
    for t in teams:
        existing = conn.execute("SELECT id FROM level_states WHERE team_id=? AND level_number=?", (t['id'], level)).fetchone()
        if existing:
            conn.execute("UPDATE level_states SET status='UNLOCKED' WHERE team_id=? AND level_number=?", (t['id'], level))
        else:
            conn.execute(
                "INSERT INTO level_states (team_id, level_number, status, allowed_seconds) VALUES (?,?,'UNLOCKED',?)",
                (t['id'], level, config.LEVEL_TIMES.get(level, 900))
            )
    conn.commit()
    log_event("LEVEL_UNLOCKED_ALL", f"Admin unlocked L{level} for all teams")
    return {"ok": True, "teams_affected": len(teams)}

@router.post("/admin/event/add-time-all")
async def add_time_all(body: TeamControlBody, _: str = Depends(require_admin)):
    secs = body.seconds or 300
    conn = get_conn()
    conn.execute(
        "UPDATE level_states SET allowed_seconds = allowed_seconds + ? WHERE status='ACTIVE'", (secs,)
    )
    conn.commit()
    log_event("ADMIN_TIME_ADJUSTED_ALL", f"Added {secs}s to all active teams")
    return {"ok": True}

# ---- FORMATION OVERRIDES ----

class AddMemberBody(BaseModel):
    participant_id: int
    reason: Optional[str] = None

@router.post("/admin/teams/{team_id}/add-member")
async def add_member(team_id: str, body: AddMemberBody, _: str = Depends(require_admin)):
    conn = get_conn()
    p = conn.execute("SELECT * FROM participants WHERE id=?", (body.participant_id,)).fetchone()
    if not p:
        raise HTTPException(404, detail={"error": "NOT_FOUND"})
    if p['team_id']:
        raise HTTPException(400, detail={"error": "ALREADY_IN_TEAM", "message": "Participant already in a team."})
    size = conn.execute("SELECT COUNT(*) FROM participants WHERE team_id=?", (team_id,)).fetchone()[0]
    if size >= config.MAX_TEAM_SIZE:
        raise HTTPException(400, detail={"error": "TEAM_FULL"})
    conn.execute("UPDATE participants SET team_id=?, status='IN_TEAM' WHERE id=?", (team_id, body.participant_id))
    new_size = size + 1
    status = 'READY' if new_size >= config.MIN_TEAM_SIZE else 'FORMING'
    conn.execute("UPDATE teams SET status=? WHERE id=?", (status, team_id))
    conn.commit()
    conn.execute("INSERT INTO admin_actions (action, team_id, participant_id, reason) VALUES ('ADD_MEMBER',?,?,?)",
                 (team_id, body.participant_id, body.reason))
    conn.commit()
    log_event("ADMIN_TEAM_OVERRIDE", f"Admin added {p['name']} to {team_id}", team_id=team_id)
    return {"ok": True}

class RemoveMemberBody(BaseModel):
    participant_id: int
    reason: Optional[str] = None

@router.post("/admin/teams/{team_id}/remove-member")
async def remove_member(team_id: str, body: RemoveMemberBody, _: str = Depends(require_admin)):
    conn = get_conn()
    conn.execute("UPDATE participants SET team_id=NULL, status='LOGGED_IN' WHERE id=? AND team_id=?",
                 (body.participant_id, team_id))
    size = conn.execute("SELECT COUNT(*) FROM participants WHERE team_id=?", (team_id,)).fetchone()[0]
    status = 'READY' if size >= config.MIN_TEAM_SIZE else 'FORMING'
    conn.execute("UPDATE teams SET status=? WHERE id=?", (status, team_id))
    conn.commit()
    log_event("ADMIN_TEAM_OVERRIDE", f"Admin removed participant {body.participant_id} from {team_id}", team_id=team_id)
    return {"ok": True}

# ---- LIVE FEED ----

@router.get("/admin/feed")
async def get_feed(since: int = 0, _: str = Depends(require_admin)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT el.*, p.name as participant_name, t.name as team_name FROM event_log el LEFT JOIN participants p ON el.participant_id = p.id LEFT JOIN teams t ON el.team_id = t.id WHERE el.id > ? ORDER BY el.id DESC LIMIT 50",
        (since,)
    ).fetchall()
    return {"events": [dict(r) for r in rows]}

# ---- CHALLENGES (with answers for admin) ----

@router.get("/admin/challenges")
async def get_challenges_admin(_: str = Depends(require_admin)):
    from services.game import admin_challenge
    challenges = []
    for i in range(1, 7):
        c = admin_challenge(i)
        if c:
            challenges.append(c)
    return {"challenges": challenges}

# ---- LEADERBOARD ----

@router.get("/admin/leaderboard")
async def leaderboard(_: str = Depends(require_admin)):
    conn = get_conn()
    teams = conn.execute("""
        SELECT t.*,
               (SELECT COUNT(*) FROM level_states ls WHERE ls.team_id=t.id AND ls.status='COMPLETED') as levels_completed,
               (SELECT COUNT(*) FROM hint_events he WHERE he.team_id=t.id) as total_hints,
               (SELECT COUNT(*) FROM violations v WHERE v.team_id=t.id) as total_violations
        FROM teams t
        WHERE t.status IN ('PLAYING','COMPLETED','ELIMINATED')
        ORDER BY levels_completed DESC, total_hints ASC, total_violations ASC
    """).fetchall()
    result = []
    for t in teams:
        members = conn.execute("SELECT name, roll_number FROM participants WHERE team_id=?", (t['id'],)).fetchall()
        result.append({
            "team_id": t['id'],
            "team_name": t['name'],
            "status": t['status'],
            "levels_completed": t['levels_completed'],
            "total_hints": t['total_hints'],
            "total_violations": t['total_violations'],
            "completed_at": t['completed_at'],
            "members": [{"name": m['name'], "roll_number": m['roll_number']} for m in members],
        })
    return {"leaderboard": result}

# ---- RELOAD PARTICIPANTS ----

@router.post("/admin/reload-participants")
async def reload_participants(_: str = Depends(require_admin)):
    result = load_participants()
    return {"ok": True, "stats": result}

# ---- VIOLATIONS ----

@router.get("/admin/violations")
async def list_violations(_: str = Depends(require_admin)):
    conn = get_conn()
    rows = conn.execute("""
        SELECT v.*, t.name as team_name, p.name as participant_name
        FROM violations v
        JOIN teams t ON v.team_id = t.id
        LEFT JOIN participants p ON v.participant_id = p.id
        ORDER BY v.timestamp DESC LIMIT 100
    """).fetchall()
    return {"violations": [dict(r) for r in rows]}
