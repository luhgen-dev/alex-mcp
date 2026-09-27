from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from context import ActorContext
from db import connect, utc_now
import phase2_work
import profile_config


def _to_dt(value: str, tz_name: str) -> datetime:
    text = (value or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("date/time is required")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def _to_utc(value: str, tz_name: str) -> str:
    return _to_dt(value, tz_name).astimezone(timezone.utc).isoformat()


def _local_date_from_utc(value: str, tz_name: str) -> str:
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz_name)).date().isoformat()


def _space(actor: ActorContext, shared: bool) -> str:
    space = "FAMILY_SHARED" if shared or actor.conversation_type == "GROUP" else actor.private_space
    if space not in actor.allowed_spaces:
        raise PermissionError("requested space is not accessible")
    return space


def _minor(value: float | int | str | Decimal) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid monetary amount")
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be zero or greater")
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def set_work_roster(actor: ActorContext, work_date: str, shift_name: str,
                    start_local: str | None = None, end_local: str | None = None,
                    notes: str | None = None, status: str = "CONFIRMED") -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    state = status.upper()
    if state not in {"PLANNED", "CONFIRMED", "CANCELLED"}:
        raise ValueError("roster status must be PLANNED, CONFIRMED or CANCELLED")
    start_utc = _to_utc(start_local, actor.timezone) if start_local else None
    end_utc = _to_utc(end_local, actor.timezone) if end_local else None
    conn = connect()
    try:
        existing = conn.execute("SELECT * FROM work_roster WHERE action_key=?", (actor.action_key,)).fetchone()
        if existing:
            return {"status": "already_applied", **dict(existing)}
        roster_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO work_roster(
                roster_id,action_key,owner_id,work_date,shift_name,start_at_utc,end_at_utc,notes,status
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (roster_id, actor.action_key, actor.user_id, work_date, shift_name[:120],
             start_utc, end_utc, notes, state),
        )
        conn.commit()
        return {"status": "saved", "roster_id": roster_id, "work_date": work_date,
                "shift_name": shift_name, "state": state}
    finally:
        conn.close()


def list_work_roster(actor: ActorContext, start_date: str | None = None,
                     end_date: str | None = None, limit: int = 60) -> dict:
    where = "owner_id=? AND status!='CANCELLED'"
    params: list = [actor.user_id]
    if start_date:
        where += " AND work_date>=?"
        params.append(start_date)
    if end_date:
        where += " AND work_date<=?"
        params.append(end_date)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT roster_id,work_date,shift_name,start_at_utc,end_at_utc,notes,status
                FROM work_roster WHERE {where} ORDER BY work_date,start_at_utc LIMIT ?""",
            params + [max(1, min(180, int(limit)))],
        ).fetchall()
        return {"roster": [dict(r) for r in rows]}
    finally:
        conn.close()


def set_leave_record(actor: ActorContext, leave_date: str, status: str = "PLANNED",
                     portion: str = "FULL", notes: str | None = None) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    state = status.upper()
    if state not in {"PLANNED", "CONFIRMED", "TAKEN", "CANCELLED"}:
        raise ValueError("leave status must be PLANNED, CONFIRMED, TAKEN or CANCELLED")
    portion = (portion or "FULL").upper()[:20]
    conn = connect()
    try:
        existing = conn.execute("SELECT * FROM leave_records WHERE action_key=?", (actor.action_key,)).fetchone()
        if existing:
            return {"status": "already_applied", **dict(existing)}
        row = conn.execute(
            "SELECT leave_id FROM leave_records WHERE owner_id=? AND leave_date=? AND portion=?",
            (actor.user_id, leave_date, portion),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE leave_records SET status=?,notes=?,updated_at_utc=? WHERE leave_id=?",
                (state, notes, utc_now(), row["leave_id"]),
            )
            leave_id = row["leave_id"]
        else:
            leave_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO leave_records(
                    leave_id,action_key,owner_id,leave_date,portion,status,notes
                   ) VALUES(?,?,?,?,?,?,?)""",
                (leave_id, actor.action_key, actor.user_id, leave_date, portion, state, notes),
            )
        conn.commit()
        return {"status": "saved", "leave_id": leave_id, "leave_date": leave_date,
                "portion": portion, "state": state}
    finally:
        conn.close()


def list_leave_records(actor: ActorContext, start_date: str | None = None,
                       end_date: str | None = None, include_cancelled: bool = False) -> dict:
    where = "owner_id=?"
    params: list = [actor.user_id]
    if not include_cancelled:
        where += " AND status!='CANCELLED'"
    if start_date:
        where += " AND leave_date>=?"
        params.append(start_date)
    if end_date:
        where += " AND leave_date<=?"
        params.append(end_date)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT leave_id,leave_date,portion,status,notes FROM leave_records
                WHERE {where} ORDER BY leave_date""", params,
        ).fetchall()
        return {"leave": [dict(r) for r in rows]}
    finally:
        conn.close()


def create_plan(actor: ActorContext, title: str, start_local: str | None = None,
                end_local: str | None = None, notes: str | None = None,
                shared: bool = False, locked: bool = False) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    space = _space(actor, shared)
    conn = connect()
    try:
        existing = conn.execute("SELECT * FROM plans WHERE action_key=?", (actor.action_key,)).fetchone()
        if existing:
            return {"status": "already_applied", **dict(existing)}
        pid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO plans(
                plan_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                timezone_name,notes,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (pid, actor.action_key, actor.user_id, space, title[:240],
             _to_utc(start_local, actor.timezone) if start_local else None,
             _to_utc(end_local, actor.timezone) if end_local else None,
             actor.timezone, notes, "LOCKED" if locked else "DRAFT"),
        )
        conn.commit()
        return {"status": "saved", "plan_id": pid, "title": title[:240],
                "state": "LOCKED" if locked else "DRAFT", "space": space}
    finally:
        conn.close()


def list_plans(actor: ActorContext, include_cancelled: bool = False, limit: int = 50) -> dict:
    marks = ",".join("?" for _ in actor.allowed_spaces)
    where = f"space_id IN ({marks})"
    if not include_cancelled:
        where += " AND status!='CANCELLED'"
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT plan_id,title,start_at_utc,end_at_utc,notes,status,space_id
                FROM plans WHERE {where} ORDER BY COALESCE(start_at_utc,created_at_utc) LIMIT ?""",
            list(actor.allowed_spaces) + [max(1, min(100, int(limit)))],
        ).fetchall()
        return {"plans": [dict(r) for r in rows]}
    finally:
        conn.close()


def update_plan(actor: ActorContext, plan_id: str, status: str | None = None,
                title: str | None = None, start_local: str | None = None,
                end_local: str | None = None, notes: str | None = None) -> dict:
    marks = ",".join("?" for _ in actor.allowed_spaces)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM plans WHERE plan_id=? AND space_id IN ({marks})",
            [plan_id] + list(actor.allowed_spaces),
        ).fetchone()
        if not row:
            raise PermissionError("plan not found in your accessible spaces")
        state = row["status"] if status is None else status.upper()
        if state not in {"DRAFT", "LOCKED", "CONFIRMED", "CANCELLED"}:
            raise ValueError("plan status must be DRAFT, LOCKED, CONFIRMED or CANCELLED")
        conn.execute(
            """UPDATE plans SET title=?,start_at_utc=?,end_at_utc=?,notes=?,status=?,updated_at_utc=?
               WHERE plan_id=?""",
            (
                (title or row["title"])[:240],
                _to_utc(start_local, actor.timezone) if start_local else row["start_at_utc"],
                _to_utc(end_local, actor.timezone) if end_local else row["end_at_utc"],
                notes if notes is not None else row["notes"],
                state, utc_now(), plan_id,
            ),
        )
        conn.commit()
        return {"status": "updated", "plan_id": plan_id, "state": state}
    finally:
        conn.close()


def confirm_plan(actor: ActorContext, plan_id: str,
                 add_to_diary: bool = True,
                 reminder_minutes_before: int | None = None,
                 reminder_recipient: str = "me") -> dict:
    """Confirm a plan; dated confirmations materialize a linked Diary event."""
    marks = ",".join("?" for _ in actor.allowed_spaces)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM plans WHERE plan_id=? AND space_id IN ({marks})",
            [plan_id] + list(actor.allowed_spaces),
        ).fetchone()
        if not row:
            raise PermissionError("plan not found in your accessible spaces")
        if row["status"] == "CANCELLED":
            raise ValueError("cancelled plan cannot be confirmed")
        link = conn.execute(
            "SELECT diary_id FROM plan_diary_links WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if link:
            conn.execute(
                "UPDATE plans SET status='CONFIRMED',updated_at_utc=? WHERE plan_id=?",
                (utc_now(), plan_id),
            )
            conn.commit()
            return {"status": "confirmed", "plan_id": plan_id,
                    "diary_id": link["diary_id"], "already_linked": True}
        if not add_to_diary or not row["start_at_utc"]:
            conn.execute(
                "UPDATE plans SET status='CONFIRMED',updated_at_utc=? WHERE plan_id=?",
                (utc_now(), plan_id),
            )
            conn.commit()
            return {"status": "confirmed", "plan_id": plan_id,
                    "diary_id": None, "materialized": False}
        start_local = datetime.fromisoformat(row["start_at_utc"]).astimezone(
            ZoneInfo(actor.timezone)
        ).isoformat()
        end_local = (
            datetime.fromisoformat(row["end_at_utc"]).astimezone(
                ZoneInfo(actor.timezone)
            ).isoformat()
            if row["end_at_utc"] else None
        )
    finally:
        conn.close()

    plan_actor = ActorContext(
        user_id=actor.user_id, phone=actor.phone, allowed_spaces=actor.allowed_spaces,
        private_space=actor.private_space, conversation_id=actor.conversation_id,
        conversation_type=actor.conversation_type, source_message_id=actor.source_message_id,
        media_ids=actor.media_ids, timezone=actor.timezone,
        action_key=f"{actor.action_key}:confirm-plan:{plan_id}",
    )
    result = add_diary_event(
        plan_actor, row["title"], start_local, end_local, row["notes"],
        shared=(row["space_id"] == "FAMILY_SHARED"),
        reminder_minutes_before=reminder_minutes_before,
        reminder_recipient=reminder_recipient,
        source_plan_id=plan_id,
    )
    if result.get("status") == "needs_choice":
        conn = connect()
        try:
            conn.execute(
                "UPDATE plans SET status='LOCKED',updated_at_utc=? WHERE plan_id=?",
                (utc_now(), plan_id),
            )
            conn.commit()
        finally:
            conn.close()
        result["plan_id"] = plan_id
        result["plan_status"] = "LOCKED"
    return result


def share_plan(actor: ActorContext, plan_id: str) -> dict:
    marks = ",".join("?" for _ in actor.allowed_spaces)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM plans WHERE plan_id=? AND space_id IN ({marks}) AND status!='CANCELLED'",
            [plan_id] + list(actor.allowed_spaces),
        ).fetchone()
        if not row:
            raise PermissionError("plan not found in your accessible spaces")
        if row["space_id"] == "FAMILY_SHARED":
            return {"status": "already_shared", "plan_id": plan_id}
        copy_id = str(uuid.uuid4())
        action_key = f"{actor.action_key}:share:{plan_id}"
        conn.execute(
            """INSERT OR IGNORE INTO plans(
                plan_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                timezone_name,notes,status,source_plan_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (copy_id, action_key, actor.user_id, "FAMILY_SHARED", row["title"],
             row["start_at_utc"], row["end_at_utc"], row["timezone_name"], row["notes"],
             row["status"], plan_id),
        )
        conn.commit()
        copied = conn.execute("SELECT plan_id FROM plans WHERE action_key=?", (action_key,)).fetchone()
        return {"status": "shared_copy_created", "plan_id": copied["plan_id"], "source_plan_id": plan_id}
    finally:
        conn.close()


def _roster_conflict(conn, actor: ActorContext, start_utc: str, end_utc: str | None):
    # A family-group planning turn may not inspect an individual's private
    # work roster, even to reveal only the existence of a clash.
    if actor.conversation_type == "GROUP":
        return None
    local_date = _local_date_from_utc(start_utc, actor.timezone)
    rows = conn.execute(
        """SELECT * FROM work_roster WHERE owner_id=? AND work_date=?
           AND status IN ('PLANNED','CONFIRMED') ORDER BY start_at_utc""",
        (actor.user_id, local_date),
    ).fetchall()
    start = datetime.fromisoformat(start_utc)
    end = datetime.fromisoformat(end_utc) if end_utc else start + timedelta(hours=1)
    for row in rows:
        if not row["start_at_utc"] or not row["end_at_utc"]:
            return row
        rs = datetime.fromisoformat(row["start_at_utc"])
        re = datetime.fromisoformat(row["end_at_utc"])
        if start < re and end > rs:
            return row

    # The mature Phase-2 roster profile is authoritative when configured.
    # In a family-group turn this lookup intentionally returns no private
    # profile, so a private work clash is never disclosed in group.
    try:
        shift = phase2_work.effective_shift(
            local_date, actor.phone, actor.conversation_type
        )
        if shift.get("shift") == "off":
            return None
        records = profile_config.authorized_records(
            actor.phone, actor.conversation_type, kind="roster",
            requested_scope="private",
        )
        active = [r for r in records if r["payload"].get("active", True)]
        if len(active) != 1:
            return None
        payload = active[0]["payload"]
        if shift["shift"] == "morning":
            start_clock = payload.get("day_start")
            end_clock = payload.get("day_end")
        else:
            start_clock = payload.get("evening_start")
            end_clock = payload.get("evening_end")
        if not start_clock or not end_clock:
            # We know it is a work day but not exact hours: conservative
            # same-day conflict rather than inventing a time.
            return {"roster_id": None, "source": "PROFILE", "shift": shift["shift"],
                    "work_date": local_date, "time_known": False}

        zone = ZoneInfo(actor.timezone)
        rs_local = datetime.fromisoformat(f"{local_date}T{start_clock}:00").replace(tzinfo=zone)
        re_local = datetime.fromisoformat(f"{local_date}T{end_clock}:00").replace(tzinfo=zone)
        if re_local <= rs_local:
            re_local += timedelta(days=1)
        rs = rs_local.astimezone(timezone.utc)
        re = re_local.astimezone(timezone.utc)
        if start < re and end > rs:
            return {"roster_id": None, "source": "PROFILE", "shift": shift["shift"],
                    "work_date": local_date, "start_at_utc": rs.isoformat(),
                    "end_at_utc": re.isoformat(), "time_known": True}
    except (ValueError, PermissionError):
        pass
    return None


def _diary_conflict(conn, actor: ActorContext, start_utc: str, end_utc: str | None,
                    exclude_diary_id: str | None = None):
    marks = ",".join("?" for _ in actor.allowed_spaces)
    params = list(actor.allowed_spaces)
    sql = f"""SELECT * FROM diary_events
              WHERE space_id IN ({marks}) AND status='ACTIVE'"""
    if exclude_diary_id:
        sql += " AND diary_id!=?"
        params.append(exclude_diary_id)
    rows = conn.execute(sql, params).fetchall()
    start = datetime.fromisoformat(start_utc)
    end = datetime.fromisoformat(end_utc) if end_utc else start + timedelta(hours=1)
    for row in rows:
        rs = datetime.fromisoformat(row["start_at_utc"])
        re = datetime.fromisoformat(row["end_at_utc"]) if row["end_at_utc"] else rs + timedelta(hours=1)
        if start < re and end > rs:
            return row
    return None


def _ticket_expired(row) -> bool:
    if not row["expires_at_utc"]:
        return False
    try:
        expiry = datetime.fromisoformat(row["expires_at_utc"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expiry.astimezone(timezone.utc)
    except Exception:
        return True


def _recipient_targets(actor: ActorContext, recipient: str) -> list[str]:
    value = (recipient or "me").strip().lower()
    if value in {"me", "self", "myself"}:
        return [actor.user_id]
    if value in {"husband", "him"}:
        return ["USR_HUSBAND"]
    if value in {"wife", "her"}:
        return ["USR_WIFE"]
    if value in {"spouse", "partner"}:
        return ["USR_WIFE" if actor.user_id == "USR_HUSBAND" else "USR_HUSBAND"]
    if value in {"both", "both of us", "everyone"}:
        return ["USR_HUSBAND", "USR_WIFE"]
    raise ValueError("reminder recipient must be me, spouse, husband, wife, or both")


def _active_phone(conn, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT phone_number FROM user_phone_history WHERE user_id=? AND valid_to_utc IS NULL LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["phone_number"] if row else None


def _insert_diary(conn, actor: ActorContext, action_key: str, title: str, start_utc: str,
                  end_utc: str | None, notes: str | None, space: str,
                  reminder_minutes_before: int | None = None,
                  reminder_recipient: str = "me") -> dict:
    existing = conn.execute("SELECT * FROM diary_events WHERE action_key=?", (action_key,)).fetchone()
    if existing:
        return {"status": "already_applied", **dict(existing)}
    diary_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO diary_events(
            diary_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
            timezone_name,notes
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (diary_id, action_key, actor.user_id, space, title[:240], start_utc, end_utc,
         actor.timezone, notes),
    )

    reminder_ids: list[str] = []
    if reminder_minutes_before is not None:
        mins = max(0, min(60 * 24 * 30, int(reminder_minutes_before)))
        due_utc = (datetime.fromisoformat(start_utc) - timedelta(minutes=mins)).isoformat()
        targets = _recipient_targets(actor, reminder_recipient)
        for target_user in targets:
            if target_user == actor.user_id:
                conversation_id = actor.conversation_id
            else:
                phone = _active_phone(conn, target_user)
                if not phone:
                    raise ValueError("target household member has no configured WhatsApp number")
                conversation_id = phone.replace("+", "") + "@s.whatsapp.net"
            reminder_space = "FAMILY_SHARED" if space == "FAMILY_SHARED" or target_user != actor.user_id or len(targets) > 1 else actor.private_space
            if reminder_space not in actor.allowed_spaces:
                raise PermissionError("linked reminder would cross the active privacy boundary")
            reminder_id = str(uuid.uuid4())
            reminder_key = f"{action_key}:linked-reminder:{target_user}"
            conn.execute(
                """INSERT INTO reminders(
                    reminder_id,action_key,source_message_id,owner_id,space_id,conversation_id,
                    task_text,due_at_utc,timezone_name
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (reminder_id, reminder_key, actor.source_message_id, target_user, reminder_space,
                 conversation_id, title[:500], due_utc, actor.timezone),
            )
            conn.execute(
                "INSERT INTO diary_reminder_links(diary_id,reminder_id) VALUES(?,?)",
                (diary_id, reminder_id),
            )
            reminder_ids.append(reminder_id)

    return {"status": "created", "diary_id": diary_id, "space": space,
            "linked_reminder_id": reminder_ids[0] if len(reminder_ids) == 1 else None,
            "linked_reminder_ids": reminder_ids}


def add_diary_event(actor: ActorContext, title: str, start_local: str,
                    end_local: str | None = None, notes: str | None = None,
                    shared: bool = False, reminder_minutes_before: int | None = None,
                    reminder_recipient: str = "me",
                    source_plan_id: str | None = None) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    start_utc = _to_utc(start_local, actor.timezone)
    end_utc = _to_utc(end_local, actor.timezone) if end_local else None
    space = _space(actor, shared)
    conn = connect()
    try:
        already = conn.execute("SELECT * FROM diary_events WHERE action_key=?", (actor.action_key,)).fetchone()
        if already:
            return {"status": "already_applied", **dict(already)}

        pending = conn.execute(
            "SELECT * FROM schedule_conflicts WHERE action_key=? AND status='OPEN'",
            (actor.action_key,),
        ).fetchone()
        if pending:
            if _ticket_expired(pending):
                conn.execute(
                    "UPDATE schedule_conflicts SET status='CANCELLED' WHERE conflict_id=?",
                    (pending["conflict_id"],),
                )
                conn.commit()
            else:
                choices = (
                    {"1": "add event and create PLANNED leave",
                     "2": "add event and keep the work clash",
                     "3": "cancel"}
                    if pending["conflict_kind"] == "WORK"
                    else {"1": "add event anyway", "2": "cancel"}
                )
                return {"status": "needs_choice", "conflict_id": pending["conflict_id"],
                        "conflict_kind": pending["conflict_kind"], "choices": choices}

        roster = _roster_conflict(conn, actor, start_utc, end_utc)
        if roster:
            cid = str(uuid.uuid4())
            expiry = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
            conn.execute(
                """INSERT INTO schedule_conflicts(
                    conflict_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                    timezone_name,notes,reminder_minutes_before,roster_id,
                    source_plan_id,conflict_kind,expires_at_utc,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, actor.action_key, actor.user_id, space, title[:240], start_utc, end_utc,
                 actor.timezone, notes, reminder_minutes_before, roster["roster_id"],
                 source_plan_id, "WORK", expiry, utc_now()),
            )
            conn.commit()
            return {
                "status": "needs_choice", "conflict_id": cid, "conflict_kind": "WORK",
                "expires_at_utc": expiry,
                "message": "This clashes with your work roster.",
                "choices": {"1": "add event and create PLANNED leave",
                            "2": "add event and keep the work clash",
                            "3": "cancel"},
            }

        diary_clash = _diary_conflict(conn, actor, start_utc, end_utc)
        if diary_clash:
            cid = str(uuid.uuid4())
            expiry = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
            conn.execute(
                """INSERT INTO schedule_conflicts(
                    conflict_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                    timezone_name,notes,reminder_minutes_before,conflicting_diary_id,
                    source_plan_id,conflict_kind,expires_at_utc,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, actor.action_key, actor.user_id, space, title[:240], start_utc, end_utc,
                 actor.timezone, notes, reminder_minutes_before, diary_clash["diary_id"],
                 source_plan_id, "DIARY", expiry, utc_now()),
            )
            conn.commit()
            return {
                "status": "needs_choice", "conflict_id": cid, "conflict_kind": "DIARY",
                "expires_at_utc": expiry,
                "message": "This overlaps an existing diary event.",
                "conflicting_event": {
                    "diary_id": diary_clash["diary_id"],
                    "title": diary_clash["title"],
                    "start_at_utc": diary_clash["start_at_utc"],
                    "end_at_utc": diary_clash["end_at_utc"],
                },
                "choices": {"1": "add event anyway", "2": "cancel"},
            }

        result = _insert_diary(
            conn, actor, actor.action_key, title, start_utc, end_utc, notes, space,
            reminder_minutes_before, reminder_recipient,
        )
        if source_plan_id and result.get("diary_id"):
            conn.execute(
                "INSERT OR IGNORE INTO plan_diary_links(plan_id,diary_id) VALUES(?,?)",
                (source_plan_id, result["diary_id"]),
            )
            conn.execute(
                "UPDATE plans SET status='CONFIRMED',updated_at_utc=? WHERE plan_id=?",
                (utc_now(), source_plan_id),
            )
            result["source_plan_id"] = source_plan_id
            result["plan_confirmed"] = True
        conn.commit()
        return result
    finally:
        conn.close()

def resolve_diary_conflict(actor: ActorContext, conflict_id: str, choice: int,
                           reminder_recipient: str = "me") -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM schedule_conflicts WHERE conflict_id=? AND owner_id=? AND status='OPEN'",
            (conflict_id, actor.user_id),
        ).fetchone()
        if not row:
            raise PermissionError("open conflict not found")
        if _ticket_expired(row):
            conn.execute(
                "UPDATE schedule_conflicts SET status='CANCELLED' WHERE conflict_id=?",
                (conflict_id,),
            )
            conn.commit()
            return {"status": "expired", "conflict_id": conflict_id}

        choice = int(choice)
        kind = row["conflict_kind"] or "WORK"
        if kind == "DIARY":
            if choice not in {1, 2}:
                raise ValueError("diary conflict choice must be 1 or 2")
            if choice == 2:
                conn.execute(
                    "UPDATE schedule_conflicts SET status='CANCELLED',choice=2 WHERE conflict_id=?",
                    (conflict_id,),
                )
                conn.commit()
                return {"status": "cancelled", "conflict_id": conflict_id}
            result = _insert_diary(
                conn, actor, f"{row['action_key']}:choice:1", row["title"],
                row["start_at_utc"], row["end_at_utc"], row["notes"], row["space_id"],
                row["reminder_minutes_before"], reminder_recipient,
            )
            if row["source_plan_id"] and result.get("diary_id"):
                conn.execute(
                    "INSERT OR IGNORE INTO plan_diary_links(plan_id,diary_id) VALUES(?,?)",
                    (row["source_plan_id"], result["diary_id"]),
                )
                conn.execute(
                    "UPDATE plans SET status='CONFIRMED',updated_at_utc=? WHERE plan_id=?",
                    (utc_now(), row["source_plan_id"]),
                )
                result["source_plan_id"] = row["source_plan_id"]
                result["plan_confirmed"] = True
            conn.execute(
                "UPDATE schedule_conflicts SET status='RESOLVED',choice=1 WHERE conflict_id=?",
                (conflict_id,),
            )
            conn.commit()
            return {"status": "resolved", "choice": 1, "diary": result, "leave": None}

        if choice not in {1, 2, 3}:
            raise ValueError("work conflict choice must be 1, 2 or 3")
        if choice == 3:
            conn.execute(
                "UPDATE schedule_conflicts SET status='CANCELLED',choice=3 WHERE conflict_id=?",
                (conflict_id,),
            )
            conn.commit()
            return {"status": "cancelled", "conflict_id": conflict_id}

        result = _insert_diary(
            conn, actor, f"{row['action_key']}:choice:{choice}", row["title"],
            row["start_at_utc"], row["end_at_utc"], row["notes"], row["space_id"],
            row["reminder_minutes_before"], reminder_recipient,
        )
        if row["source_plan_id"] and result.get("diary_id"):
            conn.execute(
                "INSERT OR IGNORE INTO plan_diary_links(plan_id,diary_id) VALUES(?,?)",
                (row["source_plan_id"], result["diary_id"]),
            )
            conn.execute(
                "UPDATE plans SET status='CONFIRMED',updated_at_utc=? WHERE plan_id=?",
                (utc_now(), row["source_plan_id"]),
            )
            result["source_plan_id"] = row["source_plan_id"]
            result["plan_confirmed"] = True
        leave = None
        if choice == 1:
            leave_date = _local_date_from_utc(row["start_at_utc"], actor.timezone)
            portion = "FULL"
            existing_leave = conn.execute(
                "SELECT * FROM leave_records WHERE owner_id=? AND leave_date=? AND portion=?",
                (actor.user_id, leave_date, portion),
            ).fetchone()
            if existing_leave:
                leave_state = existing_leave["status"]
                if leave_state == "CANCELLED":
                    conn.execute(
                        "UPDATE leave_records SET status='PLANNED',notes=?,updated_at_utc=? WHERE leave_id=?",
                        (f"Planned automatically from diary conflict: {row['title']}",
                         utc_now(), existing_leave["leave_id"]),
                    )
                    leave_state = "PLANNED"
                leave = {"status": "existing", "leave_id": existing_leave["leave_id"],
                         "leave_date": leave_date, "state": leave_state}
            else:
                leave_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO leave_records(
                        leave_id,action_key,owner_id,leave_date,portion,status,notes
                       ) VALUES(?,?,?,?,?,'PLANNED',?)""",
                    (leave_id, f"{row['action_key']}:planned-leave", actor.user_id,
                     leave_date, portion,
                     f"Planned automatically from diary conflict: {row['title']}"),
                )
                leave = {"status": "saved", "leave_id": leave_id,
                         "leave_date": leave_date, "state": "PLANNED"}

        conn.execute(
            "UPDATE schedule_conflicts SET status='RESOLVED',choice=? WHERE conflict_id=?",
            (choice, conflict_id),
        )
        conn.commit()
        return {"status": "resolved", "choice": choice, "diary": result, "leave": leave}
    finally:
        conn.close()


def resolve_latest_diary_conflict(actor: ActorContext, choice: int,
                                  reminder_recipient: str = "me") -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM schedule_conflicts
               WHERE owner_id=? AND status='OPEN'
               ORDER BY created_at_utc DESC""",
            (actor.user_id,),
        ).fetchall()
        for row in rows:
            if not _ticket_expired(row):
                conflict_id = row["conflict_id"]
                break
        else:
            raise ValueError("no active conflict choice is waiting")
    finally:
        conn.close()
    return resolve_diary_conflict(actor, conflict_id, choice, reminder_recipient)

def update_diary_event(actor: ActorContext, diary_id: str, status: str | None = None,
                       start_local: str | None = None, end_local: str | None = None,
                       title: str | None = None, notes: str | None = None,
                       linked_reminders: str = "ask") -> dict:
    marks = ",".join("?" for _ in actor.allowed_spaces)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM diary_events WHERE diary_id=? AND space_id IN ({marks})",
            [diary_id] + list(actor.allowed_spaces),
        ).fetchone()
        if not row:
            raise PermissionError("diary event not found")
        state = row["status"] if status is None else status.upper()
        if state not in {"ACTIVE", "CANCELLED"}:
            raise ValueError("diary status must be ACTIVE or CANCELLED")
        new_start = _to_utc(start_local, actor.timezone) if start_local else row["start_at_utc"]
        new_end = _to_utc(end_local, actor.timezone) if end_local else row["end_at_utc"]
        links = conn.execute(
            """SELECT r.* FROM reminders r JOIN diary_reminder_links l ON l.reminder_id=r.reminder_id
               WHERE l.diary_id=? AND r.status NOT IN ('COMP','CANC')""", (diary_id,),
        ).fetchall()

        changing_time = new_start != row["start_at_utc"] or new_end != row["end_at_utc"]
        cancelling = state == "CANCELLED" and row["status"] != "CANCELLED"
        action = (linked_reminders or "ask").strip().lower()

        if links and (changing_time or cancelling) and action == "ask":
            return {
                "status": "needs_reminder_choice",
                "diary_id": diary_id,
                "linked_reminder_count": len(links),
                "change": "cancel" if cancelling else "reschedule",
                "choices": (
                    {"keep": "cancel diary but keep reminder(s)",
                     "cancel": "cancel diary and reminder(s)"}
                    if cancelling else
                    {"keep": "move diary but keep reminder time(s)",
                     "shift": "move diary and shift reminder(s) by same interval"}
                ),
            }

        allowed_actions = {"ask", "keep"}
        if cancelling:
            allowed_actions.add("cancel")
        if changing_time and not cancelling:
            allowed_actions.add("shift")
        if action not in allowed_actions:
            raise ValueError(f"linked_reminders must be one of {sorted(allowed_actions)}")

        if state == "ACTIVE" and changing_time:
            roster = _roster_conflict(conn, actor, new_start, new_end)
            diary_clash = _diary_conflict(conn, actor, new_start, new_end, exclude_diary_id=diary_id)
            if roster or diary_clash:
                return {
                    "status": "needs_conflict_resolution",
                    "diary_id": diary_id,
                    "work_conflict": bool(roster),
                    "diary_conflict": (
                        {"diary_id": diary_clash["diary_id"], "title": diary_clash["title"]}
                        if diary_clash else None
                    ),
                    "message": "The new time conflicts with an existing commitment. Confirm the conflict separately before moving it.",
                }

        conn.execute(
            """UPDATE diary_events SET title=?,start_at_utc=?,end_at_utc=?,notes=?,status=?,updated_at_utc=?
               WHERE diary_id=?""",
            ((title or row["title"])[:240], new_start, new_end,
             notes if notes is not None else row["notes"], state, utc_now(), diary_id),
        )

        changed_reminders = 0
        for reminder in links:
            if cancelling and action == "cancel":
                conn.execute(
                    "UPDATE reminders SET status='CANC' WHERE reminder_id=?",
                    (reminder["reminder_id"],),
                )
                conn.execute(
                    """INSERT INTO reminder_events(
                        event_id,reminder_id,event_type,previous_state,new_state,note
                       ) VALUES(?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), reminder["reminder_id"], "CANCELLED",
                     reminder["status"], "CANC", "linked diary cancelled"),
                )
                changed_reminders += 1
            elif changing_time and action == "shift":
                old_start = datetime.fromisoformat(row["start_at_utc"])
                old_due = datetime.fromisoformat(reminder["due_at_utc"])
                delta = old_start - old_due
                new_due = datetime.fromisoformat(new_start) - delta
                conn.execute(
                    """UPDATE reminders SET due_at_utc=?,status='OPEN',
                       next_delivery_at_utc=NULL,defer_reason=NULL WHERE reminder_id=?""",
                    (new_due.isoformat(), reminder["reminder_id"]),
                )
                conn.execute(
                    """INSERT INTO reminder_events(
                        event_id,reminder_id,event_type,previous_state,new_state,
                        previous_due_at_utc,new_due_at_utc,note
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), reminder["reminder_id"], "RESCHEDULED",
                     reminder["status"], "OPEN", reminder["due_at_utc"],
                     new_due.isoformat(), "shifted with linked diary"),
                )
                changed_reminders += 1

        conn.commit()
        return {"status": "updated", "diary_id": diary_id, "state": state,
                "linked_reminders_updated": changed_reminders,
                "linked_reminder_action": action}
    finally:
        conn.close()

def check_my_availability(actor: ActorContext, start_local: str,
                          end_local: str | None = None) -> dict:
    """Owner-only private availability check; never callable from family group."""
    if actor.conversation_type == "GROUP":
        raise PermissionError("private availability checks are not allowed in the family group")
    start_utc = _to_utc(start_local, actor.timezone)
    end_utc = _to_utc(end_local, actor.timezone) if end_local else (
        datetime.fromisoformat(start_utc) + timedelta(hours=1)
    ).isoformat()
    conn = connect()
    try:
        roster = _roster_conflict(conn, actor, start_utc, end_utc)
        diary = _diary_conflict(conn, actor, start_utc, end_utc)
        return {
            "availability": "busy" if roster or diary else "no_conflict_found",
            "has_work_conflict": bool(roster),
            "has_diary_conflict": bool(diary),
            "output_space": actor.private_space,
            "may_be_posted_to_group": False,
            "privacy": "owner_only",
        }
    finally:
        conn.close()


def check_spouse_availability(actor: ActorContext, start_local: str,
                              end_local: str | None = None) -> dict:
    """Check only shared facts; never inspect the spouse's private roster/Diary."""
    spouse = "USR_WIFE" if actor.user_id == "USR_HUSBAND" else "USR_HUSBAND"
    start_utc = _to_utc(start_local, actor.timezone)
    end_utc = _to_utc(end_local, actor.timezone) if end_local else (
        datetime.fromisoformat(start_utc) + timedelta(hours=1)
    ).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT start_at_utc,end_at_utc FROM diary_events
               WHERE owner_id=? AND space_id='FAMILY_SHARED' AND status='ACTIVE'""",
            (spouse,),
        ).fetchall()
        start_dt = datetime.fromisoformat(start_utc)
        end_dt = datetime.fromisoformat(end_utc)
        shared_busy = False
        for row in rows:
            rs = datetime.fromisoformat(row["start_at_utc"])
            re = datetime.fromisoformat(row["end_at_utc"]) if row["end_at_utc"] else rs + timedelta(hours=1)
            if start_dt < re and end_dt > rs:
                shared_busy = True
                break
        return {
            "spouse": "wife" if spouse == "USR_WIFE" else "husband",
            "shared_conflict": shared_busy,
            "availability": "busy_from_shared_data" if shared_busy else "private_check_required",
            "private_schedule_read": False,
            "privacy": "details_hidden",
            "message": (
                "A shared commitment conflicts with that time."
                if shared_busy else
                "No shared conflict is visible. The spouse's private availability cannot be read from this conversation."
            ),
        }
    finally:
        conn.close()


def resolve_date_range(phrase: str, timezone_name: str,
                       reference_date: str | None = None) -> dict:
    """Resolve common household date ranges deterministically."""
    if reference_date:
        today = date.fromisoformat(str(reference_date)[:10])
    else:
        today = datetime.now(ZoneInfo(timezone_name)).date()
    low = " ".join(str(phrase or "").casefold().split())

    if low in {"today", "tdy"} or " today" in " " + low:
        start = end = today
    elif "tomorrow" in low or low == "tmr":
        start = end = today + timedelta(days=1)
    elif "next week" in low:
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday + timedelta(days=7)
        end = start + timedelta(days=6)
    elif "this week" in low:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif "next 7 days" in low or "next seven days" in low:
        start = today
        end = today + timedelta(days=6)
    else:
        # Exact ISO date/range is also deterministic.
        match = re.search(r"(\d{4}-\d{2}-\d{2})(?:\s*(?:to|through|until|-)\s*(\d{4}-\d{2}-\d{2}))?", low)
        if not match:
            raise ValueError("Use today, tomorrow, this week, next week, next 7 days, or an ISO date/range")
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2)) if match.group(2) else start
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def get_agenda_range(actor: ActorContext, phrase: str,
                     reference_date: str | None = None,
                     include_plans: bool = True) -> dict:
    window = resolve_date_range(phrase, actor.timezone, reference_date)
    result = get_agenda(actor, window["start_date"], window["end_date"], include_plans)
    result["resolved_from"] = phrase
    return result


def get_agenda(actor: ActorContext, start_date: str, end_date: str,
               include_plans: bool = True) -> dict:
    start_utc = _to_utc(start_date + "T00:00:00", actor.timezone)
    end_utc = _to_utc(end_date + "T23:59:59", actor.timezone)
    marks = ",".join("?" for _ in actor.allowed_spaces)
    conn = connect()
    try:
        diary = [dict(r) for r in conn.execute(
            f"""SELECT diary_id,title,start_at_utc,end_at_utc,notes,space_id,'DIARY' AS kind
                FROM diary_events WHERE space_id IN ({marks}) AND status='ACTIVE'
                  AND start_at_utc BETWEEN ? AND ? ORDER BY start_at_utc""",
            list(actor.allowed_spaces) + [start_utc, end_utc],
        ).fetchall()]
        reminders = [dict(r) for r in conn.execute(
            f"""SELECT reminder_id AS id,task_text AS title,due_at_utc AS start_at_utc,
                       NULL AS end_at_utc,status,space_id,'REMINDER' AS kind
                FROM reminders WHERE space_id IN ({marks})
                  AND status IN ('OPEN','DUE','DEFERRED') AND due_at_utc BETWEEN ? AND ?
                ORDER BY due_at_utc""",
            list(actor.allowed_spaces) + [start_utc, end_utc],
        ).fetchall()]
        roster = [dict(r) for r in conn.execute(
            """SELECT roster_id AS id,shift_name AS title,start_at_utc,end_at_utc,status,
                      work_date,'ROSTER' AS kind
               FROM work_roster WHERE owner_id=? AND work_date BETWEEN ? AND ?
                 AND status!='CANCELLED' ORDER BY work_date""",
            (actor.user_id, start_date, end_date),
        ).fetchall()]
        leave = [dict(r) for r in conn.execute(
            """SELECT leave_id AS id,leave_date AS title,NULL AS start_at_utc,NULL AS end_at_utc,
                      status,portion,'LEAVE' AS kind
               FROM leave_records WHERE owner_id=? AND leave_date BETWEEN ? AND ?
                 AND status!='CANCELLED' ORDER BY leave_date""",
            (actor.user_id, start_date, end_date),
        ).fetchall()]
        plans = []
        if include_plans:
            plans = [dict(r) for r in conn.execute(
                f"""SELECT plan_id AS id,title,start_at_utc,end_at_utc,status,space_id,'PLAN' AS kind
                    FROM plans WHERE space_id IN ({marks}) AND status!='CANCELLED'
                      AND (start_at_utc IS NULL OR start_at_utc BETWEEN ? AND ?)
                    ORDER BY COALESCE(start_at_utc,created_at_utc)""",
                list(actor.allowed_spaces) + [start_utc, end_utc],
            ).fetchall()]
        return {"start_date": start_date, "end_date": end_date,
                "diary": diary, "reminders": reminders, "roster": roster,
                "leave": leave, "plans": plans}
    finally:
        conn.close()


def set_cashflow_baseline(actor: ActorContext, currency: str,
                          guaranteed_income: float = 0, fixed_commitments: float = 0,
                          locked_allocations: float = 0, reserves: float = 0,
                          notes: str | None = None) -> dict:
    cur = currency.upper()
    if cur not in {"MYR", "SGD"}:
        raise ValueError("currency must be MYR or SGD")
    values = [_minor(v) for v in (guaranteed_income, fixed_commitments, locked_allocations, reserves)]
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO cashflow_baselines(
                user_id,currency,guaranteed_income_minor,fixed_commitments_minor,
                locked_allocations_minor,reserves_minor,notes,updated_at_utc
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id,currency) DO UPDATE SET
                 guaranteed_income_minor=excluded.guaranteed_income_minor,
                 fixed_commitments_minor=excluded.fixed_commitments_minor,
                 locked_allocations_minor=excluded.locked_allocations_minor,
                 reserves_minor=excluded.reserves_minor,notes=excluded.notes,
                 updated_at_utc=excluded.updated_at_utc""",
            (actor.user_id, cur, *values, notes, utc_now()),
        )
        conn.commit()
        return get_cashflow_baseline(actor, cur)
    finally:
        conn.close()


def get_cashflow_baseline(actor: ActorContext, currency: str) -> dict:
    cur = currency.upper()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM cashflow_baselines WHERE user_id=? AND currency=?",
            (actor.user_id, cur),
        ).fetchone()
        if not row:
            return {"known": False, "currency": cur}
        available = (
            row["guaranteed_income_minor"] - row["fixed_commitments_minor"]
            - row["locked_allocations_minor"] - row["reserves_minor"]
        )
        return {
            "known": True, "currency": cur,
            "guaranteed_income": row["guaranteed_income_minor"] / 100,
            "fixed_commitments": row["fixed_commitments_minor"] / 100,
            "locked_allocations": row["locked_allocations_minor"] / 100,
            "reserves": row["reserves_minor"] / 100,
            "baseline_unallocated": available / 100,
            "notes": row["notes"],
            "rule": "Variable/OT/extra cash is excluded until the user explicitly allocates it.",
        }
    finally:
        conn.close()
