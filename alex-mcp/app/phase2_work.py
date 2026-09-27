"""Project Jarvis Phase 2 roster, overtime and leave engine.

Pure/default calculations live here. No Phase-1 orchestrator or reminder service
imports this module yet.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import profile_config
import compat_tools as tools


SCHEMA = """
CREATE TABLE IF NOT EXISTS alex_phase2_work_events (
    work_event_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'ANNUAL_LEAVE','MEDICAL_LEAVE','SHIFT_SWAP',
        'OT_OFFERED','OT_PENDING','OT_PLANNED','OT_WORKED','OT_UNAVAILABLE'
    )),
    shift_code TEXT,
    start_time TEXT,
    end_time TEXT,
    hours REAL,
    units_days REAL,
    work_scope TEXT,
    manager_override INTEGER NOT NULL DEFAULT 0 CHECK(manager_override IN (0,1)),
    note TEXT,
    source_message_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_work_events
    ON alex_phase2_work_events(owner_user_id,event_date,event_type);
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


def resolve_work_query_date(day, month, *, reference_date=None, year=None):
    """Resolve a conversational day/month using the current/reference year.

    The language layer may pass an explicit year when the user supplied one.
    Otherwise the reference year's value is used exactly as agreed.
    """
    ref = _parse_date(reference_date) if reference_date else date.today()
    target_year = int(year) if year is not None else ref.year
    if isinstance(month, int):
        target_month = month
    else:
        text = str(month or "").strip()
        if not text:
            raise ValueError("Month is required")
        target_month = None
        for fmt in ("%B", "%b"):
            try:
                target_month = datetime.strptime(text, fmt).month
                break
            except ValueError:
                continue
        if target_month is None:
            raise ValueError("Month must be a valid month name or number")
    return date(target_year, int(target_month), int(day))


def _ctx(conn, sender_phone, conversation_type):
    user_id, private_space = tools.resolve_user_and_space(
        conn, sender_phone, conversation_type)
    shared = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND space_id='FAMILY_SHARED'",
        (user_id,),
    ).fetchone() is not None
    return user_id, private_space, shared


def _event_space(conn, sender_phone, conversation_type, visibility):
    user_id, private_space, shared = _ctx(conn, sender_phone, conversation_type)
    visibility = str(visibility or "private").lower()
    if conversation_type == "GROUP":
        if visibility != "family":
            raise PermissionError("Private work data cannot be written in the family group")
        return user_id, "FAMILY_SHARED"
    if visibility == "family":
        if not shared:
            raise PermissionError("Sender lacks family-space membership")
        return user_id, "FAMILY_SHARED"
    if visibility != "private":
        raise ValueError("Visibility must be private or family")
    return user_id, private_space


def shift_for_profile(payload, on_date):
    """Resolve a repeating weekly shift pattern from an anchored cycle.

    pattern is comma separated, e.g. "morning,evening". Each entry lasts one
    full Monday-Sunday style seven-day block beginning at cycle_start.
    """
    d = _parse_date(on_date)
    anchor = _parse_date(payload.get("cycle_start"))
    pattern = [
        x.strip().lower()
        for x in str(payload.get("pattern") or "").split(",")
        if x.strip()
    ]
    if not pattern:
        raise ValueError("Roster pattern is empty")
    delta_days = (d - anchor).days
    week_index = delta_days // 7
    code = pattern[week_index % len(pattern)]
    if code not in ("morning", "evening", "off"):
        raise ValueError("Roster pattern may contain morning, evening or off")
    return {
        "date": d.isoformat(),
        "shift": code,
        "week_index": week_index,
        "cycle_position": week_index % len(pattern),
    }


def _active_profile(sender_phone, conversation_type, kind):
    records = profile_config.authorized_records(
        sender_phone, conversation_type, kind=kind, requested_scope="private")
    active = [r for r in records if r["payload"].get("active", True)]
    if not active:
        raise ValueError(f"No active {kind} profile configured")
    if len(active) > 1:
        raise ValueError(f"More than one active {kind} profile; choose one explicitly")
    return active[0]


def effective_shift(on_date, sender_phone, conversation_type="DIRECT_DM"):
    """Return roster shift, then apply an explicit SHIFT_SWAP event if present."""
    roster = _active_profile(sender_phone, conversation_type, "roster")
    base = shift_for_profile(roster["payload"], on_date)
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, _ = _ctx(conn, sender_phone, conversation_type)
        row = conn.execute("""
            SELECT shift_code FROM alex_phase2_work_events
            WHERE owner_user_id=? AND space_id=? AND event_date=?
              AND event_type='SHIFT_SWAP'
            ORDER BY created_at_utc DESC LIMIT 1
        """, (user_id, private_space, base["date"])).fetchone()
        if row and row["shift_code"]:
            base["default_shift"] = base["shift"]
            base["shift"] = row["shift_code"]
            base["override"] = True
        else:
            base["override"] = False
        return base
    finally:
        conn.close()


def default_ot_for_date(on_date, sender_phone,
                        conversation_type="DIRECT_DM",
                        work_scope="standard", manager_override=False):
    """Return default OT expectation without inventing pay.

    work_scope can be standard or low. Evening Sundays remain off unless the
    caller explicitly states a manager override.
    """
    d = _parse_date(on_date)
    shift = effective_shift(d, sender_phone, conversation_type)["shift"]
    rule = _active_profile(sender_phone, conversation_type, "overtime_rule")["payload"]
    weekday = d.strftime("%A").lower()
    work_scope = str(work_scope or "standard").lower()
    if work_scope not in ("standard", "low"):
        raise ValueError("Work scope must be standard or low")

    if shift == "off":
        return {"date": d.isoformat(), "shift": "off", "ot": False}

    if shift == "morning":
        if weekday in ("saturday", "sunday"):
            prefix = "morning_weekend_low" if work_scope == "low" else "morning_weekend_standard"
            return {
                "date": d.isoformat(), "shift": shift, "ot": True,
                "kind": "WEEKEND",
                "start": rule.get(prefix + "_start"),
                "end": rule.get(prefix + "_end"),
                "hours": None,
                "pay_amount": None, "pay_status": "RATE_NOT_CONFIGURED",
            }
        return {
            "date": d.isoformat(), "shift": shift,
            "ot": bool(rule.get("morning_pre_hours")),
            "kind": "PRE_SHIFT",
            "start": rule.get("morning_pre_start"),
            "end": None,
            "hours": rule.get("morning_pre_hours"),
            "pay_amount": None, "pay_status": "RATE_NOT_CONFIGURED",
        }

    # evening
    if weekday == "sunday":
        if not manager_override:
            return {
                "date": d.isoformat(), "shift": shift, "ot": False,
                "kind": "NATURAL_OFF_DAY",
            }
        # Rare manager-requested Sunday morning uses the configured morning
        # weekend window. The override must be explicit; Alex never assumes it.
        prefix = "morning_weekend_low" if work_scope == "low" else "morning_weekend_standard"
        return {
            "date": d.isoformat(), "shift": shift, "ot": True,
            "kind": "MANAGER_SUNDAY_OVERRIDE",
            "start": rule.get(prefix + "_start"),
            "end": rule.get(prefix + "_end"),
            "hours": None,
            "pay_amount": None, "pay_status": "RATE_NOT_CONFIGURED",
        }

    weekend_days = set(rule.get("evening_weekend_days") or [])
    if weekday in weekend_days:
        prefix = "evening_weekend_low" if work_scope == "low" else "evening_weekend_standard"
        return {
            "date": d.isoformat(), "shift": shift, "ot": True,
            "kind": "WEEKEND",
            "start": rule.get(prefix + "_start"),
            "end": rule.get(prefix + "_end"),
            "hours": None,
            "pay_amount": None, "pay_status": "RATE_NOT_CONFIGURED",
        }

    return {
        "date": d.isoformat(), "shift": shift,
        "ot": bool(rule.get("evening_post_hours")),
        "kind": "POST_SHIFT",
        "start": rule.get("evening_post_start"),
        "end": rule.get("evening_post_end"),
        # Use configured credited OT hours, not wall-clock subtraction, because
        # the user's evening rule explicitly excludes break time.
        "hours": rule.get("evening_post_hours"),
        "pay_amount": None, "pay_status": "RATE_NOT_CONFIGURED",
    }


def ot_eligibility_for_date(on_date, sender_phone,
                            conversation_type="DIRECT_DM"):
    """Apply the previously agreed deterministic absence/OT eligibility rules."""
    d = _parse_date(on_date)
    shift = effective_shift(d, sender_phone, conversation_type)["shift"]
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        allowed_spaces = [private_space]
        if shared:
            allowed_spaces.append("FAMILY_SHARED")
        placeholders = ",".join("?" for _ in allowed_spaces)

        same_day_absence = conn.execute(f"""
            SELECT event_type FROM alex_phase2_work_events
            WHERE owner_user_id=? AND event_date=?
              AND event_type IN ('ANNUAL_LEAVE','MEDICAL_LEAVE')
              AND space_id IN ({placeholders})
            ORDER BY created_at_utc DESC,rowid DESC LIMIT 1
        """, (user_id, d.isoformat(), *allowed_spaces)).fetchone()
        if same_day_absence:
            return {
                "eligible": False,
                "reason": "SAME_DAY_ABSENCE",
                "absence_type": same_day_absence["event_type"],
            }

        if d.weekday() in (5, 6):  # Saturday / Sunday
            friday = d - timedelta(days=(d.weekday() - 4))
            friday_absence = conn.execute(f"""
                SELECT event_type FROM alex_phase2_work_events
                WHERE owner_user_id=? AND event_date=?
                  AND event_type IN ('ANNUAL_LEAVE','MEDICAL_LEAVE')
                  AND space_id IN ({placeholders})
                ORDER BY created_at_utc DESC,rowid DESC LIMIT 1
            """, (user_id, friday.isoformat(), *allowed_spaces)).fetchone()
            if friday_absence:
                friday_shift = effective_shift(
                    friday, sender_phone, conversation_type)["shift"]
                if friday_shift == "morning":
                    return {
                        "eligible": False,
                        "reason": "FRIDAY_ABSENCE_BLOCKS_WEEKEND_OT",
                        "friday_shift": "morning",
                        "absence_type": friday_absence["event_type"],
                    }
                if friday_shift == "evening" and d.weekday() == 5:
                    return {
                        "eligible": False,
                        "reason": "FRIDAY_ABSENCE_BLOCKS_SATURDAY_OT",
                        "friday_shift": "evening",
                        "absence_type": friday_absence["event_type"],
                    }

        return {"eligible": True, "reason": "ELIGIBLE", "shift": shift}
    finally:
        conn.close()


def effective_ot_for_date(on_date, sender_phone,
                          conversation_type="DIRECT_DM",
                          work_scope="standard", manager_override=False):
    """Return the configured default unless an explicit dated OT event overrides it.

    OT_UNAVAILABLE disables that day's default. OT_PLANNED/OT_WORKED may
    override start/end/hours/work scope. The most recently recorded explicit
    event wins for that day.
    """
    d = _parse_date(on_date)
    base = default_ot_for_date(
        d, sender_phone, conversation_type,
        work_scope=work_scope, manager_override=manager_override)
    eligibility = ot_eligibility_for_date(
        d, sender_phone, conversation_type)
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        allowed_spaces = [private_space]
        if shared:
            allowed_spaces.append("FAMILY_SHARED")
        placeholders = ",".join("?" for _ in allowed_spaces)
        row = conn.execute(f"""
            SELECT * FROM alex_phase2_work_events
            WHERE owner_user_id=? AND event_date=?
              AND event_type IN ('OT_UNAVAILABLE','OT_PLANNED','OT_WORKED')
              AND space_id IN ({placeholders})
            ORDER BY created_at_utc DESC,rowid DESC LIMIT 1
        """, (user_id, d.isoformat(), *allowed_spaces)).fetchone()
        if not row:
            if not eligibility["eligible"]:
                return {
                    "date": d.isoformat(),
                    "shift": base.get("shift"),
                    "ot": False,
                    "kind": "ELIGIBILITY_BLOCK",
                    "start": None,
                    "end": None,
                    "hours": 0.0,
                    "pay_amount": None,
                    "pay_status": "RATE_NOT_CONFIGURED",
                    "override": False,
                    "eligibility": eligibility,
                }
            return {**base, "override": False, "eligibility": eligibility}

        if row["event_type"] == "OT_UNAVAILABLE":
            return {
                "date": d.isoformat(),
                "shift": base.get("shift"),
                "ot": False,
                "kind": "EXPLICIT_UNAVAILABLE",
                "start": None,
                "end": None,
                "hours": 0.0,
                "pay_amount": None,
                "pay_status": "RATE_NOT_CONFIGURED",
                "override": True,
                "source_event_type": row["event_type"],
                "note": row["note"],
                "eligibility": eligibility,
            }

        # Historical OT_WORKED is an observed fact and is preserved even if
        # policy would normally have made the user ineligible. OT_PLANNED,
        # however, must still respect the deterministic eligibility rules.
        if row["event_type"] == "OT_PLANNED" and not eligibility["eligible"]:
            return {
                "date": d.isoformat(),
                "shift": base.get("shift"),
                "ot": False,
                "kind": "ELIGIBILITY_BLOCK",
                "start": None,
                "end": None,
                "hours": 0.0,
                "pay_amount": None,
                "pay_status": "RATE_NOT_CONFIGURED",
                "override": True,
                "source_event_type": row["event_type"],
                "note": row["note"],
                "eligibility": eligibility,
            }

        override_scope = row["work_scope"] or work_scope
        fallback = default_ot_for_date(
            d, sender_phone, conversation_type,
            work_scope=override_scope,
            manager_override=bool(row["manager_override"]) or manager_override)
        return {
            **fallback,
            "ot": True,
            "start": row["start_time"] or fallback.get("start"),
            "end": row["end_time"] or fallback.get("end"),
            "hours": (
                float(row["hours"])
                if row["hours"] is not None else fallback.get("hours")
            ),
            "kind": row["event_type"],
            "override": True,
            "source_event_type": row["event_type"],
            "note": row["note"],
            "eligibility": eligibility,
        }
    finally:
        conn.close()


def ot_status_for_date(on_date, sender_phone,
                       conversation_type="DIRECT_DM"):
    """Return the strongest explicit OT state recorded for a date.

    OFFERED/PENDING are possibilities, not worked hours and never income.
    """
    d = _parse_date(on_date)
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        spaces = [private_space]
        if shared:
            spaces.append("FAMILY_SHARED")
        placeholders = ",".join("?" for _ in spaces)
        rows = conn.execute(f"""
            SELECT * FROM alex_phase2_work_events
            WHERE owner_user_id=? AND event_date=?
              AND event_type IN (
                  'OT_OFFERED','OT_PENDING','OT_PLANNED',
                  'OT_WORKED','OT_UNAVAILABLE'
              )
              AND space_id IN ({placeholders})
            ORDER BY created_at_utc,rowid
        """, (user_id, d.isoformat(), *spaces)).fetchall()
        if not rows:
            default = default_ot_for_date(
                d, sender_phone, conversation_type)
            return {
                "date": d.isoformat(),
                "state": "DEFAULT" if default.get("ot") else "NONE",
                "explicit": False,
                "hours": default.get("hours"),
                "start": default.get("start"),
                "end": default.get("end"),
                "worked": False,
                "income_confirmed": False,
            }

        priority = {
            "OT_OFFERED": 1,
            "OT_PENDING": 2,
            "OT_PLANNED": 3,
            "OT_UNAVAILABLE": 4,
            "OT_WORKED": 5,
        }
        row = max(rows, key=lambda item: (
            priority[item["event_type"]], item["created_at_utc"] or ""))
        return {
            "date": d.isoformat(),
            "state": row["event_type"].replace("OT_", ""),
            "explicit": True,
            "hours": float(row["hours"]) if row["hours"] is not None else None,
            "start": row["start_time"],
            "end": row["end_time"],
            "worked": row["event_type"] == "OT_WORKED",
            "income_confirmed": False,
            "note": row["note"],
        }
    finally:
        conn.close()


def next_ot_payout_dates(after_date, sender_phone,
                         conversation_type="DIRECT_DM", count=4):
    """Return configured OT payout calendar dates without assigning pay amounts."""
    d = _parse_date(after_date)
    rule = _active_profile(
        sender_phone, conversation_type, "overtime_rule")["payload"]
    days = sorted(set(int(x) for x in (rule.get("payout_days") or [])))
    if not days:
        return []
    out = []
    year, month = d.year, d.month
    while len(out) < int(count):
        for day in days:
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate > d:
                out.append(candidate.isoformat())
                if len(out) >= int(count):
                    return out
        month += 1
        if month == 13:
            month = 1
            year += 1
    return out


def record_work_event(event_type, event_date, sender_phone,
                      conversation_type="DIRECT_DM", visibility="private",
                      shift_code=None, start_time=None, end_time=None,
                      hours=None, units_days=None, work_scope=None,
                      manager_override=False, note=None, source_message_id=None):
    allowed = {
        "ANNUAL_LEAVE", "MEDICAL_LEAVE", "SHIFT_SWAP",
        "OT_OFFERED", "OT_PENDING", "OT_PLANNED", "OT_WORKED", "OT_UNAVAILABLE",
    }
    event_type = str(event_type or "").upper()
    if event_type not in allowed:
        raise ValueError("Unsupported work event type")
    if event_type == "SHIFT_SWAP" and shift_code not in ("morning", "evening", "off"):
        raise ValueError("Shift swap needs morning, evening or off")
    d = _parse_date(event_date)

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _event_space(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_work_events(
                work_event_id,space_id,owner_user_id,event_date,event_type,
                shift_code,start_time,end_time,hours,units_days,work_scope,
                manager_override,note,source_message_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, d.isoformat(), event_type, shift_code,
            start_time, end_time, float(hours) if hours is not None else None,
            float(units_days) if units_days is not None else None,
            work_scope, 1 if manager_override else 0, note, source_message_id,
        ))
        conn.commit()
        return {
            "work_event_id": ident, "event_type": event_type,
            "date": d.isoformat(), "space": space_id,
        }
    finally:
        conn.close()


def leave_balance(leave_id, sender_phone, conversation_type="DIRECT_DM",
                  as_of_date=None):
    """Calculate remaining leave from configured snapshot plus later events."""
    records = profile_config.authorized_records(
        sender_phone, conversation_type, kind="leave_balance",
        requested_scope="private")
    wanted = [
        r for r in records
        if r["payload"].get("id") == leave_id and r["payload"].get("active", True)
    ]
    if len(wanted) != 1:
        raise ValueError("Leave balance profile not found or ambiguous")
    rec = wanted[0]
    payload = rec["payload"]
    snapshot_date = _parse_date(payload["as_of_date"])
    end = _parse_date(as_of_date) if as_of_date else date.today()
    event_type = (
        "ANNUAL_LEAVE" if leave_id in ("annual", "annual_leave", "al")
        else "MEDICAL_LEAVE" if leave_id in ("medical", "medical_leave", "mc")
        else None
    )
    if not event_type:
        raise ValueError("Leave id must identify annual leave or medical leave")

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, _ = _ctx(conn, sender_phone, conversation_type)
        row = conn.execute("""
            SELECT COALESCE(SUM(COALESCE(units_days,1)),0) AS used
            FROM alex_phase2_work_events
            WHERE owner_user_id=? AND space_id=? AND event_type=?
              AND event_date>? AND event_date<=?
        """, (
            user_id, private_space, event_type,
            snapshot_date.isoformat(), end.isoformat(),
        )).fetchone()
        used = float(row["used"] or 0)
        remaining = max(0.0, float(payload["remaining_days"]) - used)
        return {
            "id": leave_id, "name": payload["name"],
            "entitlement_days": float(payload["entitlement_days"]),
            "snapshot_remaining_days": float(payload["remaining_days"]),
            "snapshot_date": snapshot_date.isoformat(),
            "used_since_snapshot": used,
            "remaining_days": remaining,
            "as_of_date": end.isoformat(),
        }
    finally:
        conn.close()


def _human_date(d):
    d = _parse_date(d)
    return f"{d.day} {d.strftime('%B')}"


def historical_work_day(on_date, sender_phone,
                        conversation_type="DIRECT_DM",
                        answer_focus="summary"):
    """Explain what actually happened on a past work date.

    Precedence:
    1. recorded leave/absence
    2. recorded shift override / observed OT
    3. configured roster

    A scheduled workday with no recorded absence is treated as worked, matching
    the household rule agreed for historical queries. The structured facts are
    returned together with the natural-language brief so later conversational
    wiring cannot change the underlying truth.
    """
    d = _parse_date(on_date)
    if answer_focus not in ("summary", "shift", "attendance", "leave"):
        raise ValueError("answer_focus must be summary, shift, attendance or leave")

    roster = _active_profile(sender_phone, conversation_type, "roster")
    scheduled = shift_for_profile(roster["payload"], d)
    effective = effective_shift(d, sender_phone, conversation_type)

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        allowed_spaces = [private_space]
        if shared:
            allowed_spaces.append("FAMILY_SHARED")
        placeholders = ",".join("?" for _ in allowed_spaces)
        rows = conn.execute(f"""
            SELECT * FROM alex_phase2_work_events
            WHERE owner_user_id=? AND event_date=?
              AND space_id IN ({placeholders})
            ORDER BY created_at_utc,rowid
        """, (user_id, d.isoformat(), *allowed_spaces)).fetchall()
        events = [dict(row) for row in rows]
    finally:
        conn.close()

    leave = next(
        (e for e in reversed(events)
         if e["event_type"] in ("ANNUAL_LEAVE", "MEDICAL_LEAVE")),
        None,
    )
    observed_ot = next(
        (e for e in reversed(events) if e["event_type"] == "OT_WORKED"),
        None,
    )
    shift_swap = next(
        (e for e in reversed(events) if e["event_type"] == "SHIFT_SWAP"),
        None,
    )

    scheduled_shift = scheduled["shift"]
    actual_shift = effective["shift"]
    worked = None
    attendance_source = None
    leave_label = None

    if leave:
        worked = False
        attendance_source = "RECORDED_LEAVE"
        leave_label = (
            "MC" if leave["event_type"] == "MEDICAL_LEAVE"
            else "annual leave"
        )
    elif observed_ot:
        worked = True
        attendance_source = "RECORDED_OT_WORKED"
    elif actual_shift == "off":
        worked = False
        attendance_source = "ROSTER_OFF"
    elif d.weekday() >= 5:
        # The weekly pattern identifies the shift week, not proof that a
        # weekend was actually worked. Historical weekend attendance needs an
        # observed OT/work event rather than an assumption.
        worked = False
        attendance_source = "WEEKEND_NO_WORK_RECORD"
    else:
        worked = True
        attendance_source = "ROSTER_NO_ABSENCE_RECORDED"

    date_label = _human_date(d)
    shift_name = actual_shift
    scheduled_name = scheduled_shift

    if leave:
        brief = (
            f"You weren't working on {date_label} — it was recorded as "
            f"{leave_label}. "
        )
        if scheduled_name == "off":
            brief += "It was also an off day on your roster."
        else:
            brief += (
                f"You were scheduled for the {scheduled_name} shift that day."
            )
    elif observed_ot and scheduled_name == "off":
        detail = ""
        if observed_ot.get("hours") is not None:
            detail = f" for {float(observed_ot['hours']):g} hours"
        elif observed_ot.get("start_time") or observed_ot.get("end_time"):
            start = observed_ot.get("start_time") or "?"
            end = observed_ot.get("end_time") or "?"
            detail = f" from {start} to {end}"
        brief = (
            f"You were scheduled off on {date_label}, but OT was recorded"
            f"{detail}."
        )
    elif actual_shift == "off":
        brief = f"You weren't scheduled to work on {date_label}; it was an off day."
    elif attendance_source == "WEEKEND_NO_WORK_RECORD":
        brief = (
            f"I don't have a recorded work or OT event for {date_label}. "
            f"That date fell in your {scheduled_name}-shift week."
        )
    elif shift_swap and scheduled_name != actual_shift:
        brief = (
            f"You worked the {shift_name} shift on {date_label}. "
            f"Your normal roster was {scheduled_name}, but a shift change "
            "was recorded."
        )
    else:
        brief = f"You worked the {shift_name} shift on {date_label}."

    if observed_ot and actual_shift != "off" and not leave:
        if observed_ot.get("hours") is not None:
            brief += f" You also had {float(observed_ot['hours']):g} hours of OT recorded."
        elif observed_ot.get("start_time") or observed_ot.get("end_time"):
            start = observed_ot.get("start_time") or "?"
            end = observed_ot.get("end_time") or "?"
            brief += f" OT was also recorded from {start} to {end}."

    facts = {
        "date": d.isoformat(),
        "scheduled_shift": scheduled_shift,
        "actual_shift": actual_shift,
        "worked": worked,
        "attendance_source": attendance_source,
        "leave_type": leave["event_type"] if leave else None,
        "leave_label": leave_label,
        "shift_override": bool(shift_swap),
        "ot_worked_recorded": bool(observed_ot),
        "events": [e["event_type"] for e in events],
        "brief": brief,
    }

    if answer_focus == "shift":
        facts["primary_fact"] = actual_shift if worked else scheduled_shift
    elif answer_focus == "attendance":
        facts["primary_fact"] = worked
    elif answer_focus == "leave":
        facts["primary_fact"] = leave_label
    else:
        facts["primary_fact"] = brief
    return facts


def work_summary(start_date, end_date, sender_phone,
                 conversation_type="DIRECT_DM"):
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        raise ValueError("End date cannot be before start date")
    days = []
    d = start
    while d <= end:
        shift = effective_shift(d, sender_phone, conversation_type)
        ot = effective_ot_for_date(d, sender_phone, conversation_type)
        days.append({
            "date": d.isoformat(),
            "shift": shift["shift"],
            "effective_ot": ot,
        })
        d += timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days}
