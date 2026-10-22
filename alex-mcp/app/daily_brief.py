from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import db
import phase2
import phase2_finance
import phase2_work
import profile_config
import runtime_clock
from context import ActorContext, use_actor


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_briefs (
    brief_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    brief_date TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','ACKNOWLEDGED')) DEFAULT 'PENDING',
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acknowledged_at_utc TEXT,
    UNIQUE(user_id,brief_date),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_brief_status
ON daily_briefs(user_id,status,brief_date);
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


def _local(value, tz_name):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None


def _fmt_time(value, tz_name):
    dt = _local(value, tz_name)
    if not dt:
        return None
    return dt.strftime("%-I:%M %p")


def _all_scope_actor(actor: ActorContext) -> ActorContext:
    if actor.conversation_type == "GROUP":
        raise PermissionError("Daily Brief is private and available only in the owner's DM")
    return replace(actor, read_scope="all")


def _work_section(actor, day):
    try:
        shift = phase2_work.effective_shift(day, actor.phone, "DIRECT_DM")
    except ValueError as exc:
        if "No active roster profile configured" in str(exc):
            return {"configured": False, "line": "Work roster is not configured. Import your profile privately to enable it."}
        return {"configured": False, "line": "Work roster needs clarification: more than one profile or an invalid configuration."}
    code = shift.get("shift")
    label = {"morning": "Morning shift", "evening": "Afternoon shift", "off": "Off"}.get(code, str(code))
    start = end = None
    try:
        rows = profile_config.authorized_records(
            actor.phone, "DIRECT_DM", kind="roster", requested_scope="private", effective_date=day
        )
        active = [r for r in rows if r["payload"].get("active", True)]
        payload = active[0]["payload"] if len(active) == 1 else {}
        if code == "morning":
            start, end = payload.get("day_start"), payload.get("day_end")
        elif code == "evening":
            start, end = payload.get("evening_start"), payload.get("evening_end")
    except Exception:
        pass
    line = label + " week — weekend" if day.weekday() >= 5 and code != "off" else label
    if start and end and day.weekday() < 5:
        line += f" — {start}–{end}"
    try:
        ot = phase2_work.effective_ot_for_date(day, actor.phone, "DIRECT_DM")
    except Exception:
        ot = None
    if ot and ot.get("ot"):
        window = "–".join(x for x in (ot.get("start"), ot.get("end")) if x)
        if window:
            line += f"; {'explicit OT' if ot.get('override') else 'default OT'} {window}"
        elif ot.get("hours"):
            line += f"; default OT {ot.get('hours'):g}h"
    if day.weekday() >= 5 and ot is not None and not ot.get("ot"):
        line += "; no OT scheduled"
    return {"configured": True, "shift": code, "line": line, "ot": ot}


def build(actor: ActorContext, on_date: str | None = None) -> dict:
    actor = _all_scope_actor(actor)
    # Helpers consult the authenticated ContextVar, not just their argument.
    with use_actor(actor):
        return _build(actor, on_date)


def _build(actor: ActorContext, on_date: str | None = None) -> dict:
    day = date.fromisoformat(str(on_date)[:10]) if on_date else runtime_clock.today(actor.timezone)
    day_text = day.isoformat()
    agenda = phase2.get_agenda(actor, day_text, day_text, include_plans=True)
    tasks = phase2.list_tasks(actor, "open", limit=100).get("tasks", [])

    day_end_local = datetime.combine(day, time.max, tzinfo=ZoneInfo(actor.timezone))
    day_end_utc = day_end_local.astimezone(timezone.utc)
    due_tasks = []
    for row in tasks:
        due = _local(row.get("due_at_utc"), actor.timezone)
        due_date = row.get("due_date_local")
        if (due and due <= day_end_local) or (due_date and str(due_date) <= day_text):
            due_tasks.append(row)

    spaces = list(actor.allowed_spaces)
    marks = ",".join("?" for _ in spaces)
    conn = db.connect()
    try:
        reminder_rows = conn.execute(
            f"""SELECT reminder_id,task_text,due_at_utc,status,space_id
                FROM reminders
                WHERE space_id IN ({marks})
                  AND status IN ('OPEN','DUE','DEFERRED')
                  AND due_at_utc<=?
                ORDER BY due_at_utc""",
            spaces + [day_end_utc.isoformat()],
        ).fetchall()
        pending_finance = conn.execute(
            """SELECT p.item_id,p.note,p.created_at_utc
               FROM pending_items p
               WHERE p.owner_id=? AND p.status='PENDING' AND p.kind='FINANCE_REVIEW'
               ORDER BY p.created_at_utc""",
            (actor.user_id,),
        ).fetchall()
        pending_other = conn.execute(
            """SELECT p.item_id,p.kind,p.note,p.created_at_utc
               FROM pending_items p
               WHERE p.owner_id=? AND p.status='PENDING'
                 AND p.kind NOT IN ('FINANCE_REVIEW','REMINDER_DRAFT','PRIVATE_SEARCH_OFFER')
               ORDER BY p.created_at_utc""",
            (actor.user_id,),
        ).fetchall()
        unactioned_plans = conn.execute(
            f"""SELECT p.plan_id,p.title,p.start_at_utc,p.status
                FROM plans p
                LEFT JOIN plan_diary_links l ON l.plan_id=p.plan_id
                WHERE p.space_id IN ({marks}) AND p.status='CONFIRMED'
                  AND l.plan_id IS NULL
                ORDER BY p.updated_at_utc""",
            spaces,
        ).fetchall()
    finally:
        conn.close()

    period = day.strftime("%Y-%m")
    try:
        phase2_finance.ensure_obligation_instances(period, actor.phone, "DIRECT_DM", "all")
        phase2_finance.refresh_obligation_states(day_text, actor.phone, "DIRECT_DM", "all")
        obligations = phase2_finance.list_obligations(
            actor.phone, "DIRECT_DM", "all", period
        )
    except Exception:
        obligations = []
    due_bills = [
        row for row in obligations
        if row.get("state") not in {"PAID", "CANCELLED"}
        and row.get("due_date")
        and str(row.get("due_date")) <= day_text
    ]

    work = _work_section(actor, day)
    leave = agenda.get("leave") or []
    diary = agenda.get("diary") or []
    tentative = agenda.get("plans") or []

    lines = ["🌅 *Good morning — here’s your day*",
             day.strftime("%A, %d %B %Y") + f" ({actor.timezone})", ""]
    lines.append("*Work*")
    lines.append(work["line"])
    if leave:
        labels = ", ".join(str(row.get("leave_type") or "Leave").replace("_", " ").title() for row in leave)
        lines.append(f"Leave recorded today: {labels}.")
    else:
        lines.append("No planned leave recorded today.")

    lines.extend(["", "*Agenda*"])
    if diary:
        for i, row in enumerate(diary, 1):
            when = _fmt_time(row.get("start_at_utc"), actor.timezone)
            suffix = f" — {when}" if when else ""
            lines.append(f"{i}. {row.get('title')}{suffix}")
    else:
        lines.append("No appointments scheduled today.")
    if tentative:
        lines.append("Tentative plans:")
        for row in tentative:
            lines.append(f"• {row.get('title')}")

    lines.extend(["", f"*Tasks due today or overdue ({len(due_tasks)} of {len(tasks)} open)*"])
    if due_tasks:
        for i, row in enumerate(due_tasks, 1):
            label = "overdue" if (
                (row.get("due_date_local") and str(row.get("due_date_local")) < day_text)
                or (_local(row.get("due_at_utc"), actor.timezone) and _local(row.get("due_at_utc"), actor.timezone).date() < day)
            ) else "due today"
            lines.append(f"{i}. {row.get('title')} — {label}")
    else:
        lines.append("No tasks due today.")

    lines.extend(["", "*Reminders*"])
    if reminder_rows:
        for i, row in enumerate(reminder_rows, 1):
            when = _fmt_time(row["due_at_utc"], actor.timezone)
            local = _local(row["due_at_utc"], actor.timezone)
            label = "overdue" if local and local.date() < day else (when or "due today")
            lines.append(f"{i}. {row['task_text']} — {label}")
    else:
        lines.append("No reminders due today.")

    if due_bills:
        lines.extend(["", "*Bills & commitments*"])
        for row in due_bills:
            money = (
                f"{row.get('currency','MYR')} {float(row.get('expected')):.2f}"
                if row.get("expected") is not None else "amount variable"
            )
            lines.append(f"⚠️ {row.get('name')} — {money} — {row.get('state')}")

    waiting = []
    for row in pending_finance:
        try:
            note = json.loads(row["note"] or "{}")
        except Exception:
            note = {}
        waiting.append(f"Finance: {note.get('description') or 'receipt/expense clarification'}")
    for row in pending_other:
        waiting.append(f"{str(row['kind']).replace('_',' ').title()}: {row['note'] or 'still unresolved'}")
    for row in unactioned_plans:
        waiting.append(f"Plan finalised but not actioned: {row['title']}")
    if waiting:
        lines.extend(["", "*Waiting for you*"])
        for i, item in enumerate(waiting, 1):
            lines.append(f"{i}. ⏳ {item}")

    summary = (
        f"{len(due_tasks)} task{'s' if len(due_tasks)!=1 else ''} due/overdue ({len(tasks)} open) · "
        f"{len(reminder_rows)} reminder{'s' if len(reminder_rows)!=1 else ''} · "
        f"{len(due_bills)} bill{'s' if len(due_bills)!=1 else ''} due · "
        f"{len(diary)} appointment{'s' if len(diary)!=1 else ''}"
    )
    lines.extend(["", f"*Today at a glance:* {summary}", "", "React ✅ once you’ve seen this."])
    return {
        "date": day_text,
        "text": "\n".join(lines),
        "actionable": bool(due_tasks or reminder_rows or due_bills or diary or waiting or work.get("configured")),
        "counts": {
            "tasks": len(due_tasks), "open_tasks": len(tasks), "reminders": len(reminder_rows),
            "bills": len(due_bills), "diary": len(diary), "waiting": len(waiting),
        },
    }


def create_or_get(actor: ActorContext, on_date: str | None = None, *, refresh: bool = False) -> dict:
    actor = _all_scope_actor(actor)
    payload = build(actor, on_date)
    ensure_schema()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM daily_briefs WHERE user_id=? AND brief_date=?",
            (actor.user_id, payload["date"]),
        ).fetchone()
        if existing:
            if refresh:
                conn.execute("UPDATE daily_briefs SET payload_json=? WHERE brief_id=?",
                             (json.dumps(payload, ensure_ascii=False, sort_keys=True), existing["brief_id"]))
                conn.commit()
            return {
                "brief_id": existing["brief_id"],
                "status": existing["status"],
                **(payload if refresh else json.loads(existing["payload_json"])),
            }
        brief_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO daily_briefs(
                   brief_id,user_id,brief_date,conversation_id,payload_json
               ) VALUES(?,?,?,?,?)""",
            (
                brief_id, actor.user_id, payload["date"], actor.conversation_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
        return {"brief_id": brief_id, "status": "PENDING", **payload}
    finally:
        conn.close()


def acknowledge_reaction(actor: ActorContext, provider_message_id: str, reaction_text: str | None) -> dict:
    if actor.conversation_type == "GROUP":
        return {"status": "not_daily_brief_ack"}
    reaction = str(reaction_text or "")
    if "✅" not in reaction:
        return {"status": "not_daily_brief_ack"}
    ensure_schema()
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT b.brief_id,b.user_id,b.status
               FROM outbound_messages o
               JOIN daily_briefs b ON b.brief_id=o.context_id
               WHERE o.provider_message_id=?
                 AND o.context_kind='DAILY_BRIEF'
                 AND b.user_id=? AND o.conversation_id=?
               ORDER BY o.delivered_at_utc DESC,o.created_at_utc DESC LIMIT 1""",
            (provider_message_id, actor.user_id, actor.conversation_id),
        ).fetchone()
        if not row:
            return {"status": "not_daily_brief_ack"}
        if row["status"] == "ACKNOWLEDGED":
            return {"status": "already_acknowledged", "brief_id": row["brief_id"]}
        conn.execute(
            """UPDATE daily_briefs
               SET status='ACKNOWLEDGED',acknowledged_at_utc=CURRENT_TIMESTAMP
               WHERE brief_id=?""",
            (row["brief_id"],),
        )
        conn.commit()
        return {"status": "acknowledged", "brief_id": row["brief_id"]}
    finally:
        conn.close()
