"""Project Jarvis Phase 2 schedule intelligence.

This module is intentionally dormant: it owns planned leave and personal
commitments, and can consume already-authorized reminder snapshots when the
Phase-1 reminder connector is wired later. It never sends a reminder itself.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import phase2_work
import compat_tools as tools


SCHEMA = """
CREATE TABLE IF NOT EXISTS alex_phase2_leave_plans (
    plan_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    leave_type TEXT NOT NULL CHECK(leave_type IN ('ANNUAL_LEAVE','OTHER_LEAVE')),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED'
        CHECK(status IN ('PLANNED','CONFIRMED','TAKEN','CANCELLED')),
    reason TEXT,
    location TEXT,
    related_commitment_id TEXT,
    source_message_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_leave_plans
    ON alex_phase2_leave_plans(owner_user_id,start_date,end_date,status);

CREATE TABLE IF NOT EXISTS alex_phase2_commitments (
    commitment_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'OTHER'
        CHECK(category IN (
            'EVENT','TRAVEL','APPOINTMENT','VEHICLE_SERVICE',
            'FAMILY','PERSONAL','OTHER'
        )),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    location TEXT,
    notes TEXT,
    travel_required INTEGER NOT NULL DEFAULT 0 CHECK(travel_required IN (0,1)),
    status TEXT NOT NULL DEFAULT 'PLANNED'
        CHECK(status IN ('PLANNED','CONFIRMED','COMPLETED','CANCELLED')),
    related_saved_item_id TEXT,
    source_message_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_commitments
    ON alex_phase2_commitments(owner_user_id,start_date,end_date,status);
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


def _parse_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _clock(value):
    """Normalize provider/user clock variants to the stored HH:MM form."""
    if value in (None, ""):
        return None
    raw = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError("Time must be a valid clock time, for example 10:00 AM")


def _ctx(conn, sender_phone, conversation_type):
    user_id, private_space = tools.resolve_user_and_space(
        conn, sender_phone, conversation_type)
    shared = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND space_id='FAMILY_SHARED'",
        (user_id,),
    ).fetchone() is not None
    return user_id, private_space, shared


def _space_for(conn, sender_phone, conversation_type, visibility):
    user_id, private_space, shared = _ctx(
        conn, sender_phone, conversation_type)
    visibility = str(visibility or "private").lower()
    if visibility not in ("private", "family"):
        raise ValueError("Visibility must be private or family")
    if conversation_type == "GROUP":
        if visibility != "family":
            raise PermissionError("Private schedule data cannot be created in group")
        if not shared:
            raise PermissionError("Sender is not a family member")
        return user_id, "FAMILY_SHARED"
    if visibility == "family":
        if not shared:
            raise PermissionError("Sender is not a family member")
        return user_id, "FAMILY_SHARED"
    return user_id, private_space


def _allowed_spaces(conn, sender_phone, conversation_type):
    user_id, private_space, shared = _ctx(
        conn, sender_phone, conversation_type)
    if conversation_type == "GROUP":
        return user_id, ["FAMILY_SHARED"]
    spaces = [private_space]
    if shared:
        spaces.append("FAMILY_SHARED")
    return user_id, spaces


def create_leave_plan(start_date, end_date, sender_phone,
                      conversation_type="DIRECT_DM", visibility="private",
                      leave_type="ANNUAL_LEAVE", status="PLANNED",
                      reason=None, location=None, related_commitment_id=None,
                      source_message_id=None):
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        raise ValueError("Leave end date cannot be before start date")
    leave_type = str(leave_type or "ANNUAL_LEAVE").upper()
    if leave_type not in ("ANNUAL_LEAVE", "OTHER_LEAVE"):
        raise ValueError("Unsupported planned leave type")
    status = str(status or "PLANNED").upper()
    if status not in ("PLANNED", "CONFIRMED", "TAKEN", "CANCELLED"):
        raise ValueError("Unsupported planned leave status")

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_leave_plans(
                plan_id,space_id,owner_user_id,leave_type,start_date,end_date,
                status,reason,location,related_commitment_id,source_message_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, leave_type, start.isoformat(),
            end.isoformat(), status, reason, location,
            related_commitment_id, source_message_id,
        ))
        conn.commit()
        return {
            "plan_id": ident, "start_date": start.isoformat(),
            "end_date": end.isoformat(), "status": status,
            "space": space_id,
        }
    finally:
        conn.close()


def update_leave_plan_status(plan_id, status, sender_phone,
                             conversation_type="DIRECT_DM"):
    """Change planned-leave state and materialize annual leave only when TAKEN."""
    status = str(status or "").upper()
    if status not in ("PLANNED", "CONFIRMED", "TAKEN", "CANCELLED"):
        raise ValueError("Unsupported planned leave status")
    ensure_schema()
    phase2_work.ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user_id, spaces = _allowed_spaces(
            conn, sender_phone, conversation_type)
        placeholders = ",".join("?" for _ in spaces)
        row = conn.execute(f"""
            SELECT * FROM alex_phase2_leave_plans
            WHERE plan_id=? AND owner_user_id=? AND space_id IN ({placeholders})
        """, (plan_id, user_id, *spaces)).fetchone()
        if not row:
            raise PermissionError("Leave plan not authorized")
        if row["status"] == "TAKEN" and status != "TAKEN":
            raise ValueError(
                "Taken leave is historical fact; correct the underlying work record "
                "instead of cancelling the plan")
        if row["status"] == status:
            conn.commit()
            return {
                "plan_id": plan_id, "status": status,
                "changed": False, "materialized_days": 0,
            }

        materialized = 0
        if status == "TAKEN" and row["leave_type"] == "ANNUAL_LEAVE":
            current = _parse_date(row["start_date"])
            end_date = _parse_date(row["end_date"])
            while current <= end_date:
                exists = conn.execute("""
                    SELECT 1 FROM alex_phase2_work_events
                    WHERE owner_user_id=? AND space_id=? AND event_date=?
                      AND event_type='ANNUAL_LEAVE'
                    LIMIT 1
                """, (
                    row["owner_user_id"], row["space_id"],
                    current.isoformat(),
                )).fetchone()
                if not exists:
                    conn.execute("""
                        INSERT INTO alex_phase2_work_events(
                            work_event_id,space_id,owner_user_id,event_date,
                            event_type,units_days,note,source_message_id
                        ) VALUES (?,?,?,?, 'ANNUAL_LEAVE',1,?,?)
                    """, (
                        str(uuid.uuid4()), row["space_id"], row["owner_user_id"],
                        current.isoformat(),
                        (
                            "Taken from planned leave"
                            + (f": {row['reason']}" if row["reason"] else "")
                        ),
                        row["source_message_id"],
                    ))
                    materialized += 1
                current += timedelta(days=1)

        conn.execute("""
            UPDATE alex_phase2_leave_plans
            SET status=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE plan_id=?
        """, (status, plan_id))
        conn.commit()
        return {
            "plan_id": plan_id, "status": status,
            "changed": True, "materialized_days": materialized,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_commitment(title, start_date, sender_phone,
                      conversation_type="DIRECT_DM", visibility="private",
                      end_date=None, start_time=None, end_time=None,
                      category="OTHER", location=None, notes=None,
                      travel_required=False, status="PLANNED",
                      related_saved_item_id=None, source_message_id=None):
    if not str(title or "").strip():
        raise ValueError("Commitment title is required")
    start = _parse_date(start_date)
    end = _parse_date(end_date or start_date)
    if end < start:
        raise ValueError("Commitment end date cannot be before start date")
    category = str(category or "OTHER").upper()
    allowed_categories = {
        "EVENT", "TRAVEL", "APPOINTMENT", "VEHICLE_SERVICE",
        "FAMILY", "PERSONAL", "OTHER",
    }
    if category not in allowed_categories:
        raise ValueError("Unsupported commitment category")
    status = str(status or "PLANNED").upper()
    if status not in ("PLANNED", "CONFIRMED", "COMPLETED", "CANCELLED"):
        raise ValueError("Unsupported commitment status")

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_commitments(
                commitment_id,space_id,owner_user_id,title,category,
                start_date,end_date,start_time,end_time,location,notes,
                travel_required,status,related_saved_item_id,source_message_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, str(title).strip(), category,
            start.isoformat(), end.isoformat(), _clock(start_time),
            _clock(end_time), location, notes,
            1 if travel_required else 0, status,
            related_saved_item_id, source_message_id,
        ))
        conn.commit()
        return {
            "commitment_id": ident, "title": str(title).strip(),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "space": space_id, "status": status,
        }
    finally:
        conn.close()


def update_commitment_status(commitment_id, status, sender_phone,
                             conversation_type="DIRECT_DM"):
    status = str(status or "").upper()
    if status not in ("PLANNED", "CONFIRMED", "COMPLETED", "CANCELLED"):
        raise ValueError("Unsupported commitment status")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, spaces = _allowed_spaces(
            conn, sender_phone, conversation_type)
        placeholders = ",".join("?" for _ in spaces)
        row = conn.execute(f"""
            SELECT * FROM alex_phase2_commitments
            WHERE commitment_id=? AND owner_user_id=? AND space_id IN ({placeholders})
        """, (commitment_id, user_id, *spaces)).fetchone()
        if not row:
            raise PermissionError("Commitment not authorized")
        conn.execute("""
            UPDATE alex_phase2_commitments
            SET status=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE commitment_id=?
        """, (status, commitment_id))
        conn.commit()
        return {"commitment_id": commitment_id, "status": status}
    finally:
        conn.close()


def _leave_for_date(conn, d, user_id, spaces):
    placeholders = ",".join("?" for _ in spaces)
    return [
        dict(row) for row in conn.execute(f"""
            SELECT * FROM alex_phase2_leave_plans
            WHERE owner_user_id=? AND space_id IN ({placeholders})
              AND start_date<=? AND end_date>=?
              AND status IN ('PLANNED','CONFIRMED','TAKEN')
            ORDER BY
              CASE status WHEN 'TAKEN' THEN 0 WHEN 'CONFIRMED' THEN 1 ELSE 2 END,
              created_at_utc
        """, (user_id, *spaces, d.isoformat(), d.isoformat())).fetchall()
    ]


def _commitments_for_window(conn, start, end, user_id, spaces):
    placeholders = ",".join("?" for _ in spaces)
    return [
        dict(row) for row in conn.execute(f"""
            SELECT * FROM alex_phase2_commitments
            WHERE owner_user_id=? AND space_id IN ({placeholders})
              AND start_date<=? AND end_date>=?
              AND status IN ('PLANNED','CONFIRMED')
            ORDER BY start_date,COALESCE(start_time,''),created_at_utc
        """, (user_id, *spaces, end.isoformat(), start.isoformat())).fetchall()
    ]


def commitments_for_date(on_date, sender_phone,
                         conversation_type="DIRECT_DM"):
    d = _parse_date(on_date)
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, spaces = _allowed_spaces(
            conn, sender_phone, conversation_type)
        return _commitments_for_window(conn, d, d, user_id, spaces)
    finally:
        conn.close()


def leave_plans_for_date(on_date, sender_phone,
                         conversation_type="DIRECT_DM"):
    d = _parse_date(on_date)
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, spaces = _allowed_spaces(
            conn, sender_phone, conversation_type)
        return _leave_for_date(conn, d, user_id, spaces)
    finally:
        conn.close()


def _human_date(d):
    d = _parse_date(d)
    return f"{d.day} {d.strftime('%B')}"


def _normalize_reminders(reminder_items, target_date, allowed_spaces):
    result = []
    for item in reminder_items or []:
        due_date = item.get("due_date")
        if not due_date and item.get("due_at"):
            due_date = str(item["due_at"])[:10]
        if not due_date or str(due_date)[:10] != target_date.isoformat():
            continue
        space = item.get("space_id")
        if space and space not in allowed_spaces:
            continue
        text = str(item.get("text") or item.get("reminder_text") or "").strip()
        if not text:
            continue
        result.append({
            "text": text,
            "due_time": item.get("due_time"),
            "space_id": space,
            "source": "REMINDER",
        })
    return result


def _future_shift_sentence(d, effective_shift, leaves):
    shift = effective_shift["shift"]
    date_label = _human_date(d)
    sender_phone = effective_shift["_sender_phone"]
    conversation_type = effective_shift["_conversation_type"]

    normal_text = None
    if d.weekday() >= 5:
        ot = phase2_work.default_ot_for_date(
            d, sender_phone, conversation_type)
        if ot.get("kind") == "NATURAL_OFF_DAY":
            normal_text = (
                f"{date_label} falls in your {shift}-shift week, "
                "and Sunday is normally off"
            )
        elif ot.get("ot"):
            window = ""
            if ot.get("start") and ot.get("end"):
                window = f" ({ot['start']}–{ot['end']})"
            normal_text = (
                f"{date_label} falls in your {shift}-shift week and your "
                f"default weekend OT pattern applies{window}"
            )
        else:
            normal_text = (
                f"{date_label} falls in your {shift}-shift week"
            )
    else:
        normal_text = (
            f"You would normally be on the {shift} shift on {date_label}"
        )

    if leaves:
        leave = leaves[0]
        label = "annual leave" if leave["leave_type"] == "ANNUAL_LEAVE" else "leave"
        state = leave["status"]
        if state == "CONFIRMED":
            state_phrase = f"confirmed {label}"
        elif state == "TAKEN":
            state_phrase = f"{label} recorded as taken"
        else:
            state_phrase = f"planned {label}"

        leave_start = _parse_date(leave["start_date"])
        leave_end = _parse_date(leave["end_date"])
        days = (leave_end - leave_start).days + 1
        if days > 1:
            range_text = (
                f" for {days} days, {_human_date(leave_start)}–"
                f"{_human_date(leave_end)}"
            )
        else:
            range_text = ""

        reason = f", for {leave['reason']}" if leave.get("reason") else ""
        location = f" in {leave['location']}" if leave.get("location") else ""
        return (
            normal_text + f", but you have {state_phrase}{range_text}"
            f"{reason}{location}."
        )

    if d.weekday() >= 5:
        if "normally off" in normal_text:
            return normal_text + "."
        if "default weekend OT pattern" in normal_text:
            return normal_text + ", unless the day's workscope changes."
        return normal_text + "."
    return f"You're scheduled for the {shift} shift on {date_label}."


def _schedule_observations(target, leaves, on_day, nearby):
    observations = []
    travel_events = [
        item for item in on_day
        if item["travel_required"] or item["category"] == "TRAVEL"
    ]
    if not travel_events and leaves:
        # A leave plan with an explicit location/reason can define the trip
        # window when the user supplied that context.
        for leave in leaves:
            if leave.get("location"):
                travel_events.append({
                    "title": leave.get("reason") or "your leave",
                    "location": leave.get("location"),
                    "start_date": leave["start_date"],
                    "end_date": leave["end_date"],
                    "travel_required": 1,
                    "category": "TRAVEL",
                })

    services = [
        item for item in nearby if item["category"] == "VEHICLE_SERVICE"
    ]
    for travel in travel_events:
        travel_start = _parse_date(travel["start_date"])
        travel_end = _parse_date(travel["end_date"])
        relevant = [
            svc for svc in services
            if travel_start <= _parse_date(svc["start_date"]) <= travel_end + timedelta(days=1)
        ]
        if not relevant:
            continue
        svc = relevant[0]
        location = f" to {travel['location']}" if travel.get("location") else ""
        observations.append({
            "kind": "VEHICLE_SERVICE_BEFORE_TRAVEL",
            "text": (
                f"You also have {svc['title']} on {_human_date(svc['start_date'])}. "
                f"Since you have travel{location} around this time, "
                "you may want to service the car before you leave."
            ),
            "action_taken": False,
        })
    return observations


def build_day_brief(on_date, sender_phone,
                    conversation_type="DIRECT_DM",
                    reminder_items=None, reference_date=None,
                    include_observations=True):
    """Build one natural schedule brief from authorized Phase-2 facts.

    reminder_items is the future plug boundary for Phase-1 reminders: the live
    connector will pass already-authorized reminder snapshots here.
    """
    d = _parse_date(on_date)
    ref = _parse_date(reference_date) if reference_date else date.today()
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, spaces = _allowed_spaces(
            conn, sender_phone, conversation_type)
        leaves = _leave_for_date(conn, d, user_id, spaces)
        on_day = _commitments_for_window(conn, d, d, user_id, spaces)
        nearby = _commitments_for_window(
            conn, d - timedelta(days=2), d + timedelta(days=2),
            user_id, spaces)
    finally:
        conn.close()

    effective = phase2_work.effective_shift(
        d, sender_phone, conversation_type)
    effective["_sender_phone"] = sender_phone
    effective["_conversation_type"] = conversation_type

    if d < ref:
        work = phase2_work.historical_work_day(
            d, sender_phone, conversation_type)
        lead = work["brief"]
    else:
        lead = _future_shift_sentence(d, effective, leaves)

    reminders = _normalize_reminders(reminder_items, d, spaces)
    details = []
    for item in on_day:
        location = f" in {item['location']}" if item.get("location") else ""
        time_text = f" at {item['start_time']}" if item.get("start_time") else ""
        details.append(f"{item['title']}{location}{time_text}")
    for reminder in reminders:
        if reminder["text"] not in details:
            details.append(reminder["text"])

    text = lead
    if details:
        if len(details) == 1:
            text += f" You also have {details[0]}."
        else:
            text += " You also have " + "; ".join(details) + "."

    observations = (
        _schedule_observations(d, leaves, on_day, nearby)
        if include_observations else []
    )
    for observation in observations:
        text += " " + observation["text"]

    return {
        "date": d.isoformat(),
        "mode": "historical" if d < ref else "future",
        "scheduled_shift": effective["shift"],
        "leave_plans": leaves,
        "commitments": on_day,
        "reminders": reminders,
        "observations": observations,
        "brief": text,
        "action_taken": False,
    }
