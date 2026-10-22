from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import db
import runtime_clock
from config import DATA_DIR


STATE_FILE = os.path.join(DATA_DIR, "listening_sessions.json")
TIMEOUT_MINUTES = 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS discussion_sessions (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    started_by_user_id TEXT NOT NULL,
    parent_session_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','STOPPED','TIMED_OUT')) DEFAULT 'ACTIVE',
    previous_summary TEXT,
    summary_text TEXT,
    started_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at_utc TEXT,
    FOREIGN KEY(started_by_user_id) REFERENCES users(user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_discussion_one_active
ON discussion_sessions(conversation_id) WHERE status='ACTIVE';

CREATE TABLE IF NOT EXISTS discussion_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    sender_phone TEXT NOT NULL,
    text TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES discussion_sessions(session_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_discussion_messages_session
ON discussion_messages(session_id,created_at_utc);
"""


def ensure_schema(conn=None):
    own = conn is None
    if own:
        conn = db.connect()
    try:
        conn.executescript(SCHEMA)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _now():
    return runtime_clock.now_utc()


def _sync_state_file(conn=None):
    own = conn is None
    if own:
        conn = db.connect()
    try:
        ensure_schema(conn)
        rows = conn.execute(
            """SELECT conversation_id,last_message_at_utc FROM discussion_sessions
               WHERE status='ACTIVE'"""
        ).fetchall()
        payload = {}
        for row in rows:
            try:
                stamp = datetime.fromisoformat(str(row["last_message_at_utc"]).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except Exception:
                stamp = _now()
            payload[str(row["conversation_id"])] = {
                "expires_at_ms": int((stamp + timedelta(minutes=TIMEOUT_MINUTES)).timestamp() * 1000)
            }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    finally:
        if own:
            conn.close()


def expire_inactive():
    ensure_schema()
    cutoff = (_now() - timedelta(minutes=TIMEOUT_MINUTES)).isoformat()
    conn = db.connect()
    try:
        conn.execute(
            """UPDATE discussion_sessions
               SET status='TIMED_OUT',ended_at_utc=CURRENT_TIMESTAMP
               WHERE status='ACTIVE' AND datetime(last_message_at_utc)<=datetime(?)""",
            (cutoff,),
        )
        conn.commit()
        _sync_state_file(conn)
    finally:
        conn.close()


def active_session(conversation_id: str) -> dict | None:
    expire_inactive()
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT * FROM discussion_sessions
               WHERE conversation_id=? AND status='ACTIVE' LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _search_prior(conn, conversation_id: str, query: str) -> dict | None:
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", str(query or "").casefold())
        if len(token) > 2 and token not in {
            "resume","recall","our","the","discussion","plan","again","alex","listening"
        }
    ]
    sessions = conn.execute(
        """SELECT * FROM discussion_sessions
           WHERE conversation_id=? AND status IN ('STOPPED','TIMED_OUT')
           ORDER BY COALESCE(ended_at_utc,last_message_at_utc) DESC LIMIT 30""",
        (conversation_id,),
    ).fetchall()
    if not tokens:
        return dict(sessions[0]) if sessions else None
    for session in sessions:
        rows = conn.execute(
            "SELECT text FROM discussion_messages WHERE session_id=?",
            (session["session_id"],),
        ).fetchall()
        hay = " ".join(str(row["text"] or "") for row in rows).casefold()
        if all(token in hay for token in tokens):
            return dict(session)
    return None


def start(actor, resume_query: str | None = None) -> dict:
    if actor.conversation_type != "GROUP":
        raise ValueError("Listening Mode is for the Family Shared group")
    expire_inactive()
    conn = db.connect()
    try:
        ensure_schema(conn)
        current = conn.execute(
            "SELECT * FROM discussion_sessions WHERE conversation_id=? AND status='ACTIVE'",
            (actor.conversation_id,),
        ).fetchone()
        if current:
            return {"status": "already_listening", "session_id": current["session_id"]}
        prior = _search_prior(conn, actor.conversation_id, resume_query or "") if resume_query else None
        session_id = str(uuid.uuid4())
        now = _now().isoformat()
        conn.execute(
            """INSERT INTO discussion_sessions(
                   session_id,conversation_id,started_by_user_id,parent_session_id,
                   previous_summary,started_at_utc,last_message_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                session_id, actor.conversation_id, actor.user_id,
                prior["session_id"] if prior else None,
                prior.get("summary_text") if prior else None,
                now, now,
            ),
        )
        conn.commit()
        _sync_state_file(conn)
        return {
            "status": "listening",
            "session_id": session_id,
            "resumed": bool(prior),
            "previous_summary": prior.get("summary_text") if prior else None,
        }
    finally:
        conn.close()


def capture(actor, message_id: str, text: str, received_at_utc: str | None = None) -> dict:
    if actor.conversation_type != "GROUP":
        return {"status": "not_group"}
    expire_inactive()
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT * FROM discussion_sessions
               WHERE conversation_id=? AND status='ACTIVE' LIMIT 1""",
            (actor.conversation_id,),
        ).fetchone()
        if not row:
            return {"status": "not_listening"}
        stamp = str(received_at_utc or _now().isoformat())
        conn.execute(
            """INSERT OR IGNORE INTO discussion_messages(
                   message_id,session_id,user_id,sender_phone,text,created_at_utc
               ) VALUES(?,?,?,?,?,?)""",
            (
                str(message_id), row["session_id"], actor.user_id, actor.phone,
                str(text or "")[:8000], stamp,
            ),
        )
        conn.execute(
            "UPDATE discussion_sessions SET last_message_at_utc=? WHERE session_id=?",
            (stamp, row["session_id"]),
        )
        conn.commit()
        _sync_state_file(conn)
        return {"status": "captured", "session_id": row["session_id"]}
    finally:
        conn.close()


def context(conversation_id: str, max_messages: int = 120) -> dict | None:
    session = active_session(conversation_id)
    if not session:
        return None
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT user_id,text,created_at_utc FROM discussion_messages
               WHERE session_id=? ORDER BY created_at_utc DESC LIMIT ?""",
            (session["session_id"], max(1, min(200, int(max_messages)))),
        ).fetchall()
        messages = [dict(row) for row in reversed(rows)]
        return {
            "session_id": session["session_id"],
            "previous_summary": session.get("previous_summary"),
            "messages": messages,
        }
    finally:
        conn.close()


def stop(actor) -> dict:
    if actor.conversation_type != "GROUP":
        raise ValueError("Listening Mode is for the Family Shared group")
    session = active_session(actor.conversation_id)
    if not session:
        return {"status": "not_listening"}
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT user_id,text,created_at_utc FROM discussion_messages
               WHERE session_id=? ORDER BY created_at_utc""",
            (session["session_id"],),
        ).fetchall()
        conn.execute(
            """UPDATE discussion_sessions
               SET status='STOPPED',ended_at_utc=CURRENT_TIMESTAMP
               WHERE session_id=? AND status='ACTIVE'""",
            (session["session_id"],),
        )
        conn.commit()
        _sync_state_file(conn)
        return {
            "status": "stopped",
            "session_id": session["session_id"],
            "previous_summary": session.get("previous_summary"),
            "messages": [dict(row) for row in rows],
        }
    finally:
        conn.close()


def save_summary(session_id: str, summary: str) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE discussion_sessions SET summary_text=? WHERE session_id=?",
            (str(summary or "")[:20000], session_id),
        )
        conn.commit()
    finally:
        conn.close()


def recall(actor, query: str) -> dict:
    if actor.conversation_type == "GROUP":
        conversation_id = actor.conversation_id
    else:
        # A DM recall is allowed only for this owner's configured Family group.
        try:
            data = json.loads(open(os.path.join(DATA_DIR, "family_group.json"), "r", encoding="utf-8").read())
            conversation_id = str(data.get("group_jid") or "")
        except Exception:
            conversation_id = ""
    if not conversation_id:
        return {"status": "not_found"}
    ensure_schema()
    conn = db.connect()
    try:
        session = _search_prior(conn, conversation_id, query)
        if not session:
            return {"status": "not_found"}
        rows = conn.execute(
            """SELECT text,created_at_utc FROM discussion_messages
               WHERE session_id=? ORDER BY created_at_utc""",
            (session["session_id"],),
        ).fetchall()
        return {
            "status": "found",
            "summary": session.get("summary_text") or session.get("previous_summary"),
            "messages": [dict(row) for row in rows[-60:]],
            "presentation_rule": (
                "Summarize the remembered discussion naturally. Distinguish agreed, tentative, "
                "rejected and unresolved items; do not turn it into a plan/task/reminder without explicit instruction."
            ),
        }
    finally:
        conn.close()
