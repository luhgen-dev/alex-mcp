from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

import runtime_clock
import scope_policy
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


def _row_get(row, key: str, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row.get(key, default)
        except AttributeError:
            return default


def _has_explicit_time(value: str | None) -> bool:
    raw = str(value or "").strip()
    # ISO date-only input is a valid all-day/date-known item, not midnight.
    return "T" in raw or (" " in raw and ":" in raw)


def _local_date_from_utc(value: str, tz_name: str) -> str:
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz_name)).date().isoformat()


def _local_iso_from_utc(value: str | None, tz_name: str) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        ZoneInfo(tz_name)
    ).isoformat()


def _space(actor: ActorContext, shared: bool) -> str:
    space = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
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
                     portion: str = "FULL", notes: str | None = None,
                     end_date: str | None = None,
                     leave_type: str = "ANNUAL_LEAVE") -> dict:
    """Store future leave lifecycle. Only TAKEN becomes historical work absence."""
    if actor.conversation_type == "GROUP":
        raise PermissionError("private leave lifecycle must be managed in the owner's DM")
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    state = status.upper()
    if state not in {"PLANNED", "CONFIRMED", "TAKEN", "CANCELLED"}:
        raise ValueError("leave status must be PLANNED, CONFIRMED, TAKEN or CANCELLED")
    kind = (leave_type or "ANNUAL_LEAVE").upper()
    if kind not in {"ANNUAL_LEAVE", "MEDICAL_LEAVE", "OTHER_LEAVE"}:
        raise ValueError("leave_type must be ANNUAL_LEAVE, MEDICAL_LEAVE or OTHER_LEAVE")
    portion = (portion or "FULL").upper()[:20]
    start_d = datetime.fromisoformat(str(leave_date)[:10]).date()
    end_d = datetime.fromisoformat(str(end_date or leave_date)[:10]).date()
    if end_d < start_d:
        raise ValueError("leave end_date cannot be before leave_date")

    conn = connect()
    try:
        existing_action = conn.execute(
            "SELECT * FROM leave_records WHERE action_key=?", (actor.action_key,)
        ).fetchone()
        if existing_action:
            return {"status": "already_applied", **dict(existing_action)}

        # Portion is editable state, not identity. Matching on portion caused a
        # FULL -> HALF correction to insert a second active leave row.
        row = conn.execute(
            """SELECT * FROM leave_records
               WHERE owner_id=? AND leave_date=? AND status!='CANCELLED'
               ORDER BY updated_at_utc DESC LIMIT 1""",
            (actor.user_id, start_d.isoformat()),
        ).fetchone()

        if row and row["status"] == "TAKEN" and state != "TAKEN":
            raise ValueError(
                "Taken leave is historical fact; correct the underlying work record "
                "rather than silently reverting it."
            )

        if row:
            leave_id = row["leave_id"]
            conn.execute(
                """UPDATE leave_records SET status=?,end_date=?,leave_type=?,portion=?,notes=?,
                   updated_at_utc=? WHERE leave_id=?""",
                (state, end_d.isoformat(), kind, portion, notes, utc_now(), leave_id),
            )
        else:
            leave_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO leave_records(
                    leave_id,action_key,owner_id,leave_date,end_date,leave_type,
                    portion,status,notes
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (leave_id, actor.action_key, actor.user_id, start_d.isoformat(),
                 end_d.isoformat(), kind, portion, state, notes),
            )

        materialized = 0
        if state == "TAKEN" and kind in {"ANNUAL_LEAVE", "MEDICAL_LEAVE"}:
            # Materialize dated historical absence exactly once. This is what
            # the proven roster/OT engine consumes; PLANNED/CONFIRMED never do.
            import phase2_work
            phase2_work.ensure_schema(conn)
            day = start_d
            while day <= end_d:
                exists = conn.execute(
                    """SELECT 1 FROM alex_phase2_work_events
                       WHERE owner_user_id=? AND event_date=? AND event_type=?
                       LIMIT 1""",
                    (actor.user_id, day.isoformat(), kind),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """INSERT INTO alex_phase2_work_events(
                            work_event_id,space_id,owner_user_id,event_date,event_type,
                            units_days,note,source_message_id
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), actor.private_space, actor.user_id,
                         day.isoformat(), kind,
                         0.5 if portion.startswith("HALF") else 1.0,
                         notes or "Taken from Alex leave lifecycle",
                         actor.source_message_id),
                    )
                    materialized += 1
                day += timedelta(days=1)

        conn.commit()
        return {
            "status": "saved", "leave_id": leave_id,
            "leave_date": start_d.isoformat(), "end_date": end_d.isoformat(),
            "leave_type": kind, "portion": portion, "state": state,
            "materialized_days": materialized,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_leave_records(actor: ActorContext, start_date: str | None = None,
                       end_date: str | None = None,
                       include_cancelled: bool = False) -> dict:
    if actor.conversation_type == "GROUP":
        # Work/leave is private unless the owner explicitly publishes another
        # family-safe artifact. Never expose private leave state in group.
        return {"leave": []}
    where = "owner_id=?"
    params: list = [actor.user_id]
    if not include_cancelled:
        where += " AND status!='CANCELLED'"
    if start_date:
        where += " AND COALESCE(end_date,leave_date)>=?"
        params.append(start_date)
    if end_date:
        where += " AND leave_date<=?"
        params.append(end_date)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT leave_id,leave_date,COALESCE(end_date,leave_date) AS end_date,
                       leave_type,portion,status,notes
                FROM leave_records WHERE {where} ORDER BY leave_date""", params,
        ).fetchall()
        return {"leave": [dict(r) for r in rows]}
    finally:
        conn.close()


def create_plan(actor: ActorContext, title: str, start_local: str | None = None,
                end_local: str | None = None, notes: str | None = None,
                shared: bool = False, locked: bool = False,
                time_known: bool | None = None) -> dict:
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
                time_known,timezone_name,notes,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, actor.action_key, actor.user_id, space, title[:240],
             _to_utc(start_local, actor.timezone) if start_local else None,
             _to_utc(end_local, actor.timezone) if end_local else None,
             1 if (
                 (_has_explicit_time(start_local) if time_known is None else bool(time_known))
                 and start_local
             ) else 0,
             actor.timezone, notes, "LOCKED" if locked else "DRAFT"),
        )
        conn.commit()
        created = conn.execute("SELECT time_known FROM plans WHERE plan_id=?", (pid,)).fetchone()
        return {"status": "saved", "plan_id": pid, "title": title[:240],
                "state": "LOCKED" if locked else "DRAFT", "space": space,
                "time_known": bool(created["time_known"])}
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
            f"""SELECT plan_id,title,start_at_utc,end_at_utc,time_known,notes,status,space_id
                FROM plans WHERE {where} ORDER BY COALESCE(start_at_utc,created_at_utc) LIMIT ?""",
            list(actor.allowed_spaces) + [max(1, min(100, int(limit)))],
        ).fetchall()
        return {"plans": [dict(r) for r in rows]}
    finally:
        conn.close()



def resolve_plan_reference(actor: ActorContext, plan_id: str | None = None,
                           plan_name: str | None = None) -> str:
    """Resolve one accessible plan by exact id or natural title without model guessing."""
    supplied_id = str(plan_id or "").strip()
    supplied_name = str(plan_name or "").strip()
    if not supplied_id and not supplied_name:
        raise ValueError("provide plan_id or plan_name")
    marks = ",".join("?" for _ in actor.allowed_spaces)
    conn = connect()
    try:
        if supplied_id:
            row = conn.execute(
                f"SELECT plan_id FROM plans WHERE plan_id=? AND space_id IN ({marks})",
                [supplied_id] + list(actor.allowed_spaces),
            ).fetchone()
            if not row:
                raise PermissionError("plan not found in your accessible spaces")
            return row["plan_id"]
        rows = conn.execute(
            f"""SELECT plan_id,title,status FROM plans
                WHERE LOWER(title)=LOWER(?) AND space_id IN ({marks})
                ORDER BY status='CANCELLED',updated_at_utc DESC""",
            [supplied_name] + list(actor.allowed_spaces),
        ).fetchall()
        active = [row for row in rows if row["status"] != "CANCELLED"]
        candidates = active or rows
        if not candidates:
            raise ValueError(f"no plan named {supplied_name!r} was found")
        if len(candidates) > 1:
            raise ValueError(f"more than one accessible plan is named {supplied_name!r}")
        return candidates[0]["plan_id"]
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
        new_start = _to_utc(start_local, actor.timezone) if start_local else row["start_at_utc"]
        new_end = _to_utc(end_local, actor.timezone) if end_local else row["end_at_utc"]
        new_time_known = _has_explicit_time(start_local) if start_local is not None else bool(row["time_known"])

        link = conn.execute(
            "SELECT diary_id FROM plan_diary_links WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        linked_diary_result = None
        if link:
            changing_time = (
                new_start != row["start_at_utc"] or new_end != row["end_at_utc"]
            )
            cancelling = state == "CANCELLED" and row["status"] != "CANCELLED"
            changing_content = title is not None or notes is not None
            if cancelling or changing_time or changing_content:
                # A confirmed plan and its materialized diary event are one
                # lifecycle. Cancellation cascades; time edits shift linked
                # reminders by the same delta; text-only edits keep reminders.
                linked_diary_result = update_diary_event(
                    actor,
                    link["diary_id"],
                    status="CANCELLED" if cancelling else None,
                    start_local=start_local if changing_time else None,
                    end_local=end_local if changing_time else None,
                    title=title,
                    notes=notes,
                    linked_reminders=(
                        "cancel" if cancelling else ("shift" if changing_time else "keep")
                    ),
                )
                if linked_diary_result.get("status") in {
                    "needs_reminder_choice", "needs_conflict_resolution"
                }:
                    return {
                        **linked_diary_result,
                        "plan_id": plan_id,
                        "plan_unchanged": True,
                    }

        conn.execute(
            """UPDATE plans SET title=?,start_at_utc=?,end_at_utc=?,time_known=?,
               notes=?,status=?,updated_at_utc=? WHERE plan_id=?""",
            (
                (title or row["title"])[:240], new_start, new_end,
                1 if new_time_known else 0,
                notes if notes is not None else row["notes"],
                state, utc_now(), plan_id,
            ),
        )
        conn.commit()
        return {
            "status": "updated", "plan_id": plan_id, "state": state,
            "linked_diary_id": link["diary_id"] if link else None,
            "linked_diary_updated": bool(linked_diary_result),
        }
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
        time_known=bool(row["time_known"]),
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


def share_plan(actor: ActorContext, plan_id: str,
               shared_notes: str | None = None) -> dict:
    """Create a separate FAMILY_SHARED copy without promoting private notes.

    Only title/dates/status are copied automatically. Notes are copied only when
    the owner explicitly supplies family-safe shared_notes in this action.
    """
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
        safe_notes = (shared_notes or "").strip() or None
        conn.execute(
            """INSERT OR IGNORE INTO plans(
                plan_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                time_known,timezone_name,notes,status,source_plan_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (copy_id, action_key, actor.user_id, "FAMILY_SHARED", row["title"],
             row["start_at_utc"], row["end_at_utc"], row["time_known"],
             row["timezone_name"], safe_notes, row["status"], plan_id),
        )
        conn.commit()
        copied = conn.execute(
            "SELECT plan_id,notes FROM plans WHERE action_key=?", (action_key,)
        ).fetchone()
        return {
            "status": "shared_copy_created",
            "plan_id": copied["plan_id"],
            "source_plan_id": plan_id,
            "private_notes_copied": False,
            "shared_notes": copied["notes"],
        }
    finally:
        conn.close()


def _normalize_task_assignee(value: str | None) -> str:
    raw = (value or "unassigned").strip().casefold()
    aliases = {
        "": "unassigned", "none": "unassigned", "unassigned": "unassigned",
        "me": "me", "self": "me", "myself": "me", "user": "me", "owner": "me",
        "spouse": "spouse", "partner": "spouse", "wife": "spouse", "husband": "spouse",
        "both": "both", "both of us": "both", "everyone": "both",
    }
    if raw not in aliases:
        raise ValueError("task assignee must be me, spouse, both, or unassigned")
    return aliases[raw]


def _task_due_parts(due_local: str | None, tz_name: str) -> tuple[str | None, str | None]:
    """Preserve date-only task due dates without inventing midnight."""
    if due_local is None:
        return None, None
    raw = str(due_local).strip()
    if not raw:
        return None, None
    if _has_explicit_time(raw):
        dt = _to_dt(raw, tz_name)
        return dt.astimezone(timezone.utc).isoformat(), dt.astimezone(ZoneInfo(tz_name)).date().isoformat()
    try:
        due_date = date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("task due date/time must be ISO date or date-time") from exc
    return None, due_date


def _authorized_task(conn, actor: ActorContext, task_id: str):
    marks = ",".join("?" for _ in actor.allowed_spaces)
    row = conn.execute(
        f"SELECT * FROM tasks WHERE task_id=? AND space_id IN ({marks})",
        [task_id] + list(actor.allowed_spaces),
    ).fetchone()
    if not row:
        raise PermissionError("task not found in your accessible spaces")
    return row


def _validate_task_plan(conn, actor: ActorContext, plan_id: str | None, task_space: str) -> str | None:
    if not plan_id:
        return None
    marks = ",".join("?" for _ in actor.allowed_spaces)
    row = conn.execute(
        f"SELECT plan_id,space_id,status FROM plans WHERE plan_id=? AND space_id IN ({marks})",
        [plan_id] + list(actor.allowed_spaces),
    ).fetchone()
    if not row or row["status"] == "CANCELLED":
        raise PermissionError("plan not found in your accessible active plans")
    if task_space == "FAMILY_SHARED" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("a family task cannot expose a private plan link")
    return row["plan_id"]


def _set_task_reminder_link(conn, actor: ActorContext, task_id: str,
                            task_space: str, reminder_id: str | None) -> None:
    if reminder_id is None:
        return
    conn.execute("DELETE FROM task_reminder_links WHERE task_id=?", (task_id,))
    rid = str(reminder_id).strip()
    if not rid:
        return
    marks = ",".join("?" for _ in actor.allowed_spaces)
    row = conn.execute(
        f"SELECT reminder_id,space_id FROM reminders WHERE reminder_id=? AND space_id IN ({marks})",
        [rid] + list(actor.allowed_spaces),
    ).fetchone()
    if not row:
        raise PermissionError("reminder not found in your accessible spaces")
    if task_space == "FAMILY_SHARED" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("a family task cannot expose a private reminder link")
    conn.execute(
        "INSERT INTO task_reminder_links(task_id,reminder_id) VALUES(?,?)",
        (task_id, rid),
    )


def _record_task_event(conn, actor: ActorContext, task_id: str, event_type: str,
                       from_status: str | None, to_status: str,
                       title: str, notes: str | None,
                       action_key: str | None = None) -> None:
    key = action_key or f"{actor.action_key}:task-event:{event_type.lower()}"
    conn.execute(
        """INSERT INTO task_events(
            event_id,action_key,task_id,actor_id,event_type,from_status,to_status,
            title_snapshot,notes_snapshot
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), key, task_id, actor.user_id, event_type,
            from_status, to_status, title[:240], notes,
        ),
    )


def create_task(actor: ActorContext, title: str, notes: str | None = None,
                assignee: str = "unassigned", shared: bool = False,
                due_local: str | None = None, plan_id: str | None = None,
                reminder_id: str | None = None) -> dict:
    """Create a first-class task without silently creating a plan or reminder."""
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("task title is required")
    assignment = _normalize_task_assignee(assignee)
    space = _space(actor, shared)
    if assignment in {"spouse", "both"} and space != "FAMILY_SHARED":
        raise ValueError("a spouse/both task must be explicitly shared with the family")
    due_at_utc, due_date_local = _task_due_parts(due_local, actor.timezone)

    conn = connect()
    try:
        existing = conn.execute(
            "SELECT * FROM tasks WHERE action_key=?", (actor.action_key,)
        ).fetchone()
        if existing:
            return {"status": "already_applied", **dict(existing)}

        linked_plan = _validate_task_plan(conn, actor, plan_id, space)
        task_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO tasks(
                task_id,action_key,source_message_id,owner_id,space_id,title,notes,
                status,assignee,due_at_utc,due_date_local,timezone_name,plan_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, actor.action_key, actor.source_message_id, actor.user_id,
                space, clean_title[:240], notes, "OPEN", assignment,
                due_at_utc, due_date_local, actor.timezone, linked_plan,
            ),
        )
        _set_task_reminder_link(conn, actor, task_id, space, reminder_id)
        _record_task_event(
            conn, actor, task_id, "CREATED", None, "OPEN",
            clean_title, notes, f"{actor.action_key}:created",
        )
        conn.commit()
        return {
            "status": "created",
            "task_id": task_id,
            "task_status": "OPEN",
            "title": clean_title[:240],
            "space": space,
            "assignee": assignment,
            "due_at_utc": due_at_utc,
            "due_date_local": due_date_local,
            "plan_id": linked_plan,
            "reminder_id": str(reminder_id).strip() if reminder_id else None,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_tasks(actor: ActorContext, status: str = "open",
               plan_id: str | None = None, limit: int = 50) -> dict:
    """Read tasks visible in the active privacy boundary."""
    state = (status or "open").strip().upper()
    aliases = {
        "OPEN": "OPEN", "OUTSTANDING": "OPEN", "UNFINISHED": "OPEN",
        "DONE": "DONE", "COMPLETED": "DONE", "COMPLETE": "DONE",
        "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
        "ALL": "ALL", "ANY": "ALL",
    }
    if state not in aliases:
        raise ValueError("task status must be open, done, cancelled, or all")
    state = aliases[state]
    marks = ",".join("?" for _ in actor.allowed_spaces)
    where = [f"t.space_id IN ({marks})"]
    params: list = list(actor.allowed_spaces)
    if state != "ALL":
        where.append("t.status=?")
        params.append(state)
    if plan_id:
        where.append("t.plan_id=?")
        params.append(plan_id)
    params.append(max(1, min(100, int(limit))))

    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT t.task_id,t.title,t.notes,t.status,t.assignee,t.space_id,
                       t.due_at_utc,t.due_date_local,t.timezone_name,t.plan_id,
                       t.created_at_utc,t.updated_at_utc,
                       (SELECT tr.reminder_id FROM task_reminder_links tr
                        WHERE tr.task_id=t.task_id LIMIT 1) AS reminder_id,
                       (SELECT COUNT(*) FROM task_events te
                        WHERE te.task_id=t.task_id) AS history_events
                FROM tasks t
                WHERE {' AND '.join(where)}
                ORDER BY CASE t.status WHEN 'OPEN' THEN 0 WHEN 'DONE' THEN 1 ELSE 2 END,
                         COALESCE(t.due_at_utc,t.due_date_local,t.updated_at_utc),
                         t.updated_at_utc DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return {"tasks": [dict(r) for r in rows], "status_filter": state}
    finally:
        conn.close()


def update_task(actor: ActorContext, task_id: str,
                title: str | None = None, notes: str | None = None,
                due_local: str | None = None, assignee: str | None = None,
                plan_id: str | None = None,
                reminder_id: str | None = None) -> dict:
    """Edit task fields while preserving lifecycle state and history."""
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    conn = connect()
    try:
        prior_event = conn.execute(
            "SELECT task_id FROM task_events WHERE action_key=?", (actor.action_key,)
        ).fetchone()
        if prior_event:
            row = _authorized_task(conn, actor, prior_event["task_id"])
            return {"status": "already_applied", "task_id": row["task_id"],
                    "task_status": row["status"]}

        row = _authorized_task(conn, actor, task_id)
        if row["status"] == "CANCELLED":
            raise ValueError("cancelled task must be reopened by creating a new task")

        new_title = row["title"] if title is None else str(title).strip()
        if not new_title:
            raise ValueError("task title cannot be empty")
        new_notes = row["notes"] if notes is None else notes
        new_assignee = row["assignee"] if assignee is None else _normalize_task_assignee(assignee)
        if new_assignee in {"spouse", "both"} and row["space_id"] != "FAMILY_SHARED":
            raise ValueError("a spouse/both task must be explicitly shared with the family")

        if due_local is None:
            due_at_utc, due_date_local = row["due_at_utc"], row["due_date_local"]
        else:
            due_at_utc, due_date_local = _task_due_parts(due_local, actor.timezone)

        if plan_id is None:
            linked_plan = row["plan_id"]
        else:
            linked_plan = _validate_task_plan(
                conn, actor, str(plan_id).strip() or None, row["space_id"]
            )

        conn.execute(
            """UPDATE tasks
               SET title=?,notes=?,assignee=?,due_at_utc=?,due_date_local=?,
                   plan_id=?,updated_at_utc=?
               WHERE task_id=?""",
            (
                new_title[:240], new_notes, new_assignee, due_at_utc,
                due_date_local, linked_plan, utc_now(), task_id,
            ),
        )
        _set_task_reminder_link(
            conn, actor, task_id, row["space_id"], reminder_id
        )
        _record_task_event(
            conn, actor, task_id, "UPDATED", row["status"], row["status"],
            new_title, new_notes, actor.action_key,
        )
        conn.commit()
        return {
            "status": "updated", "task_id": task_id,
            "task_status": row["status"], "title": new_title[:240],
            "assignee": new_assignee, "due_at_utc": due_at_utc,
            "due_date_local": due_date_local, "plan_id": linked_plan,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _transition_task(actor: ActorContext, task_id: str, target: str,
                     event_type: str, allowed_from: set[str]) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    conn = connect()
    try:
        prior_event = conn.execute(
            "SELECT task_id,to_status FROM task_events WHERE action_key=?",
            (actor.action_key,),
        ).fetchone()
        if prior_event:
            row = _authorized_task(conn, actor, prior_event["task_id"])
            return {"status": "already_applied", "task_id": row["task_id"],
                    "task_status": row["status"]}

        row = _authorized_task(conn, actor, task_id)
        current = row["status"]
        if current == target:
            return {"status": "already_in_state", "task_id": task_id,
                    "task_status": current}
        if current not in allowed_from:
            raise ValueError(
                f"task cannot move from {current} to {target} with this action"
            )
        conn.execute(
            "UPDATE tasks SET status=?,updated_at_utc=? WHERE task_id=?",
            (target, utc_now(), task_id),
        )
        _record_task_event(
            conn, actor, task_id, event_type, current, target,
            row["title"], row["notes"], actor.action_key,
        )
        conn.commit()
        return {
            "status": "updated", "task_id": task_id,
            "previous_status": current, "task_status": target,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_task(actor: ActorContext, task_id: str) -> dict:
    return _transition_task(actor, task_id, "DONE", "COMPLETED", {"OPEN"})


def reopen_task(actor: ActorContext, task_id: str) -> dict:
    return _transition_task(actor, task_id, "OPEN", "REOPENED", {"DONE"})


def cancel_task(actor: ActorContext, task_id: str) -> dict:
    # Cancellation changes only this task. Linked plan/reminder rows are retained.
    return _transition_task(actor, task_id, "CANCELLED", "CANCELLED", {"OPEN", "DONE"})


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
                    exclude_diary_id: str | None = None,
                    candidate_time_known: bool = True):
    if not candidate_time_known:
        return None
    marks = ",".join("?" for _ in actor.allowed_spaces)
    params = list(actor.allowed_spaces)
    sql = f"""SELECT * FROM diary_events
              WHERE space_id IN ({marks}) AND status='ACTIVE' AND time_known=1"""
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


def _same_day_heads_up(conn, actor: ActorContext, local_date: str,
                       exclude_diary_id: str | None = None) -> list[dict]:
    marks = ",".join("?" for _ in actor.allowed_spaces)
    start_utc = _to_utc(local_date + "T00:00:00", actor.timezone)
    end_utc = _to_utc(local_date + "T23:59:59", actor.timezone)
    params = list(actor.allowed_spaces) + [start_utc, end_utc]
    sql = f"""SELECT diary_id,title,start_at_utc,end_at_utc,time_known
              FROM diary_events
              WHERE space_id IN ({marks}) AND status='ACTIVE'
                AND start_at_utc BETWEEN ? AND ?"""
    if exclude_diary_id:
        sql += " AND diary_id!=?"
        params.append(exclude_diary_id)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "kind": "DIARY_SAME_DAY",
            "diary_id": r["diary_id"],
            "title": r["title"],
            "time_known": bool(r["time_known"]),
        }
        for r in rows
    ]


def _leave_heads_up(conn, actor: ActorContext, local_date: str) -> list[dict]:
    if actor.conversation_type == "GROUP":
        return []
    rows = conn.execute(
        """SELECT leave_id,leave_date,COALESCE(end_date,leave_date) AS end_date,
                  leave_type,status
           FROM leave_records
           WHERE owner_id=? AND status IN ('PLANNED','CONFIRMED','TAKEN')
             AND leave_date<=? AND COALESCE(end_date,leave_date)>=?""",
        (actor.user_id, local_date, local_date),
    ).fetchall()
    return [
        {
            "kind": "LEAVE_COVERS_DAY",
            "leave_id": r["leave_id"],
            "leave_type": r["leave_type"],
            "status": r["status"],
        }
        for r in rows
    ]


def _ticket_expired(row) -> bool:
    if not row["expires_at_utc"]:
        return False
    try:
        expiry = datetime.fromisoformat(row["expires_at_utc"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return runtime_clock.now_utc() > expiry.astimezone(timezone.utc)
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
                  reminder_recipient: str = "me",
                  time_known: bool = True) -> dict:
    existing = conn.execute("SELECT * FROM diary_events WHERE action_key=?", (action_key,)).fetchone()
    if existing:
        return {"status": "already_applied", **dict(existing)}
    diary_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO diary_events(
            diary_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
            time_known,timezone_name,notes
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (diary_id, action_key, actor.user_id, space, title[:240], start_utc, end_utc,
         1 if time_known else 0, actor.timezone, notes),
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
            "time_known": bool(time_known),
            "linked_reminder_id": reminder_ids[0] if len(reminder_ids) == 1 else None,
            "linked_reminder_ids": reminder_ids}


def add_diary_event(actor: ActorContext, title: str, start_local: str,
                    end_local: str | None = None, notes: str | None = None,
                    shared: bool = False, reminder_minutes_before: int | None = None,
                    reminder_recipient: str = "me",
                    source_plan_id: str | None = None,
                    time_known: bool | None = None) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    start_utc = _to_utc(start_local, actor.timezone)
    end_utc = _to_utc(end_local, actor.timezone) if end_local else None
    candidate_time_known = _has_explicit_time(start_local) if time_known is None else bool(time_known)
    if candidate_time_known and not _has_explicit_time(start_local):
        raise ValueError("time_known=true requires an explicit time; do not invent midnight")
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

        local_date = _local_date_from_utc(start_utc, actor.timezone)
        heads_up = _same_day_heads_up(conn, actor, local_date)
        heads_up.extend(_leave_heads_up(conn, actor, local_date))

        roster = _roster_conflict(conn, actor, start_utc, end_utc) if candidate_time_known else None
        if roster and _row_get(roster, "time_known") is False:
            heads_up.append({
                "kind": "WORK_SAME_DAY",
                "shift": _row_get(roster, "shift"),
                "time_known": False,
            })
            roster = None
        elif not candidate_time_known and actor.conversation_type != "GROUP":
            # Date-only events get a non-blocking work heads-up if the roster says
            # this is a work day. No time overlap is invented.
            try:
                work = phase2_work.effective_shift(local_date, actor.phone, actor.conversation_type)
                if work.get("shift") not in {None, "off"}:
                    heads_up.append({
                        "kind": "WORK_SAME_DAY",
                        "shift": work.get("shift"),
                        "time_known": False,
                    })
            except (ValueError, PermissionError):
                pass

        if roster:
            cid = str(uuid.uuid4())
            expiry = (runtime_clock.now_utc() + timedelta(hours=48)).isoformat()
            conn.execute(
                """INSERT INTO schedule_conflicts(
                    conflict_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                    timezone_name,notes,reminder_minutes_before,roster_id,
                    conflict_kind,expires_at_utc,source_plan_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, actor.action_key, actor.user_id, space, title[:240], start_utc, end_utc,
                 actor.timezone, notes, reminder_minutes_before, _row_get(roster, "roster_id"),
                 "WORK", expiry, source_plan_id),
            )
            conn.commit()
            return {
                "status": "needs_choice", "conflict_id": cid, "conflict_kind": "WORK",
                "expires_at_utc": expiry,
                "message": "This clashes with your work roster.",
                "heads_up": heads_up,
                "choices": {"1": "add event and create PLANNED leave",
                            "2": "add event and keep the work clash",
                            "3": "cancel"},
            }

        diary_clash = _diary_conflict(
            conn, actor, start_utc, end_utc,
            candidate_time_known=candidate_time_known,
        )
        if diary_clash:
            cid = str(uuid.uuid4())
            expiry = (runtime_clock.now_utc() + timedelta(hours=48)).isoformat()
            conn.execute(
                """INSERT INTO schedule_conflicts(
                    conflict_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                    timezone_name,notes,reminder_minutes_before,conflicting_diary_id,
                    conflict_kind,expires_at_utc,source_plan_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, actor.action_key, actor.user_id, space, title[:240], start_utc, end_utc,
                 actor.timezone, notes, reminder_minutes_before, diary_clash["diary_id"],
                 "DIARY", expiry, source_plan_id),
            )
            conn.commit()
            return {
                "status": "needs_choice", "conflict_id": cid, "conflict_kind": "DIARY",
                "expires_at_utc": expiry,
                "message": "This overlaps an existing diary event.",
                "heads_up": heads_up,
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
            time_known=candidate_time_known,
        )
        result["heads_up"] = heads_up
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
        new_time_known = (
            _has_explicit_time(start_local) if start_local is not None
            else bool(row["time_known"])
        )
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
            roster = _roster_conflict(conn, actor, new_start, new_end) if new_time_known else None
            if roster and _row_get(roster, "time_known") is False:
                roster = None
            diary_clash = _diary_conflict(
                conn, actor, new_start, new_end,
                exclude_diary_id=diary_id, candidate_time_known=new_time_known,
            )
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
            """UPDATE diary_events SET title=?,start_at_utc=?,end_at_utc=?,time_known=?,
               notes=?,status=?,updated_at_utc=? WHERE diary_id=?""",
            ((title or row["title"])[:240], new_start, new_end, 1 if new_time_known else 0,
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
        today = runtime_clock.today(timezone_name)
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
        # "Next Thursday" means the next occurrence after today, matching
        # ordinary household usage rather than "Thursday of next week".
        weekday_names = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        weekday_match = re.search(
            r"\b(?:next|coming)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            low,
        )
        if weekday_match:
            target = weekday_names[weekday_match.group(1)]
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7
            start = end = today + timedelta(days=delta)
        else:
            # Exact ISO date/range remains the canonical path.
            match = re.search(r"(\d{4}-\d{2}-\d{2})(?:\s*(?:to|through|until|-)\s*(\d{4}-\d{2}-\d{2}))?", low)
            if match:
                start = date.fromisoformat(match.group(1))
                end = date.fromisoformat(match.group(2)) if match.group(2) else start
            else:
                # Accept ordinary spoken/written calendar dates such as
                # "1 October 2026" and "October 1st, 2026".
                cleaned = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", low)
                cleaned = re.sub(r"[,]+", " ", cleaned)
                cleaned = " ".join(cleaned.split())
                parsed = None
                for fmt in ("%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y",
                            "%d %B", "%B %d", "%d %b", "%b %d"):
                    try:
                        candidate = datetime.strptime(cleaned, fmt).date()
                    except ValueError:
                        continue
                    if "%Y" not in fmt:
                        candidate = candidate.replace(year=today.year)
                        if candidate < today:
                            candidate = candidate.replace(year=today.year + 1)
                    parsed = candidate
                    break
                if parsed is None:
                    raise ValueError(
                        "Use today, tomorrow, this/next week, next weekday, or a calendar/ISO date"
                    )
                start = end = parsed
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
            f"""SELECT diary_id,title,start_at_utc,end_at_utc,time_known,notes,space_id,'DIARY' AS kind
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
        if actor.conversation_type == "GROUP":
            # Roster and leave lifecycle are owner-private work facts. A family
            # Agenda must never reveal even the existence of those private facts.
            roster = []
            leave = []
        else:
            roster = [dict(r) for r in conn.execute(
                """SELECT roster_id AS id,shift_name AS title,start_at_utc,end_at_utc,status,
                          work_date,'ROSTER' AS kind
                   FROM work_roster WHERE owner_id=? AND work_date BETWEEN ? AND ?
                     AND status!='CANCELLED' ORDER BY work_date""",
                (actor.user_id, start_date, end_date),
            ).fetchall()]
            leave = [dict(r) for r in conn.execute(
                """SELECT leave_id AS id,leave_date AS title,
                          COALESCE(end_date,leave_date) AS end_date,NULL AS start_at_utc,
                          status,portion,leave_type,'LEAVE' AS kind
                   FROM leave_records WHERE owner_id=?
                     AND COALESCE(end_date,leave_date)>=? AND leave_date<=?
                     AND status!='CANCELLED' ORDER BY leave_date""",
                (actor.user_id, start_date, end_date),
            ).fetchall()]
        plans = []
        if include_plans:
            plans = [dict(r) for r in conn.execute(
                f"""SELECT plan_id AS id,title,start_at_utc,end_at_utc,time_known,status,space_id,'PLAN' AS kind
                    FROM plans WHERE space_id IN ({marks}) AND status!='CANCELLED'
                      AND (start_at_utc IS NULL OR start_at_utc BETWEEN ? AND ?)
                    ORDER BY COALESCE(start_at_utc,created_at_utc)""",
                list(actor.allowed_spaces) + [start_utc, end_utc],
            ).fetchall()]
        # Provider-facing agenda rows expose canonical local timestamps so the
        # model never has to mentally convert stored UTC values.
        for row in diary:
            row["start_local"] = _local_iso_from_utc(row.get("start_at_utc"), actor.timezone)
            row["end_local"] = _local_iso_from_utc(row.get("end_at_utc"), actor.timezone)
        for row in reminders:
            row["due_local"] = _local_iso_from_utc(row.get("start_at_utc"), actor.timezone)
        for row in roster:
            row["start_local"] = _local_iso_from_utc(row.get("start_at_utc"), actor.timezone)
            row["end_local"] = _local_iso_from_utc(row.get("end_at_utc"), actor.timezone)
        for row in plans:
            row["start_local"] = _local_iso_from_utc(row.get("start_at_utc"), actor.timezone)
            row["end_local"] = _local_iso_from_utc(row.get("end_at_utc"), actor.timezone)
        return {"start_date": start_date, "end_date": end_date,
                "timezone": actor.timezone,
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
