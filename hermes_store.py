import os
import json
import sqlite3
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# 💾 HERMES DATA STORE: chat history and category suggestions
# A local SQLite file next to the scripts for the proof of concept; a database server in production (backlog S5)
# =====================================================================
DB_PATH = os.getenv("PORTAL_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_data.db")

def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    # Deleted rows are overwritten, not just unlinked, so a deleted conversation cannot be recovered from the file
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            user_name TEXT,
            title TEXT,
            state_json TEXT NOT NULL,
            ticket_id INTEGER,
            outcome TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS demo_sessions (
            token TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            user_name TEXT,
            created_at TEXT NOT NULL
        )""")
    # Suggested new categories for tickets that fit no existing category; read by the category review (F2 / F4)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS category_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            suggested_label TEXT NOT NULL,
            fallback_category TEXT,
            summary TEXT,
            source TEXT,
            created_at TEXT NOT NULL
        )""")
    # Review decisions on suggestions (added after the table first shipped, so older files get the columns here)
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(category_suggestions)")}
    for column, definition in (("status", "TEXT NOT NULL DEFAULT 'pending'"), ("approved_label", "TEXT"), ("decision_note", "TEXT"),
                               ("decided_by", "TEXT"), ("decided_at", "TEXT"), ("glpi_category_id", "INTEGER")):
        if column not in existing:
            connection.execute(f"ALTER TABLE category_suggestions ADD COLUMN {column} {definition}")
    # Decided routing suggestions (group in charge, regroup, reassign). Pending ones are worked out live from GLPI;
    # once decided, a suggestion (identified by its fingerprint) is never proposed again
    connection.execute("""
        CREATE TABLE IF NOT EXISTS routing_decisions (
            fingerprint TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            suggestion_json TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            result_json TEXT,
            decided_by TEXT NOT NULL,
            decided_at TEXT NOT NULL
        )""")
    # Technicians on leave (GLPI has no such field): Hermes does not assign them tickets during these dates
    connection.execute("""
        CREATE TABLE IF NOT EXISTS technician_absences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            starts_on TEXT NOT NULL,
            ends_on TEXT NOT NULL,
            note TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    # Every decision taken in the service desk admin section: who, what and when (append-only)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_email TEXT NOT NULL,
            actor_name TEXT,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    # Last known state of each ticket, so changes can be detected and turned into notifications
    connection.execute("""
        CREATE TABLE IF NOT EXISTS ticket_snapshots (
            ticket_id INTEGER PRIMARY KEY,
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    # Ticket updates waiting to be delivered to the intranet's notification bell
    connection.execute("""
        CREATE TABLE IF NOT EXISTS ticket_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            ticket_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT
        )""")
    # New tickets Hermes deliberately did not answer (automated senders, recalls, forwarded GLPI notifications,
    # duplicates), so the poller does not pick them up again and the console can report them
    connection.execute("""
        CREATE TABLE IF NOT EXISTS skipped_tickets (
            ticket_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )""")
    # ---- Safety controls (hermes_guard) ----
    # Settings that must survive a restart, such as the pause switch
    connection.execute("CREATE TABLE IF NOT EXISTS guard_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
    # Every change Hermes made (or, in read-only mode, would have made) in GLPI. Field values only, never message text.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            method TEXT NOT NULL,
            target TEXT NOT NULL,
            ticket_id INTEGER,
            fields_json TEXT NOT NULL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_agent_actions_created ON agent_actions (created_at)")
    # AI replies the safety checks withheld, and other safety events (automatic pauses)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS guard_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            ticket_id INTEGER,
            source TEXT,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    # Read-only (shadow) mode: what Hermes would have done with each production ticket
    connection.execute("""
        CREATE TABLE IF NOT EXISTS shadow_tickets (
            ticket_id INTEGER PRIMARY KEY,
            decision TEXT NOT NULL,
            analysis_json TEXT,
            created_at TEXT NOT NULL
        )""")
    # Satisfaction surveys: one per closed ticket and requester. Copied to GLPI's own survey on the ticket when allowed
    # (glpi_synced), so technicians see the rating in GLPI.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            ticket_id INTEGER NOT NULL,
            user_email TEXT NOT NULL,
            title TEXT,
            channel TEXT,
            category TEXT,
            team TEXT,
            technicians TEXT,
            closed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rating INTEGER,
            reason TEXT,
            comment TEXT,
            answered_at TEXT,
            dismissed_at TEXT,
            glpi_synced INTEGER NOT NULL DEFAULT 0,
            followed_up_by TEXT,
            followed_up_at TEXT,
            follow_up_note TEXT,
            PRIMARY KEY (ticket_id, user_email)
        )""")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_surveys_user ON surveys (user_email, status)")
    return connection

# ---------------- satisfaction surveys ----------------
SURVEY_DAYS = 14
SURVEY_REASONS = ("Not fixed", "Too slow", "Poor communication", "Other")

def open_survey(ticket_id, user_email, details):
    """Creates the survey for a closed ticket, once. A reopened and re-closed ticket keeps its first survey."""
    now = datetime.now()
    with connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO surveys (ticket_id, user_email, title, channel, category, team, technicians, closed_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, user_email.lower(), (details.get("title") or "")[:200], details.get("channel"), details.get("category"),
             details.get("team"), details.get("technicians"), now.isoformat(timespec="seconds"),
             (now + timedelta(days=SURVEY_DAYS)).isoformat(timespec="seconds")))
        return cursor.rowcount == 1

def pending_surveys(user_email):
    """Surveys this person can still answer, newest first. Dismissed ones are included (still answerable from My tickets)."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        rows = connection.execute(
            "SELECT ticket_id, title, closed_at, expires_at, dismissed_at FROM surveys WHERE user_email = ? AND status = 'pending' AND expires_at > ? "
            "ORDER BY closed_at DESC", (user_email.lower(), now)).fetchall()
    return [dict(row) for row in rows]

def user_surveys(user_email):
    """{ticket_id: {'status', 'rating'}} for one person; 'expired' once the answer window has passed"""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        rows = connection.execute("SELECT ticket_id, status, rating, expires_at FROM surveys WHERE user_email = ?", (user_email.lower(),)).fetchall()
    return {row["ticket_id"]: {"status": "expired" if row["status"] == "pending" and row["expires_at"] <= now else row["status"],
                               "rating": row["rating"]} for row in rows}

def get_survey(ticket_id, user_email):
    with connect() as connection:
        row = connection.execute("SELECT * FROM surveys WHERE ticket_id = ? AND user_email = ?", (ticket_id, user_email.lower())).fetchone()
    return dict(row) if row else None

def answer_survey(ticket_id, user_email, rating, reason, comment):
    """Records an answer once, while the survey is open. Returns False if it is not open any more."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE surveys SET status = 'answered', rating = ?, reason = ?, comment = ?, answered_at = ? "
            "WHERE ticket_id = ? AND user_email = ? AND status = 'pending' AND expires_at > ?",
            (rating, reason, comment, now, ticket_id, user_email.lower(), now))
        return cursor.rowcount == 1

def dismiss_survey(ticket_id, user_email):
    """The person closed the floating card: it stops popping up, but can still be answered from My tickets"""
    with connect() as connection:
        connection.execute("UPDATE surveys SET dismissed_at = ? WHERE ticket_id = ? AND user_email = ? AND dismissed_at IS NULL",
                           (datetime.now().isoformat(timespec="seconds"), ticket_id, user_email.lower()))

def unsynced_survey_answers(limit=50):
    with connect() as connection:
        rows = connection.execute("SELECT * FROM surveys WHERE status = 'answered' AND glpi_synced = 0 ORDER BY answered_at LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]

def mark_survey_synced(ticket_id, user_email):
    with connect() as connection:
        connection.execute("UPDATE surveys SET glpi_synced = 1 WHERE ticket_id = ? AND user_email = ?", (ticket_id, user_email))

def list_surveys(since):
    """Every survey created since a date, for the console's satisfaction figures"""
    with connect() as connection:
        rows = connection.execute("SELECT * FROM surveys WHERE closed_at >= ? ORDER BY closed_at DESC", (since,)).fetchall()
    return [dict(row) for row in rows]

def follow_up_survey(ticket_id, user_email, actor, note):
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE surveys SET followed_up_by = ?, followed_up_at = ?, follow_up_note = ? WHERE ticket_id = ? AND user_email = ? AND status = 'answered'",
            (f"{actor['name']} <{actor['email']}>", now, note[:500], ticket_id, user_email.lower()))
        if cursor.rowcount:
            _audit(connection, actor, "satisfaction.followed_up", f"ticket #{ticket_id}", {"note": note[:500]}, now)
        return cursor.rowcount == 1

# ---------------- safety controls ----------------
def get_guard_state(key, default=None):
    with connect() as connection:
        row = connection.execute("SELECT value_json FROM guard_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default

def set_guard_state(key, value):
    with connect() as connection:
        connection.execute("INSERT INTO guard_state (key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                           (key, json.dumps(value)))

def record_agent_action(mode, method, target, ticket_id, fields, outcome):
    with connect() as connection:
        connection.execute(
            "INSERT INTO agent_actions (mode, method, target, ticket_id, fields_json, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mode, method, target, ticket_id, json.dumps(fields), outcome[:300], datetime.now().isoformat(timespec="seconds")))

def list_agent_actions(limit=200, ticket_id=None):
    query = "SELECT * FROM agent_actions" + (" WHERE ticket_id = ?" if ticket_id else "") + " ORDER BY id DESC LIMIT ?"
    with connect() as connection:
        rows = connection.execute(query, ((ticket_id, limit) if ticket_id else (limit,))).fetchall()
    return [{**dict(row), "fields": json.loads(row["fields_json"])} for row in rows]

def count_agent_actions(since):
    with connect() as connection:
        rows = connection.execute("SELECT mode, outcome, COUNT(*) AS n FROM agent_actions WHERE created_at >= ? GROUP BY mode, outcome", (since,)).fetchall()
    return [dict(row) for row in rows]

def record_guard_event(kind, ticket_id, source, reasons):
    with connect() as connection:
        connection.execute("INSERT INTO guard_events (kind, ticket_id, source, reasons_json, created_at) VALUES (?, ?, ?, ?, ?)",
                           (kind, ticket_id, source, json.dumps(reasons), datetime.now().isoformat(timespec="seconds")))

def list_guard_events(limit=100):
    with connect() as connection:
        rows = connection.execute("SELECT * FROM guard_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(row), "reasons": json.loads(row["reasons_json"])} for row in rows]

def record_shadow_ticket(ticket_id, decision, analysis):
    with connect() as connection:
        connection.execute("INSERT OR REPLACE INTO shadow_tickets (ticket_id, decision, analysis_json, created_at) VALUES (?, ?, ?, ?)",
                           (ticket_id, decision, json.dumps(analysis) if analysis is not None else None, datetime.now().isoformat(timespec="seconds")))

def shadow_ticket_ids():
    with connect() as connection:
        return {row["ticket_id"] for row in connection.execute("SELECT ticket_id FROM shadow_tickets")}

def list_shadow_tickets(limit=300):
    with connect() as connection:
        rows = connection.execute("SELECT * FROM shadow_tickets ORDER BY ticket_id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(row), "analysis": json.loads(row["analysis_json"]) if row["analysis_json"] else None} for row in rows]

def purge_safety_records(action_days=90, shadow_days=30):
    """The change log is kept 90 days; shadow results hold production ticket analysis, so only 30"""
    now = datetime.now()
    with connect() as connection:
        connection.execute("DELETE FROM agent_actions WHERE created_at < ?", ((now - timedelta(days=action_days)).isoformat(timespec="seconds"),))
        connection.execute("DELETE FROM guard_events WHERE created_at < ?", ((now - timedelta(days=action_days)).isoformat(timespec="seconds"),))
        connection.execute("DELETE FROM shadow_tickets WHERE created_at < ?", ((now - timedelta(days=shadow_days)).isoformat(timespec="seconds"),))

def audit(actor, action, target, details):
    """Adds one entry to the admin audit log"""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        _audit(connection, actor, action, target, details, now)

# ---------------- tickets Hermes did not answer ----------------
def record_skipped_ticket(ticket_id, reason, detail=""):
    with connect() as connection:
        connection.execute("INSERT OR IGNORE INTO skipped_tickets (ticket_id, reason, detail, created_at) VALUES (?, ?, ?, ?)",
                           (ticket_id, reason, detail[:500], datetime.now().isoformat(timespec="seconds")))

def skipped_ticket_ids():
    with connect() as connection:
        return {row["ticket_id"] for row in connection.execute("SELECT ticket_id FROM skipped_tickets")}

def list_skipped_tickets(since=None):
    with connect() as connection:
        rows = connection.execute("SELECT ticket_id, reason, detail, created_at FROM skipped_tickets WHERE created_at >= ? ORDER BY ticket_id DESC",
                                  (since or "0000",)).fetchall()
    return [dict(row) for row in rows]

# ---------------- ticket change tracking ----------------
def get_ticket_snapshot(ticket_id):
    with connect() as connection:
        row = connection.execute("SELECT snapshot_json FROM ticket_snapshots WHERE ticket_id = ?", (ticket_id,)).fetchone()
    return json.loads(row["snapshot_json"]) if row else None

def save_ticket_snapshot(ticket_id, snapshot):
    with connect() as connection:
        connection.execute(
            "INSERT INTO ticket_snapshots (ticket_id, snapshot_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ticket_id) DO UPDATE SET snapshot_json = excluded.snapshot_json, updated_at = excluded.updated_at",
            (ticket_id, json.dumps(snapshot), datetime.now().isoformat(timespec="seconds")))

def add_ticket_event(user_email, ticket_id, title, message):
    with connect() as connection:
        connection.execute(
            "INSERT INTO ticket_events (user_email, ticket_id, title, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_email, ticket_id, title[:200], message[:500], datetime.now().isoformat(timespec="seconds")))

def pending_ticket_events(limit=100):
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, user_email, ticket_id, title, message, created_at FROM ticket_events WHERE delivered_at IS NULL ORDER BY id LIMIT ?",
            (limit,)).fetchall()
    return [dict(row) for row in rows]

def mark_ticket_events_delivered(event_ids):
    if not event_ids:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        cursor = connection.executemany("UPDATE ticket_events SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                                        [(now, int(event_id)) for event_id in event_ids])
        return cursor.rowcount

# ---------------- conversations ----------------
def save_conversation(conversation_id, user, state, outcome):
    """Inserts or updates one conversation and returns its ID. 'state' holds messages, ticket, pending choice and counters."""
    now = datetime.now().isoformat(timespec="seconds")
    title = next((m["content"] for m in state["messages"] if m["role"] == "user"), "New conversation")[:60]
    with connect() as connection:
        if conversation_id is None:
            cursor = connection.execute(
                "INSERT INTO conversations (user_email, user_name, title, state_json, ticket_id, outcome, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user["email"], user["name"], title, json.dumps(state), state.get("ticket_id"), outcome, now, now))
            return cursor.lastrowid
        connection.execute(
            "UPDATE conversations SET title = ?, state_json = ?, ticket_id = ?, outcome = ?, updated_at = ? WHERE id = ? AND user_email = ?",
            (title, json.dumps(state), state.get("ticket_id"), outcome, now, conversation_id, user["email"]))
        return conversation_id

def list_conversations(user, limit=20):
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, title, ticket_id, outcome, updated_at FROM conversations WHERE user_email = ? ORDER BY updated_at DESC LIMIT ?",
            (user["email"], limit)).fetchall()
    return [dict(row) for row in rows]

def load_conversation(conversation_id, user):
    """Returns (state, outcome) for one of the user's own conversations, or None"""
    with connect() as connection:
        row = connection.execute(
            "SELECT state_json, outcome FROM conversations WHERE id = ? AND user_email = ?",
            (conversation_id, user["email"])).fetchone()
    return (json.loads(row["state_json"]), row["outcome"]) if row else None

def chat_escalated_ticket(ticket_id):
    """True when a portal conversation has already handed this ticket to a technician"""
    with connect() as connection:
        row = connection.execute("SELECT 1 FROM conversations WHERE ticket_id = ? AND outcome = 'ticket' LIMIT 1", (ticket_id,)).fetchone()
    return row is not None

def all_conversation_outcomes():
    """One row per portal conversation, for analytics"""
    with connect() as connection:
        rows = connection.execute("SELECT id, user_email, title, ticket_id, outcome, created_at FROM conversations").fetchall()
    return [dict(row) for row in rows]

# ---------------- category suggestions ----------------
def record_category_suggestion(ticket_id, suggested_label, fallback_category, summary, source):
    """Stores one suggestion per ticket (later calls for the same ticket are ignored)"""
    with connect() as connection:
        if connection.execute("SELECT 1 FROM category_suggestions WHERE ticket_id = ?", (ticket_id,)).fetchone():
            return
        connection.execute(
            "INSERT INTO category_suggestions (ticket_id, suggested_label, fallback_category, summary, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, suggested_label.strip()[:80], fallback_category, (summary or "")[:300], source, datetime.now().isoformat(timespec="seconds")))

def list_category_suggestions(since=None):
    with connect() as connection:
        if since:
            rows = connection.execute("SELECT * FROM category_suggestions WHERE created_at >= ? ORDER BY created_at DESC", (since,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM category_suggestions ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]

def suggestion_key(label):
    """Suggestions that differ only in case or spacing ("VPN access" / "vpn  Access") are the same proposal"""
    return " ".join((label or "").lower().split())

def category_suggestion_groups(status):
    """Suggestions with the given status, grouped into one proposal per suggested name (most tickets first)"""
    with connect() as connection:
        rows = connection.execute("SELECT * FROM category_suggestions WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    groups = {}
    for row in map(dict, rows):
        group = groups.setdefault(suggestion_key(row["suggested_label"]), {"labels": {}, "suggestions": []})
        group["labels"][row["suggested_label"]] = group["labels"].get(row["suggested_label"], 0) + 1
        group["suggestions"].append(row)
    result = []
    for key, group in groups.items():
        suggestions = group["suggestions"]
        latest = suggestions[0]
        result.append({
            "key": key,
            # The spelling most tickets used
            "label": max(group["labels"].items(), key=lambda item: item[1])[0],
            "ticket_count": len(suggestions),
            "first_seen": suggestions[-1]["created_at"],
            "last_seen": latest["created_at"],
            "filed_under": sorted({s["fallback_category"] for s in suggestions if s["fallback_category"]}),
            "approved_label": latest["approved_label"], "decision_note": latest["decision_note"],
            "decided_by": latest["decided_by"], "decided_at": latest["decided_at"], "glpi_category_id": latest["glpi_category_id"],
            "tickets": [{"suggestion_id": s["id"], "ticket_id": s["ticket_id"], "summary": s["summary"],
                         "filed_under": s["fallback_category"], "source": s["source"], "created_at": s["created_at"]} for s in suggestions],
        })
    return sorted(result, key=lambda g: (g["ticket_count"], g["last_seen"]), reverse=True)

def decide_category_suggestions(keys, decision, actor, note, approved_labels, glpi_result=None):
    """Approves or declines whole proposals (every pending suggestion with that name) and writes one audit entry per
    proposal. Only pending suggestions change, so a proposal decided by someone else meanwhile is left alone.
    glpi_result records what was changed in GLPI for an approval (category ID, tickets moved or skipped).
    Returns the number of proposals decided."""
    now = datetime.now().isoformat(timespec="seconds")
    status = "approved" if decision == "approve" else "declined"
    decided = 0
    with connect() as connection:
        pending = connection.execute("SELECT id, suggested_label FROM category_suggestions WHERE status = 'pending'").fetchall()
        for key in dict.fromkeys(keys):
            ids = [row["id"] for row in pending if suggestion_key(row["suggested_label"]) == key]
            if not ids:
                continue
            label = (approved_labels.get(key) or next(row["suggested_label"] for row in pending if row["id"] == ids[0])).strip()[:80]
            category_id = (glpi_result or {}).get("category_id")
            connection.executemany(
                "UPDATE category_suggestions SET status = ?, approved_label = ?, decision_note = ?, decided_by = ?, decided_at = ?, "
                "glpi_category_id = ? WHERE id = ? AND status = 'pending'",
                [(status, label if status == "approved" else None, note, actor["email"], now, category_id, suggestion_id) for suggestion_id in ids])
            _audit(connection, actor, f"category_suggestion.{status}", label,
                   {"suggestion_ids": ids, "tickets": len(ids), "note": note, **({"glpi": glpi_result} if glpi_result else {})}, now)
            decided += 1
    return decided

# ---------------- routing suggestions ----------------
def routing_decided_fingerprints():
    with connect() as connection:
        return {row["fingerprint"] for row in connection.execute("SELECT fingerprint FROM routing_decisions")}

def record_routing_decision(suggestion, status, actor, note, result=None):
    """Stores one decided routing suggestion and its audit entry. Returns False if it was already decided."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO routing_decisions (fingerprint, kind, title, suggestion_json, status, note, result_json, decided_by, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (suggestion["id"], suggestion["kind"], suggestion["title"], json.dumps(suggestion), status, note,
             json.dumps(result) if result else None, actor["email"], now))
        if not cursor.rowcount:
            return False
        _audit(connection, actor, f"routing_suggestion.{status}", suggestion["title"],
               {"kind": suggestion["kind"], "suggestion_id": suggestion["id"], "note": note, **({"glpi": result} if result else {})}, now)
        return True

def list_routing_decisions(status):
    with connect() as connection:
        rows = connection.execute("SELECT * FROM routing_decisions WHERE status = ? ORDER BY decided_at DESC", (status,)).fetchall()
    return [{**json.loads(row["suggestion_json"]), "status": row["status"], "note": row["note"],
             "result": json.loads(row["result_json"]) if row["result_json"] else None,
             "decided_by": row["decided_by"], "decided_at": row["decided_at"]} for row in rows]

# ---------------- technician availability ----------------
def absent_technicians(on_date=None):
    """{GLPI user ID: last day of leave} for technicians on leave on that day (today by default)"""
    day = (on_date or datetime.now()).strftime("%Y-%m-%d")
    with connect() as connection:
        rows = connection.execute("SELECT user_id, MAX(ends_on) AS ends_on FROM technician_absences "
                                  "WHERE starts_on <= ? AND ends_on >= ? GROUP BY user_id", (day, day)).fetchall()
    return {row["user_id"]: row["ends_on"] for row in rows}

def list_absences(include_past_days=30):
    """Current and upcoming leave, plus leave that ended recently"""
    since = (datetime.now() - timedelta(days=include_past_days)).strftime("%Y-%m-%d")
    with connect() as connection:
        rows = connection.execute("SELECT * FROM technician_absences WHERE ends_on >= ? ORDER BY starts_on, user_name", (since,)).fetchall()
    return [dict(row) for row in rows]

def add_absence(user_id, user_name, starts_on, ends_on, note, actor):
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        absence_id = connection.execute(
            "INSERT INTO technician_absences (user_id, user_name, starts_on, ends_on, note, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, user_name, starts_on, ends_on, note, actor["email"], now)).lastrowid
        _audit(connection, actor, "technician.leave_added", user_name or f"User {user_id}",
               {"user_id": user_id, "starts_on": starts_on, "ends_on": ends_on, "note": note}, now)
    return absence_id

def remove_absence(absence_id, actor):
    """Cancels leave (for example a technician back early). Returns False if there was no such leave."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        row = connection.execute("SELECT * FROM technician_absences WHERE id = ?", (absence_id,)).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM technician_absences WHERE id = ?", (absence_id,))
        _audit(connection, actor, "technician.leave_removed", row["user_name"] or f"User {row['user_id']}",
               {"user_id": row["user_id"], "starts_on": row["starts_on"], "ends_on": row["ends_on"]}, now)
    return True

# ---------------- data protection (retention, and one person's data on request) ----------------
# Chats are deleted this long after their last message (agreed 2026-10-20). GLPI tickets are IT's business record
# and are not affected.
CHAT_RETENTION_DAYS = int(os.getenv("HERMES_CHAT_RETENTION_DAYS", "365") or 365)
SYSTEM_ACTOR = {"email": "hermes@system", "name": "Hermes (automatic)"}

def retention_summary():
    with connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS conversations, MIN(updated_at) AS oldest FROM conversations").fetchone()
        last_purge = connection.execute(
            "SELECT created_at, details_json FROM admin_audit_log WHERE action = 'data_protection.retention' ORDER BY id DESC LIMIT 1").fetchone()
    return {"retention_days": CHAT_RETENTION_DAYS, "conversations": row["conversations"], "oldest_activity": row["oldest"],
            "last_purge": ({"at": last_purge["created_at"], **json.loads(last_purge["details_json"])} if last_purge else None)}

def purge_expired_chats():
    """Deletes conversations with no activity for the retention period, and delivered notifications as old.
    Writes an audit entry when anything was deleted. Returns the counts."""
    cutoff = (datetime.now() - timedelta(days=CHAT_RETENTION_DAYS)).isoformat(timespec="seconds")
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as connection:
        conversations = connection.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,)).rowcount
        events = connection.execute("DELETE FROM ticket_events WHERE delivered_at IS NOT NULL AND created_at < ?", (cutoff,)).rowcount
        if conversations or events:
            _audit(connection, SYSTEM_ACTOR, "data_protection.retention", f"Older than {CHAT_RETENTION_DAYS} days",
                   {"conversations": conversations, "notifications": events}, now)
    return {"conversations": conversations, "notifications": events}

def person_data_counts(email):
    """How much Hermes holds about one person (no content), so an admin can confirm before exporting or deleting"""
    with connect() as connection:
        conversations = connection.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS first, MAX(updated_at) AS last FROM conversations WHERE user_email = ?", (email,)).fetchone()
        events = connection.execute("SELECT COUNT(*) AS n FROM ticket_events WHERE user_email = ?", (email,)).fetchone()
        surveys = connection.execute("SELECT COUNT(*) AS n FROM surveys WHERE user_email = ?", (email,)).fetchone()
    return {"email": email, "conversations": conversations["n"], "first_activity": conversations["first"],
            "last_activity": conversations["last"], "notifications": events["n"], "surveys": surveys["n"]}

def export_person_data(email, actor, reference):
    """Everything Hermes holds about one person, for a data access request. The export is audited."""
    with connect() as connection:
        conversations = connection.execute(
            "SELECT id, title, ticket_id, outcome, created_at, updated_at, state_json FROM conversations WHERE user_email = ? ORDER BY id",
            (email,)).fetchall()
        events = connection.execute(
            "SELECT ticket_id, title, message, created_at, delivered_at FROM ticket_events WHERE user_email = ? ORDER BY id", (email,)).fetchall()
        surveys = connection.execute(
            "SELECT ticket_id, title, closed_at, status, rating, reason, comment, answered_at FROM surveys WHERE user_email = ? ORDER BY closed_at",
            (email,)).fetchall()
        _audit(connection, actor, "data_protection.export", email,
               {"reference": reference, "conversations": len(conversations), "notifications": len(events), "surveys": len(surveys)},
               datetime.now().isoformat(timespec="seconds"))
    return {
        "subject": email,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "held_by": "Hermes, SBC IT Service Desk assistant",
        "note": "IT tickets are kept in the IT service desk system (GLPI) as business records and are not part of this export.",
        "conversations": [{"id": c["id"], "title": c["title"], "ticket_id": c["ticket_id"], "outcome": c["outcome"],
                           "started": c["created_at"], "last_activity": c["updated_at"],
                           "messages": [{k: m.get(k) for k in ("role", "content", "time")}
                                        for m in json.loads(c["state_json"]).get("messages", [])]} for c in conversations],
        "notifications": [dict(e) for e in events],
        "satisfaction_surveys": [dict(s) for s in surveys],
    }

def delete_person_data(email, actor, reference):
    """Deletes one person's chats and Hermes notifications (erasure request). GLPI tickets are kept. Audited."""
    with connect() as connection:
        conversations = connection.execute("DELETE FROM conversations WHERE user_email = ?", (email,)).rowcount
        events = connection.execute("DELETE FROM ticket_events WHERE user_email = ?", (email,)).rowcount
        # Survey answers are anonymised, not deleted, so the service desk's satisfaction figures stay correct
        surveys = connection.execute("UPDATE surveys SET user_email = 'deleted:' || ticket_id || ':' || rowid, comment = NULL, title = NULL "
                                     "WHERE user_email = ?", (email,)).rowcount
        _audit(connection, actor, "data_protection.delete", email,
               {"reference": reference, "conversations": conversations, "notifications": events, "surveys_anonymised": surveys},
               datetime.now().isoformat(timespec="seconds"))
    return {"conversations": conversations, "notifications": events, "surveys_anonymised": surveys}

# ---------------- admin audit log ----------------
def _audit(connection, actor, action, target, details, now):
    connection.execute(
        "INSERT INTO admin_audit_log (actor_email, actor_name, action, target, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor["email"], actor["name"], action, target[:200], json.dumps(details), now))

def list_audit_log(limit=200):
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, actor_email, actor_name, action, target, details_json, created_at FROM admin_audit_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    return [{**{k: row[k] for k in row.keys() if k != "details_json"}, "details": json.loads(row["details_json"])} for row in rows]
