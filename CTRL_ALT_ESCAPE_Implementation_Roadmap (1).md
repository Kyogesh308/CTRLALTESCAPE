# CTRL + ALT + ESCAPE — Phase-by-Phase Implementation Roadmap (v2)

**Based on:** `CTRL_ALT_ESCAPE_Website_Specification.md` + `CTRL_ALT_ESCAPE_UI_UX_Specification.md` + **`IMPORTANT LOGIC CHANGE`** (placeholder registration + in-app team formation)
**Goal:** Ship a reliable participant game interface + admin control center for a live team-based AI & Tech escape room (~20–60 participants, 6 levels, ~120 min of gameplay).

---

## What Changed in v2 (read this first)

| Area | Current model (follows the logic-change document) |
|---|---|
| **Registration file** | **Excel workbook** with columns `name`, `roll number`, `email`, `eligibility`. Contains real participants **and ~40 placeholder records** (name is just `1`, `2`, …). |
| **Login identity** | **Roll number + email** are the identity. **Name must match only for existing participants.** For **placeholder** records the submitted name **replaces the placeholder name** (the record is "claimed"). |
| **Double claiming** | A claimed placeholder can never be claimed again → *"This registration has already been claimed. Please contact the event administrator."* (enforced in the backend, never only in the UI) |
| **Participant states** | `REGISTERED → CLAIMED (placeholders only) → LOGGED_IN → TEAM_FORMING → IN_TEAM → PLAYING → ELIMINATED / COMPLETED` |
| **Team formation** | **Entirely in-app.** Participants see a live list of available logged-in people, press **REQUEST TEAM**, the receiver gets a **popup/notification** and presses **ACCEPT / REJECT**. The first participant to form the team becomes **team leader**. |
| **Email service (Resend)** | **Not part of team formation.** Config stays in `.env` as *optional* (future announcements/confirmations/organizer communication only). Nothing in the game depends on it. |
| **Realtime** | **WebSocket is the preferred transport; short polling every 2–3 s is the accepted MVP** (small event size). Either way it drives **notifications** and the **live lobby**, and the API shape is identical. |
| **Formation lifecycle** | Explicit phases: `REGISTRATION → TEAM_FORMATION → FORMATION_LOCKED → GAME_STARTED`. No new requests, team changes or new members after the game starts unless the admin overrides. |
| **Admin** | Sees **all team requests** and has **Add member / Remove member / Create team / Merge-Move** (restricted to the formation phase unless explicitly overridden). Also sees **claimed vs. unclaimed placeholders**. |
| **Data model** | `team_requests` + `notifications` tables; participants gain `claimed`, `is_placeholder`, `original_name`. No invitation-style tables exist. |

> **Rule from the change document (§32):** the earlier email/code-based flow is fully removed from code, UI and docs. All wording uses **Request Team, Pending Request, Accept, Reject, Team Notification**. Phase 3I contains a verification sweep to prove no remnants are left.

---

## Part A — The Project in One Page

### What is being built
A web app where students verify themselves against a registration workbook, find teammates inside a live in-app lobby, form teams of 3–4 through request/accept popups, and then play **as a team** through six timed levels to "restore" a fictional AI called **NEXUS**. An **admin control center** watches everything live, reviews each team's answer + explanation, and decides whether to unlock the next level. The admin also has emergency overrides (unlock for all, add time, pause, eliminate/restore) and team-formation overrides.

### The governing ideas
1. **Backend owns the truth.** The browser only collects actions and displays state. Timers, unlocks, answers, violations, team membership, claims — all server-side.
2. **Team is the unit of play.** Participant identity is kept for auditing only. Participant identity itself is **`roll_number` / `participant_id` — never the name** (names can change, placeholders get renamed).
3. **Reliable event operation beats visual polish.** Core loop: `LOGIN → TEAM → LEVEL → TIMER → HINT → SUBMIT → ADMIN REVIEW → UNLOCK → NEXT LEVEL`.
4. **Everything happens inside one app.** No email, no copying codes, no switching apps, no manual refreshing. It should feel like a **live multiplayer game lobby**.

### The two experiences
| Participant mode | Admin mode |
|---|---|
| Immersive, game-like, light-first theme, bento cards, one dominant action per screen. Live lobby + notification bell. | Information-dense mission control: alerts → live teams → pending submissions → team requests → controls |
| Terms: Operator, Recovery Team, Security Layer, Recovery Key, Intelligence Assist | Alerts feed, team detail, review card, formation panel, emergency panel |

### Decisions & gaps to resolve in Phase 0
| # | Issue | Recommended resolution |
|---|---|---|
| 1 | Workbook column headers are human style (`roll number`, `eligibility`) and the file is `.xlsx` | Importer normalizes headers (lowercase, trim, spaces/underscores equal) and supports `.xlsx` (via `openpyxl`) **and** `.csv`. Parse eligibility `TRUE/true/1/yes` as true (Excel may deliver a real boolean). |
| 2 | **How is a record recognized as a placeholder?** (Spec only shows `name: 1`) | Importer sets `is_placeholder = true` when the name is empty or purely numeric (configurable regex `PLACEHOLDER_NAME_PATTERN=^\d+$`). Keep `original_name`. Show the list to the admin in Phase 5/8 so you can verify. |
| 3 | A claimed placeholder blocks everyone — but a legit student who refreshes/closes the tab must come back | Issue a **session token** at claim/login (`localStorage`). Same browser → **resume**. Different browser/device → blocked with the *already claimed* (placeholders) / *already registered* (existing) message. Admin gets **Reset session** and **Unclaim record** buttons. |
| 4 | Name rules for claiming | Trim, collapse spaces, 2–60 chars, not purely numeric, strip control chars. Name is **display only**. |
| 5 | **Contradiction in the spec:** §15 says *sender.team_id == NULL AND receiver.team_id == NULL* but §13 lets the leader (who already has a team) request more members | **Rule:** receiver must have no team. Sender must be **either** (a) team-less (→ accepting creates a new team, sender becomes leader) **or** (b) the **leader** of a team with free slots (→ accepting adds receiver). Non-leader members cannot send requests. |
| 6 | Team **name** (examples show "Cyber Sentinels") but flow never asks for one | Team is auto-named by ID (`TEAM-007`) on creation; the **leader can rename** it during formation (unique, 3–30 chars). |
| 7 | What does the leader's **READY** button do vs. automatic readiness at 3/4? | Team status is **automatic**: `<3 → WAITING_FOR_MEMBERS`, `3–4 → READY`. The leader's READY button is a **roster confirmation** (UX): it cancels the team's other pending requests and shows a "Roster confirmed" badge; it can be undone until formation locks. Admin "Start event" only requires size ≥ 3. |
| 8 | Two people send requests to each other at the same moment | If a pending request exists in the **reverse** direction, block the new one and tell the sender: *"Rahul already sent you a request — check notifications."* |
| 9 | Receiver accepts one request, other requests to them are still pending | On accept, all other pending requests **to or from** that receiver are auto-`CANCELLED` (reason recorded) and both sides get a notification. |
| 10 | Requests to offline people sit forever | `EXPIRED` state is used: pending requests expire after `REQUEST_EXPIRY_SECONDS` (default 180). "Online" = seen within `ONLINE_THRESHOLD_SECONDS` (default 15). Offline people are shown dimmed; requests allowed but expire. |
| 11 | Teams under 3 at lock time / leftover participants | Admin override tools (add member, move, merge, create team). Surface "teams below minimum" and "participants without team" as admin alerts when formation is locked. |
| 12 | Duplicate-login + violation counting + who starts game + retry policy + time-banking + expected answers + leftover logic | Same decisions as v1: session resume; monitor every member, count strikes per team (debounced); admin starts event; timer frozen during review and resumes on reject; carry-over capped by `MAX_CARRYOVER_SECONDS`; expected answers server-side and shown only to admin (optional ✓/✗ helper, never auto-accept). |
| 13 | Nobody has written the six challenges yet | Phase 0 deliverable: `challenges.json` (title, objective, body, 2 hints, expected answer, difficulty, base time). |
| 14 | Challenge text must not leak before START | Backend withholds challenge body until the level is `ACTIVE`; hint text until requested. |

---

## Part B — Architecture & Foundations

### Stack
- **Frontend:** plain HTML + CSS + vanilla JS (hand-written CSS variables; avoid CDN dependence on a flaky event network).
- **Backend:** Python + FastAPI + Uvicorn (**single worker**).
- **Storage:** **SQLite** (WAL mode) — survives a server restart mid-event.
- **Workbook import:** `openpyxl` (+ `csv` fallback).
- **Tokens:** Python `secrets`.
- **Live updates:** WebSocket is preferred by the change document; **short polling every 2–3 s is the accepted MVP** (`GET /notifications`, lobby/game state). Build the endpoints so a WebSocket push can replace polling without changing the API shape.
- **Email (Resend):** installed/configured but **optional and unused by the critical path**.

### Updated architecture (team formation)
```text
Participant A
      | POST /team/requests {receiver}
      v
   FastAPI  ──(writes team_request + notification)──►  DB
      |
      | (Participant B polls /lobby + /notifications every 2–3 s)
      v
Participant B sees popup ──► POST /team/requests/{id}/accept
      |
      v
   FastAPI (single transaction: validate → create/update team → update both participants → cancel conflicting requests → notify)
      |
      +───────────────┐
      v               v
Participant A     Participant B     (both see the team on next poll)
```

### Suggested folder structure
```
ctrl-alt-escape/
├── .env                      # secrets (never committed)
├── .env.example
├── .gitignore
├── requirements.txt
├── data/
│   ├── Event_Registration_With_Eligibility.xlsx   # PARTICIPANT_CSV_PATH points here
│   ├── challenges.json       # titles, bodies, hints, expected answers (server only)
│   └── event.db              # SQLite (auto-created)
├── backend/
│   ├── main.py               # FastAPI app, routers, static mounting
│   ├── config.py             # reads .env
│   ├── db.py                 # connection, schema, helpers
│   ├── schemas.py
│   ├── services/
│   │   ├── importer.py       # workbook → participants (placeholder detection)
│   │   ├── participants.py   # login, claim, session, presence
│   │   ├── formation.py      # event phase, team create/update, readiness, admin overrides
│   │   ├── requests.py       # team requests: send/accept/reject/cancel/expire
│   │   ├── notifications.py  # in-app notifications
│   │   ├── game.py           # level state machine, timer, hints, submissions
│   │   ├── monitoring.py     # violations
│   │   └── events.py         # event log / live feed
│   └── routers/
│       ├── participant.py
│       ├── team.py           # lobby, requests, notifications
│       ├── game.py
│       └── admin.py
└── frontend/
    ├── index.html            # landing
    ├── participant.html
    ├── admin.html
    ├── css/ (tokens.css, base.css, components.css, animations.css)
    ├── js/ (api.js, state.js, poller.js, monitor.js, ui/*.js, admin/*.js)
    └── assets/ (logos, icons, sounds)
```
> There is **no** invitation module. A `mailer.py` (Resend wrapper) may exist but is not imported by any team-formation code.

### Updated `.env` (example)
```env
APP_ENV=development
APP_NAME=CTRL_ALT_ESCAPE
APP_URL=http://localhost:8000

# Participant data (xlsx or csv)
PARTICIPANT_CSV_PATH=./data/Event_Registration_With_Eligibility.xlsx
PLACEHOLDER_NAME_PATTERN=^\d+$

# Resend — OPTIONAL, not used for team formation
RESEND_API_KEY=
RESEND_FROM_EMAIL=

# Admin
ADMIN_USERNAME=...
ADMIN_PASSWORD_HASH=...   # (or ADMIN_PASSWORD for MVP)

# Team formation
MIN_TEAM_SIZE=3
MAX_TEAM_SIZE=4
REQUEST_EXPIRY_SECONDS=180
ONLINE_THRESHOLD_SECONDS=15
INITIAL_EVENT_PHASE=REGISTRATION   # or TEAM_FORMATION to skip the admin "open" click

# Game
MAX_WARNINGS=3
LEVEL_1_TIME=900
LEVEL_2_TIME=1080
LEVEL_3_TIME=1320
LEVEL_4_TIME=1320
LEVEL_5_TIME=1380
LEVEL_6_TIME=1200
MAX_CARRYOVER_SECONDS=300
```

### Data model (SQLite tables)
| Table | Key fields |
|---|---|
| `participants` | id, roll_number (UNIQUE), name, **original_name**, email, **eligibility**, **is_placeholder**, **claimed**, **claimed_at**, logged_in, login_at, session_token, team_id, status, last_seen |
| `teams` | id (`TEAM-007`), name (UNIQUE), leader_id, created_at, status (`FORMING/READY/PLAYING/COMPLETED/ELIMINATED`), **roster_confirmed**, current_level, game_started_at, completed_at, carryover_seconds, paused, paused_at |
| **`team_requests`** | id, sender_id, receiver_id, status (`PENDING/ACCEPTED/REJECTED/EXPIRED/CANCELLED`), cancel_reason, created_at, responded_at, expires_at |
| **`notifications`** | id, participant_id, type, payload(JSON), created_at, read |
| `level_states` | team_id, level_number, status, started_at, submitted_at, completed_at, allowed_seconds, carryover_seconds, adjustment_seconds, paused_seconds, frozen_remaining |
| `submissions` | id, team_id, level_number, answer, explanation, submitted_by, submitted_at, elapsed, remaining, evaluation_status, reviewed_at, reviewed_by, admin_note |
| `hint_events` | id, team_id, level_number, hint_number, requested_by, requested_at |
| `violations` | id, team_id, participant_id, level_number, type, timestamp, warning_number |
| `admin_actions` | id, action, team_id, participant_id, level_number, payload(JSON), reason, timestamp |
| `event_log` | id, ts, type, team_id, participant_id, level_number, message (powers the live feed) |
| `settings` | key, value (**event_phase**, event started, paused, max carryover, max warnings, team sizes, retry policy, formation override) |

*(Team formation uses only `team_requests` and `notifications`; there is no invitation-style table.)*

### State machines
**Event phase:** `REGISTRATION → TEAM_FORMATION → FORMATION_LOCKED → GAME_STARTED` (admin-controlled; starting the game auto-locks formation).
**Participant:** placeholder `REGISTERED → CLAIMED → LOGGED_IN → TEAM_FORMING → IN_TEAM → PLAYING → ELIMINATED/COMPLETED`; existing `REGISTERED → LOGGED_IN → …`.
**Team:** `FORMING (<min) → READY (≥min) → PLAYING → COMPLETED` (branch `ELIMINATED`, restorable).
**Team request:** `PENDING → ACCEPTED | REJECTED | CANCELLED | EXPIRED`.
**Level per team:** `LOCKED → UNLOCKED → ACTIVE → UNDER_REVIEW → (APPROVED → COMPLETED) | REJECTED (→ ACTIVE) | TIMEOUT`.

### Timer rule (critical)
Never store a ticking counter. Store `started_at` + durations and compute:
```
allowed   = base + carryover + adjustment
elapsed   = now - started_at - paused_seconds     (frozen at submit/under review)
remaining = allowed - elapsed
```
Every state poll returns `remaining_seconds` and `server_time`; the browser counts down locally between polls and re-syncs each poll.

---

## Part C — The Roadmap

> **Time guide:** each phase lists a **realistic estimate** and a **compressed MVP estimate** (in the table). If you're truly time-boxed, follow the compressed numbers and the *Cut List* in Part D.
>
> | Phase | Realistic | Compressed MVP |
> |---|---|---|
> | 0 Preparation & decisions | 1.5 h | 20 min |
> | 1 Backend foundation | 1.5 h | 25 min |
> | 2 Registration import, claiming & login | 2 h | 30 min |
> | 3 In-app team formation (requests, notifications, lifecycle) | 3.5 h | 40 min |
> | 4 Game engine | 3 h | 35 min |
> | 5 Admin backend | 2.5 h | 30 min |
> | 6 Frontend foundation | 2 h | 20 min |
> | 7 Participant UI | 4.5 h | 50 min |
> | 8 Admin UI | 3.5 h | 40 min |
> | 9 Browser monitoring | 1 h | 15 min |
> | 10 Emergency controls (end-to-end) | 1 h | 15 min |
> | 11 Polish & animation | 3 h | 15 min (Priority-2 only) |
> | 12 Testing & rehearsal | 3.5 h | 35 min |
> | 13 Deployment & event day | 2 h | 20 min |

---

### PHASE 0 — Preparation & Decisions *(do not skip)*
**Goal:** Remove every blocker so building is never interrupted by a missing asset or an undecided rule.

**Tasks**
1. **Collect assets:** three logos (college, department, society) as transparent PNG/SVG; Robotics & Automation Society brand colors.
2. **Inspect the registration workbook:** confirm sheet name, header names (`name`, `roll number`, `email`, `eligibility`), the **40 placeholder rows** (name `1`…`40`, roll like `CAE26D001`, email like `cae.participant01@gmail.com`), unique roll numbers, correct emails, eligibility values (TRUE/FALSE). Make a **backup copy**. Decide the rule that marks a record as placeholder (default: numeric-only name).
3. **Decide who gets the placeholder credentials** and how (printed slips / WhatsApp): each late-added student needs their **roll number + email** from a placeholder row — plan this distribution now.
4. **Write the six challenges** into `challenges.json`: title, narrative, body, objective, difficulty (1–5), base seconds, Hint 1, Hint 2, expected answer (+ accepted variants). Suggested levels: Decode (15 min), AI/ML (18), Data/Pattern (22), Cybersecurity (22), Coding/Debugging (23), Final reasoning/Master key (20).
5. **Decide policies and freeze them in `.env`/settings:**
   - Team size 3–4; request expiry; whether offline people can be requested.
   - Ranking order (*completion → fewest hints → fewest violations → fastest* **or** *completion → fastest → hints → violations*).
   - Max carry-over (300 s), max warnings (3), retry policy after reject, timeout policy (admin decides).
6. **Resend:** **no setup needed for the event.** Keep the keys blank/optional in `.env`. (Do not spend time verifying a sending domain.)
7. **Create `.env` and `.env.example`** (see Part B); add `.env` and `data/event.db` to `.gitignore`.
8. **Set up repo, Python venv (`fastapi uvicorn openpyxl python-dotenv`), README with run command.**

**Deliverables:** assets folder, validated workbook, `challenges.json`, `.env`, written policy decisions.
**Exit criteria:** you can load the workbook in Python and print counts of *real* vs *placeholder* vs *ineligible* rows; all six challenges + answers exist.

---

### PHASE 1 — Backend Foundation
**Goal:** A running FastAPI server with database, config, logging and the core "truth" helpers.

**Tasks**
1. `config.py`: load `.env`; fail loudly on missing critical keys (Resend keys are **not** critical).
2. `db.py`: SQLite connection (WAL mode), schema creation on startup, query helpers. Wrap multi-step changes (accept request → create team; accept submission → unlock next) in **transactions**.
3. `main.py`: FastAPI app, mount `/frontend` static, `/health` endpoint.
4. **Event log service:** `log_event(type, team_id, participant_id, level, message)` used everywhere — powers the admin live feed and post-event audit.
5. **Time helpers:** one `now()` (UTC server clock); `compute_remaining(level_state)`.
6. **Session helpers:** `create_participant_session()`, dependency `get_current_participant()` (header `X-Session`), dependency `require_admin()` (admin token). Update `last_seen` inside `get_current_participant` for presence.
7. **Event-phase helper:** `get_phase()`, `require_phase(...)` guards (team requests only in `TEAM_FORMATION`).
8. **Challenge loader:** `public_challenge(level)` (no answer/hints) and `admin_challenge(level)` (with answer).
9. Standard error format `{ "error": "CODE", "message": "..." }` so the frontend can show friendly/game-style messages.

**Deliverables:** server starts, `/health` OK, DB created, tables exist.
**Exit criteria:** restart the server and data persists.

---

### PHASE 2 — Registration Import, Placeholder Claiming & Login
**Goal:** Only eligible registered people get in; placeholder records can be claimed exactly once; identity is never based on name alone.

**Tasks**
1. **Importer (`importer.py`)** — on startup and via `POST /admin/reload-participants`:
   - Read `.xlsx`/`.csv`; normalize headers; normalize values (trim; roll → UPPERCASE; email → lowercase; eligibility → boolean).
   - Upsert by `roll_number`: **never overwrite** a record that is already `claimed`/`logged_in` (reload must be safe mid-event — only add new rows and update eligibility/email for untouched rows).
   - Set `is_placeholder` (numeric/empty name), store `original_name`, `claimed=false`, status `REGISTERED`.
   - Report counts (real / placeholder / ineligible / skipped-with-reason) to the log and admin.
2. **`POST /api/participant/login`** `{name, roll_number, email}` — backend logic:
   1. Look up by roll number (normalized). Unknown → `ACCESS_DENIED` (generic message; don't reveal which field was wrong).
   2. Email (normalized) must equal stored email. Else `ACCESS_DENIED`.
   3. `eligibility` must be TRUE. Else `ACCESS_DENIED`/`NOT_ELIGIBLE` (generic).
   4. **If the record is a placeholder:**
      - Not yet claimed → **name not compared**; validate the submitted name (rules in Part A #4); inside one transaction set `name = submitted`, `claimed = true`, `claimed_at = now`, status `CLAIMED` → `LOGGED_IN`, generate session token, log `PLACEHOLDER_CLAIMED`.
      - Already claimed → if request carries the matching session token → **resume**; otherwise return *"This registration has already been claimed. Please contact the event administrator."*
   5. **If the record is an existing participant:**
      - Submitted name must match stored name (case-insensitive, whitespace-normalized). Else `ACCESS_DENIED`.
      - Not yet logged in → mark `LOGGED_IN`, create session, log `PARTICIPANT_LOGGED_IN`.
      - Already logged in → resume with token, else *"This participant is already registered in the current event. Please contact the event administrator."*
   6. All of 4/5 must be atomic (a race between two people claiming the same placeholder must produce exactly one winner — rely on a conditional `UPDATE ... WHERE claimed = 0` and check rows affected).
3. **`GET /api/participant/me`** — profile, team (if any), event phase, pending-request counts, and the "next screen" the UI should show. Makes refresh safe.
4. **Rate-limit** login attempts per IP (simple in-memory counter) to stop roll/email guessing.
5. **Presence:** updated on every authenticated call (`last_seen`); `online = now - last_seen < ONLINE_THRESHOLD_SECONDS`.
6. **Admin helpers (backend):** `reset-session` (clears token/`logged_in`, keeps claim), **`unclaim`** (restore placeholder: `name = original_name`, `claimed = false`, clear token, remove from team only if formation still open), `reload-participants`.
7. **Never identify by name** in any query, foreign key or URL — always `participant_id`/`roll_number`.

**Deliverables:** working login/claim API.
**Exit criteria (tests):**
| Case | Expected |
|---|---|
| Existing participant, all correct | ✔ logged in |
| Existing participant, wrong name | ✘ denied |
| Placeholder, correct roll+email, any name | ✔ name replaced, `claimed=true` |
| Same placeholder, second person | ✘ "already been claimed" |
| Same placeholder, same browser refresh | ✔ resume |
| Wrong email / unknown roll / eligibility false | ✘ denied |
| Two simultaneous claims of one placeholder | exactly one succeeds |
| Admin unclaim → claim again | ✔ works, audit logged |

---

### PHASE 3 — In-App Team Formation (Requests, Notifications, Lifecycle)
**Goal:** Team formation happens entirely inside the app through a live lobby. Participants find each other, request, accept/reject — all server-enforced, all inside the app.

**3A. Event phase control**
1. Settings key `event_phase` with admin endpoints `open-formation`, `lock-formation`, (`unlock-formation` for emergencies). `start-event` auto-locks.
2. **Participant status transitions** (§6): login → `LOGGED_IN`; while phase is `TEAM_FORMATION` and the participant has no team → `TEAM_FORMING` (shown as ● AVAILABLE); accepting/joining a team → `IN_TEAM`; event start → `PLAYING`; then `ELIMINATED` / `COMPLETED`. Unclaim/reset returns the record to `REGISTERED`.
3. Guards: team requests/accepts/leave/rename/ready only in `TEAM_FORMATION` (or when `formation_override` is on). After start: *no new requests, no team changes, no new members* unless admin overrides.

**3B. Lobby / available participants — `GET /api/team/lobby`**
1. Returns only participants with `logged_in AND team_id IS NULL AND eligibility AND status not eliminated` (excluding self). **Placeholders that were never claimed never appear** (they're not logged in).
2. Per entry: `participant_id`, display name, roll number, online flag, and a **relationship to me**: `NONE | REQUEST_PENDING (sent by me) | REQUEST_RECEIVED`. (Never expose email.)
3. Also returns: my status, my team (members, leader, size x/max, status, roster_confirmed), outgoing pending requests, incoming pending requests, phase, and unread notification count — **one cheap call per poll**.

**3C. Sending a request — `POST /api/team/requests {receiver_id}`**
Validate (all server-side, in a transaction):
- Phase is `TEAM_FORMATION`; sender logged in.
- Receiver exists, eligible, logged in, **has no team**; receiver ≠ sender.
- Sender has **no team** *or* sender is the **leader** of a team with `size + pending_to_distinct_people < max` *(optional: don't count pending against capacity — decide in Phase 0; the accept step re-checks capacity anyway)*.
- **Duplicate prevention:** no existing `PENDING` request sender→receiver (UI shows **REQUEST PENDING**); no reverse `PENDING` request (return "already requested you — check notifications").
- Create `team_requests` row with `expires_at`, create notification for receiver (`TEAM_REQUEST_RECEIVED`), log `TEAM_REQUEST_SENT`.

**3D. Accepting — `POST /api/team/requests/{id}/accept` (receiver only)**
One transaction, re-checking everything at accept time:
1. Request still `PENDING` (not expired/cancelled).
2. Sender still eligible; receiver still eligible.
3. **Receiver has no team** (and sender has none *or* is leader of a team) — otherwise *"This participant is already part of a team."*
4. Phase still `TEAM_FORMATION`.
5. Team size < max (when adding to an existing team).
6. If sender had no team → **create team** (`TEAM-00x`, leader = sender, name default = ID); add both. Else add receiver to sender's team.
7. Update both participants (`team_id`, status `IN_TEAM`), set request `ACCEPTED`, `responded_at`.
8. **Auto-cancel** all other `PENDING` requests involving the receiver (to or from) and, if the team just became full, all pending requests from that team's leader. Notify affected senders.
9. Recompute team status: `size ≥ MIN` → `READY`; else `FORMING / WAITING_FOR_MEMBERS`.
10. Notify the sender (`REQUEST_ACCEPTED`) and, for existing teams, all members (`TEAM_MEMBER_JOINED`); log `TEAM_REQUEST_ACCEPTED`, `TEAM_CREATED` / `TEAM_MEMBER_JOINED`, and `TEAM_READY` when it crosses the minimum.
11. Race safety: two senders → one receiver accepting twice, or two receivers filling the last seat — the transaction's re-check (`UPDATE participants SET team_id=? WHERE id=? AND team_id IS NULL`, rows-affected check) must make exactly one win.

**3E. Rejecting / cancelling / expiring**
- **Reject** (receiver): `REJECTED`, notify sender ("*Rahul declined the team request.*"), the request disappears from Rahul's pending list.
- **Cancel** (sender, optional-but-included): `CANCELLED`; receiver's popup disappears on next poll.
- **Expire:** lazy check in lobby/notification calls + a background task flips overdue `PENDING → EXPIRED`; notify sender.

**3F. Team management during formation (leader only)**
- `POST /api/team/rename {name}` (unique, 3–30 chars, sanitized).
- `POST /api/team/ready` / `unready` — roster confirmation: sets `roster_confirmed`, cancels the team's other pending outgoing requests; blocked below the minimum size.
- Leader sees the lobby list to **REQUEST MORE MEMBERS** while `size < max`; non-leader members and full teams see the list **hidden/read-only**.
- *(Optional / cut-list)* `POST /api/team/leave` for non-leaders before lock.

**3G. Notifications — `GET /api/notifications`, `POST /api/notifications/{id}/read`**
- Types: `TEAM_REQUEST_RECEIVED`, `REQUEST_ACCEPTED`, `REQUEST_REJECTED`, `REQUEST_CANCELLED`, `REQUEST_EXPIRED`, `TEAM_MEMBER_JOINED`, `TEAM_READY`, `REMOVED_FROM_TEAM`, `FORMATION_LOCKED`, `ADMIN_MESSAGE`.
- Incoming requests are **actionable** (accept/reject) while `PENDING`; outcomes are informational toasts.
- Polling contract: client polls every 2–3 s; the response is small and uses `?since=<id>` so only new items return. (The change document prefers WebSocket but explicitly accepts polling for this event size; a WebSocket push can replace polling later and the API shape stays the same.)

**3H. Admin team-formation overrides (service layer; endpoints in Phase 5)**
`add-member`, `remove-member`, `create-team`, `merge/move` — **only in the formation phase** unless `formation_override` is enabled (every use logged in `admin_actions` with reason). Recompute team status and notify affected participants after each change.

**3I. Remnant sweep (§32)**
Search the whole repo (code, routes, DB schema, UI strings, README, docs) for any trace of the previous email/code-based invitation flow — e.g. `grep -ri -E "invit|team.?code|resend" backend/services/formation.py backend/services/requests.py backend/routers frontend` — and remove or rename it. The only allowed Resend references are the optional `.env` keys and the unused `mailer.py`.

**Deliverables:** complete in-app team formation API.
**Exit criteria (tests):**
- A → B request appears as a popup for B within one poll; accept creates `TEAM-xxx` with A as leader.
- Leader requests C and D → team grows to 4; a 5th request is impossible; a pending request when the team becomes full is cancelled.
- B can't join two teams (two senders race → one wins); double request shows REQUEST PENDING; reverse request is blocked with a helpful message.
- Reject / cancel / expire each notify the right person.
- Any request/accept after lock → rejected with *formation closed*.
- With Resend keys blank, everything above still works.

---

### PHASE 4 — Game Engine (Levels, Timer, Hints, Submissions)
**Goal:** The authoritative level pipeline — the heart of the event.

**Tasks**
1. **Event start:** `POST /admin/event/start` — requires phase `TEAM_FORMATION` or `FORMATION_LOCKED`; auto-locks formation, marks the phase `GAME_STARTED`, and for each team with size ≥ `MIN_TEAM_SIZE` initializes level states (Level 1 `UNLOCKED`, others `LOCKED`), sets team `PLAYING`, participants `PLAYING`. Teams below minimum / unassigned participants are returned as a warning list for the admin (they can still be fixed by an explicit override).
2. **`GET /api/game/state`** (polled every 2–3 s) for the caller's team: team status, pipeline, current level, `remaining_seconds`, `server_time`, hint usage + revealed hint text, submission status, last admin decision/note, paused flag, event message. **Public challenge text only when the level is ACTIVE.**
3. **`GET /api/game/level/{n}/intro`** — title, difficulty, base time, available time (base + carry-over), objective, warning text. No challenge body.
4. **`POST /api/game/level/{n}/start`** — validate: level `UNLOCKED`, not paused, team not eliminated, no other level ACTIVE. Set `started_at = now`, `allowed_seconds = base + min(carryover, MAX)`, status `ACTIVE`. **Idempotent** (a second teammate pressing START must not restart the clock). Log `LEVEL_STARTED`.
5. **Hints** `POST /api/game/level/{n}/hint {hint_number}` — only when ACTIVE; record once per hint (idempotent); return hint text only now; log `HINT_REQUESTED`. Hint text visible to all teammates via game state.
6. **Submit** `POST /api/game/level/{n}/submit {answer, explanation}` — ACTIVE, time remaining > 0, non-empty answer and explanation, no pending submission, caller belongs to the team. Store `submitted_by`, times, hint flags; status → `UNDER_REVIEW`; **freeze remaining**; monitoring OFF; log `ANSWER_SUBMITTED`. Compute `matches_expected` for admin display only.
7. **Review decisions** (service functions used by admin endpoints):
   - **Accept** → level `COMPLETED`; `carryover = min(remaining, MAX_CARRYOVER)`; next level `UNLOCKED` (or team `COMPLETED` after Level 6 with total time/hints/violations) — **one transaction**.
   - **Reject** → `REJECTED`, then back to `ACTIVE` per retry policy with frozen remaining restored (shift `started_at` forward by the review duration).
   - **Clarify** → back to editable with admin message.
8. **Timeout monitor:** background task (~2 s) + lazy check in state calls: `remaining ≤ 0` and `ACTIVE` → `TIMEOUT`, log `LEVEL_TIMEOUT`, raise admin alert. Late submits refused unless admin intervenes.
9. **Time adjustments:** `adjustment_seconds` (± admin).
10. **Pause:** team/event pause freezes the clock (`paused_at`; on resume add gap to `paused_seconds`); start/submit/hint refused while paused.
11. **Ranking query** `GET /admin/leaderboard` per the Phase 0 policy; always expose raw metrics.

**Deliverables:** full level lifecycle through API.
**Exit criteria (tests):** scripted team goes through all six levels; clock survives refresh; carry-over capped; Level 2 stays locked until accept; duplicate START doesn't reset timer; hint recorded once.

---

### PHASE 5 — Admin Backend
**Goal:** All data and controls the Control Center needs, behind admin auth.

**Tasks**
1. **Admin login** `POST /api/admin/login` — compare to `.env` (prefer hash; else `secrets.compare_digest`); random admin token; basic rate limit.
2. **Overview** `GET /api/admin/overview`: participants (total / **claimed** / **unclaimed placeholders** / logged in), teams (forming / ready / playing), pending requests, under review, alerts, eliminated, completed, **current event phase**.
3. **Participants** `GET /api/admin/participants` (search/filter): name, roll, email, **is_placeholder, claimed, claimed_at**, login time, team, status, online. Filters: *placeholders unclaimed*, *no team*, *logged in*.
4. **Team requests** `GET /api/admin/team-requests` (filter by status): `Yogesh → Rahul — Pending/Accepted/Rejected/Cancelled/Expired`, timestamps. Admin can **cancel** a pending request.
5. **Teams** `GET /api/admin/teams` and `GET /api/admin/teams/{id}`: leader, members (online), size, status, roster confirmed, created/started times, level timeline, submissions, violations, admin actions.
6. **Formation controls:** `open-formation`, `lock-formation`, `unlock-formation`, toggle `formation-override`.
7. **Formation overrides:** `add-member`, `remove-member`, `create-team`, `move/merge` (formation phase only unless override on; reason required, logged, participants notified).
8. **Submissions queue** `GET /api/admin/submissions?status=pending` — answer, explanation, expected answer, `matches_expected`, hints, time used/allotted, submitted-by. **Decisions:** `accept | reject | clarify` (optional note).
9. **Alerts + feed** `GET /api/admin/feed?since=<id>` (cursor). Alert types: timeout, violation count, pending submission, elimination, **team below minimum at lock**, **participants without team at lock**.
10. **Control actions** (all write `admin_actions` with reason): `unlock-level` (team/all), `add-time` (team/all, ±), `pause/resume` (team/event), `eliminate/restore`, `bypass-level`, `reopen-level`, `start-event`, `reset-session`, `unclaim`.
11. **Settings endpoint** (carry-over, warnings, team size, request expiry) and **Export** `GET /admin/export` (CSV of teams, members, requests, submissions, violations, hints).

**Deliverables:** admin API complete and protected.
**Exit criteria:** every endpoint is 401 without admin token; accept/reject changes participant-visible state within one poll; every control logs an `admin_actions` row; formation overrides are refused after game start unless override is on.

---

### PHASE 6 — Frontend Foundation & Design System
**Goal:** The reusable visual + technical layer so screens can be built quickly and consistently.

**Tasks**
1. **Design tokens** (`tokens.css`): Background `#F7F9FC`, Surface `#FFFFFF`, Text `#172033`, Secondary `#667085`, Border `#E4E7EC`, Primary `#4F46E5`, Secondary accent `#06B6D4`, Success `#16A34A`, Warning `#F59E0B`, Danger `#DC2626`, Locked `#98A2B3`.
2. **Typography:** Inter / Plus Jakarta Sans for UI; JetBrains Mono for technical labels and timer digits only.
3. **Components:** bento card, status chip (icon + text + color), button (primary/secondary/danger + loading), input, textarea, modal, toast, overlay, pipeline, timer, connection indicator, empty state, **notification bell with badge**, **notification panel**, **request popup modal**, **participant card** (name, roll, online dot, action button states: `REQUEST TEAM` / `REQUEST PENDING` / `CANCEL REQUEST`).
4. **Layout shell:** header with **three logos balanced** around the title, plus bell + connection indicator; 1280×720 minimum target.
5. **Icon set:** one library (e.g. Lucide).
6. **JS infrastructure:**
   - `api.js`: fetch wrapper (token, error format, retries, connection indicator).
   - `poller.js`: `setInterval` 2–3 s with back-off; separate cadence for lobby/game state and notifications (can be one combined poll); emits `stateChanged` + `notificationsArrived` events; pauses when offline. Keep the transport behind this module so it can be swapped for a WebSocket (preferred transport per the change document) without touching the UI.
   - `state.js`: tiny store + view router (login → formation lobby → team panel → game dashboard → intro → challenge → review → result).
   - `timer.js`: local 1-second tick re-synced from server remaining.
7. **Accessibility baseline:** visible focus, keyboard navigation, `prefers-reduced-motion`, no color-only status.
8. **Terminology map** in one JS file (Participant→Operator, Level→Security Layer, Answer→Recovery Key …). Use lightly; clarity first. Use **Request Team / Pending Request / Accept / Reject / Team Notification** consistently.

**Deliverables:** a style-guide test page showing every component and state.
**Exit criteria:** components render correctly at 1280×720 and 1920×1080.

---

### PHASE 7 — Participant UI
**Goal:** The full player journey, from landing to NEXUS restored, built against the real APIs.

| # | Screen | What to implement |
|---|---|---|
| 1 | **Landing** | Logos, title, tagline, **Participant Login** / **Admin Control Center**; soft CSS-only grid background. |
| 2 | **Login — "Nexus Recovery Access"** | Name / Roll / Email, helper text *"Enter the details used during event registration."* **Verify Access** with *"◌ VERIFYING IDENTITY…"*. Success: ✓ + short particles (<1 s). Failure: shake + red border + "ACCESS DENIED". Special messages: *"This registration has already been claimed. Please contact the event administrator."* and *"This participant is already registered…"*. For first-time placeholder claims, add a small helper: *"If this is your first login, the name you enter becomes your operator name."* |
| 3 | **Team Formation lobby (main screen after login)** | Header: *TEAM FORMATION*, **YOUR STATUS** (● AVAILABLE), **YOUR TEAM** (NOT CREATED). **AVAILABLE PARTICIPANTS** as live cards: name, roll number, ● Online / ○ Offline, button **REQUEST TEAM**. States per card: `REQUEST TEAM` → after click **REQUEST PENDING** (with **CANCEL REQUEST** link) → disappears when the person joins a team. If the person sent *me* a request, card shows "Requested you — see notifications". Loading state *"◌ SENDING REQUEST…"*; confirmation text *"Request sent — waiting for Rahul."* Search/filter box for 50+ people. List refreshes by polling (no manual refresh). |
| 4 | **Notification bell + panel** | Bell with unread badge in header (🔔 2). Panel lists notifications: actionable **Team Request** cards with **[ACCEPT] [REJECT]**, plus informational items (declined, cancelled, expired, member joined, team ready, removed, formation locked). |
| 5 | **Incoming request popup** | When a `TEAM_REQUEST_RECEIVED` arrives, show an interactive modal/toast immediately: *"🔔 TEAM REQUEST — Yogesh Kondapathni wants to form a team with you. [ACCEPT] [REJECT]"*. Don't stack many modals: queue them or collapse into the bell with a banner. Disable the buttons while the request is processing; show a friendly message if the request is no longer valid (*"This request is no longer available."* / *"This participant is already part of a team."*). |
| 6 | **Team panel (once in a team)** | *TEAM: CYBER SENTINELS* (editable by leader), members with ✓ and leader badge, **TEAM SIZE x / 4**, **STATUS**: `WAITING FOR MORE MEMBERS` (<3) / `READY` (≥3) / `TEAM FULL` (4). Non-leader members: the available list is hidden. Leader: **[ REQUEST MORE MEMBERS ]** reveals the available list (while size < 4) and **[ READY ]** (roster confirmation, undoable until lock). Live updates when members join. |
| 7 | **Declined / cancelled feedback** | Sender sees toast *"Rahul declined the team request."*; the card returns to `REQUEST TEAM`. |
| 8 | **Formation locked / waiting lobby** | After lock: requests disabled, banner *"TEAM FORMATION LOCKED"*, team panel read-only: *"NEXUS IS WAITING FOR RECOVERY OPERATORS."* / "Waiting for event start…". Participants without a team see *"You are not part of a team. Contact the event organizers."* |
| 9 | **Main dashboard (most polished)** | Bento: NEXUS status, team status, time bank, **Recovery Pipeline** (locked / unlocked / active-pulse / completed ✓ / timeout ✕), "Current Mission" card + **Enter**. Names: Decode, AI Core, Data Vault, Security Breach, Debug Protocol, Nexus Core. |
| 10 | **Level intro** | Difficulty dots, available time (incl. carry-over), objective, warning that timer can't be paused and monitoring turns on, **START LEVEL**. Challenge not shown yet. |
| 11 | **Active challenge** | `LEVEL n / 6` + large timer; challenge dominates; answer + explanation fields; *"This is a collective team submission. Discuss with your teammates before submitting."*; two hint cards (hover text → reveal via API → REVEALED card, visible to the whole team); Submit → **confirmation modal** (answer + explanation) → *"◌ TRANSMITTING…"*. Draft text survives refresh (`localStorage` keyed by team+level). |
| 12 | **Under review** | "✓ SUBMISSION TRANSMITTED — control center is evaluating", read-only fields, timer frozen, ⏳. |
| 13 | **Decisions** | **Accepted:** check + small confetti (1–2 s) → **unlock animation** → **Enter Level n**. **Rejected:** red shake, "NEXUS REJECTED THE RECOVERY KEY", admin note, retry. **Clarification:** admin message, editable fields. |
| 14 | **Timeout** | "TIME EXPIRED — awaiting instructions from the NEXUS Control Center"; read-only. |
| 15 | **Paused** | Overlay "EVENT PAUSED", timer frozen. |
| 16 | **Violation overlay** | Warning 1/3, 2/3 ("Final warning"), strong but dismissible overlay. |
| 17 | **Eliminated** | "SYSTEM LOCKDOWN", red overlay, pipeline fades, "contact organizers if this was an error". |
| 18 | **Victory** | "NEXUS ONLINE / MASTER ACCESS GRANTED", team, completion time, hints, violations; large confetti. |
| 19 | **Connection indicator** | ● CONNECTED / ● RECONNECTING… / ● OFFLINE; during outage: "Your game state is still maintained by the control server. Please remain on this page." Never claim an answer/request is lost. |

**Implementation notes**
- The UI is a **function of server state** (`/me`, `/team/lobby`, `/game/state`): on each poll decide which screen to show; transitions fire when the previous state differs from the new one.
- Disable buttons during requests; all endpoints are idempotent against double-clicks.
- Animations never block workflow; unlock overlay ≈1.5 s but **Enter** is instantly clickable.
- Timer states: normal → 5:00 pulse → 2:00 prominent → 1:00 urgent (pulse + flash + countdown; optional beep).
- The participant should **never wonder "did my request reach them?"**: always show *"Request sent — waiting for <name>"*, and the receiver sees the popup immediately.

**Deliverables:** participant app complete against the real backend.
**Exit criteria:** with two browsers, a user can claim a placeholder, request a teammate, accept in the other window, and reach the lobby/dashboard without touching the API manually; refresh on any screen returns to the right screen.

---

### PHASE 8 — Admin UI (Control Center)
**Goal:** One organizer understands the whole event in seconds and acts instantly.

**Layout priority:** Alerts → Teams playing → Pending submissions → **Team formation** → Progression → Timers → Violations → Hints → History.

**Tasks**
1. **Admin login** (separate, token in `sessionStorage`; logout).
2. **Top stat cards:** Participants (claimed / unclaimed placeholders), Teams (forming / ready), Playing, Under review, Alerts, **Event phase** chip.
3. **Event phase bar (sticky):** `REGISTRATION → TEAM FORMATION → LOCKED → GAME STARTED` with buttons **Open Team Formation**, **Lock Formation**, **START EVENT** (confirmation dialog listing teams below minimum and participants without a team).
4. **Persistent alerts panel:** 🔴 timeout, 🟠 violation x/3, 🔵 pending submission, ⚠ team below minimum, ⚠ participants without team. Click → opens the relevant team/participant.
5. **Formation panel (new):**
   - **Team requests table:** `Yogesh → Rahul`, status chip (Pending/Accepted/Rejected/Cancelled/Expired), time; cancel-pending action.
   - **Teams forming:** name, leader, members, size x/4, status (WAITING FOR MORE MEMBERS / READY).
   - **Participants without team** list with quick actions.
   - **Override actions:** ADD MEMBER, REMOVE MEMBER, CREATE TEAM, MERGE / MOVE — enabled in formation phase (disabled with a tooltip afterward unless *formation override* toggle is on); confirmation + reason field.
6. **Live teams table:** team, level, timer, status chip (icon+text+color), hints, violations; sort/filter/search; pending-review rows pinned top.
7. **Event feed:** timestamped list incl. `PLACEHOLDER_CLAIMED`, `TEAM_REQUEST_SENT/ACCEPTED/REJECTED`, `TEAM_CREATED`, level events; cursor polling.
8. **Submission review card:** team + level, expected answer, team answer (✓/✗ helper), explanation, hints, time used/allotted, submitted-by; **Accept / Reject / Clarify** with note; queue navigation; toast on new submission.
9. **Team detail drawer:** leader, members, times, level timeline, violations, submission history, admin action history, **per-team controls** (unlock level, +1/+5 min, pause/resume, eliminate/restore, bypass, reopen).
10. **Participants tab:** table with search; badges **PLACEHOLDER (unclaimed)** / **CLAIMED** / **EXISTING**; actions: reset session, **unclaim**, assign to team.
11. **Event Control panel (visually separated):** PAUSE ALL, RESUME, UNLOCK LEVEL n FOR ALL, ADD 5 MIN TO ALL; danger styling only for dangerous actions; confirmation dialogs (eliminate, unlock-all, pause-all, remove time).
12. **Leaderboard tab** per ranking policy + raw metrics; export button.
13. **Settings tab** (carry-over, warnings, team size, request expiry) and **Challenge answer sheet** (read-only panel of all six challenges + answers).

**Deliverables:** admin dashboard complete.
**Exit criteria:** with 10+ simulated teams, a new request/submission appears within ~3 s, and every control visibly changes the participant side within one poll.

---

### PHASE 9 — Browser Monitoring & Violations
**Goal:** Detect leaving the game window during ACTIVE levels only, fairly and reliably.

**Client (`monitor.js`)**
1. Monitor **only** when server state says the level is `ACTIVE` and the event isn't paused — **not** during login, team formation, lobby, review, transitions, or finished game.
2. Listen to `visibilitychange` (hidden) and `window.blur`; treat both as one incident with a **1–2 s debounce**; ignore blur caused by your own modals.
3. Optional hardening (only if time): fullscreen at level start; detect fullscreen exit; block context-menu/copy on challenge text. Don't promise what browsers can't enforce.
4. On incident → `POST /api/game/violation {type, level}`; server re-validates level ACTIVE and not paused.

**Server (`monitoring.py`)**
1. Insert violation with `warning_number = count + 1` (per team), log `TAB_SWITCH_DETECTED`, raise admin alert.
2. 1 → warning; 2 → final warning; 3 → `team.status = ELIMINATED`, level access blocked, log `TEAM_ELIMINATED`.
3. Admin override: restore team + reset/decrement counter (logged).
4. Grace: ignore events in the first ~2 s after page load and while paused; drop duplicates inside the debounce window.

**UI:** participant warning overlay (1/3, 2/3); admin violation alert (team, member, level, type, time, count).

**Exit criteria (tests):** one tab switch = exactly one increment; switching during lobby/team formation/review does nothing; third violation eliminates; restore works; refresh doesn't create a violation.

---

### PHASE 10 — Emergency Controls (End-to-End Hardening)
**Goal:** Guarantee the organizer can rescue any situation.

**Tasks**
1. Verify each control on **both sides** (admin action → participant reaction): unlock for team / for **all** ("NEXUS Control Team has overridden the previous security layer"), add/remove time, pause team/event, eliminate/restore, bypass a broken challenge, reopen timed-out level, reset session, **unclaim record**, **formation overrides** (add/remove/move/merge/create team), formation unlock/override.
2. Make every action **idempotent** and **logged** (who/when/why).
3. Add a **repair endpoint** to directly set a team's `current_level`, level statuses, remaining time, and membership if state ever gets corrupted.
4. Rehearse worst cases: too-hard challenge, server hiccup, accidental elimination, timer problem, browser false positive, ambiguous answer, **participant can't find a teammate**, **team stuck below minimum at lock**, **placeholder claimed by the wrong person**.

**Exit criteria:** every item in the *Emergency* section of the Pre-Event Checklist (Part D) is ticked.

---

### PHASE 11 — Polish, Animation & Sound
**Goal:** Make it feel like a game — **only after Phases 1–10 work.**

**Priority 2 (do these)**
- Login success check + particles; failure shake.
- **Request sent** micro-animation, **incoming request popup** slide-in (with bell shake), **team formed** success pulse.
- Lock-opening unlock animation with glow (≈1–1.5 s).
- Hint reveal flip; submission "data transmission" feedback.
- Accept check + small confetti (1–2 s); reject red shake.
- Low-time timer states (5:00 / 2:00 / 1:00).
- Victory celebration; elimination lockdown.
- Branding pass: balanced logos, subtle circuit/grid motifs, game wording.
- Bento polish; admin status chips/icons.

**Priority 3 (only if everything else is done):** sound pack (user-gated; must work with sound off), advanced particles, 3D, page transitions, charts, fancy leaderboard.

**Rules:** each animation answers *"what just happened?"*; respect `prefers-reduced-motion`; no flashing; never delay timer/submit/access/warnings; one dominant action per screen.

---

### PHASE 12 — Testing & Rehearsal
**Goal:** Prove the system under realistic conditions before real participants touch it.

1. **Functional test matrix**
   - **Registration/claiming:** existing correct/wrong-name; placeholder claim with any name; double claim; simultaneous claim; wrong email; ineligible; resume; admin reset/unclaim; workbook reload mid-event doesn't overwrite claimed records.
   - **Team formation:** request → popup → accept; reject; cancel; expire; duplicate request (REQUEST PENDING); reverse request; receiver accepts two requests concurrently (one wins, other auto-cancelled); leader adds 3rd/4th; 5th impossible; non-leader can't send; participant already in team can't be requested; formation locked blocks everything; admin add/remove/move/merge only in formation (or with override); team status flips WAITING ↔ READY correctly; **works with Resend keys blank**.
   - **Game:** Level 2 locked before accept; start doesn't auto-run; double start safe; hint once; empty answer rejected; submit after timeout blocked; accept → unlock; reject → retry; clarify; carry-over cap; Level 6 → completed.
   - **Timer:** refresh keeps correct time; devtools tampering doesn't change server time; pause/resume; +5 min.
   - **Monitoring:** single increment; only during ACTIVE; elimination at 3; restore.
   - **Admin:** auth required; all controls; feed cursor; leaderboard order; request table matches reality.
2. **Security checks:** view-source/Network shows no admin password, Resend key, expected answers, hint text (before reveal), challenge text (before start), other participants' **emails**, or other teams' data; participant tokens can't call admin endpoints; `team_id`/`participant_id` always derived from the session, never from the request body (the only IDs accepted from the client are *receiver_id* for requests and *request_id*, each re-validated against ownership).
3. **Load/soak:** ~60 simulated clients polling lobby + notifications every 2–3 s plus admin poller for 10+ min; no SQLite lock errors; responses < 300 ms. Include a "burst": 60 clients logging in within 30 s and sending ~30 requests simultaneously.
4. **Full dress rehearsal:** 6–8 people (≥ 2 teams) on separate devices + 1 admin; use real placeholder records; form teams purely in-app; play all six levels with shortened timers (e.g. `LEVEL_x_TIME=120`). Deliberately: refresh at each screen, lose WiFi 20 s, tab-switch, time out one level, reject one answer, eliminate & restore, global unlock, claim a placeholder with a nickname, try to double-claim.
5. **Browser matrix:** Chrome, Edge, Firefox on laptops (visibilitychange + timer + popups).
6. **Fix list triage:** only fix bugs affecting the core loop; defer cosmetics.

**Exit criteria:** the dress rehearsal completes with zero unrecoverable states.

---

### PHASE 13 — Deployment & Event-Day Operations
**Goal:** A stable server, a practiced operator, and a fallback for everything.

**Deployment**
1. Host on a **laptop/server on the same LAN** (most reliable in-room) or a cloud VM. Single Uvicorn worker (SQLite + background tasks).
2. Process manager / auto-restart (`systemd`, `pm2`, or a restart loop); DB on persistent disk.
3. HTTP on LAN is fine; HTTPS only if public. Give participants a short URL + QR code.
4. Production `.env`: real timers (`900/1080/1320/1320/1380/1200`), strong admin credentials, `APP_ENV=production`. **Resend key not required.**
5. Lock down: remove debug routes, restrict CORS, disable auto-reload.
6. **Backups:** copy `event.db` every few minutes; keep the original workbook backed up.
7. **Time sync:** verify server clock (NTP).

**Event-day runbook**
- **T-60:** start server, load the workbook (check the real/placeholder counts match expectations), log in as admin, verify six challenges + answers, logos load.
- **T-30:** verify Wi-Fi capacity for ~60 laptops + admin; distribute placeholder credentials (roll + email) to late-added students.
- **T-20:** admin presses **Open Team Formation**; participants log in and form teams in-app; admin watches *Participants without team* and the *Team requests* table; helps stragglers.
- **T-5:** admin checks teams below minimum; fixes with **Add member / Move / Merge / Create team**; then **Lock Formation**.
- **T-0:** admin presses **START EVENT** → READY teams get Level 1.
- **During:** admin watches alerts → pending submissions → timeouts → violations; uses +time/unlock/bypass freely with reasons.
- **Finish:** leaderboard + export CSV; final DB backup.

**Fallbacks (print these)**
| Failure | Action |
|---|---|
| Server crashes | Restart (DB persists; participants refresh and resume with tokens; timers recompute). |
| Participant can't log in | Check workbook row (roll/email/eligibility); **Reset session** if a refresh issue; check for wrong claimant → **Unclaim**. |
| Placeholder claimed by the wrong person | Admin **Unclaim**, give correct student the record. |
| Participant can't find a teammate / team stuck at 2 | Admin **Add member / Move / Merge**. |
| Request popup not showing | Participant opens the bell panel; check polling/connection indicator; refresh (state is server-side). |
| Wi-Fi drops | Participants stay on page; indicator shows RECONNECTING; server clock keeps time; use **Add time** if the outage was long. |
| Challenge too hard / ambiguous | Accept manually, bypass, or **Unlock for all**. |
| Tab false positive | **Restore team** and reset strikes. |
| Formation locked too early | **Unlock formation** / enable override. |

---

## Part D — Priorities, Cut List & Definition of Done

### Must-have (never cut)
Workbook import with placeholder detection · **roll+email login and placeholder claiming** · **double-claim prevention** · duplicate prevention/session resume · **in-app team requests (send / accept / reject) with live notification popup** · team size 3–4 with READY logic · backend rule: no one in two teams · **formation phase lock** · team dashboard · six-level pipeline · server-authoritative level timer · answer + explanation submission · hint tracking · browser violation tracking · admin login · admin team list + **team requests view** · admin submissions + accept/reject · admin level unlock (team + global) · admin formation overrides · team elimination/restore · completion state.

### Cut in this order if time runs out
1. Sound effects, advanced particles, 3D, page transitions *(UI Priority 3)*
2. WebSockets (stay on polling — explicitly allowed by the change document) · charts · AI answer evaluation · event replay
3. Fullscreen enforcement and clipboard blocking
4. Fancy confetti → simple CSS check/pulse
5. Settings tab (edit `.env` instead)
6. Leave-team, team rename UI (keep default `TEAM-xxx` names), request search box
7. *(Never cut)* request cancellation is cheap — keep it; the spec explicitly asks for it as optional, but it prevents stuck lobbies.

### Definition of Done (ship checklist)
- [ ] An existing participant and a placeholder participant can each go landing → verified → lobby → request/accept → team panel → game with no manual intervention
- [ ] Placeholder name replaced by the claimant's name; second claim blocked in the backend
- [ ] Receiver sees a request popup without refreshing; sender gets accept/reject feedback
- [ ] No participant can be in two teams (verified at API level, including races)
- [ ] After formation lock/game start: no new requests or team changes (except admin overrides)
- [ ] **No email or Resend dependency anywhere in team formation**; remnant sweep (3I) is clean; app works with Resend keys blank
- [ ] Refresh on any screen returns to the correct state; server restart loses nothing
- [ ] Timer is server-driven; correct after refresh, pause, +time
- [ ] Level N+1 cannot be accessed without an accept/unlock (API-level)
- [ ] No secrets, answers, hint text, locked challenge text, or others' emails in the browser
- [ ] Admin sees requests/submissions/violations/timeouts within ~3 s and can act
- [ ] Every emergency control works and is logged
- [ ] Dress rehearsal passed on separate devices
- [ ] Backups running; runbook printed

### Pre-event checklist (condensed)
**Participant data:** [ ] workbook complete, headers right [ ] roll numbers unique [ ] emails correct [ ] eligibility flags correct [ ] placeholder rows detected (count matches 40) [ ] backup exists
**Admin:** [ ] login works, credentials not exposed [ ] dashboard loads [ ] team requests view works [ ] formation open/lock/override work [ ] submission review works [ ] unlock controls work
**Team formation:** [ ] request → popup works across two devices [ ] reject/cancel/expire work [ ] team size 3–4 enforced [ ] no double-team [ ] lock blocks requests
**Game:** [ ] Level 1 unlocks, Level 2 locked [ ] timer starts/ends correctly [ ] answer + explanation submit [ ] hints recorded [ ] violations recorded and reach admin [ ] accept/reject/unlock [ ] global unlock, add time, eliminate/restore
**Emergency:** [ ] workbook backup [ ] answers available to admin [ ] manual unlock/add time/bypass [ ] unclaim/reset session [ ] server restart procedure known [ ] network failure procedure known

### Recommended build order (if you are alone and short on time)
```
Phase 0 → 1 → 2 (import + claim + login) → 3 (requests + notifications + phase lock)
→ 4 (engine core) → 5 (accept/reject/unlock + formation admin)
→ 6 → 7 (login → lobby → request popup → team panel → challenge → submit)
→ 8 (formation panel + review + teams + controls) → 9 → 10
→ 12 (smoke test) → 11 (Priority-2 polish) → 13
```
Rationale: get **two browsers forming a team in-app, then one team playing one admin-approved level** end to end as early as possible, then widen.

---

## Part E — Quick Reference

### Core API surface
| Area | Endpoints |
|---|---|
| Participant | `POST /api/participant/login` (claims placeholders), `GET /api/participant/me` |
| Lobby & requests | `GET /api/team/lobby`, `POST /api/team/requests`, `POST /api/team/requests/{id}/accept`, `/reject`, `/cancel` |
| Team mgmt | `POST /api/team/rename`, `/ready`, `/unready`, *(optional)* `/leave` |
| Notifications | `GET /api/notifications?since=`, `POST /api/notifications/{id}/read` |
| Game | `GET /api/game/state`, `GET /api/game/level/{n}/intro`, `POST /api/game/level/{n}/start`, `/hint`, `/submit`, `POST /api/game/violation` |
| Admin auth | `POST /api/admin/login` |
| Admin read | `/overview`, `/participants`, `/team-requests`, `/teams`, `/teams/{id}`, `/submissions`, `/feed`, `/leaderboard`, `/export`, `/challenges` |
| Admin formation | `/event/open-formation`, `/event/lock-formation`, `/event/unlock-formation`, `/event/formation-override`, `/teams/create`, `/teams/{id}/add-member`, `/teams/{id}/remove-member`, `/teams/merge`, `/team-requests/{id}/cancel` |
| Admin act | `/submissions/{id}/accept|reject|clarify`, `/teams/{id}/unlock-level|add-time|pause|resume|eliminate|restore|bypass|reopen`, `/event/start|pause|resume`, `/event/unlock-level-all`, `/event/add-time-all`, `/participants/{id}/reset-session|unclaim|assign`, `/reload-participants` |

### Event types for the live feed
`PLACEHOLDER_CLAIMED` · `PARTICIPANT_LOGGED_IN` · `FORMATION_OPENED` · `TEAM_REQUEST_SENT` · `TEAM_REQUEST_ACCEPTED` · `TEAM_REQUEST_REJECTED` · `TEAM_REQUEST_CANCELLED` · `TEAM_REQUEST_EXPIRED` · `TEAM_CREATED` · `TEAM_MEMBER_JOINED` · `TEAM_READY` · `FORMATION_LOCKED` · `EVENT_STARTED` · `LEVEL_STARTED` · `HINT_REQUESTED` · `TAB_SWITCH_DETECTED` · `ANSWER_SUBMITTED` · `LEVEL_APPROVED` · `LEVEL_REJECTED` · `LEVEL_TIMEOUT` · `LEVEL_UNLOCKED` · `TEAM_PAUSED` · `ADMIN_TIME_ADJUSTED` · `ADMIN_TEAM_OVERRIDE` · `TEAM_ELIMINATED` · `TEAM_RESTORED` · `TEAM_COMPLETED`

### Golden rules to re-read when stuck
1. The browser never decides — it displays.
2. Identify people by `participant_id` / `roll_number`, **never by name**.
3. Roll number + email claim a record; name only matters for non-placeholder records.
4. Always derive the caller's identity/team from the session, never from the request body.
5. Every accept/claim/unlock is **one transaction** with a conditional update (`WHERE team_id IS NULL`, `WHERE claimed = 0`) — races must have one winner.
6. Reveal nothing early: challenge text on START, hint text on REQUEST, answers never.
7. No email, no codes — team formation lives entirely in the app.
8. Animations are feedback, never gates.
9. When in doubt, give the admin a button.
