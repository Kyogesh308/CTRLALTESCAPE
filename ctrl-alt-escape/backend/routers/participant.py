import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from db import get_conn, get_setting, log_event
from services.participants import get_by_token, login as do_login
from services.formation import (
    get_lobby, send_request, accept_request, reject_request,
    cancel_request, rename_team, ready_team, get_team_detail, expire_pending_requests
)
from services.game import (
    get_game_state, start_level, request_hint, submit_answer,
    record_violation
)

router = APIRouter()

# Rate limiting (simple in-memory)
_login_attempts: dict = {}

def get_current_participant(x_session: Optional[str] = Header(None, alias="X-Session")):
    if not x_session:
        raise HTTPException(401, detail={"error": "UNAUTHORIZED", "message": "Session required."})
    p = get_by_token(x_session)
    if not p:
        raise HTTPException(401, detail={"error": "UNAUTHORIZED", "message": "Invalid or expired session."})
    return p

# ---- AUTH ----

class LoginRequest(BaseModel):
    name: str
    roll_number: str
    email: str
    session_token: Optional[str] = None

@router.post("/participant/login")
async def participant_login(body: LoginRequest, request: Request):
    ip = request.client.host
    attempts = _login_attempts.get(ip, 0)
    if attempts > 20:
        raise HTTPException(429, detail={"error": "RATE_LIMITED", "message": "Too many login attempts."})
    _login_attempts[ip] = attempts + 1

    result = do_login(body.name, body.roll_number, body.email, body.session_token)
    if "error" in result:
        code = 401 if result["error"] in ("ACCESS_DENIED", "ALREADY_CLAIMED", "ALREADY_REGISTERED") else 400
        raise HTTPException(code, detail=result)
    _login_attempts[ip] = 0
    return result

@router.get("/participant/me")
async def get_me(x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    phase = get_setting('event_phase', 'REGISTRATION')
    conn = get_conn()
    team = None
    if p['team_id']:
        team = get_team_detail(p['team_id'])
    pending_in = conn.execute(
        "SELECT COUNT(*) FROM team_requests WHERE receiver_id=? AND status='PENDING'", (p['id'],)
    ).fetchone()[0]
    pending_out = conn.execute(
        "SELECT COUNT(*) FROM team_requests WHERE sender_id=? AND status='PENDING'", (p['id'],)
    ).fetchone()[0]
    unread = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE participant_id=? AND read=0", (p['id'],)
    ).fetchone()[0]
    return {
        "id": p['id'],
        "name": p['name'],
        "roll_number": p['roll_number'],
        "email": p['email'],
        "is_placeholder": bool(p['is_placeholder']),
        "claimed": bool(p['claimed']),
        "team_id": p['team_id'],
        "status": p['status'],
        "phase": phase,
        "team": team,
        "pending_in": pending_in,
        "pending_out": pending_out,
        "unread_notifications": unread,
    }

# ---- LOBBY & TEAM REQUESTS ----

@router.get("/team/lobby")
async def get_lobby_api(x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    expire_pending_requests()
    return get_lobby(p)

class RequestBody(BaseModel):
    receiver_id: int

@router.post("/team/requests")
async def create_request(body: RequestBody, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = send_request(p, body.receiver_id)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

@router.post("/team/requests/{request_id}/accept")
async def accept_request_api(request_id: int, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = accept_request(p, request_id)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

@router.post("/team/requests/{request_id}/reject")
async def reject_request_api(request_id: int, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = reject_request(p, request_id)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

@router.post("/team/requests/{request_id}/cancel")
async def cancel_request_api(request_id: int, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = cancel_request(p, request_id)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

class RenameBody(BaseModel):
    name: str

@router.post("/team/rename")
async def rename_team_api(body: RenameBody, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = rename_team(p, body.name)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

class ReadyBody(BaseModel):
    ready: bool = True

@router.post("/team/ready")
async def ready_team_api(body: ReadyBody, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = ready_team(p, body.ready)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

# ---- NOTIFICATIONS ----

@router.get("/notifications")
async def get_notifications(since: int = 0, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE participant_id=? AND id > ? ORDER BY id ASC LIMIT 50",
        (p['id'], since)
    ).fetchall()
    result = []
    for r in rows:
        payload = {}
        try:
            payload = json.loads(r['payload']) if r['payload'] else {}
        except Exception:
            pass
        result.append({
            "id": r['id'],
            "type": r['type'],
            "payload": payload,
            "created_at": r['created_at'],
            "read": bool(r['read']),
        })
    return {"notifications": result}

@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: int, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET read=1 WHERE id=? AND participant_id=?", (notification_id, p['id'])
    )
    conn.commit()
    return {"ok": True}

@router.post("/notifications/read-all")
async def mark_all_read(x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    conn = get_conn()
    conn.execute("UPDATE notifications SET read=1 WHERE participant_id=?", (p['id'],))
    conn.commit()
    return {"ok": True}

# ---- GAME ----

@router.get("/game/state")
async def game_state(x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    return get_game_state(p)

@router.get("/game/level/{level_number}/intro")
async def level_intro(level_number: int, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    from services.game import public_challenge, get_challenges
    from config import config as cfg
    team_id = p['team_id']
    conn = get_conn()
    ls = conn.execute("SELECT * FROM level_states WHERE team_id=? AND level_number=?", (team_id, level_number)).fetchone()
    if not ls:
        raise HTTPException(403, detail={"error": "LOCKED", "message": "Level is locked."})

    ch = public_challenge(level_number)
    if not ch:
        raise HTTPException(404, detail={"error": "NOT_FOUND"})

    team = conn.execute("SELECT carryover_seconds FROM teams WHERE id=?", (team_id,)).fetchone()
    carryover = min(int(team['carryover_seconds'] or 0), cfg.MAX_CARRYOVER_SECONDS) if team else 0

    return {
        **ch,
        "level_status": ls['status'],
        "carryover_seconds": carryover,
        "available_seconds": cfg.LEVEL_TIMES.get(level_number, 900) + carryover,
    }

class StartLevelBody(BaseModel):
    level_number: int

@router.post("/game/level/{level_number}/start")
async def start_level_api(level_number: int, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = start_level(p, level_number)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

class HintBody(BaseModel):
    hint_number: int

@router.post("/game/level/{level_number}/hint")
async def get_hint(level_number: int, body: HintBody, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = request_hint(p, level_number, body.hint_number)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

class SubmitBody(BaseModel):
    answer: str
    explanation: str

@router.post("/game/level/{level_number}/submit")
async def submit(level_number: int, body: SubmitBody, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = submit_answer(p, level_number, body.answer, body.explanation)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result

class ViolationBody(BaseModel):
    type: str
    level: int

@router.post("/game/violation")
async def report_violation(body: ViolationBody, x_session: Optional[str] = Header(None, alias="X-Session")):
    p = get_current_participant(x_session)
    result = record_violation(p, body.level, body.type)
    if "error" in result:
        raise HTTPException(400, detail=result)
    return result
