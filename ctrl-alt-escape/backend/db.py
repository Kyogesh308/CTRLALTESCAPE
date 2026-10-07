import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'event.db')
_local = threading.local()

def get_conn():
    if not hasattr(_local, 'conn') or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn

@contextmanager
def transaction():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roll_number TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        original_name TEXT,
        email TEXT NOT NULL,
        eligibility INTEGER DEFAULT 1,
        is_placeholder INTEGER DEFAULT 0,
        claimed INTEGER DEFAULT 0,
        claimed_at TEXT,
        logged_in INTEGER DEFAULT 0,
        login_at TEXT,
        session_token TEXT,
        team_id TEXT,
        status TEXT DEFAULT 'REGISTERED',
        last_seen TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE,
        leader_id INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        status TEXT DEFAULT 'FORMING',
        roster_confirmed INTEGER DEFAULT 0,
        current_level INTEGER DEFAULT 0,
        game_started_at TEXT,
        completed_at TEXT,
        carryover_seconds INTEGER DEFAULT 0,
        paused INTEGER DEFAULT 0,
        paused_at TEXT,
        FOREIGN KEY (leader_id) REFERENCES participants(id)
    );

    CREATE TABLE IF NOT EXISTS team_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        status TEXT DEFAULT 'PENDING',
        cancel_reason TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        responded_at TEXT,
        expires_at TEXT,
        FOREIGN KEY (sender_id) REFERENCES participants(id),
        FOREIGN KEY (receiver_id) REFERENCES participants(id)
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        read INTEGER DEFAULT 0,
        FOREIGN KEY (participant_id) REFERENCES participants(id)
    );

    CREATE TABLE IF NOT EXISTS level_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id TEXT NOT NULL,
        level_number INTEGER NOT NULL,
        status TEXT DEFAULT 'LOCKED',
        started_at TEXT,
        submitted_at TEXT,
        completed_at TEXT,
        allowed_seconds INTEGER DEFAULT 0,
        carryover_seconds INTEGER DEFAULT 0,
        adjustment_seconds INTEGER DEFAULT 0,
        paused_seconds INTEGER DEFAULT 0,
        frozen_remaining INTEGER,
        UNIQUE(team_id, level_number),
        FOREIGN KEY (team_id) REFERENCES teams(id)
    );

    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id TEXT NOT NULL,
        level_number INTEGER NOT NULL,
        answer TEXT,
        explanation TEXT,
        submitted_by INTEGER,
        submitted_at TEXT DEFAULT (datetime('now')),
        elapsed INTEGER,
        remaining INTEGER,
        hints_used INTEGER DEFAULT 0,
        evaluation_status TEXT DEFAULT 'PENDING',
        matches_expected INTEGER DEFAULT 0,
        reviewed_at TEXT,
        reviewed_by TEXT,
        admin_note TEXT,
        FOREIGN KEY (team_id) REFERENCES teams(id)
    );

    CREATE TABLE IF NOT EXISTS hint_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id TEXT NOT NULL,
        level_number INTEGER NOT NULL,
        hint_number INTEGER NOT NULL,
        requested_by INTEGER,
        requested_at TEXT DEFAULT (datetime('now')),
        UNIQUE(team_id, level_number, hint_number),
        FOREIGN KEY (team_id) REFERENCES teams(id)
    );

    CREATE TABLE IF NOT EXISTS violations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id TEXT NOT NULL,
        participant_id INTEGER,
        level_number INTEGER,
        type TEXT,
        timestamp TEXT DEFAULT (datetime('now')),
        warning_number INTEGER,
        FOREIGN KEY (team_id) REFERENCES teams(id)
    );

    CREATE TABLE IF NOT EXISTS admin_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT,
        team_id TEXT,
        participant_id INTEGER,
        level_number INTEGER,
        payload TEXT,
        reason TEXT,
        timestamp TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT DEFAULT (datetime('now')),
        type TEXT,
        team_id TEXT,
        participant_id INTEGER,
        level_number INTEGER,
        message TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT OR IGNORE INTO settings (key, value) VALUES ('event_phase', 'REGISTRATION');
    INSERT OR IGNORE INTO settings (key, value) VALUES ('formation_override', '0');
    INSERT OR IGNORE INTO settings (key, value) VALUES ('event_paused', '0');
    INSERT OR IGNORE INTO settings (key, value) VALUES ('event_started_at', '');
    """)
    conn.commit()

def get_setting(key: str, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default

def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()

def log_event(type: str, message: str, team_id=None, participant_id=None, level_number=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO event_log (type, team_id, participant_id, level_number, message) VALUES (?,?,?,?,?)",
        (type, team_id, participant_id, level_number, message)
    )
    conn.commit()

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()
