from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db
from config import normalize_phone


def get_db():
    return db.connect()


def resolve_user_and_space(conn, sender_phone, conversation_type):
    clean = normalize_phone(sender_phone)
    row = conn.execute(
        """SELECT u.user_id FROM users u
           JOIN user_phone_history p ON p.user_id=u.user_id
           WHERE p.phone_number=? AND p.valid_to_utc IS NULL LIMIT 1""",
        (clean,),
    ).fetchone()
    if not row:
        raise PermissionError("Sender is not configured as an Alex household user")
    user_id = row["user_id"]
    if str(conversation_type or "").upper() == "GROUP":
        return user_id, "FAMILY_SHARED"
    private = "HUSBAND_PVT" if user_id == "USR_HUSBAND" else "WIFE_PVT"
    membership = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND space_id=?",
        (user_id, private),
    ).fetchone()
    if not membership:
        raise PermissionError("Private space membership is missing")
    return user_id, private


def format_local_time(value, timezone_name="Asia/Kuala_Lumpur"):
    if not value:
        return "Unknown Date"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return str(value)
