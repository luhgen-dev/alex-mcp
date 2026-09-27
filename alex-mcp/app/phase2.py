from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from context import ActorContext
from db import connect, utc_now
import services


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
        if state not in {"DRAFT", "LOCKED", "CANCELLED"}:
            raise ValueError("plan status must be DRAFT, LOCKED or CANCELLED")
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
    return None


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
    reminder_id = None
    if reminder_minutes_before is not None:
        mins = max(0, min(60 * 24 * 30, int(reminder_minutes_before)))
        due = datetime.fromisoformat(start_utc) - timedelta(minutes=mins)
        linked_actor = replace(actor, action_key=f"{action_key}:linked-reminder")
        reminder = services.create_reminder(
            linked_actor, title, due.astimezone(ZoneInfo(actor.timezone)).isoformat(),
            recurrence_rule=None, shared=(space == "FAMILY_SHARED"), recipient=reminder_recipient,
        )
        reminder_id = reminder.get("reminder_id")
        if reminder_id:
            conn.execute(
                "INSERT OR IGNORE INTO diary_reminder_links(diary_id,reminder_id) VALUES(?,?)",
                (diary_id, reminder_id),
            )
    return {"status": "created", "diary_id": diary_id, "space": space,
            "linked_reminder_id": reminder_id}


def add_diary_event(actor: ActorContext, title: str, start_local: str,
                    end_local: str | None = None, notes: str | None = None,
                    shared: bool = False, reminder_minutes_before: int | None = None,
                    reminder_recipient: str = "me") -> dict:
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
        pending = conn.execute("SELECT * FROM schedule_conflicts WHERE action_key=?", (actor.action_key,)).fetchone()
        if pending and pending["status"] == "OPEN":
            return {"status": "needs_choice", "conflict_id": pending["conflict_id"],
                    "choices": {"1": "add event and create PLANNED leave",
                                "2": "add event and keep the work clash",
                                "3": "cancel"}}
        roster = _roster_conflict(conn, actor, start_utc, end_utc)
        if roster:
            cid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO schedule_conflicts(
                    conflict_id,action_key,owner_id,space_id,title,start_at_utc,end_at_utc,
                    timezone_name,notes,reminder_minutes_before,roster_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, actor.action_key, actor.user_id, space, title[:240], start_utc, end_utc,
                 actor.timezone, notes, reminder_minutes_before, roster["roster_id"]),
            )
            conn.commit()
            return {
                "status": "needs_choice", "conflict_id": cid,
                "message": "This clashes with your work roster.",
                "choices": {"1": "add event and create PLANNED leave",
                            "2": "add event and keep the work clash",
                            "3": "cancel"},
            }
        result = _insert_diary(
            conn, actor, actor.action_key, title, start_utc, end_utc, notes, space,
            reminder_minutes_before, reminder_recipient,
        )
        conn.commit()
        return result
    finally:
        conn.close()


def resolve_diary_conflict(actor: ActorContext, conflict_id: str, choice: int,
                           reminder_recipient: str = "me") -> dict:
    if int(choice) not in {1, 2, 3}:
        raise ValueError("choice must be 1, 2 or 3")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM schedule_conflicts WHERE conflict_id=? AND owner_id=? AND status='OPEN'",
            (conflict_id, actor.user_id),
        ).fetchone()
        if not row:
            raise PermissionError("open conflict not found")
        if int(choice) == 3:
            conn.execute(
                "UPDATE schedule_conflicts SET status='CANCELLED',choice=3 WHERE conflict_id=?",
                (conflict_id,),
            )
            conn.commit()
            return {"status": "cancelled", "conflict_id": conflict_id}

        result = _insert_diary(
            conn, actor, f"{row['action_key']}:choice:{int(choice)}", row["title"],
            row["start_at_utc"], row["end_at_utc"], row["notes"], row["space_id"],
            row["reminder_minutes_before"], reminder_recipient,
        )
        leave = None
        if int(choice) == 1:
            leave_date = _local_date_from_utc(row["start_at_utc"], actor.timezone)
            leave_actor = replace(actor, action_key=f"{row['action_key']}:planned-leave")
            leave = set_leave_record(
                leave_actor, leave_date, status="PLANNED",
                notes=f"Planned automatically from diary conflict: {row['title']}",
            )
        conn.execute(
            "UPDATE schedule_conflicts SET status='RESOLVED',choice=? WHERE conflict_id=?",
            (int(choice), conflict_id),
        )
        conn.commit()
        return {"status": "resolved", "choice": int(choice), "diary": result, "leave": leave}
    finally:
        conn.close()


def update_diary_event(actor: ActorContext, diary_id: str, status: str | None = None,
                       start_local: str | None = None, end_local: str | None = None,
                       title: str | None = None, notes: str | None = None) -> dict:
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
        conn.execute(
            """UPDATE diary_events SET title=?,start_at_utc=?,end_at_utc=?,notes=?,status=?,updated_at_utc=?
               WHERE diary_id=?""",
            ((title or row["title"])[:240], new_start, new_end,
             notes if notes is not None else row["notes"], state, utc_now(), diary_id),
        )
        links = conn.execute(
            """SELECT r.* FROM reminders r JOIN diary_reminder_links l ON l.reminder_id=r.reminder_id
               WHERE l.diary_id=?""", (diary_id,),
        ).fetchall()
        for reminder in links:
            if state == "CANCELLED":
                conn.execute("UPDATE reminders SET status='CANC' WHERE reminder_id=?", (reminder["reminder_id"],))
            elif start_local:
                old_start = datetime.fromisoformat(row["start_at_utc"])
                old_due = datetime.fromisoformat(reminder["due_at_utc"])
                delta = old_start - old_due
                new_due = datetime.fromisoformat(new_start) - delta
                conn.execute(
                    "UPDATE reminders SET due_at_utc=?,status='OPEN' WHERE reminder_id=?",
                    (new_due.isoformat(), reminder["reminder_id"]),
                )
        conn.commit()
        return {"status": "updated", "diary_id": diary_id, "state": state,
                "linked_reminders_updated": len(links)}
    finally:
        conn.close()


def check_spouse_availability(actor: ActorContext, start_local: str,
                              end_local: str | None = None) -> dict:
    spouse = "USR_WIFE" if actor.user_id == "USR_HUSBAND" else "USR_HUSBAND"
    start_utc = _to_utc(start_local, actor.timezone)
    end_utc = _to_utc(end_local, actor.timezone) if end_local else (
        datetime.fromisoformat(start_utc) + timedelta(hours=1)
    ).isoformat()
    local_date = _local_date_from_utc(start_utc, actor.timezone)
    conn = connect()
    try:
        busy = False
        roster = conn.execute(
            """SELECT 1 FROM work_roster WHERE owner_id=? AND work_date=?
               AND status IN ('PLANNED','CONFIRMED') LIMIT 1""",
            (spouse, local_date),
        ).fetchone()
        if roster:
            busy = True
        if not busy:
            rows = conn.execute(
                """SELECT start_at_utc,end_at_utc FROM diary_events
                   WHERE owner_id=? AND status='ACTIVE'""", (spouse,),
            ).fetchall()
            start = datetime.fromisoformat(start_utc)
            end = datetime.fromisoformat(end_utc)
            for row in rows:
                rs = datetime.fromisoformat(row["start_at_utc"])
                re = datetime.fromisoformat(row["end_at_utc"]) if row["end_at_utc"] else rs + timedelta(hours=1)
                if start < re and end > rs:
                    busy = True
                    break
        return {"spouse": "wife" if spouse == "USR_WIFE" else "husband",
                "date": local_date, "availability": "busy" if busy else "no_conflict_found",
                "privacy": "details_hidden"}
    finally:
        conn.close()


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
