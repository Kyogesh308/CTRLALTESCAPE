import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db import get_conn, log_event, now_utc, transaction, get_setting
from config import config
from services.participants import is_online

def _next_team_id():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    return f"TEAM-{(count + 1):03d}"

def get_lobby(current_participant):
    """Returns available participants + current user's team state + notifications count."""
    conn = get_conn()
    phase = get_setting('event_phase', 'REGISTRATION')
    pid = current_participant['id']

    # Get all eligible logged-in people without a team (excluding self)
    available = conn.execute("""
        SELECT id, name, roll_number, last_seen, status
        FROM participants
        WHERE logged_in=1 AND team_id IS NULL AND eligibility=1
          AND status NOT IN ('ELIMINATED','COMPLETED')
          AND id != ?
    """, (pid,)).fetchall()

    # Get my pending outgoing requests
    my_sent = {
        r['receiver_id']: r['id']
        for r in conn.execute(
            "SELECT id, receiver_id FROM team_requests WHERE sender_id=? AND status='PENDING'", (pid,)
        ).fetchall()
    }
    # Get pending incoming requests to me
    my_received = {
        r['sender_id']: r['id']
        for r in conn.execute(
            "SELECT id, sender_id FROM team_requests WHERE receiver_id=? AND status='PENDING'", (pid,)
        ).fetchall()
    }

    participants_out = []
    for p in available:
        rel = 'NONE'
        req_id = None
        if p['id'] in my_sent:
            rel = 'REQUEST_PENDING'
            req_id = my_sent[p['id']]
        elif p['id'] in my_received:
            rel = 'REQUEST_RECEIVED'
            req_id = my_received[p['id']]
        participants_out.append({
            "id": p['id'],
            "name": p['name'],
            "roll_number": p['roll_number'],
            "online": is_online(p),
            "relationship": rel,
            "request_id": req_id,
        })

    # My team info
    my_team = None
    if current_participant['team_id']:
        my_team = get_team_detail(current_participant['team_id'])

    # Unread notifications count
    unread = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE participant_id=? AND read=0", (pid,)
    ).fetchone()[0]

    return {
        "phase": phase,
        "available_participants": participants_out,
        "my_team": my_team,
        "unread_notifications": unread,
        "my_status": current_participant['status'],
    }

def send_request(sender, receiver_id: int):
    conn = get_conn()
    phase = get_setting('event_phase', 'REGISTRATION')
    formation_override = get_setting('formation_override', '0') == '1'

    if phase not in ('REGISTRATION', 'TEAM_FORMATION') and not formation_override:
        return {"error": "PHASE_ERROR", "message": "Team formation is not currently open."}

    sid = sender['id']
    if sid == receiver_id:
        return {"error": "INVALID", "message": "You cannot send a request to yourself."}

    receiver = conn.execute(
        "SELECT * FROM participants WHERE id=? AND logged_in=1 AND eligibility=1",
        (receiver_id,)
    ).fetchone()
    if not receiver:
        return {"error": "NOT_FOUND", "message": "Participant not found or not available."}

    if receiver['team_id']:
        return {"error": "ALREADY_IN_TEAM", "message": "This participant is already part of a team."}

    # Check sender eligibility to send requests
    sender_team = None
    if sender['team_id']:
        sender_team = conn.execute("SELECT * FROM teams WHERE id=?", (sender['team_id'],)).fetchone()
        if not sender_team:
            return {"error": "TEAM_ERROR", "message": "Team not found."}
        # Must be team leader
        if sender_team['leader_id'] != sid:
            return {"error": "NOT_LEADER", "message": "Only the team leader can send requests."}
        # Check team size
        size = conn.execute("SELECT COUNT(*) FROM participants WHERE team_id=?", (sender['team_id'],)).fetchone()[0]
        if size >= config.MAX_TEAM_SIZE:
            return {"error": "TEAM_FULL", "message": "Your team is already full."}

    # Duplicate: I already sent to them
    existing = conn.execute(
        "SELECT id FROM team_requests WHERE sender_id=? AND receiver_id=? AND status='PENDING'",
        (sid, receiver_id)
    ).fetchone()
    if existing:
        return {"error": "ALREADY_SENT", "message": "You already have a pending request to this participant."}

    # Reverse: they already sent to me
    reverse = conn.execute(
        "SELECT id FROM team_requests WHERE sender_id=? AND receiver_id=? AND status='PENDING'",
        (receiver_id, sid)
    ).fetchone()
    if reverse:
        return {"error": "REVERSE_REQUEST",
                "message": f"{receiver['name']} already sent you a request — check your notifications."}

    from datetime import datetime, timezone, timedelta
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=config.REQUEST_EXPIRY_SECONDS)).isoformat()

    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO team_requests (sender_id, receiver_id, status, expires_at) VALUES (?,?,'PENDING',?)",
            (sid, receiver_id, expires_at)
        )
        req_id = cur.lastrowid
        # Notify receiver
        payload = json.dumps({
            "request_id": req_id,
            "sender_id": sid,
            "sender_name": sender['name'],
            "sender_roll": sender['roll_number'],
        })
        conn.execute(
            "INSERT INTO notifications (participant_id, type, payload) VALUES (?,?,?)",
            (receiver_id, 'TEAM_REQUEST_RECEIVED', payload)
        )
    log_event("TEAM_REQUEST_SENT", f"{sender['name']} → {receiver['name']}", participant_id=sid)
    return {"ok": True, "request_id": req_id, "message": f"Request sent — waiting for {receiver['name']}."}

def accept_request(current_participant, request_id: int):
    conn = get_conn()
    phase = get_setting('event_phase', 'REGISTRATION')
    formation_override = get_setting('formation_override', '0') == '1'

    req = conn.execute("SELECT * FROM team_requests WHERE id=?", (request_id,)).fetchone()
    if not req:
        return {"error": "NOT_FOUND", "message": "Request not found."}
    if req['receiver_id'] != current_participant['id']:
        return {"error": "FORBIDDEN", "message": "This request was not sent to you."}
    if req['status'] != 'PENDING':
        return {"error": "ALREADY_RESPONDED", "message": "This request is no longer available."}

    # Check expiry
    from datetime import datetime, timezone
    expires = datetime.fromisoformat(req['expires_at'].replace('Z', '+00:00'))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        conn.execute("UPDATE team_requests SET status='EXPIRED' WHERE id=?", (request_id,))
        conn.commit()
        return {"error": "EXPIRED", "message": "This request has expired."}

    if phase not in ('REGISTRATION', 'TEAM_FORMATION') and not formation_override:
        return {"error": "PHASE_ERROR", "message": "Team formation is no longer open."}

    rid = current_participant['id']
    sid = req['sender_id']

    # Re-validate everything in transaction
    with transaction() as conn:
        receiver = conn.execute("SELECT * FROM participants WHERE id=? AND team_id IS NULL", (rid,)).fetchone()
        if not receiver:
            return {"error": "ALREADY_IN_TEAM", "message": "You are already part of a team."}

        sender = conn.execute("SELECT * FROM participants WHERE id=? AND eligibility=1", (sid,)).fetchone()
        if not sender:
            return {"error": "SENDER_UNAVAILABLE", "message": "The sender is no longer available."}

        if sender['team_id']:
            # Add receiver to sender's existing team
            team = conn.execute("SELECT * FROM teams WHERE id=?", (sender['team_id'],)).fetchone()
            if not team:
                return {"error": "TEAM_ERROR", "message": "Team no longer exists."}
            if team['leader_id'] != sid:
                return {"error": "NOT_LEADER", "message": "Sender is no longer the team leader."}
            size = conn.execute("SELECT COUNT(*) FROM participants WHERE team_id=?", (team['id'],)).fetchone()[0]
            if size >= config.MAX_TEAM_SIZE:
                return {"error": "TEAM_FULL", "message": "Team is already full."}

            rows = conn.execute(
                "UPDATE participants SET team_id=?, status='IN_TEAM' WHERE id=? AND team_id IS NULL",
                (team['id'], rid)
            ).rowcount
            if rows == 0:
                return {"error": "RACE", "message": "You just joined another team."}

            new_size = size + 1
            new_status = 'READY' if new_size >= config.MIN_TEAM_SIZE else 'FORMING'
            conn.execute("UPDATE teams SET status=? WHERE id=?", (new_status, team['id']))
            conn.execute("UPDATE team_requests SET status='ACCEPTED', responded_at=? WHERE id=?", (now_utc(), request_id))

            # Cancel all other pending requests involving receiver
            _cancel_conflicting(conn, rid, request_id)
            if new_size >= config.MAX_TEAM_SIZE:
                _cancel_team_outgoing(conn, team['id'], sid)

            # Notify sender
            _notify(conn, sid, 'REQUEST_ACCEPTED', {
                "request_id": request_id,
                "receiver_name": receiver['name'],
                "team_id": team['id'],
                "team_name": team['name'],
            })
            # Notify all members
            members = conn.execute("SELECT id FROM participants WHERE team_id=? AND id != ?", (team['id'], rid)).fetchall()
            for m in members:
                if m['id'] != sid:
                    _notify(conn, m['id'], 'TEAM_MEMBER_JOINED', {
                        "name": receiver['name'],
                        "team_id": team['id'],
                    })
            team_id = team['id']
            team_name = team['name']
        else:
            # Create new team
            team_id = _next_team_id()
            team_name = team_id
            conn.execute(
                "INSERT INTO teams (id, name, leader_id, status) VALUES (?,?,?,?)",
                (team_id, team_name, sid, 'FORMING')
            )
            # Add both
            conn.execute(
                "UPDATE participants SET team_id=?, status='IN_TEAM' WHERE id=? AND team_id IS NULL",
                (team_id, sid)
            )
            rows = conn.execute(
                "UPDATE participants SET team_id=?, status='IN_TEAM' WHERE id=? AND team_id IS NULL",
                (team_id, rid)
            ).rowcount
            if rows == 0:
                return {"error": "RACE", "message": "You just joined another team."}

            size = 2
            new_status = 'READY' if size >= config.MIN_TEAM_SIZE else 'FORMING'
            conn.execute("UPDATE teams SET status=? WHERE id=?", (new_status, team_id))
            conn.execute("UPDATE team_requests SET status='ACCEPTED', responded_at=? WHERE id=?", (now_utc(), request_id))
            _cancel_conflicting(conn, rid, request_id)
            _cancel_conflicting(conn, sid, request_id)
            _notify(conn, sid, 'REQUEST_ACCEPTED', {
                "request_id": request_id,
                "receiver_name": receiver['name'],
                "team_id": team_id,
                "team_name": team_name,
            })
            log_event("TEAM_CREATED", f"Team {team_id} created by {sender['name']} + {receiver['name']}", team_id=team_id)

    log_event("TEAM_REQUEST_ACCEPTED", f"{receiver['name']} accepted {sender['name']}'s request", team_id=team_id)
    return {"ok": True, "team_id": team_id, "team_name": team_name}

def reject_request(current_participant, request_id: int):
    conn = get_conn()
    req = conn.execute("SELECT * FROM team_requests WHERE id=?", (request_id,)).fetchone()
    if not req or req['receiver_id'] != current_participant['id']:
        return {"error": "NOT_FOUND", "message": "Request not found."}
    if req['status'] != 'PENDING':
        return {"error": "ALREADY_RESPONDED", "message": "Request already handled."}

    sender = conn.execute("SELECT name FROM participants WHERE id=?", (req['sender_id'],)).fetchone()
    conn.execute("UPDATE team_requests SET status='REJECTED', responded_at=? WHERE id=?", (now_utc(), request_id))
    _notify(conn, req['sender_id'], 'REQUEST_REJECTED', {
        "request_id": request_id,
        "receiver_name": current_participant['name'],
    })
    conn.commit()
    log_event("TEAM_REQUEST_REJECTED", f"{current_participant['name']} rejected {sender['name'] if sender else '?'}'s request")
    return {"ok": True}

def cancel_request(current_participant, request_id: int):
    conn = get_conn()
    req = conn.execute("SELECT * FROM team_requests WHERE id=?", (request_id,)).fetchone()
    if not req or req['sender_id'] != current_participant['id']:
        return {"error": "NOT_FOUND", "message": "Request not found."}
    if req['status'] != 'PENDING':
        return {"error": "ALREADY_RESPONDED", "message": "Request already handled."}

    conn.execute("UPDATE team_requests SET status='CANCELLED', cancel_reason='sender_cancelled', responded_at=? WHERE id=?",
                 (now_utc(), request_id))
    _notify(conn, req['receiver_id'], 'REQUEST_CANCELLED', {"request_id": request_id})
    conn.commit()
    return {"ok": True}

def rename_team(current_participant, new_name: str):
    conn = get_conn()
    if not current_participant['team_id']:
        return {"error": "NO_TEAM", "message": "You are not in a team."}
    team = conn.execute("SELECT * FROM teams WHERE id=?", (current_participant['team_id'],)).fetchone()
    if not team or team['leader_id'] != current_participant['id']:
        return {"error": "NOT_LEADER", "message": "Only the team leader can rename the team."}

    name = new_name.strip()
    if len(name) < 3 or len(name) > 30:
        return {"error": "INVALID_NAME", "message": "Team name must be 3-30 characters."}

    existing = conn.execute("SELECT id FROM teams WHERE name=? AND id != ?", (name, team['id'])).fetchone()
    if existing:
        return {"error": "NAME_TAKEN", "message": "This team name is already taken. Choose another."}

    conn.execute("UPDATE teams SET name=? WHERE id=?", (name, team['id']))
    conn.commit()
    return {"ok": True, "team_name": name}

def ready_team(current_participant, ready: bool):
    conn = get_conn()
    if not current_participant['team_id']:
        return {"error": "NO_TEAM", "message": "You are not in a team."}
    team = conn.execute("SELECT * FROM teams WHERE id=?", (current_participant['team_id'],)).fetchone()
    if not team or team['leader_id'] != current_participant['id']:
        return {"error": "NOT_LEADER", "message": "Only the team leader can confirm readiness."}
    size = conn.execute("SELECT COUNT(*) FROM participants WHERE team_id=?", (team['id'],)).fetchone()[0]
    if ready and size < config.MIN_TEAM_SIZE:
        return {"error": "TOO_SMALL", "message": f"Need at least {config.MIN_TEAM_SIZE} members to confirm readiness."}

    conn.execute("UPDATE teams SET roster_confirmed=? WHERE id=?", (int(ready), team['id']))
    conn.commit()
    return {"ok": True, "roster_confirmed": ready}

def get_team_detail(team_id: str):
    conn = get_conn()
    team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return None
    members = conn.execute("SELECT id, name, roll_number, last_seen, status FROM participants WHERE team_id=?", (team_id,)).fetchall()
    return {
        "id": team['id'],
        "name": team['name'],
        "leader_id": team['leader_id'],
        "status": team['status'],
        "roster_confirmed": bool(team['roster_confirmed']),
        "current_level": team['current_level'],
        "paused": bool(team['paused']),
        "members": [{
            "id": m['id'],
            "name": m['name'],
            "roll_number": m['roll_number'],
            "online": is_online(m),
            "status": m['status'],
        } for m in members],
        "size": len(members),
        "max_size": config.MAX_TEAM_SIZE,
        "min_size": config.MIN_TEAM_SIZE,
    }

def expire_pending_requests():
    """Called periodically to expire old requests."""
    from datetime import datetime, timezone
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    expired = conn.execute(
        "SELECT id, sender_id, receiver_id FROM team_requests WHERE status='PENDING' AND expires_at < ?", (now,)
    ).fetchall()
    for req in expired:
        conn.execute("UPDATE team_requests SET status='EXPIRED' WHERE id=?", (req['id'],))
        _notify(conn, req['sender_id'], 'REQUEST_EXPIRED', {"request_id": req['id']})
    if expired:
        conn.commit()

def _cancel_conflicting(conn, participant_id: int, except_req_id: int):
    reqs = conn.execute(
        "SELECT id, sender_id, receiver_id FROM team_requests WHERE (sender_id=? OR receiver_id=?) AND status='PENDING' AND id != ?",
        (participant_id, participant_id, except_req_id)
    ).fetchall()
    for r in reqs:
        conn.execute("UPDATE team_requests SET status='CANCELLED', cancel_reason='participant_joined_team', responded_at=? WHERE id=?",
                     (now_utc(), r['id']))
        other = r['sender_id'] if r['receiver_id'] == participant_id else r['receiver_id']
        _notify(conn, other, 'REQUEST_CANCELLED', {"request_id": r['id'], "reason": "participant_joined_team"})

def _cancel_team_outgoing(conn, team_id: str, leader_id: int):
    reqs = conn.execute(
        "SELECT id, receiver_id FROM team_requests WHERE sender_id=? AND status='PENDING'", (leader_id,)
    ).fetchall()
    for r in reqs:
        conn.execute("UPDATE team_requests SET status='CANCELLED', cancel_reason='team_full', responded_at=? WHERE id=?",
                     (now_utc(), r['id']))
        _notify(conn, r['receiver_id'], 'REQUEST_CANCELLED', {"request_id": r['id'], "reason": "team_full"})

def _notify(conn, participant_id: int, type: str, payload: dict):
    conn.execute(
        "INSERT INTO notifications (participant_id, type, payload) VALUES (?,?,?)",
        (participant_id, type, json.dumps(payload))
    )
