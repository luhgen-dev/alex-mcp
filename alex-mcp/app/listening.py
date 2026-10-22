from __future__ import annotations

import json
import os
import re
import uuid
import tempfile
from datetime import datetime, timedelta, timezone

import db
import runtime_clock
import scope_policy
import discussion_journal
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
        discussion_journal.ensure_schema(conn)
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
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="listening-", suffix=".tmp", dir=os.path.dirname(STATE_FILE))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, STATE_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
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
    if "FAMILY_SHARED" not in scope_policy.read_spaces(actor):
        raise PermissionError("Family discussion access required")
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
        discussion_journal.register(conn, actor, session_id)
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
    if "FAMILY_SHARED" not in scope_policy.read_spaces(actor):
        raise PermissionError("Family discussion access required")
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
            """SELECT message_id,user_id,text,created_at_utc FROM discussion_messages
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
    if "FAMILY_SHARED" not in scope_policy.read_spaces(actor):
        raise PermissionError("Family discussion access required")
    session = active_session(actor.conversation_id)
    if not session:
        return {"status": "not_listening"}
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT message_id,user_id,text,created_at_utc FROM discussion_messages
               WHERE session_id=? ORDER BY created_at_utc""",
            (session["session_id"],),
        ).fetchall()
        # Persist an evidence-only fallback in the same transaction as stopping.
        # A provider failure after this point cannot erase the captured journal.
        fallback = "Discussion saved — unstructured capture; decisions/actions need review.\n" + "\n".join(
            str(row["text"] or "") for row in rows
        )
        conn.execute(
            """UPDATE discussion_sessions
               SET status='STOPPED',ended_at_utc=?,summary_text=COALESCE(summary_text,?)
               WHERE session_id=? AND status='ACTIVE'""",
            (_now().isoformat(), fallback, session["session_id"]),
        )
        conn.commit()
        _sync_state_file(conn)
        return {
            "status": "stopped",
            "summary": fallback,
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
    """Search the Ear journal, never generic saved notes or unrelated lists."""
    result = discussion_journal.list_discussions(actor, query)
    if result["status"] == "not_found":
        return result
    if re.search(r"\bdiscussions\b", str(query).casefold()):
        return result
    found = result["discussions"][0]
    return discussion_journal.detail(actor, found["session_id"])
