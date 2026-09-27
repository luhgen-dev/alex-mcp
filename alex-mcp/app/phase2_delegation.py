"""Project Jarvis Phase 2 explicit delegation gate.

Phase 2 is quiet by default. Cross-domain monitoring/proactivity only exists
after an authenticated user explicitly delegates that job.
"""
from __future__ import annotations

import json
import uuid

import compat_tools as tools


SCHEMA = """
CREATE TABLE IF NOT EXISTS alex_phase2_delegations (
    delegation_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    delegation_type TEXT NOT NULL CHECK(delegation_type IN (
        'OT_GOAL_TRACK','BILL_MONITOR','GOAL_MONITOR',
        'PRESENCE_REMINDER','CUSTOM'
    )),
    subject TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','PAUSED','COMPLETED','CANCELLED')),
    explicit_source_message_id TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_delegations
    ON alex_phase2_delegations(owner_user_id,space_id,status,delegation_type);
"""


def ensure_schema(conn=None):
    own = conn is None
    if own:
        conn = tools.get_db()
    try:
        conn.executescript(SCHEMA)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _ctx(conn, sender_phone, conversation_type):
    user_id, private_space = tools.resolve_user_and_space(
        conn, sender_phone, conversation_type)
    shared = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND space_id='FAMILY_SHARED'",
        (user_id,),
    ).fetchone() is not None
    return user_id, private_space, shared


def create_delegation(delegation_type, subject, sender_phone,
                      explicit_source_message_id,
                      conversation_type="DIRECT_DM", visibility="private",
                      parameters=None, explicit_user_instruction=False):
    if not explicit_user_instruction:
        raise PermissionError(
            "Phase-2 monitoring requires an explicit user delegation")
    if not explicit_source_message_id:
        raise ValueError("Explicit source message id is required")
    delegation_type = str(delegation_type or "").upper()
    allowed = {
        "OT_GOAL_TRACK", "BILL_MONITOR", "GOAL_MONITOR",
        "PRESENCE_REMINDER", "CUSTOM",
    }
    if delegation_type not in allowed:
        raise ValueError("Unsupported delegation type")
    if not str(subject or "").strip():
        raise ValueError("Delegation subject is required")

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        visibility = str(visibility or "private").lower()
        if conversation_type == "GROUP":
            if visibility != "family":
                raise PermissionError("Private delegation cannot be created in group")
            space_id = "FAMILY_SHARED"
        elif visibility == "family":
            if not shared:
                raise PermissionError("Sender lacks family-space membership")
            space_id = "FAMILY_SHARED"
        elif visibility == "private":
            space_id = private_space
        else:
            raise ValueError("Visibility must be private or family")

        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_delegations(
                delegation_id,space_id,owner_user_id,delegation_type,subject,
                parameters_json,explicit_source_message_id
            ) VALUES (?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, delegation_type, str(subject).strip(),
            json.dumps(parameters or {}, sort_keys=True),
            explicit_source_message_id,
        ))
        conn.commit()
        return {"delegation_id": ident, "status": "ACTIVE", "space": space_id}
    finally:
        conn.close()


def active_delegations(sender_phone, conversation_type="DIRECT_DM",
                       delegation_type=None):
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        if conversation_type == "GROUP":
            clause, args = "space_id='FAMILY_SHARED'", []
        elif shared:
            clause, args = "(space_id=? OR space_id='FAMILY_SHARED')", [private_space]
        else:
            clause, args = "space_id=?", [private_space]
        sql = (
            "SELECT * FROM alex_phase2_delegations "
            "WHERE owner_user_id=? AND status='ACTIVE' AND " + clause
        )
        values = [user_id, *args]
        if delegation_type:
            sql += " AND delegation_type=?"
            values.append(str(delegation_type).upper())
        rows = conn.execute(sql + " ORDER BY created_at_utc", values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(item.pop("parameters_json"))
            result.append(item)
        return result
    finally:
        conn.close()


def can_proactively_track(delegation_type, subject, sender_phone,
                          conversation_type="DIRECT_DM"):
    wanted = " ".join(str(subject or "").casefold().split())
    for item in active_delegations(
            sender_phone, conversation_type, delegation_type):
        if " ".join(item["subject"].casefold().split()) == wanted:
            return True
    return False


def close_delegation(delegation_id, sender_phone,
                     conversation_type="DIRECT_DM", final_status="CANCELLED"):
    if final_status not in ("PAUSED", "COMPLETED", "CANCELLED"):
        raise ValueError("Invalid delegation close status")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        row = conn.execute(
            "SELECT * FROM alex_phase2_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if not row or row["owner_user_id"] != user_id:
            raise PermissionError("Delegation not authorized")
        if row["space_id"] != "FAMILY_SHARED" and row["space_id"] != private_space:
            raise PermissionError("Delegation not authorized")
        if conversation_type == "GROUP" and row["space_id"] != "FAMILY_SHARED":
            raise PermissionError("Private delegation cannot be changed in group")
        conn.execute("""
            UPDATE alex_phase2_delegations
            SET status=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE delegation_id=?
        """, (final_status, delegation_id))
        conn.commit()
        return {"delegation_id": delegation_id, "status": final_status}
    finally:
        conn.close()
