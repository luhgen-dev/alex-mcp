from __future__ import annotations

import ast
import hashlib
import json
import math
import operator
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import runtime_clock
import scope_policy
import db

from dateutil.rrule import rrulestr

from context import ActorContext
from config import DATA_DIR, get_settings
from db import connect, utc_now


def _spaces_sql(actor: ActorContext, scope: str | None = None) -> tuple[str, list[str]]:
    # The current trusted command fixes the maximum read boundary once in
    # ingress. Tool/model scope arguments may narrow an explicit all-spaces
    # request, but may never widen the actor's normalized policy.
    spaces = scope_policy.read_spaces(actor, scope)
    marks = ",".join("?" for _ in spaces)
    return marks, spaces


def _minor(amount: float | int | str | Decimal | None) -> int | None:
    if amount is None:
        return None
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid monetary amount")
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


GENERIC_CATEGORIES = {
    "general", "other", "misc", "miscellaneous", "unknown",
    "payment", "transfer", "bank_transfer", "fund_transfer", "duitnow",
}
SCOPE_ONLY_CATEGORIES = {
    "private", "privately", "personal", "family", "shared", "family_shared",
    "family-shared", "just_for_me", "only_for_me", "my_private",
}


CATEGORY_ALIASES = {
    "food_drink": "food",
    "food_and_drink": "food",
    "food_&_drink": "food",
    "dining": "food",
    "meals": "food",
    "petrol": "fuel",
    "gasoline": "fuel",
    "transportation": "transport",
}


def _clean_category(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip().lower().replace(" ", "_")[:80]
    if cleaned in GENERIC_CATEGORIES or cleaned in SCOPE_ONLY_CATEGORIES:
        return None
    return CATEGORY_ALIASES.get(cleaned, cleaned)


def _routing_keyword_matches(keyword: str, text: str) -> bool:
    """Match routing keywords as lexical phrases, not arbitrary substrings.

    Short catalogue entries such as "fine", "tng" and "bhp" must not match
    inside unrelated words. Flexible whitespace is allowed for multi-word
    phrases while preserving word boundaries.
    """
    key = str(keyword or "").strip().casefold()
    value = str(text or "").casefold()
    if not key:
        return False
    pieces = [re.escape(part) for part in key.split() if part]
    if not pieces:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)"
    return bool(re.search(pattern, value))


def _find_rule(conn, actor: ActorContext, text: str):
    rows = conn.execute(
        "SELECT * FROM routing_rules WHERE user_id=? ORDER BY LENGTH(keyword) DESC",
        (actor.user_id,),
    ).fetchall()
    for row in rows:
        if _routing_keyword_matches(row["keyword"], text):
            return row
    return None


def _route(conn, actor: ActorContext, text: str, category: str | None) -> tuple[str, str | None, int | None]:
    rule = _find_rule(conn, actor, text)
    proposed = _clean_category(category)
    lowered = (text or "").lower()
    generic_transfer = any(term in lowered for term in (
        "fund transfer", "bank transfer", "duitnow", "instant transfer", "transfer to", "payment to"
    ))
    # If a transfer description contains no established purpose keyword, do not trust
    # a model-supplied category. Alex must ask the user instead.
    resolved_category = (rule["category"] if rule else None) or (None if generic_transfer else proposed)

    fallback = (rule["force_space_id"] if rule and rule["force_space_id"] else actor.private_space)
    space = scope_policy.resolve_new_write_space(actor, fallback_space=fallback)
    if space not in actor.allowed_spaces:
        raise PermissionError("Resolved space is outside the authenticated user's memberships")
    threshold = rule["high_value_threshold_minor"] if rule else None
    return space, resolved_category, threshold

def _parse_event_time(value: str | None, tz_name: str) -> str:
    if not value:
        return utc_now()
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc).isoformat()


_EXACT_TIME_STATED_RE = re.compile(
    r"(?i)(?:"
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)\b|"
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|"
    r"\b\d{1,2}\.[0-5]\d\s*(?:am|pm)\b|"
    r"\b(?:noon|midnight)\b"
    r")"
)


def _user_stated_time(text: str | None) -> bool:
    """True only when the user supplied an exact clock time.

    Relative/vague phrases such as "just now", "last night" or "afternoon"
    never authorize a model-invented clock value.
    """
    return bool(_EXACT_TIME_STATED_RE.search(text or ""))


_RELATIVE_REMINDER_TIME_RE = re.compile(
    r"(?i)\b(?:in|after)\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\b"
)
_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_WEEKDAY_RE = re.compile(
    r"(?i)\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b"
)


def _stated_weekday(text: str | None) -> int | None:
    values = {
        _WEEKDAY_NAMES[m.group(1).casefold()]
        for m in _WEEKDAY_RE.finditer(str(text or ""))
        if m.group(1).casefold() in _WEEKDAY_NAMES
    }
    return next(iter(values)) if len(values) == 1 else None


def _validate_reminder_time_intent(actor: ActorContext, due_utc: str) -> None:
    """Reject model-generated reminder times that contradict trusted user intent."""
    trusted = str(getattr(actor, "trusted_text", "") or "")
    if trusted and not (_user_stated_time(trusted) or _RELATIVE_REMINDER_TIME_RE.search(trusted)):
        raise ValueError(
            "REMINDER_NEEDS_TIME: ask the user for an exact time before creating the reminder"
        )

    due = datetime.fromisoformat(str(due_utc).replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    due = due.astimezone(timezone.utc)
    now = runtime_clock.now_utc().astimezone(timezone.utc)
    tz = ZoneInfo(actor.timezone)
    local = due.astimezone(tz)
    now_local = now.astimezone(tz)

    # A newly created/rescheduled reminder may never be persisted in the past.
    # The small grace prevents an exact "now" value from racing the validator.
    if due <= now + timedelta(seconds=60):
        raise ValueError(
            "REMINDER_TIME_PASSED: the requested reminder time is already past; "
            "ask the user for a future time"
        )

    # Without an explicit year, a model must not jump to an unrelated distant
    # year. This is a sanity boundary, not a replacement for user intent.
    if trusted and not re.search(r"\b(?:19|20)\d{2}\b", trusted):
        if due > now + timedelta(days=366):
            raise ValueError(
                "REMINDER_DATE_TOO_FAR: no year was stated; ask the user to confirm the date"
            )

    wanted = _stated_weekday(trusted)
    if wanted is None:
        return

    if local.weekday() != wanted:
        requested = [
            name.title() for name, idx in _WEEKDAY_NAMES.items()
            if idx == wanted and len(name) > 3
        ][0]
        raise ValueError(
            f"DATE_WEEKDAY_MISMATCH: user requested {requested}, but "
            f"{local.date().isoformat()} is {local.strftime('%A')}; resolve the correct date before saving"
        )

    # "next Saturday" is genuinely ambiguous in ordinary English: some users
    # mean the coming Saturday, others the one after. When no calendar date was
    # supplied, reject rather than silently choosing one interpretation.
    weekday_token = _WEEKDAY_RE.search(trusted)
    if weekday_token and re.search(
        r"(?i)\bnext\s+" + re.escape(weekday_token.group(1)) + r"\b",
        trusted,
    ):
        has_calendar_date = bool(re.search(
            r"(?i)\b\d{1,2}(?:st|nd|rd|th)?\s+"
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\b",
            trusted,
        ))
        if not has_calendar_date:
            coming_delta = (wanted - now_local.weekday()) % 7
            if coming_delta == 0:
                coming_delta = 7
            coming = now_local.date() + timedelta(days=coming_delta)
            following = coming + timedelta(days=7)
            raise ValueError(
                "REMINDER_AMBIGUOUS_NEXT_WEEKDAY: ask whether the user means "
                f"{coming.isoformat()} or {following.isoformat()}"
            )

    # Bare weekday and "this <weekday>" mean the next occurrence. If today is
    # that weekday but the stated time has already passed, the next occurrence
    # is seven days later. A stale-but-matching weekday (the live Aug-2025 bug)
    # can no longer pass this guard.
    delta = (wanted - now_local.weekday()) % 7
    expected = now_local.date() + timedelta(days=delta)
    if delta == 0:
        candidate_today = datetime.combine(
            now_local.date(), local.timetz()
        ).astimezone(tz)
        if candidate_today <= now_local + timedelta(seconds=60):
            expected = now_local.date() + timedelta(days=7)
    if local.date() != expected:
        raise ValueError(
            f"DATE_MISMATCH: user requested the next matching weekday; "
            f"expected {expected.isoformat()}, got {local.date().isoformat()}"
        )


def _combine_local_date_with_received_clock(event_day, received: str, tz_name: str) -> str:
    """Use the intended local date with the real message-receive clock."""
    tz = ZoneInfo(tz_name)
    received_local = datetime.fromisoformat(received.replace("Z", "+00:00")).astimezone(tz)
    combined = datetime.combine(event_day, received_local.timetz())
    return combined.astimezone(timezone.utc).isoformat()


def _resolve_new_event_time(actor: ActorContext, event_date_local: str | None) -> str:
    """Deterministic timestamp for a NEW money record (v0.4.4).

    - No date/time from the model -> WhatsApp receive time.
    - Receipt/document date/time -> trusted extraction result.
    - Exact clock explicitly spoken/typed by the user -> keep it.
    - Otherwise preserve only the intended date and use the real message clock,
      so vague/date-only phrases never become fake midnight or invented times.
    """
    received = getattr(actor, "received_at_utc", "") or utc_now()
    if not event_date_local or not str(event_date_local).strip():
        return received

    raw = str(event_date_local).strip()
    parsed_utc = _parse_event_time(raw, actor.timezone)
    try:
        tz = ZoneInfo(actor.timezone)
        event_local_day = datetime.fromisoformat(parsed_utc).astimezone(tz).date()
    except Exception:
        return parsed_utc

    has_document = getattr(actor, "source", "text") in {"image", "document", "mixed"}
    if has_document:
        return parsed_utc

    if _user_stated_time(getattr(actor, "trusted_text", "")):
        return parsed_utc

    return _combine_local_date_with_received_clock(event_local_day, received, actor.timezone)


_TEMPORAL_CORRECTION_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:change|correct|fix|update|move|set)\b.{0,30}\b(?:date|time|when)\b|"
    r"\b(?:actually|it\s+was|was|not)\s+(?:today|yesterday|tomorrow)\b|"
    r"\b(?:on|at)\s+\d{4}-\d{2}-\d{2}\b"
    r")"
)


def _user_requested_event_time_change(text: str | None) -> bool:
    return bool(_user_stated_time(text) or _TEMPORAL_CORRECTION_RE.search(text or ""))

def _local_bound(date_text: str, tz_name: str, end: bool = False) -> str:
    d = datetime.fromisoformat(date_text).date()
    local = datetime.combine(d, time.max if end else time.min, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc).isoformat()


def log_expense(actor: ActorContext, description: str, amount: float | None = None,
                category: str | None = None, currency: str | None = None,
                event_date_local: str | None = None, reference: str | None = None,
                event_type: str = "Expense") -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    resolved_currency = (currency or "").strip().upper()
    if resolved_currency not in {"MYR", "SGD"}:
        return {
            "status": "clarification_required",
            "missing": ["currency"],
            "message": "Ask whether this expense was MYR or SGD. Nothing was logged yet.",
        }
    conn = connect()
    try:
        existing = conn.execute("SELECT * FROM financial_events WHERE action_key=?", (actor.action_key,)).fetchone()
        if existing:
            return {"status": "already_applied", "event_id": existing["event_id"],
                    "amount": (existing["amount_minor"] / 100 if existing["amount_minor"] else None),
                    "currency": existing["currency"], "description": existing["description"],
                    "category": existing["category"], "review": existing["status"] != "ACTIVE"}

        amount_minor = _minor(amount)
        space, resolved_category, threshold = _route(conn, actor, description, category)
        reason = None
        if amount_minor is None:
            reason = "amount_missing"
        elif not resolved_category:
            reason = "purpose_or_category_unclear"
        elif threshold is not None and amount_minor > threshold:
            reason = "high_value_confirmation"
        status = "PENDING_HUMAN_REVIEW" if reason else "ACTIVE"
        event_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO financial_events(
                event_id,action_key,source_message_id,space_id,created_by,owner_id,event_type,
                category,amount_minor,currency,event_date_utc,timezone_name,description,reference_text,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, actor.action_key, actor.source_message_id, space, actor.user_id, actor.user_id,
             "Income" if event_type.lower() == "income" else "Expense",
             resolved_category, amount_minor, resolved_currency,
             _resolve_new_event_time(actor, event_date_local), actor.timezone,
             description.strip()[:300], (reference or "")[:300] or None, status),
        )
        for media_id in actor.media_ids:
            conn.execute("INSERT OR IGNORE INTO event_media_links(event_id,media_id) VALUES(?,?)", (event_id, media_id))
        conn.commit()
        return {
            "status": "logged" if status == "ACTIVE" else "needs_confirmation",
            "event_id": event_id,
            "amount": amount_minor / 100 if amount_minor is not None else None,
            "currency": resolved_currency,
            "description": description.strip()[:300],
            "category": resolved_category,
            "space": space,
            "pending_reason": reason,
            "receipt_saved": bool(actor.media_ids),
        }
    finally:
        conn.close()


def confirm_expense(actor: ActorContext, event_id: str, approve: bool = True,
                    category: str | None = None, amount: float | None = None) -> dict:
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM financial_events WHERE event_id=? AND space_id IN ({marks})",
            [event_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("event not found in your accessible spaces")
        if row["status"] != "PENDING_HUMAN_REVIEW":
            return {
                "status": "not_pending",
                "event_id": event_id,
                "current_status": row["status"],
            }
        if not approve:
            conn.execute("UPDATE financial_events SET status='IGNORED' WHERE event_id=?", (event_id,))
            conn.commit()
            return {"status": "ignored", "event_id": event_id}
        new_amount = _minor(amount) if amount is not None else row["amount_minor"]
        new_category = category or row["category"]
        if new_amount is None or not new_category:
            return {"status": "still_needs_information", "event_id": event_id,
                    "missing": ["amount" if new_amount is None else None, "category" if not new_category else None]}
        conn.execute(
            "UPDATE financial_events SET amount_minor=?,category=?,status='ACTIVE' WHERE event_id=?",
            (new_amount, new_category, event_id),
        )
        conn.commit()
        return {"status": "confirmed", "event_id": event_id, "amount": new_amount / 100,
                "category": new_category, "currency": row["currency"]}
    finally:
        conn.close()


def query_finances(actor: ActorContext, start_date: str | None = None, end_date: str | None = None,
                   category: str | None = None, search: str | None = None,
                   currency: str | None = None, limit: int = 20,
                   scope: str | None = None, source: str | None = None,
                   include_all_records: bool = False) -> dict:
    """Return exact aggregates over the full match set plus records.

    Aggregates are always computed by SQL over the complete match set. By
    default records are bounded for chat use; report/export callers may request
    the full matching ledger with include_all_records=True.
    """
    marks, spaces = _spaces_sql(actor, scope)
    where = f"status='ACTIVE' AND space_id IN ({marks})"
    params: list = spaces[:]
    source_kind = str(source or "all").strip().casefold()
    if source_kind in {"", "all", "any"}:
        pass
    elif source_kind in {"voice", "audio", "voice_note", "voice-note"}:
        where += """ AND EXISTS (
            SELECT 1 FROM media_objects srcm
            WHERE srcm.source_message_id=financial_events.source_message_id
              AND srcm.media_type='AUDIO'
        )"""
    elif source_kind in {"receipt", "document", "media"}:
        where += """ AND EXISTS (
            SELECT 1 FROM media_objects srcm
            WHERE srcm.source_message_id=financial_events.source_message_id
              AND srcm.media_type IN ('IMAGE','PDF')
        )"""
    elif source_kind in {"text", "typed"}:
        where += """ AND NOT EXISTS (
            SELECT 1 FROM media_objects srcm
            WHERE srcm.source_message_id=financial_events.source_message_id
              AND srcm.media_type IN ('AUDIO','IMAGE','PDF')
        )"""
    else:
        raise ValueError("source must be all, voice, receipt, or text")
    if start_date:
        where += " AND event_date_utc>=?"
        params.append(_local_bound(start_date, actor.timezone, False))
    if end_date:
        where += " AND event_date_utc<=?"
        params.append(_local_bound(end_date, actor.timezone, True))
    if category:
        where += " AND LOWER(category)=LOWER(?)"
        params.append(category)
    if currency:
        where += " AND currency=?"
        params.append(currency.upper())
    if search:
        where += " AND (LOWER(description) LIKE ? OR LOWER(COALESCE(reference_text,'')) LIKE ?)"
        needle = f"%{search.lower()}%"
        params.extend([needle, needle])

    conn = connect()
    try:
        aggregate_rows = conn.execute(
            f"""SELECT event_type,currency,COALESCE(SUM(amount_minor),0) AS total_minor,COUNT(*) AS n
                FROM financial_events WHERE {where}
                GROUP BY event_type,currency""",
            params,
        ).fetchall()
        category_expr = """CASE
            WHEN LOWER(REPLACE(COALESCE(category,''),' ','_')) IN
                 ('food_drink','food_and_drink','food_&_drink','dining','meals')
              THEN 'food'
            WHEN LOWER(REPLACE(COALESCE(category,''),' ','_')) IN ('petrol','gasoline')
              THEN 'fuel'
            WHEN LOWER(REPLACE(COALESCE(category,''),' ','_'))='transportation'
              THEN 'transport'
            ELSE COALESCE(NULLIF(LOWER(category),''),'uncategorised')
        END"""
        category_rows = conn.execute(
            f"""SELECT {category_expr} AS category,
                       currency,COALESCE(SUM(amount_minor),0) AS total_minor,COUNT(*) AS n
                FROM financial_events
                WHERE {where} AND event_type='Expense'
                GROUP BY {category_expr},currency
                ORDER BY total_minor DESC,category""",
            params,
        ).fetchall()
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM financial_events WHERE {where}", params
        ).fetchone()
        row_sql = f"""SELECT event_id,event_type,category,amount_minor,currency,event_date_utc,
                             description,reference_text,space_id
                      FROM financial_events WHERE {where}
                      ORDER BY event_date_utc DESC, created_at_utc DESC, rowid DESC"""
        row_params = list(params)
        if not include_all_records:
            row_sql += " LIMIT ?"
            row_params.append(max(1, min(100, int(limit))))
        rows = [dict(r) for r in conn.execute(row_sql, row_params).fetchall()]
        for row in rows:
            canonical = _clean_category(row.get("category"))
            if canonical:
                row["category"] = canonical
    finally:
        conn.close()

    spending_totals: dict[str, float] = {}
    income_totals: dict[str, float] = {}
    for r in aggregate_rows:
        amount = (r["total_minor"] or 0) / 100
        target = spending_totals if r["event_type"] == "Expense" else income_totals
        target[r["currency"]] = round(amount, 2)

    category_totals = [
        {
            "category": r["category"],
            "currency": r["currency"],
            "amount": round((r["total_minor"] or 0) / 100, 2),
            "count": int(r["n"] or 0),
        }
        for r in category_rows
    ]

    records = []
    tz = ZoneInfo(actor.timezone)
    for r in rows:
        amount = (r["amount_minor"] or 0) / 100
        try:
            local = datetime.fromisoformat(r["event_date_utc"]).astimezone(tz).isoformat()
        except Exception:
            local = r["event_date_utc"]
        records.append({
            "event_id": r["event_id"], "type": r["event_type"], "amount": amount,
            "currency": r["currency"], "category": r["category"], "description": r["description"],
            "date_local": local, "reference": r["reference_text"],
            "scope": "family" if r["space_id"] == "FAMILY_SHARED" else "private",
        })

    currencies = set(spending_totals) | set(income_totals)
    net_outflow = {
        cur: round(spending_totals.get(cur, 0) - income_totals.get(cur, 0), 2)
        for cur in currencies
    }
    return {
        "spending_totals": spending_totals,
        "income_totals": income_totals,
        "net_outflow": net_outflow,
        "category_totals": category_totals,
        "count": int(count_row["n"] if count_row else 0),
        "returned_records": len(records),
        "all_records_returned": bool(include_all_records),
        "latest_record": records[0] if records else None,
        "records": records,
    }

def correct_expense(actor: ActorContext, event_id: str, amount: float | None = None,
                    description: str | None = None, category: str | None = None,
                    event_date_local: str | None = None, reason: str | None = None) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        existing = conn.execute("SELECT event_id FROM financial_events WHERE action_key=?", (actor.action_key,)).fetchone()
        if existing:
            return {"status": "already_applied", "event_id": existing["event_id"]}
        parent = conn.execute(
            f"SELECT * FROM financial_events WHERE event_id=? AND space_id IN ({marks})",
            [event_id] + spaces,
        ).fetchone()
        if not parent:
            raise PermissionError("event not found in your accessible spaces")
        child_id = str(uuid.uuid4())
        conn.execute("BEGIN")
        conn.execute(
            """INSERT INTO financial_events(
                event_id,action_key,source_message_id,space_id,created_by,owner_id,event_type,
                category,amount_minor,currency,event_date_utc,timezone_name,description,reference_text,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE')""",
            (
                child_id, actor.action_key, actor.source_message_id, parent["space_id"], actor.user_id,
                parent["owner_id"], parent["event_type"], category or parent["category"],
                _minor(amount) if amount is not None else parent["amount_minor"], parent["currency"],
                (
                    _resolve_new_event_time(actor, event_date_local)
                    if event_date_local and _user_requested_event_time_change(
                        getattr(actor, "trusted_text", "")
                    )
                    else parent["event_date_utc"]
                ),
                parent["timezone_name"], description or parent["description"], parent["reference_text"],
            ),
        )
        conn.execute("UPDATE financial_events SET status='SUPERSEDED' WHERE event_id=?", (event_id,))
        conn.execute(
            "INSERT INTO financial_event_corrections(correction_id,parent_event_id,child_event_id,reason) VALUES(?,?,?,?)",
            (str(uuid.uuid4()), event_id, child_id, reason),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_media_links(event_id,media_id) SELECT ?,media_id FROM event_media_links WHERE event_id=?",
            (child_id, event_id),
        )
        conn.commit()
        return {"status": "corrected", "old_event_id": event_id, "event_id": child_id}
    finally:
        conn.close()


def _store_selection(conn, actor: ActorContext, kind: str, ids: list[str]) -> str:
    selection_id = str(uuid.uuid4())
    expires = (runtime_clock.now_utc() + timedelta(hours=48)).isoformat()
    conn.execute(
        """INSERT INTO selection_sets(
            selection_id,user_id,conversation_id,selection_kind,items_json,created_at_utc,expires_at_utc
           ) VALUES(?,?,?,?,?,?,?)""",
        (selection_id, actor.user_id, actor.conversation_id, kind,
         json.dumps(ids, ensure_ascii=False), utc_now(), expires),
    )
    return selection_id


def find_receipts(actor: ActorContext, query: str | None = None, amount: float | None = None,
                  start_date: str | None = None, end_date: str | None = None, limit: int = 10,
                  scope: str | None = None) -> dict:
    """Find linked receipts plus permitted preserved-but-unlinked media."""
    marks, spaces = _spaces_sql(actor, scope)
    bounded = max(1, min(25, int(limit)))
    sql = f"""SELECT DISTINCT m.media_id,m.media_type,m.mime_type,m.created_at_utc,
                     f.event_id,f.amount_minor,f.currency,f.event_date_utc,f.description,
                     f.reference_text,f.space_id,m.ocr_text,i.raw_text AS caption
              FROM media_objects m
              JOIN inbound_messages i ON i.message_id=m.source_message_id
              JOIN event_media_links l ON l.media_id=m.media_id
              JOIN financial_events f ON f.event_id=l.event_id
              WHERE f.status IN ('ACTIVE','PENDING_HUMAN_REVIEW') AND f.space_id IN ({marks})
                AND m.media_type IN ('IMAGE','PDF')"""
    params: list = spaces[:]
    if amount is not None:
        sql += " AND f.amount_minor=?"
        params.append(_minor(amount))
    if start_date:
        sql += " AND f.event_date_utc>=?"
        params.append(_local_bound(start_date, actor.timezone, False))
    if end_date:
        sql += " AND f.event_date_utc<=?"
        params.append(_local_bound(end_date, actor.timezone, True))
    if query:
        # Human captions are the primary semantic label. Match meaningful
        # query tokens across caption + ledger metadata + OCR instead of
        # requiring one verbatim phrase.
        stop = {"latest","recent","receipt","receipts","show","find","get","send",
                "open","original","please","my","the","a","an"}
        terms = [
            token for token in re.findall(r"[a-z0-9]+", query.casefold())
            if len(token) > 1 and token not in stop
        ]
        for token in terms or [query.casefold().strip()]:
            needle = f"%{token}%"
            sql += """ AND LOWER(
                COALESCE(i.raw_text,'') || ' ' || COALESCE(f.description,'') || ' ' ||
                COALESCE(f.reference_text,'') || ' ' || COALESCE(m.ocr_text,'')
            ) LIKE ?"""
            params.append(needle)
    sql += " ORDER BY f.event_date_utc DESC LIMIT ?"
    params.append(bounded)

    conn = connect()
    try:
        linked = conn.execute(sql, params).fetchall()
        matches = [
            {"media_id": r["media_id"], "event_id": r["event_id"],
             "amount": (r["amount_minor"]/100 if r["amount_minor"] is not None else None),
             "currency": r["currency"], "description": r["description"],
             "caption": r["caption"], "label": (r["caption"] or r["description"]),
             "event_date_utc": r["event_date_utc"], "reference": r["reference_text"],
             "scope": "family" if r["space_id"] == "FAMILY_SHARED" else "private",
             "linked": True}
            for r in linked
        ]

        # Explicitly saved receipt evidence is authorized by the saved
        # item's stored scope, not by the chat in which the image was uploaded.
        # This allows a receipt saved Family Shared from DM to be retrieved in
        # the family group while keeping private saved media invisible there.
        remaining = bounded - len(matches)
        if remaining > 0:
            saved_sql = f"""SELECT DISTINCT m.media_id,m.created_at_utc,m.ocr_text,
                                    i.raw_text AS caption,s.title,s.content,s.space_id
                             FROM media_objects m
                             JOIN inbound_messages i ON i.message_id=m.source_message_id
                             JOIN saved_items s ON s.media_id=m.media_id
                             WHERE s.space_id IN ({marks})
                               AND m.media_type IN ('IMAGE','PDF')
                               AND NOT EXISTS (
                                   SELECT 1 FROM event_media_links l WHERE l.media_id=m.media_id
                               )
                               AND (
                                   LOWER(COALESCE(s.title,'')) LIKE '%receipt%'
                                   OR LOWER(COALESCE(s.title,'')) LIKE '%invoice%'
                                   OR LOWER(COALESCE(s.content,'')) LIKE '%receipt%'
                                   OR LOWER(COALESCE(s.content,'')) LIKE '%invoice%'
                                   OR LOWER(COALESCE(i.raw_text,'')) LIKE '%receipt%'
                                   OR LOWER(COALESCE(i.raw_text,'')) LIKE '%invoice%'
                               )"""
            saved_params: list = spaces[:]
            if query:
                stop = {"latest","recent","receipt","receipts","show","find","get","send",
                        "open","original","please","my","our","the","a","an"}
                terms = [
                    token for token in re.findall(r"[a-z0-9]+", query.casefold())
                    if len(token) > 1 and token not in stop
                ]
                for token in terms:
                    saved_sql += """ AND LOWER(
                        COALESCE(s.title,'') || ' ' || COALESCE(s.content,'') || ' ' ||
                        COALESCE(i.raw_text,'') || ' ' || COALESCE(m.ocr_text,'')
                    ) LIKE ?"""
                    saved_params.append(f"%{token}%")
            saved_sql += " ORDER BY m.created_at_utc DESC LIMIT ?"
            saved_params.append(remaining)
            existing_ids = {m["media_id"] for m in matches}
            for r in conn.execute(saved_sql, saved_params).fetchall():
                if r["media_id"] in existing_ids:
                    continue
                matches.append({
                    "media_id": r["media_id"], "event_id": None, "amount": None,
                    "currency": None,
                    "description": r["title"] or "Saved receipt",
                    "caption": r["caption"],
                    "label": (r["title"] or r["caption"] or "Saved receipt"),
                    "event_date_utc": r["created_at_utc"], "reference": None,
                    "scope": "family" if r["space_id"] == "FAMILY_SHARED" else "private",
                    "linked": False,
                })
                existing_ids.add(r["media_id"])

        remaining = bounded - len(matches)
        # Bare unlinked provenance from a DM is eligible only when the user's
        # own caption explicitly identifies receipt/invoice evidence. Generic
        # saved images/documents must never pollute a receipt-only result.
        requested_scope = str(scope or "all").strip().casefold()
        if remaining > 0 and actor.conversation_type != "GROUP" and requested_scope not in {"family", "shared"}:
            orphan_sql = """SELECT m.media_id,m.created_at_utc,m.ocr_text,i.raw_text AS caption
                            FROM media_objects m
                            JOIN inbound_messages i ON i.message_id=m.source_message_id
                            WHERE i.sender_phone=? AND m.media_type IN ('IMAGE','PDF')
                              AND NOT EXISTS (
                                  SELECT 1 FROM event_media_links l WHERE l.media_id=m.media_id
                              )
                              AND NOT EXISTS (
                                  SELECT 1 FROM saved_items s WHERE s.media_id=m.media_id
                              )"""
            orphan_params: list = [actor.phone]
            if start_date:
                orphan_sql += " AND m.created_at_utc>=?"
                orphan_params.append(_local_bound(start_date, actor.timezone, False))
            if end_date:
                orphan_sql += " AND m.created_at_utc<=?"
                orphan_params.append(_local_bound(end_date, actor.timezone, True))
            if query:
                stop = {"latest","recent","receipt","receipts","show","find","get","send",
                        "open","original","please","my","the","a","an"}
                terms = [
                    token for token in re.findall(r"[a-z0-9]+", query.casefold())
                    if len(token) > 1 and token not in stop
                ]
                for token in terms or [query.casefold().strip()]:
                    orphan_sql += """ AND LOWER(
                        COALESCE(i.raw_text,'') || ' ' || COALESCE(m.ocr_text,'')
                    ) LIKE ?"""
                    orphan_params.append(f"%{token}%")
            if amount is not None:
                orphan_sql += " AND COALESCE(m.ocr_text,'') LIKE ?"
                orphan_params.append(f"%{float(amount):.2f}%")
            orphan_sql += " ORDER BY m.created_at_utc DESC LIMIT ?"
            orphan_params.append(remaining)
            for r in conn.execute(orphan_sql, orphan_params).fetchall():
                matches.append({
                    "media_id": r["media_id"], "event_id": None, "amount": None,
                    "currency": None,
                    "description": "Saved receipt/media awaiting ledger linkage",
                    "caption": r["caption"],
                    "label": (r["caption"] or "Saved receipt/media awaiting ledger linkage"),
                    "event_date_utc": r["created_at_utc"], "reference": None, "linked": False,
                })
        if matches:
            _store_selection(conn, actor, "RECEIPT", [m["media_id"] for m in matches])
            conn.commit()
        return {"matches": [{**m, "choice": i + 1} for i, m in enumerate(matches)]}
    finally:
        conn.close()


def get_receipt(actor: ActorContext, media_id: str) -> dict:
    """Return the original receipt file when linked to an accessible event or preserved from this user's own message."""
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT m.*,f.event_id,f.description,f.amount_minor,f.currency,f.event_date_utc
                FROM media_objects m
                JOIN event_media_links l ON l.media_id=m.media_id
                JOIN financial_events f ON f.event_id=l.event_id
                WHERE m.media_id=? AND f.space_id IN ({marks})
                  AND f.status IN ('ACTIVE','PENDING_HUMAN_REVIEW')
                LIMIT 1""",
            [media_id] + spaces,
        ).fetchone()
        if row:
            return {
                "status": "found", "media_id": media_id, "event_id": row["event_id"],
                "description": row["description"],
                "amount": (row["amount_minor"]/100 if row["amount_minor"] is not None else None),
                "currency": row["currency"], "event_date_utc": row["event_date_utc"],
                "_attachments": [{"path": row["local_path"], "mime_type": row["mime_type"],
                                  "kind": "IMAGE" if row["media_type"] == "IMAGE" else "DOCUMENT"}],
            }

        saved = conn.execute(
            f"""SELECT m.*,s.title,s.content,s.space_id,i.raw_text AS caption
                FROM media_objects m
                JOIN saved_items s ON s.media_id=m.media_id
                JOIN inbound_messages i ON i.message_id=m.source_message_id
                WHERE m.media_id=? AND s.space_id IN ({marks})
                  AND m.media_type IN ('IMAGE','PDF')
                  AND (
                      LOWER(COALESCE(s.title,'')) LIKE '%receipt%'
                      OR LOWER(COALESCE(s.title,'')) LIKE '%invoice%'
                      OR LOWER(COALESCE(s.content,'')) LIKE '%receipt%'
                      OR LOWER(COALESCE(s.content,'')) LIKE '%invoice%'
                      OR LOWER(COALESCE(i.raw_text,'')) LIKE '%receipt%'
                      OR LOWER(COALESCE(i.raw_text,'')) LIKE '%invoice%'
                  )
                ORDER BY s.created_at_utc DESC LIMIT 1""",
            [media_id] + spaces,
        ).fetchone()
        if saved:
            return {
                "status": "found_saved_receipt", "media_id": media_id, "event_id": None,
                "description": saved["title"] or "Saved receipt",
                "amount": None, "currency": None, "event_date_utc": saved["created_at_utc"],
                "scope": "family" if saved["space_id"] == "FAMILY_SHARED" else "private",
                "_attachments": [{"path": saved["local_path"], "mime_type": saved["mime_type"],
                                  "kind": "IMAGE" if saved["media_type"] == "IMAGE" else "DOCUMENT"}],
            }

        if actor.conversation_type == "GROUP":
            raise PermissionError("receipt not found in your accessible data")

        orphan = conn.execute(
            """SELECT m.* FROM media_objects m
               JOIN inbound_messages i ON i.message_id=m.source_message_id
               WHERE m.media_id=? AND i.sender_phone=?
                 AND m.media_type IN ('IMAGE','PDF')
                 AND NOT EXISTS (
                     SELECT 1 FROM event_media_links l WHERE l.media_id=m.media_id
                 ) LIMIT 1""",
            (media_id, actor.phone),
        ).fetchone()
        if not orphan:
            raise PermissionError("receipt not found in your accessible data")
        return {
            "status": "found_unlinked", "media_id": media_id, "event_id": None,
            "description": "Saved receipt/media awaiting ledger linkage",
            "amount": None, "currency": None, "event_date_utc": orphan["created_at_utc"],
            "_attachments": [{"path": orphan["local_path"], "mime_type": orphan["mime_type"],
                              "kind": "IMAGE" if orphan["media_type"] == "IMAGE" else "DOCUMENT"}],
        }
    finally:
        conn.close()

def _media_access_clause(actor: ActorContext) -> tuple[str, list]:
    """SQL predicate proving access to an original media object.

    DM callers may retrieve their own original uploads plus material already
    attached to spaces they can read. Group callers are structurally restricted
    to FAMILY_SHARED-origin/linked material.
    """
    spaces = list(actor.allowed_spaces)
    clauses: list[str] = []
    params: list = []

    if actor.conversation_type != "GROUP":
        clauses.append("i.sender_phone=?")
        params.append(actor.phone)

    if "FAMILY_SHARED" in spaces:
        clauses.append("i.conversation_type='GROUP'")

    if spaces:
        marks = ",".join("?" for _ in spaces)
        clauses.append(
            f"""EXISTS (
                SELECT 1
                FROM event_media_links em
                JOIN financial_events fe ON fe.event_id=em.event_id
                WHERE em.media_id=m.media_id
                  AND fe.space_id IN ({marks})
                  AND fe.status IN ('ACTIVE','PENDING_HUMAN_REVIEW')
            )"""
        )
        params.extend(spaces)
        clauses.append(
            f"""EXISTS (
                SELECT 1 FROM saved_items si
                WHERE si.media_id=m.media_id
                  AND si.space_id IN ({marks})
            )"""
        )
        params.extend(spaces)

    if not clauses:
        return "0", []
    return "(" + " OR ".join(clauses) + ")", params


def _media_type_token(value: str | None) -> str | None:
    token = str(value or "all").strip().casefold().replace("-", "_")
    if token in {"", "all", "any", "media"}:
        return None
    mapping = {
        "voice": "AUDIO", "voice_note": "AUDIO", "audio": "AUDIO",
        "recording": "AUDIO", "recordings": "AUDIO",
        "image": "IMAGE", "images": "IMAGE", "photo": "IMAGE", "photos": "IMAGE",
        "picture": "IMAGE", "pictures": "IMAGE",
        "pdf": "PDF", "document": "PDF", "documents": "PDF",
    }
    if token not in mapping:
        raise ValueError("media_type must be all, voice, image, or document")
    return mapping[token]


def _store_media_selection(conn, actor: ActorContext, media_type: str | None,
                           ids: list[str]) -> str:
    selection_id = str(uuid.uuid4())
    expires = (runtime_clock.now_utc() + timedelta(hours=48)).isoformat()
    conn.execute(
        """INSERT INTO media_selection_sets(
            selection_id,user_id,conversation_id,media_type,items_json,
            created_at_utc,expires_at_utc
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            selection_id, actor.user_id, actor.conversation_id, media_type,
            json.dumps(ids, ensure_ascii=False), utc_now(), expires,
        ),
    )
    return selection_id


def find_media(actor: ActorContext, media_type: str | None = "all",
               query: str | None = None, start_date: str | None = None,
               end_date: str | None = None, limit: int = 10) -> dict:
    """Browse preserved original voice/image/document inputs without crossing ACLs."""
    resolved_type = _media_type_token(media_type)
    bounded = max(1, min(25, int(limit)))
    access_sql, access_params = _media_access_clause(actor)
    where = [access_sql]
    params: list = list(access_params)

    if resolved_type:
        where.append("m.media_type=?")
        params.append(resolved_type)
    if start_date:
        where.append("m.created_at_utc>=?")
        params.append(_local_bound(start_date, actor.timezone, False))
    if end_date:
        where.append("m.created_at_utc<=?")
        params.append(_local_bound(end_date, actor.timezone, True))
    if query:
        needle = f"%{str(query).casefold()}%"
        where.append(
            """(
                LOWER(COALESCE(m.transcript_text,'')) LIKE ?
                OR LOWER(COALESCE(m.ocr_text,'')) LIKE ?
                OR LOWER(COALESCE(i.raw_text,'')) LIKE ?
            )"""
        )
        params.extend([needle, needle, needle])

    sql = f"""SELECT DISTINCT
                     m.media_id,m.media_type,m.mime_type,m.created_at_utc,
                     m.original_name,m.transcript_text,m.ocr_text,i.raw_text
              FROM media_objects m
              JOIN inbound_messages i ON i.message_id=m.source_message_id
              WHERE {' AND '.join(where)}
              ORDER BY m.created_at_utc DESC
              LIMIT ?"""
    params.append(bounded)

    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        matches = []
        for row in rows:
            preview = (
                row["transcript_text"]
                if row["media_type"] == "AUDIO"
                else row["ocr_text"]
            ) or row["raw_text"] or ""
            preview = " ".join(str(preview).split())[:180]
            matches.append({
                "media_id": row["media_id"],
                "media_type": row["media_type"],
                "mime_type": row["mime_type"],
                "created_at_utc": row["created_at_utc"],
                "original_name": row["original_name"],
                "preview": preview or None,
            })
        if matches:
            _store_media_selection(
                conn, actor, resolved_type,
                [row["media_id"] for row in matches],
            )
            conn.commit()
        return {
            "media_type": resolved_type or "ALL",
            "matches": [
                {**row, "choice": index + 1}
                for index, row in enumerate(matches)
            ],
            "note": (
                "Original media is retained by design for household provenance. "
                "Use get_media_original or a numbered follow-up to retrieve it."
            ),
        }
    finally:
        conn.close()


def get_media_original(actor: ActorContext, media_id: str) -> dict:
    """Retrieve one authorized original media object, including a voice note."""
    access_sql, access_params = _media_access_clause(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT m.*,i.raw_text
                FROM media_objects m
                JOIN inbound_messages i ON i.message_id=m.source_message_id
                WHERE m.media_id=? AND {access_sql}
                LIMIT 1""",
            [media_id] + access_params,
        ).fetchone()
        if not row:
            raise PermissionError("media not found in your accessible data")
        kind = "IMAGE" if row["media_type"] == "IMAGE" else "DOCUMENT"
        return {
            "status": "found",
            "media_id": row["media_id"],
            "media_type": row["media_type"],
            "created_at_utc": row["created_at_utc"],
            "preview": (
                (row["transcript_text"] if row["media_type"] == "AUDIO" else row["ocr_text"])
                or row["raw_text"]
                or ""
            )[:300],
            "_attachments": [{
                "path": row["local_path"],
                "mime_type": row["mime_type"],
                "kind": kind,
            }],
        }
    finally:
        conn.close()


def save_item(actor: ActorContext, title: str, content: str, tags: str | None = None,
              shared: bool = False) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    conn = connect()
    try:
        existing = conn.execute("SELECT item_id FROM saved_items WHERE action_key=?", (actor.action_key,)).fetchone()
        if existing:
            return {"status": "already_saved", "item_id": existing["item_id"]}
        space = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
        if space not in actor.allowed_spaces:
            raise PermissionError("requested memory space is not accessible")
        item_id = str(uuid.uuid4())
        # Only a picture/document is a saved item's "original". A voice note is
        # just how the user spoke the request; linking it would make retrieval
        # send the recording back instead of the saved content (v0.4.4).
        media_id = None
        if actor.media_ids:
            marks_m = ",".join("?" for _ in actor.media_ids)
            row_m = conn.execute(
                f"""SELECT media_id FROM media_objects
                    WHERE media_id IN ({marks_m}) AND media_type<>'AUDIO'
                    ORDER BY created_at_utc LIMIT 1""",
                list(actor.media_ids),
            ).fetchone()
            media_id = row_m["media_id"] if row_m else None
        stored_title = str(title or "")
        stored_content = str(content or "")
        if scope_policy.contains_emoji(getattr(actor, "trusted_text", "")):
            stored_title = scope_policy.strip_control_emoji(stored_title)
            stored_content = scope_policy.strip_control_emoji(stored_content)
        conn.execute(
            """INSERT INTO saved_items(item_id,action_key,source_message_id,space_id,owner_id,title,content,tags,media_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (item_id, actor.action_key, actor.source_message_id, space, actor.user_id,
             stored_title[:200], stored_content[:20000], (tags or "")[:500] or None, media_id),
        )
        conn.commit()
        return {"status": "saved", "item_id": item_id, "space": space, "media_saved": bool(media_id)}
    finally:
        conn.close()


_SAVED_GENERIC_WORDS = {
    "a", "an", "the", "my", "me", "i", "you", "your", "to", "for", "of", "in", "on",
    "and", "or", "that", "this", "those", "these", "what", "which", "all", "any",
    "every", "everything", "things", "thing", "stuff", "items", "item", "list",
    "show", "find", "send", "get", "give", "saved", "save", "remember", "remembered",
    "asked", "ask", "told", "tell", "keep", "kept", "did", "do", "have", "please",
    "pictures", "picture", "photos", "photo", "images", "image", "pics", "pic",
    "documents", "document", "docs", "doc", "files", "file", "notes", "note",
    "privately", "private", "memory", "memories", "later", "about",
    "is", "are", "was", "were",
}
_SAVED_KIND_ALIASES = {
    "picture": "IMAGE", "pictures": "IMAGE", "photo": "IMAGE", "image": "IMAGE", "images": "IMAGE",
    "document": "DOCUMENT", "documents": "DOCUMENT", "pdf": "DOCUMENT", "file": "DOCUMENT",
    "note": "NOTE", "notes": "NOTE", "text": "NOTE",
}


def _saved_local_date(value: str | None, tz_name: str) -> str | None:
    """Human local save timestamp for browse/search presentation."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(tz_name))
        hour = local.strftime("%I").lstrip("0") or "12"
        return (
            f"{local.day} {local.strftime('%B %Y')}, "
            f"{hour}:{local.strftime('%M')} {local.strftime('%p')}"
        )
    except Exception:
        return None


def search_saved_items(actor: ActorContext, query: str | None = None, limit: int = 10,
                       kind: str | None = None) -> dict:
    """Find explicitly saved items. Empty/general query = browse newest items.

    v0.4.4: supports browse ("what did I ask you to save"), a picture/document/
    note filter, and word-level matching when the whole phrase has no match.
    Space/ACL filtering is unchanged and always applied in SQL.
    """
    marks, spaces = _spaces_sql(actor)
    bounded = max(1, min(25, int(limit or 10)))
    kind_key = _SAVED_KIND_ALIASES.get(str(kind or "").strip().casefold())
    raw = str(query or "").strip().casefold()
    words = [w for w in re.findall(r"[\w'-]+", raw) if len(w) > 1 and w not in _SAVED_GENERIC_WORDS]

    base = f"""SELECT s.item_id,s.title,s.content,s.tags,s.created_at_utc,s.media_id,
                      m.media_type
               FROM saved_items s
               LEFT JOIN media_objects m ON m.media_id=s.media_id
               WHERE s.space_id IN ({marks})"""
    params: list = spaces[:]
    if kind_key == "IMAGE":
        base += " AND m.media_type='IMAGE'"
    elif kind_key == "DOCUMENT":
        base += " AND m.media_type IN ('PDF','DOCUMENT')"
    elif kind_key == "NOTE":
        base += " AND s.media_id IS NULL"

    def run(extra_sql: str, extra: list) -> list[dict]:
        rows = conn.execute(
            base + extra_sql + " ORDER BY s.created_at_utc DESC, s.rowid DESC LIMIT ?",
            params + extra + [bounded],
        ).fetchall()
        return [dict(r) for r in rows]

    field = "(LOWER(s.title) LIKE ? OR LOWER(s.content) LIKE ? OR LOWER(COALESCE(s.tags,'')) LIKE ?)"
    conn = connect()
    try:
        mode = "browse"
        matches: list[dict] = []
        if words:
            phrase = " ".join(words)
            needle = f"%{phrase}%"
            matches = run(" AND " + field, [needle, needle, needle])
            mode = "phrase"
            if not matches and len(words) > 1:
                clauses, extra = [], []
                for w in words:
                    clauses.append(field)
                    n = f"%{w}%"
                    extra.extend([n, n, n])
                # Multi-word fallback is deliberately conjunctive. A deleted
                # "v053 memory test phrase" must not be replaced by an unrelated
                # note merely because both contain "v053" or "test".
                matches = run(" AND (" + " AND ".join(clauses) + ")", extra)
                mode = "words_all"
        else:
            matches = run("", [])
        if matches:
            _store_selection(conn, actor, "SAVED_ITEM", [m["item_id"] for m in matches])
            conn.commit()
        out = []
        for i, m in enumerate(matches):
            out.append({
                "choice": i + 1,
                "item_id": m["item_id"],
                "title": m["title"],
                "content": (m["content"] or "")[:300],
                "tags": m["tags"],
                "kind": (
                    "picture" if m.get("media_type") == "IMAGE"
                    else "document" if m.get("media_type") in ("PDF", "DOCUMENT")
                    else "note"
                ),
                "has_original": bool(m["media_id"]),
                "saved_on": _saved_local_date(m["created_at_utc"], actor.timezone),
            })
        return {"mode": mode, "count": len(out), "matches": out}
    finally:
        conn.close()


def get_saved_item(actor: ActorContext, item_id: str) -> dict:
    """Retrieve one explicitly saved memory and its original attachment when present."""
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT s.item_id,s.title,s.content,s.tags,s.created_at_utc,s.media_id,
                       m.local_path,m.mime_type,m.media_type
                FROM saved_items s
                LEFT JOIN media_objects m ON m.media_id=s.media_id
                WHERE s.item_id=? AND s.space_id IN ({marks}) LIMIT 1""",
            [item_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("saved item not found in your accessible spaces")
        result = {
            "status": "found", "item_id": row["item_id"], "title": row["title"],
            "content": row["content"], "tags": row["tags"],
            "saved_on": _saved_local_date(row["created_at_utc"], actor.timezone),
        }
        if row["local_path"]:
            result["_attachments"] = [{
                "path": row["local_path"], "mime_type": row["mime_type"],
                "kind": "IMAGE" if row["media_type"] == "IMAGE" else "DOCUMENT",
            }]
        return result
    finally:
        conn.close()


def remove_saved_item(actor: ActorContext, item_id: str) -> dict:
    """Soft-remove the explicit-memory index; original archived media is retained."""
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT item_id,media_id FROM saved_items WHERE item_id=? AND space_id IN ({marks})",
            [item_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("saved item not found in your accessible spaces")
        # The media archive is evidence and remains. Only the user's explicit
        # memory index is removed.
        conn.execute("DELETE FROM saved_items WHERE item_id=?", (item_id,))
        conn.commit()
        return {"status": "removed", "item_id": item_id,
                "original_media_retained": bool(row["media_id"])}
    finally:
        conn.close()



def latest_single_selection_context(actor: ActorContext,
                                    created_after_utc: str | None = None) -> dict | None:
    """Return one exact persisted selection only when the newest set has one item."""
    conn = connect()
    try:
        where_after = " AND datetime(created_at_utc)>=datetime(?)" if created_after_utc else ""
        args_a = [actor.user_id, actor.conversation_id, utc_now()]
        args_b = [actor.user_id, actor.conversation_id, utc_now()]
        if created_after_utc:
            args_a.append(created_after_utc)
            args_b.append(created_after_utc)
        rows = conn.execute(
            f"""SELECT selection_kind,items_json,created_at_utc
                  FROM selection_sets
                 WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                       {where_after}
                UNION ALL
                SELECT 'MEDIA' AS selection_kind,items_json,created_at_utc
                  FROM media_selection_sets
                 WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                       {where_after}
                ORDER BY created_at_utc DESC LIMIT 1""",
            args_a + args_b,
        ).fetchall()
        if not rows:
            return None
        ids = json.loads(rows[0]["items_json"])
        if len(ids) != 1:
            return None
        return {"kind": rows[0]["selection_kind"], "id": str(ids[0])}
    finally:
        conn.close()


def get_selection_target(actor: ActorContext, kind: str, target_id: str) -> dict:
    """Retrieve an exact trusted SELECTION context without model re-search."""
    normalized = str(kind or "").upper()
    if normalized == "SAVED_ITEM":
        return get_saved_item(actor, target_id)
    if normalized == "RECEIPT":
        return get_receipt(actor, target_id)
    if normalized == "MEDIA":
        return get_media_original(actor, target_id)
    raise ValueError("unsupported selection context")


def latest_selection_set_context(actor: ActorContext,
                                 created_after_utc: str | None = None) -> dict | None:
    """Return the newest persisted numbered-list set for exact reply binding."""
    now = utc_now()
    after_sql = " AND datetime(created_at_utc)>=datetime(?)" if created_after_utc else ""
    args_a = [actor.user_id, actor.conversation_id, now]
    args_b = [actor.user_id, actor.conversation_id, now]
    args_c = [actor.user_id, actor.conversation_id, now]
    if created_after_utc:
        args_a.append(created_after_utc)
        args_b.append(created_after_utc)
        args_c.append(created_after_utc)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT selection_id,selection_kind,items_json,created_at_utc
                   FROM selection_sets
                  WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                        {after_sql}
                  UNION ALL
                 SELECT selection_id,'MEDIA' AS selection_kind,items_json,created_at_utc
                   FROM media_selection_sets
                  WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                        {after_sql}
                  UNION ALL
                 SELECT selection_id,'PENDING_ITEM' AS selection_kind,items_json,created_at_utc
                   FROM pending_selection_sets
                  WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                        {after_sql}
                  ORDER BY created_at_utc DESC LIMIT 1""",
            args_a + args_b + args_c,
        ).fetchone()
        if not row:
            return None
        ids = json.loads(row["items_json"] or "[]")
        return {
            "kind": str(row["selection_kind"]),
            "id": str(row["selection_id"]),
            "count": len(ids),
        }
    finally:
        conn.close()


def resolve_numbered_choice(actor: ActorContext, choice: int,
                            selection_kind: str | None = None,
                            selection_id: str | None = None) -> dict:
    """Resolve one exact numbered-list item without changing pending lifecycle."""
    index = int(choice)
    if index < 1:
        raise ValueError("choice must be 1 or greater")
    now = utc_now()
    conn = connect()
    try:
        if selection_id:
            kind = str(selection_kind or "").strip().upper()
            if kind in {"RECEIPT", "SAVED_ITEM"}:
                latest = conn.execute(
                    """SELECT selection_kind,items_json FROM selection_sets
                       WHERE selection_id=? AND user_id=? AND conversation_id=?
                         AND expires_at_utc>? AND selection_kind=? LIMIT 1""",
                    (
                        selection_id, actor.user_id, actor.conversation_id,
                        now, kind,
                    ),
                ).fetchone()
            elif kind == "MEDIA":
                latest = conn.execute(
                    """SELECT 'MEDIA' AS selection_kind,items_json
                       FROM media_selection_sets
                       WHERE selection_id=? AND user_id=? AND conversation_id=?
                         AND expires_at_utc>? LIMIT 1""",
                    (selection_id, actor.user_id, actor.conversation_id, now),
                ).fetchone()
            elif kind == "PENDING_ITEM":
                latest = conn.execute(
                    """SELECT 'PENDING_ITEM' AS selection_kind,items_json
                       FROM pending_selection_sets
                       WHERE selection_id=? AND user_id=? AND conversation_id=?
                         AND expires_at_utc>? LIMIT 1""",
                    (selection_id, actor.user_id, actor.conversation_id, now),
                ).fetchone()
            else:
                raise ValueError("unsupported numbered selection context")
        else:
            latest = conn.execute(
                """SELECT selection_id,selection_kind,items_json,created_at_utc
                     FROM selection_sets
                    WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                   UNION ALL
                   SELECT selection_id,'MEDIA' AS selection_kind,items_json,created_at_utc
                     FROM media_selection_sets
                    WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                   UNION ALL
                   SELECT selection_id,'PENDING_ITEM' AS selection_kind,items_json,created_at_utc
                     FROM pending_selection_sets
                    WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
                   ORDER BY created_at_utc DESC LIMIT 1""",
                (
                    actor.user_id, actor.conversation_id, now,
                    actor.user_id, actor.conversation_id, now,
                    actor.user_id, actor.conversation_id, now,
                ),
            ).fetchone()
        if not latest:
            raise ValueError(
                "no numbered receipt, saved-memory, media, or pending-item list is waiting"
            )
        ids = json.loads(latest["items_json"] or "[]")
        if index > len(ids):
            raise ValueError("choice is outside the selected numbered list")
        target = str(ids[index - 1])
        kind = str(latest["selection_kind"]).upper()
        pending = None
        if kind == "PENDING_ITEM":
            pending_row = conn.execute(
                """SELECT * FROM pending_items
                   WHERE item_id=? AND owner_id=? AND conversation_id=?
                     AND status='PENDING' LIMIT 1""",
                (target, actor.user_id, actor.conversation_id),
            ).fetchone()
            if not pending_row:
                raise ValueError("that pending item is no longer unresolved")
            pending = dict(pending_row)
    finally:
        conn.close()

    if kind == "RECEIPT":
        return get_receipt(actor, target)
    if kind == "SAVED_ITEM":
        return get_saved_item(actor, target)
    if kind == "MEDIA":
        return get_media_original(actor, target)
    if kind == "PENDING_ITEM":
        media_id = str((pending or {}).get("media_id") or "")
        if media_id:
            return get_media_original(actor, media_id)
        return {
            "status": "found",
            "kind": (pending or {}).get("kind"),
            "note": (pending or {}).get("note"),
        }
    raise ValueError("unsupported numbered choice type")


def _active_user_phone(conn, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT phone_number FROM user_phone_history WHERE user_id=? AND valid_to_utc IS NULL LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["phone_number"] if row else None


def _family_group_conversation_id() -> str:
    """Read the family-group JID persisted by the Baileys bridge."""
    try:
        data = json.loads((Path(DATA_DIR) / "family_group.json").read_text(encoding="utf-8"))
        value = str(data.get("group_jid") or "").strip()
        if value.endswith("@g.us"):
            return value
    except Exception:
        pass
    raise ValueError("Family Shared group is not paired yet")


def _household_display_name(user_id: str) -> str:
    settings = get_settings()
    if user_id == "USR_HUSBAND":
        return (settings.husband_name or "Husband").strip() or "Husband"
    if user_id == "USR_WIFE":
        return (settings.wife_name or "Wife").strip() or "Wife"
    return "Household member"


def _reminder_recipient_aliases(actor: ActorContext) -> dict[str, str]:
    settings = get_settings()
    aliases = {
        "me": actor.user_id,
        "self": actor.user_id,
        "myself": actor.user_id,
        "husband": "USR_HUSBAND",
        "him": "USR_HUSBAND",
        "wife": "USR_WIFE",
        "her": "USR_WIFE",
        "spouse": "USR_WIFE" if actor.user_id == "USR_HUSBAND" else "USR_HUSBAND",
        "partner": "USR_WIFE" if actor.user_id == "USR_HUSBAND" else "USR_HUSBAND",
    }
    for name, user_id in (
        (settings.husband_name, "USR_HUSBAND"),
        (settings.wife_name, "USR_WIFE"),
    ):
        key = str(name or "").strip().casefold()
        if key:
            aliases[key] = user_id
    return aliases


def _reminder_targets(actor: ActorContext, recipient: str) -> list[str]:
    value = (recipient or "me").strip().casefold()
    if value in {"both", "both of us", "everyone"}:
        return ["USR_HUSBAND", "USR_WIFE"]
    target = _reminder_recipient_aliases(actor).get(value)
    if target:
        return [target]
    raise ValueError("recipient must be me, spouse, a configured household name, husband, wife, or both")


def _trusted_named_reminder_recipient(actor: ActorContext) -> tuple[str, str] | None:
    """Resolve an explicit assignee from the current trusted command only."""
    text = str(getattr(actor, "trusted_text", "") or "").strip()
    if not text:
        return None
    aliases = _reminder_recipient_aliases(actor)
    for alias in sorted(aliases, key=len, reverse=True):
        if re.search(r"\bremind\s+" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            return alias, aliases[alias]
    return None


def _explicit_group_reminder_destination(text: str) -> bool:
    return bool(re.search(
        r"\b(?:in|to)\s+(?:this|the|our)\s+(?:family\s+)?group\b"
        r"|\b(?:put|post|send|broadcast)\b.{0,35}\b(?:family\s+)?group\b",
        str(text or ""),
        re.IGNORECASE,
    ))


def _record_reminder_event(conn, reminder_id: str, event_type: str,
                           previous_state: str | None = None, new_state: str | None = None,
                           previous_due: str | None = None, new_due: str | None = None,
                           note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO reminder_events(
            event_id,reminder_id,event_type,previous_state,new_state,
            previous_due_at_utc,new_due_at_utc,note
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), reminder_id, event_type, previous_state, new_state,
         previous_due, new_due, note),
    )


def _friendly_reminder_time(due_utc: str, tz_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(due_utc).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(tz_name))
        day = local.day
        suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        hour = local.strftime("%I").lstrip("0") or "12"
        return (
            f"{day}{suffix} {local.strftime('%B %Y')}, "
            f"{hour}.{local.strftime('%M')}{local.strftime('%p')}"
        )
    except Exception:
        return str(due_utc)


def create_reminder(actor: ActorContext, task: str, due_local: str,
                    recurrence_rule: str | None = None, shared: bool = False,
                    recipient: str = "me", destination: str = "dm",
                    claimable: bool = False,
                    presence_aware: bool = False,
                    delivery_class: str = "routine",
                    follow_up_after_hours: int = 24) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    due_utc = _parse_event_time(due_local, actor.timezone)
    _validate_reminder_time_intent(actor, due_utc)
    delivery_class = (delivery_class or "routine").strip().lower()
    if delivery_class not in {"routine", "time_critical"}:
        raise ValueError("delivery_class must be routine or time_critical")
    follow_up_after_hours = max(0, min(24 * 30, int(follow_up_after_hours)))
    destination = (destination or "dm").strip().lower()
    if destination not in {"dm", "group"}:
        raise ValueError("destination must be dm or group")

    # A named household assignee overrides the chat where the instruction was
    # written. This prevents "remind Luhgen ..." in Family Shared from becoming
    # a claimable group reminder merely because the command originated there.
    trusted_assignee = _trusted_named_reminder_recipient(actor)
    if (
        actor.conversation_type == "GROUP"
        and trusted_assignee
        and not _explicit_group_reminder_destination(getattr(actor, "trusted_text", ""))
    ):
        alias, target_user = trusted_assignee
        destination = "dm"
        recipient = alias
        claimable = False
        display = _household_display_name(target_user)
        task = re.sub(
            r"^\s*" + re.escape(display) + r"\s+to\s+",
            "",
            str(task or ""),
            flags=re.IGNORECASE,
        ).strip() or task

    # Family Shared one-shot reminders are claimable by design. Recurring
    # reminders keep the existing non-claimable restriction until each
    # occurrence has its own claim identity.
    if destination == "group" and not recurrence_rule:
        claimable = True
    if claimable and destination != "group":
        raise ValueError("claimable reminders must use destination=group")
    if claimable and recurrence_rule:
        raise ValueError("claimable reminders currently support one occurrence at a time")
    conn = connect()
    try:
        targets = [actor.user_id] if destination == "group" else _reminder_targets(actor, recipient)
        created = []
        for target_user in targets:
            action_key = actor.action_key if len(targets) == 1 else f"{actor.action_key}:{target_user}"
            existing = conn.execute(
                "SELECT reminder_id,task_text,due_at_utc,owner_id FROM reminders WHERE action_key=?",
                (action_key,),
            ).fetchone()
            if existing:
                created.append({"status": "already_created", **dict(existing)})
                continue

            if destination == "group":
                conversation_id = _family_group_conversation_id()
            else:
                phone = _active_user_phone(conn, target_user)
                if not phone:
                    raise ValueError("target household member has no configured WhatsApp number")
                conversation_id = phone.replace("+", "") + "@s.whatsapp.net"

            if destination == "group" or target_user != actor.user_id or len(targets) > 1:
                space = "FAMILY_SHARED"
            else:
                space = scope_policy.resolve_new_write_space(
                    actor, requested_shared=shared
                )
            if space not in actor.allowed_spaces:
                raise PermissionError("requested reminder space is not accessible")

            rid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO reminders(
                    reminder_id,action_key,source_message_id,owner_id,space_id,conversation_id,
                    task_text,due_at_utc,timezone_name,recurrence_rule,
                    presence_aware,delivery_class,follow_up_after_hours,claimable
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, action_key, actor.source_message_id, target_user, space,
                 conversation_id, task[:500], due_utc, actor.timezone,
                 (recurrence_rule or "")[:500] or None,
                 1 if presence_aware else 0, delivery_class, follow_up_after_hours,
                 1 if claimable else 0),
            )
            _record_reminder_event(
                conn, rid, "CREATED", None, "OPEN", None, due_utc,
                f"recipient={recipient}; destination={destination}; claimable={bool(claimable)}; delivery_class={delivery_class}"
            )

            # When an assignment originates outside the assignee's own DM,
            # push an immediate private acknowledgement. The due reminder will
            # later use this same DM conversation.
            if destination == "dm" and (
                actor.conversation_type == "GROUP" or target_user != actor.user_id
            ):
                friendly_due = _friendly_reminder_time(due_utc, actor.timezone)
                ack = (
                    f"Reminder assigned to you: {task}. "
                    f"I’ll remind you at {friendly_due}."
                )
                conn.execute(
                    """INSERT INTO outbound_messages(
                           outbound_id,source_message_id,conversation_id,kind,text_body,
                           context_kind,context_id
                       ) VALUES(?,?,?,'TEXT',?,'REMINDER_ASSIGNED',?)""",
                    (str(uuid.uuid4()), actor.source_message_id, conversation_id, ack, rid),
                )
                try:
                    import ha_mobile
                    ha_mobile.queue_assigned_ack(
                        conn, target_user, rid, task, friendly_due
                    )
                except Exception:
                    # Phone notifications are an additional delivery surface;
                    # a misconfigured device must never prevent the durable
                    # WhatsApp reminder from being created.
                    pass

            created.append({
                "status": "created", "reminder_id": rid, "task": task, "due_at_utc": due_utc,
                "timezone": actor.timezone, "recurrence_rule": recurrence_rule,
                "recipient_user_id": target_user, "destination": destination,
                "conversation_id": conversation_id, "space": space,
                "claimable": bool(claimable),
                "presence_aware": bool(presence_aware), "delivery_class": delivery_class,
                "follow_up_after_hours": follow_up_after_hours,
            })
        conn.commit()
        return created[0] if len(created) == 1 else {"status": "created", "reminders": created}
    finally:
        conn.close()

def list_reminders(actor: ActorContext, include_completed: bool = False, limit: int = 20) -> dict:
    marks, spaces = _spaces_sql(actor)
    states = "" if include_completed else " AND status IN ('OPEN','DUE','DEFERRED')"
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT reminder_id,owner_id,task_text,due_at_utc,timezone_name,
                       recurrence_rule,status,space_id,conversation_id,claimable,
                       claimed_by_user_id,claimed_at_utc
                FROM reminders WHERE space_id IN ({marks}) {states}
                ORDER BY due_at_utc ASC LIMIT ?""",
            spaces + [max(1, min(50, int(limit)))],
        ).fetchall()
        reminders = []
        tz = ZoneInfo(actor.timezone)
        for row in rows:
            item = dict(row)
            try:
                due = datetime.fromisoformat(str(item["due_at_utc"]).replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                item["due_local"] = due.astimezone(tz).isoformat()
            except Exception:
                item["due_local"] = None
            pending = conn.execute(
                """SELECT h.to_user_id,u.display_name
                   FROM reminder_handoffs h
                   LEFT JOIN users u ON u.user_id=h.to_user_id
                   WHERE h.reminder_id=? AND h.status='PENDING'
                   ORDER BY h.created_at_utc DESC LIMIT 1""",
                (row["reminder_id"],),
            ).fetchone()
            item["handoff_pending_to_user_id"] = pending["to_user_id"] if pending else None
            item["handoff_pending_to"] = pending["display_name"] if pending else None
            reminders.append(item)
        return {"reminders": reminders}
    finally:
        conn.close()


def update_reminder(actor: ActorContext, reminder_id: str, status: str = "open",
                    new_due_local: str | None = None,
                    snooze_minutes: int | None = None,
                    snooze_until_local: str | None = None) -> dict:
    status_map = {
        "ack": "ACK", "acknowledged": "ACK", "complete": "COMP", "completed": "COMP",
        "cancel": "CANC", "cancelled": "CANC", "defer": "DEFERRED", "deferred": "DEFERRED",
        "open": "OPEN", "snooze": "OPEN", "snoozed": "OPEN",
    }
    event_map = {
        "ACK": "ACKNOWLEDGED", "COMP": "COMPLETED", "CANC": "CANCELLED",
        "DEFERRED": "DEFERRED", "OPEN": "RESCHEDULED",
    }
    if snooze_minutes is not None and snooze_until_local:
        raise ValueError("provide either snooze_minutes or snooze_until_local, not both")
    resolved = status_map.get((status or "open").lower(), (status or "open").upper())
    if resolved == "DUE":
        raise ValueError("DUE is scheduler-owned; use snooze/reschedule/open instead")
    if resolved not in {"OPEN","ACK","DEFERRED","COMP","CANC"}:
        raise ValueError("unsupported reminder status")
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT reminder_id,status,due_at_utc,owner_id,task_text,claimable,
                       claimed_by_user_id,claimed_at_utc
                FROM reminders
                WHERE reminder_id=? AND space_id IN ({marks})""",
            [reminder_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("reminder not found in your accessible spaces")
        previous_state = row["status"]
        previous_due = row["due_at_utc"]
        snooze_note = None
        if snooze_minutes is not None:
            minutes = max(1, min(60 * 24 * 30, int(snooze_minutes)))
            new_due = (runtime_clock.now_utc() + timedelta(minutes=minutes)).isoformat()
            resolved = "OPEN"
            snooze_note = f"snoozed_from_now={minutes}m"
        elif snooze_until_local:
            new_due = _parse_event_time(snooze_until_local, actor.timezone)
            _validate_reminder_time_intent(actor, new_due)
            resolved = "OPEN"
            snooze_note = "snoozed_until_local"
        else:
            new_due = _parse_event_time(new_due_local, actor.timezone) if new_due_local else previous_due
            if new_due_local:
                _validate_reminder_time_intent(actor, new_due)
        acknowledged = utc_now() if resolved == "ACK" else None
        claim_clear_event = None
        if resolved == "COMP" and row["claimed_by_user_id"]:
            claim_clear_event = "CLEARED_COMPLETED"
        elif resolved == "CANC" and row["claimed_by_user_id"]:
            claim_clear_event = "CLEARED_CANCELLED"

        if resolved == "ACK":
            # Seen/acknowledged is metadata, not a lifecycle state. Keep the
            # reminder open/due so pins, listing and escalation continue.
            conn.execute(
                """UPDATE reminders
                   SET due_at_utc=?,acknowledged_at_utc=?,
                       seen_at_utc=?,seen_by_user_id=?
                   WHERE reminder_id=?""",
                (new_due, acknowledged, acknowledged, actor.user_id, reminder_id),
            )
        elif resolved in {"COMP", "CANC"}:
            conn.execute(
                """UPDATE reminders SET status=?,due_at_utc=?,next_delivery_at_utc=NULL,
                   defer_reason=NULL,claimed_by_user_id=NULL,claimed_at_utc=NULL
                   WHERE reminder_id=?""",
                (resolved, new_due, reminder_id),
            )
        else:
            conn.execute(
                """UPDATE reminders SET status=?,due_at_utc=?,next_delivery_at_utc=NULL,
                   defer_reason=NULL WHERE reminder_id=?""",
                (resolved, new_due, reminder_id),
            )
        if resolved in {"COMP", "CANC"}:
            conn.execute(
                """UPDATE reminder_handoffs
                   SET status='CANCELLED',cancelled_at_utc=?
                   WHERE reminder_id=? AND status='PENDING'""",
                (utc_now(), reminder_id),
            )
        if claim_clear_event:
            conn.execute(
                """INSERT INTO reminder_claim_events(
                       claim_event_id,reminder_id,actor_user_id,event_type,note
                   ) VALUES(?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), reminder_id, actor.user_id,
                    claim_clear_event, "claim cleared by reminder lifecycle",
                ),
            )
        _record_reminder_event(
            conn, reminder_id,
            event_map[resolved],
            previous_state,
            previous_state if resolved == "ACK" else resolved,
            previous_due, new_due, snooze_note,
        )
        if resolved in {"ACK", "COMP", "CANC"} or snooze_note:
            try:
                import ha_mobile
                notify_user = str(row["claimed_by_user_id"] or row["owner_id"])
                ha_mobile.queue_state(
                    conn, notify_user, reminder_id, row["task_text"],
                    "OPEN" if snooze_note else resolved,
                    event_key=(
                        f"state:{reminder_id}:{resolved}:"
                        f"{actor.source_message_id or utc_now()}"
                    ),
                )
            except Exception:
                pass
        conn.commit()
        return {
            "status": "seen" if resolved == "ACK" else "updated",
            "reminder_id": reminder_id,
            "state": previous_state if resolved == "ACK" else resolved,
            "seen": resolved == "ACK",
            "due_at_utc": new_due,
        }
    finally:
        conn.close()


def reminder_history(actor: ActorContext, reminder_id: str | None = None,
                      limit: int = 50) -> dict:
    marks, spaces = _spaces_sql(actor)
    bounded = max(1, min(200, int(limit)))
    tz = ZoneInfo(actor.timezone)

    def local_iso(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(tz).isoformat()
        except Exception:
            return None

    def event_view(row, include_task=False):
        out = {
            "event_type": row["event_type"],
            "previous_state": row["previous_state"],
            "new_state": row["new_state"],
            "previous_due_local": local_iso(row["previous_due_at_utc"]),
            "new_due_local": local_iso(row["new_due_at_utc"]),
            "note": row["note"],
            "created_local": local_iso(row["created_at_utc"]),
        }
        if "reminder_id" in row.keys():
            out["reminder_id"] = row["reminder_id"]
        if include_task:
            out["task"] = row["task_text"]
        return out

    conn = connect()
    try:
        if reminder_id:
            owned = conn.execute(
                f"SELECT task_text FROM reminders WHERE reminder_id=? AND space_id IN ({marks})",
                [reminder_id] + spaces,
            ).fetchone()
            if not owned:
                raise PermissionError("reminder not found in your accessible spaces")
            rows = conn.execute(
                """SELECT event_type,previous_state,new_state,previous_due_at_utc,new_due_at_utc,
                          note,created_at_utc
                   FROM reminder_events WHERE reminder_id=? ORDER BY created_at_utc""",
                (reminder_id,),
            ).fetchall()
            claim_rows = conn.execute(
                """SELECT actor_user_id,event_type,provider_message_id,reaction_text,
                          note,created_at_utc
                   FROM reminder_claim_events
                   WHERE reminder_id=? ORDER BY created_at_utc""",
                (reminder_id,),
            ).fetchall()
            history = [event_view(r) for r in rows]
            claim_history = [
                {
                    "actor_user_id": r["actor_user_id"],
                    "event_type": r["event_type"],
                    "reaction_text": r["reaction_text"],
                    "note": r["note"],
                    "created_local": local_iso(r["created_at_utc"]),
                }
                for r in claim_rows
            ]
            status_flow = " → ".join(
                r["event_type"].replace("_", " ").title() for r in rows
                if r["event_type"] not in {"CREATED"}
            ) or "Created"
            latest_due = next(
                (item["new_due_local"] for item in reversed(history) if item["new_due_local"]),
                None,
            )
            display = {
                "title": "Reminder History",
                "items": [{
                    "number": 1,
                    "task": owned["task_text"],
                    "status": status_flow,
                    "due_local": latest_due,
                }],
            }
            return {
                "history": history,
                "claim_history": claim_history,
                "display": display,
            }

        rows = conn.execute(
            f"""SELECT e.reminder_id,r.task_text,e.event_type,e.previous_state,e.new_state,
                       e.previous_due_at_utc,e.new_due_at_utc,e.note,e.created_at_utc
                FROM reminder_events e
                JOIN reminders r ON r.reminder_id=e.reminder_id
                WHERE r.space_id IN ({marks})
                ORDER BY e.created_at_utc DESC LIMIT ?""",
            spaces + [bounded],
        ).fetchall()
        history = [event_view(r, include_task=True) for r in rows]
        grouped = []
        seen = set()
        for row in rows:
            rid = row["reminder_id"]
            if rid in seen:
                continue
            seen.add(rid)
            events = [x for x in rows if x["reminder_id"] == rid]
            flow = " → ".join(
                x["event_type"].replace("_", " ").title()
                for x in reversed(events)
                if x["event_type"] != "CREATED"
            ) or "Created"
            latest_due = next(
                (local_iso(x["new_due_at_utc"]) for x in events if x["new_due_at_utc"]),
                None,
            )
            grouped.append({
                "number": len(grouped) + 1,
                "task": row["task_text"],
                "status": flow,
                "due_local": latest_due,
            })
        return {
            "history": history,
            "aggregate": True,
            "display": {"title": "Reminder History", "items": grouped},
        }
    finally:
        conn.close()

def _handoff_recipient_user(actor: ActorContext, recipient: str) -> str:
    value = str(recipient or "").strip().casefold()
    target = _reminder_recipient_aliases(actor).get(value)
    if target:
        return target
    raise ValueError("recipient must be spouse, a configured household name, wife, husband, or me")


def request_reminder_handoff(actor: ActorContext, reminder_id: str,
                             recipient: str) -> dict:
    """Ask another household member to accept a claimed Family Shared reminder.

    The current claimant remains responsible until the recipient reacts to the
    DM handoff request. A missing recipient phone never changes ownership.
    """
    target_user = _handoff_recipient_user(actor, recipient)
    if target_user == actor.user_id:
        return {"status": "already_claimant", "reminder_id": reminder_id}

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT reminder_id,task_text,status,space_id,claimable,
                      claimed_by_user_id
               FROM reminders WHERE reminder_id=?""",
            (reminder_id,),
        ).fetchone()
        if not row or row["space_id"] != "FAMILY_SHARED":
            conn.rollback()
            raise PermissionError("handoff applies only to an accessible Family Shared reminder")
        if row["status"] in {"COMP", "CANC"}:
            conn.rollback()
            return {"status": "closed", "reminder_id": reminder_id}
        if row["claimed_by_user_id"] != actor.user_id:
            conn.rollback()
            raise PermissionError("only the current claimant can hand off this reminder")

        existing = conn.execute(
            """SELECT handoff_id,to_user_id FROM reminder_handoffs
               WHERE reminder_id=? AND status='PENDING'
               ORDER BY created_at_utc DESC LIMIT 1""",
            (reminder_id,),
        ).fetchone()
        if existing:
            conn.rollback()
            return {
                "status": "handoff_pending",
                "reminder_id": reminder_id,
                "handoff_id": existing["handoff_id"],
                "to_user_id": existing["to_user_id"],
                "claimant_unchanged": True,
            }

        phone = _active_user_phone(conn, target_user)
        if not phone:
            conn.rollback()
            return {
                "status": "recipient_not_configured",
                "reminder_id": reminder_id,
                "to_user_id": target_user,
                "claimant_unchanged": True,
            }

        handoff_id = str(uuid.uuid4())
        dm_conversation = phone.replace("+", "") + "@s.whatsapp.net"
        conn.execute(
            """INSERT INTO reminder_handoffs(
                   handoff_id,reminder_id,from_user_id,to_user_id,conversation_id,status
               ) VALUES(?,?,?,?,?,'PENDING')""",
            (handoff_id, reminder_id, actor.user_id, target_user, dm_conversation),
        )
        conn.execute(
            """INSERT INTO outbound_messages(
                   outbound_id,conversation_id,kind,text_body,context_kind,context_id
               ) VALUES(?,?,'TEXT',?,'REMINDER_HANDOFF',?)""",
            (
                str(uuid.uuid4()), dm_conversation,
                f"Your spouse asked you to take over this reminder: {row['task_text']}\nReact with any emoji to accept it.",
                handoff_id,
            ),
        )
        try:
            import ha_mobile
            ha_mobile.queue_handoff(
                conn, target_user, reminder_id, handoff_id, row["task_text"]
            )
        except Exception:
            pass
        conn.commit()
        return {
            "status": "handoff_requested",
            "reminder_id": reminder_id,
            "handoff_id": handoff_id,
            "to_user_id": target_user,
            "claimant_unchanged": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_reminder_tx(
    conn, actor: ActorContext, reminder_id: str, *,
    source: str, provider_message_id: str | None = None,
    reaction_text: str | None = None,
) -> dict:
    """Atomic first-claim core shared by WhatsApp reactions and HA actions."""
    if "FAMILY_SHARED" not in actor.allowed_spaces:
        raise PermissionError("reminder not accessible to this household user")
    row = conn.execute(
        """SELECT reminder_id,task_text,status,space_id,claimable,
                  claimed_by_user_id,follow_up_after_hours
           FROM reminders WHERE reminder_id=?""",
        (reminder_id,),
    ).fetchone()
    if not row or row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("reminder not accessible from Family Shared")
    if not int(row["claimable"] or 0):
        return {"status": "not_claimable", "reminder_id": reminder_id}
    if row["status"] in {"COMP", "CANC"}:
        return {"status": "closed", "reminder_id": reminder_id}
    if row["claimed_by_user_id"]:
        winner = str(row["claimed_by_user_id"])
        return {
            "status": "already_claimed", "reminder_id": reminder_id,
            "claimed_by_user_id": winner,
            "claimed_by_name": _household_display_name(winner),
            "claimed_by_me": winner == actor.user_id,
        }

    now = runtime_clock.now_utc()
    follow_hours = max(1, int(row["follow_up_after_hours"] or 24))
    next_delivery = (now + timedelta(hours=follow_hours)).isoformat()
    updated = conn.execute(
        """UPDATE reminders
           SET claimed_by_user_id=?,claimed_at_utc=?,
               claimant_follow_up_at_utc=NULL,family_resurfaced_at_utc=NULL,
               next_delivery_at_utc=?
           WHERE reminder_id=? AND claimed_by_user_id IS NULL
             AND claimable=1 AND status NOT IN ('COMP','CANC')""",
        (actor.user_id, now.isoformat(), next_delivery, reminder_id),
    )
    if updated.rowcount != 1:
        winner = conn.execute(
            "SELECT claimed_by_user_id FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
        winner_id = str(winner["claimed_by_user_id"]) if winner and winner["claimed_by_user_id"] else None
        return {
            "status": "already_claimed", "reminder_id": reminder_id,
            "claimed_by_user_id": winner_id,
            "claimed_by_name": _household_display_name(winner_id) if winner_id else None,
            "claimed_by_me": winner_id == actor.user_id,
        }
    conn.execute(
        """INSERT INTO reminder_claim_events(
               claim_event_id,reminder_id,actor_user_id,event_type,
               provider_message_id,reaction_text,note
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), reminder_id, actor.user_id, "CLAIMED",
            provider_message_id, str(reaction_text or "")[:32],
            f"{source}: first valid claim won atomically",
        ),
    )
    return {
        "status": "claimed", "reminder_id": reminder_id,
        "task": row["task_text"], "claimed_by_user_id": actor.user_id,
        "next_claimant_follow_up_at_utc": next_delivery,
    }


def claim_reminder(actor: ActorContext, reminder_id: str, source: str = "DIRECT") -> dict:
    """Claim a Family Shared reminder through a trusted non-WhatsApp control surface."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _claim_reminder_tx(
            conn, actor, reminder_id, source=str(source or "DIRECT")
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _accept_reminder_handoff_tx(
    conn, actor: ActorContext, handoff_id: str, *,
    source: str, provider_message_id: str | None = None,
    reaction_text: str | None = None,
) -> dict:
    handoff = conn.execute(
        "SELECT * FROM reminder_handoffs WHERE handoff_id=?",
        (handoff_id,),
    ).fetchone()
    if not handoff or handoff["status"] != "PENDING":
        return {"status": "handoff_not_pending"}
    if handoff["to_user_id"] != actor.user_id:
        raise PermissionError("this reminder handoff was not addressed to you")
    reminder = conn.execute(
        """SELECT reminder_id,task_text,status,claimed_by_user_id,follow_up_after_hours
           FROM reminders WHERE reminder_id=?""",
        (handoff["reminder_id"],),
    ).fetchone()
    if not reminder or reminder["status"] in {"COMP", "CANC"}:
        conn.execute(
            """UPDATE reminder_handoffs SET status='CANCELLED',cancelled_at_utc=?
               WHERE handoff_id=? AND status='PENDING'""",
            (utc_now(), handoff_id),
        )
        return {"status": "closed", "reminder_id": handoff["reminder_id"]}
    if reminder["claimed_by_user_id"] != handoff["from_user_id"]:
        return {
            "status": "claimant_changed",
            "reminder_id": handoff["reminder_id"],
            "claimant_unchanged": True,
        }

    now = runtime_clock.now_utc()
    next_delivery = (
        now + timedelta(hours=max(1, int(reminder["follow_up_after_hours"] or 24)))
    ).isoformat()
    updated = conn.execute(
        """UPDATE reminders
           SET claimed_by_user_id=?,claimed_at_utc=?,
               claimant_follow_up_at_utc=NULL,family_resurfaced_at_utc=NULL,
               next_delivery_at_utc=?
           WHERE reminder_id=? AND claimed_by_user_id=?
             AND status NOT IN ('COMP','CANC')""",
        (
            actor.user_id, now.isoformat(), next_delivery,
            reminder["reminder_id"], handoff["from_user_id"],
        ),
    )
    if updated.rowcount != 1:
        return {"status": "claimant_changed", "reminder_id": reminder["reminder_id"]}
    conn.execute(
        """UPDATE reminder_handoffs
           SET status='ACCEPTED',accepted_at_utc=?
           WHERE handoff_id=? AND status='PENDING'""",
        (now.isoformat(), handoff_id),
    )
    conn.execute(
        """INSERT INTO reminder_claim_events(
               claim_event_id,reminder_id,actor_user_id,event_type,
               provider_message_id,reaction_text,note
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), reminder["reminder_id"], actor.user_id, "CLAIMED",
            provider_message_id, str(reaction_text or "")[:32],
            f"{source}: handoff accepted from {handoff['from_user_id']}",
        ),
    )
    from_phone = _active_user_phone(conn, handoff["from_user_id"])
    if from_phone:
        from_dm = from_phone.replace("+", "") + "@s.whatsapp.net"
        name = _household_display_name(actor.user_id)
        conn.execute(
            """INSERT INTO outbound_messages(
                   outbound_id,conversation_id,kind,text_body,context_kind,context_id
               ) VALUES(?,?,'TEXT',?,'REMINDER_HANDOFF_ACCEPTED',?)""",
            (
                str(uuid.uuid4()), from_dm,
                f"{name} has taken over: {reminder['task_text']}",
                reminder["reminder_id"],
            ),
        )
    try:
        import ha_mobile
        ha_mobile.queue_claim_confirmation(
            conn, actor.user_id, reminder["reminder_id"], reminder["task_text"],
            event_key=f"handoff-accepted:{handoff_id}",
        )
    except Exception:
        pass
    return {
        "status": "handoff_accepted",
        "reminder_id": reminder["reminder_id"],
        "claimed_by_user_id": actor.user_id,
        "from_user_id": handoff["from_user_id"],
        "claimant_notified": bool(from_phone),
    }


def accept_reminder_handoff(
    actor: ActorContext, handoff_id: str, source: str = "DIRECT"
) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _accept_reminder_handoff_tx(
            conn, actor, handoff_id, source=str(source or "DIRECT")
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def decline_reminder_handoff(actor: ActorContext, handoff_id: str) -> dict:
    """Decline a pending handoff without changing the current claimant."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        handoff = conn.execute(
            "SELECT * FROM reminder_handoffs WHERE handoff_id=?",
            (handoff_id,),
        ).fetchone()
        if not handoff or handoff["status"] != "PENDING":
            conn.rollback()
            return {"status": "handoff_not_pending"}
        if handoff["to_user_id"] != actor.user_id:
            conn.rollback()
            raise PermissionError("this reminder handoff was not addressed to you")
        reminder = conn.execute(
            "SELECT reminder_id,task_text FROM reminders WHERE reminder_id=?",
            (handoff["reminder_id"],),
        ).fetchone()
        now = utc_now()
        conn.execute(
            """UPDATE reminder_handoffs
               SET status='CANCELLED',cancelled_at_utc=?
               WHERE handoff_id=? AND status='PENDING'""",
            (now, handoff_id),
        )
        from_phone = _active_user_phone(conn, handoff["from_user_id"])
        if from_phone and reminder:
            from_dm = from_phone.replace("+", "") + "@s.whatsapp.net"
            name = _household_display_name(actor.user_id)
            conn.execute(
                """INSERT INTO outbound_messages(
                       outbound_id,conversation_id,kind,text_body,context_kind,context_id
                   ) VALUES(?,?,'TEXT',?,'REMINDER_HANDOFF_DECLINED',?)""",
                (
                    str(uuid.uuid4()), from_dm,
                    f"{name} declined the handoff: {reminder['task_text']}",
                    reminder["reminder_id"],
                ),
            )
        conn.commit()
        return {
            "status": "handoff_declined",
            "handoff_id": handoff_id,
            "reminder_id": handoff["reminder_id"],
            "claimant_unchanged": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_reminder_from_reaction(
    actor: ActorContext, provider_message_id: str, reaction_text: str | None
) -> dict:
    """Process reminder-claim and handoff-acceptance reactions atomically."""
    reaction = str(reaction_text or "")
    if not reaction.strip():
        return {"status": "ignored_reaction_removal"}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        outbound = conn.execute(
            """SELECT outbound_id,context_kind,context_id,provider_message_id
               FROM outbound_messages
               WHERE conversation_id=? AND provider_message_id=?
                 AND context_kind IN (
                     'REMINDER_SETUP','REMINDER_INITIAL','REMINDER_FOLLOWUP',
                     'REMINDER_CLAIMANT_FOLLOWUP','REMINDER_FAMILY_RESURFACE',
                     'REMINDER_INITIATOR_ESCALATION','REMINDER_HANDOFF'
                 )
               ORDER BY delivered_at_utc DESC,created_at_utc DESC LIMIT 1""",
            (actor.conversation_id, provider_message_id),
        ).fetchone()
        if not outbound:
            candidates = conn.execute(
                """SELECT outbound_id,context_kind,context_id,provider_message_id
                   FROM outbound_messages
                   WHERE conversation_id=? AND delivery_status='SENT'
                     AND context_kind IN (
                         'REMINDER_SETUP','REMINDER_INITIAL','REMINDER_FOLLOWUP',
                         'REMINDER_CLAIMANT_FOLLOWUP','REMINDER_FAMILY_RESURFACE',
                         'REMINDER_INITIATOR_ESCALATION','REMINDER_HANDOFF'
                     )
                   ORDER BY delivered_at_utc DESC,created_at_utc DESC""",
                (actor.conversation_id,),
            ).fetchall()
            wanted = str(provider_message_id or "")
            for candidate in candidates:
                expected = (
                    "ALEX"
                    + hashlib.sha256(str(candidate["outbound_id"]).encode("utf-8"))
                    .hexdigest().upper()[:28]
                )
                if wanted and wanted in {str(candidate["provider_message_id"] or ""), expected}:
                    outbound = candidate
                    break
            if not outbound and actor.conversation_type != "GROUP":
                handoff_candidates = conn.execute(
                    """SELECT o.outbound_id,o.context_kind,o.context_id,o.provider_message_id
                       FROM outbound_messages o
                       JOIN reminder_handoffs h ON h.handoff_id=o.context_id
                       WHERE o.delivery_status='SENT'
                         AND o.context_kind='REMINDER_HANDOFF'
                         AND h.to_user_id=? AND h.status='PENDING'
                       ORDER BY o.delivered_at_utc DESC,o.created_at_utc DESC""",
                    (actor.user_id,),
                ).fetchall()
                for candidate in handoff_candidates:
                    expected = (
                        "ALEX"
                        + hashlib.sha256(str(candidate["outbound_id"]).encode("utf-8"))
                        .hexdigest().upper()[:28]
                    )
                    if wanted and wanted in {str(candidate["provider_message_id"] or ""), expected}:
                        outbound = candidate
                        break
        if not outbound or not outbound["context_id"]:
            conn.rollback()
            return {"status": "not_a_reminder_message"}

        if outbound["context_kind"] == "REMINDER_HANDOFF":
            if actor.conversation_type == "GROUP":
                conn.rollback()
                return {"status": "not_a_reminder_message"}
            result = _accept_reminder_handoff_tx(
                conn, actor, str(outbound["context_id"]),
                source="WHATSAPP_REACTION",
                provider_message_id=provider_message_id,
                reaction_text=reaction,
            )
            conn.commit()
            return result

        reminder_id = str(outbound["context_id"])
        context_kind = str(outbound["context_kind"] or "")
        if context_kind == "REMINDER_SETUP":
            if actor.conversation_type != "GROUP":
                conn.rollback()
                return {"status": "ignored_direct_reminder_reaction"}
            reminder = conn.execute(
                """SELECT due_at_utc,status FROM reminders WHERE reminder_id=?""",
                (reminder_id,),
            ).fetchone()
            if not reminder:
                conn.rollback()
                return {"status": "not_a_reminder_message"}
            due = datetime.fromisoformat(str(reminder["due_at_utc"]).replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if runtime_clock.now_utc() >= due:
                conn.rollback()
                return {"status": "too_late_to_claim", "reminder_id": reminder_id}
            result = _claim_reminder_tx(
                conn, actor, reminder_id,
                source="WHATSAPP_REACTION",
                provider_message_id=provider_message_id,
                reaction_text=reaction,
            )
            if result.get("status") == "claimed":
                # A pre-due claim changes ownership/routing, not the due time.
                # Clear the old post-due follow-up timestamp so the original
                # due alert still fires on time to the claimant's DM.
                conn.execute(
                    """UPDATE reminders SET next_delivery_at_utc=NULL
                       WHERE reminder_id=?""",
                    (reminder_id,),
                )
                result["next_claimant_follow_up_at_utc"] = None
            conn.commit()
            return result

        # Compatibility for any already-bound early reminder card: if it is
        # still OPEN it is necessarily pre-due from the state machine's point
        # of view, so a Family Shared reaction may claim it. Normal scheduler
        # delivery sets DUE before REMINDER_INITIAL is sent, so live post-due
        # reactions still fall through to ACK below.
        early = conn.execute(
            """SELECT status FROM reminders WHERE reminder_id=?""",
            (reminder_id,),
        ).fetchone()
        if (
            actor.conversation_type == "GROUP"
            and early
            and early["status"] == "OPEN"
        ):
            result = _claim_reminder_tx(
                conn, actor, reminder_id,
                source="WHATSAPP_REACTION",
                provider_message_id=provider_message_id,
                reaction_text=reaction,
            )
            if result.get("status") == "claimed":
                conn.execute(
                    "UPDATE reminders SET next_delivery_at_utc=NULL WHERE reminder_id=?",
                    (reminder_id,),
                )
                result["next_claimant_follow_up_at_utc"] = None
            conn.commit()
            return result

        # The reminder has already fired. Reactions after due are completion
        # or seen signals; they are never late claims.
        reminder = conn.execute(
            """SELECT reminder_id,status,task_text,owner_id,claimed_by_user_id
               FROM reminders WHERE reminder_id=?""",
            (reminder_id,),
        ).fetchone()
        if not reminder:
            conn.rollback()
            return {"status": "not_a_reminder_message"}
        if reminder["status"] in {"COMP", "CANC"}:
            conn.rollback()
            return {"status": "closed", "reminder_id": reminder_id}
        if actor.conversation_type != "GROUP" and reminder["status"] != "DUE":
            conn.rollback()
            return {"status": "ignored_direct_reminder_reaction"}

        # A claimed reminder delivered in DM belongs to its claimant. Group
        # resurfacing remains visible to both household members, and the
        # initiator may explicitly complete it there.
        if (
            actor.conversation_type != "GROUP"
            and reminder["claimed_by_user_id"]
            and str(reminder["claimed_by_user_id"]) != actor.user_id
        ):
            conn.rollback()
            return {"status": "not_assigned_to_you", "reminder_id": reminder_id}

        normalized_reaction = reaction.replace("\ufe0f", "")
        completion = normalized_reaction in {"✅", "✔", "☑"}
        now = utc_now()

        if completion:
            previous_state = str(reminder["status"])
            had_claim = str(reminder["claimed_by_user_id"] or "") or None
            conn.execute(
                """UPDATE reminders
                   SET status='COMP',next_delivery_at_utc=NULL,defer_reason=NULL,
                       acknowledged_at_utc=COALESCE(acknowledged_at_utc,?),
                       seen_at_utc=COALESCE(seen_at_utc,?),seen_by_user_id=?,
                       claimed_by_user_id=NULL,claimed_at_utc=NULL
                   WHERE reminder_id=?""",
                (now, now, actor.user_id, reminder_id),
            )
            conn.execute(
                """UPDATE reminder_handoffs
                   SET status='CANCELLED',cancelled_at_utc=?
                   WHERE reminder_id=? AND status='PENDING'""",
                (now, reminder_id),
            )
            if had_claim:
                conn.execute(
                    """INSERT INTO reminder_claim_events(
                           claim_event_id,reminder_id,actor_user_id,event_type,
                           provider_message_id,reaction_text,note
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), reminder_id, actor.user_id,
                        "CLEARED_COMPLETED", provider_message_id, reaction,
                        "completed by explicit post-due completion reaction",
                    ),
                )
            _record_reminder_event(
                conn, reminder_id, "COMPLETED", previous_state, "COMP",
                note="explicit completion reaction after reminder delivery",
            )
            try:
                import ha_mobile
                notify_user = str(had_claim or reminder["owner_id"])
                ha_mobile.queue_state(
                    conn, notify_user, reminder_id, reminder["task_text"], "COMP",
                    event_key=f"wa-done:{provider_message_id}",
                )
            except Exception:
                pass
            conn.commit()
            return {
                "status": "completed", "reminder_id": reminder_id,
                "state": "COMP", "task": reminder["task_text"],
            }

        # Any other post-due reaction only means seen/acknowledged. It must not
        # remove the reminder from open lists, stop escalation, or unpin it.
        previous_state = str(reminder["status"])
        conn.execute(
            """UPDATE reminders
               SET acknowledged_at_utc=?,seen_at_utc=?,seen_by_user_id=?
               WHERE reminder_id=?""",
            (now, now, actor.user_id, reminder_id),
        )
        _record_reminder_event(
            conn, reminder_id, "ACKNOWLEDGED", previous_state, previous_state,
            note="non-terminal WhatsApp reaction after reminder delivery",
        )
        try:
            import ha_mobile
            notify_user = str(reminder["claimed_by_user_id"] or reminder["owner_id"])
            ha_mobile.queue_state(
                conn, notify_user, reminder_id, reminder["task_text"], previous_state,
                event_key=f"wa-seen:{provider_message_id}",
            )
        except Exception:
            pass
        conn.commit()
        return {
            "status": "seen", "reminder_id": reminder_id,
            "state": previous_state, "task": reminder["task_text"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_reminder_claim(actor: ActorContext, reminder_id: str) -> dict:
    """Release a claim explicitly; deleting the reaction never releases it."""
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""SELECT reminder_id,owner_id,status,claimable,claimed_by_user_id,
                       follow_up_after_hours
                FROM reminders
                WHERE reminder_id=? AND space_id IN ({marks})""",
            [reminder_id] + spaces,
        ).fetchone()
        if not row:
            conn.rollback()
            raise PermissionError("reminder not found in your accessible spaces")
        if not row["claimed_by_user_id"]:
            conn.rollback()
            return {"status": "already_unclaimed", "reminder_id": reminder_id}
        if actor.user_id not in {row["claimed_by_user_id"], row["owner_id"]}:
            conn.rollback()
            raise PermissionError("only the claimant or reminder owner can release the claim")
        now = runtime_clock.now_utc()
        # Releasing a due claim reopens it to the original family group on the
        # next scheduler sweep. A pre-due release keeps the original due time.
        next_delivery = now.isoformat() if row["status"] == "DUE" else None
        conn.execute(
            """UPDATE reminder_handoffs
               SET status='CANCELLED',cancelled_at_utc=?
               WHERE reminder_id=? AND status='PENDING'""",
            (utc_now(), reminder_id),
        )
        conn.execute(
            """UPDATE reminders
               SET claimed_by_user_id=NULL,claimed_at_utc=NULL,
                   claimant_follow_up_at_utc=NULL,family_resurfaced_at_utc=NULL,
                   nudged_at_utc=NULL,initiator_notified_at_utc=NULL,
                   relinquished_at_utc=?,
                   status=CASE WHEN status='DUE' THEN 'OPEN' ELSE status END,
                   next_delivery_at_utc=?
               WHERE reminder_id=?""",
            (now.isoformat(), next_delivery, reminder_id),
        )
        conn.execute(
            """INSERT INTO reminder_claim_events(
                   claim_event_id,reminder_id,actor_user_id,event_type,note
               ) VALUES(?,?,?,?,?)""",
            (
                str(uuid.uuid4()), reminder_id, actor.user_id, "RELEASED",
                "explicit release; reaction removal alone never releases",
            ),
        )
        conn.commit()
        return {"status": "released", "reminder_id": reminder_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def nudge_reminder_claimant(actor: ActorContext, reminder_id: str) -> dict:
    """Send one explicit private follow-up to the current claimant."""
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""SELECT reminder_id,owner_id,status,task_text,claimed_by_user_id
                FROM reminders
                WHERE reminder_id=? AND space_id IN ({marks})""",
            [reminder_id] + spaces,
        ).fetchone()
        if not row:
            conn.rollback()
            raise PermissionError("reminder not found in your accessible spaces")
        if actor.user_id != row["owner_id"]:
            conn.rollback()
            raise PermissionError("only the reminder initiator can remind the claimant again")
        if row["status"] != "DUE":
            conn.rollback()
            raise ValueError("only a due unresolved reminder can be followed up")
        claimant = str(row["claimed_by_user_id"] or "")
        if not claimant:
            conn.rollback()
            raise ValueError("this reminder has no claimant")
        phone = _active_user_phone(conn, claimant)
        if not phone:
            conn.rollback()
            raise ValueError("the claimant has no active household number configured")
        conversation_id = phone.replace("+", "") + "@s.whatsapp.net"
        text = f"↪️ Family reminder still unresolved: {row['task_text']}"
        conn.execute(
            """INSERT INTO outbound_messages(
                   outbound_id,source_message_id,conversation_id,kind,text_body,
                   context_kind,context_id
               ) VALUES(?,?,?,'TEXT',?,'REMINDER_CLAIMANT_FOLLOWUP',?)""",
            (
                str(uuid.uuid4()), actor.source_message_id, conversation_id,
                text, reminder_id,
            ),
        )
        now = runtime_clock.now_utc().isoformat()
        conn.execute(
            """UPDATE reminders
               SET nudged_at_utc=?,claimant_follow_up_at_utc=?
               WHERE reminder_id=?""",
            (now, now, reminder_id),
        )
        conn.commit()
        return {
            "status": "nudged",
            "reminder_id": reminder_id,
            "task": row["task_text"],
            "claimed_by_user_id": claimant,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_goal(actor: ActorContext, name: str, target_amount: float | None = None,
             current_amount: float | None = None, currency: str = "MYR",
             target_date: str | None = None, notes: str | None = None,
             shared: bool = False) -> dict:
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        existing = conn.execute(
            f"""SELECT * FROM savings_goals
                WHERE LOWER(goal_name)=LOWER(?) AND space_id IN ({marks})
                ORDER BY updated_at_utc DESC""",
            [name] + spaces,
        ).fetchall()
        if len(existing) == 1:
            row = existing[0]
            space = row["space_id"]
        elif len(existing) > 1:
            intended = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
            matches = [row for row in existing if row["space_id"] == intended]
            if len(matches) != 1:
                raise ValueError("goal name exists in more than one scope; specify which one")
            row = matches[0]
            space = row["space_id"]
        else:
            row = None
            space = scope_policy.resolve_new_write_space(actor, requested_shared=shared)

        target_minor = _minor(target_amount) if target_amount is not None else (row["target_amount_minor"] if row else None)
        current_minor = _minor(current_amount) if current_amount is not None and current_amount > 0 else (
            0 if current_amount == 0 else (row["current_amount_minor"] if row else 0)
        )
        if row:
            conn.execute(
                """UPDATE savings_goals SET target_amount_minor=?,current_amount_minor=?,currency=?,
                   target_date=?,notes=?,action_key=?,updated_at_utc=? WHERE goal_id=?""",
                (target_minor, current_minor, currency.upper(), target_date or row["target_date"],
                 notes if notes is not None else row["notes"], actor.action_key or row["action_key"],
                 utc_now(), row["goal_id"]),
            )
            gid = row["goal_id"]
        else:
            gid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO savings_goals(
                    goal_id,action_key,owner_id,space_id,goal_name,target_amount_minor,current_amount_minor,
                    currency,target_date,notes
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (gid, actor.action_key or str(uuid.uuid4()), actor.user_id, space, name[:200],
                 target_minor, current_minor, currency.upper(), target_date, notes),
            )
        conn.commit()
        return {"status": "saved", "goal_id": gid, "name": name,
                "target_amount": target_minor/100 if target_minor else None,
                "current_amount": current_minor/100, "currency": currency.upper(),
                "space": space}
    finally:
        conn.close()

def list_goals(actor: ActorContext) -> dict:
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT goal_id,goal_name,target_amount_minor,current_amount_minor,currency,target_date,notes,status
                FROM savings_goals WHERE space_id IN ({marks}) AND status='ACTIVE'
                ORDER BY updated_at_utc DESC""", spaces,
        ).fetchall()
        return {"goals": [
            {"goal_id": r["goal_id"], "name": r["goal_name"],
             "target_amount": r["target_amount_minor"]/100 if r["target_amount_minor"] else None,
             "current_amount": r["current_amount_minor"]/100, "currency": r["currency"],
             "target_date": r["target_date"], "notes": r["notes"]}
            for r in rows
        ]}
    finally:
        conn.close()


def get_leave(actor: ActorContext) -> dict:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM leave_state WHERE user_id=?", (actor.user_id,)).fetchone()
        if not row:
            return {"known": False}
        return {"known": True, "balance_days": row["balance_days"], "as_of_date": row["as_of_date"], "notes": row["notes"]}
    finally:
        conn.close()


def set_leave(actor: ActorContext, balance_days: float, as_of_date: str | None = None,
              notes: str | None = None) -> dict:
    if balance_days < 0:
        raise ValueError("leave balance cannot be negative")
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO leave_state(user_id,balance_days,as_of_date,notes,updated_at_utc)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET balance_days=excluded.balance_days,
                 as_of_date=excluded.as_of_date,notes=excluded.notes,updated_at_utc=excluded.updated_at_utc""",
            (actor.user_id, balance_days, as_of_date, notes, utc_now()),
        )
        conn.commit()
        return {"status": "saved", "balance_days": balance_days, "as_of_date": as_of_date}
    finally:
        conn.close()


_ALLOWED_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BIN:
        return _ALLOWED_BIN[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


def add_shopping_item(actor: ActorContext, item: str, quantity: str | None = None,
                      notes: str | None = None, shared: bool = True) -> dict:
    """Add one item to a shopping list. Household-shared is the normal default."""
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    clean_item = (item or "").strip()
    if not clean_item:
        raise ValueError("shopping item is required")
    space = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
    if space not in actor.allowed_spaces:
        raise PermissionError("requested shopping space is not accessible")
    conn = connect()
    try:
        prior_action = conn.execute(
            "SELECT item_id,item_name,quantity,notes,space_id,status FROM shopping_items WHERE action_key=?",
            (actor.action_key,),
        ).fetchone()
        if prior_action:
            return {**dict(prior_action), "status": "already_applied"}
        duplicate = conn.execute(
            """SELECT item_id,item_name,quantity,notes,space_id,status FROM shopping_items
               WHERE space_id=? AND LOWER(item_name)=LOWER(?) AND status='OPEN'
               ORDER BY created_at_utc DESC LIMIT 1""",
            (space, clean_item),
        ).fetchone()
        if duplicate:
            return {**dict(duplicate), "status": "already_listed"}
        item_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO shopping_items(
                item_id,action_key,owner_id,space_id,item_name,quantity,notes
               ) VALUES(?,?,?,?,?,?,?)""",
            (item_id, actor.action_key, actor.user_id, space, clean_item[:200],
             (quantity or "")[:100] or None, (notes or "")[:500] or None),
        )
        conn.commit()
        return {"status": "added", "item_id": item_id, "item": clean_item[:200],
                "quantity": quantity, "notes": notes, "space": space}
    finally:
        conn.close()


def list_shopping_items(actor: ActorContext, include_purchased: bool = False, limit: int = 50,
                        scope: str | None = None) -> dict:
    marks, spaces = _spaces_sql(actor, scope)
    status_clause = "" if include_purchased else " AND status='OPEN'"
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT item_id,item_name,quantity,notes,status,space_id,created_at_utc,updated_at_utc
                FROM shopping_items WHERE space_id IN ({marks}) {status_clause}
                ORDER BY status='OPEN' DESC,created_at_utc ASC LIMIT ?""",
            spaces + [max(1, min(100, int(limit)))],
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


def update_shopping_item(actor: ActorContext, item_id: str, status: str | None = None,
                         quantity: str | None = None, notes: str | None = None,
                         item: str | None = None) -> dict:
    resolved = None
    if status is not None:
        resolved = {"open": "OPEN", "purchased": "PURCHASED", "bought": "PURCHASED",
                    "removed": "REMOVED", "remove": "REMOVED"}.get(status.strip().lower())
        if not resolved:
            raise ValueError("shopping status must be open, purchased, or removed")
    clean_item = (item or "").strip() if item is not None else None
    if item is not None and not clean_item:
        raise ValueError("shopping item name cannot be empty")
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM shopping_items WHERE item_id=? AND space_id IN ({marks})",
            [item_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("shopping item not found in your accessible spaces")
        next_state = resolved or row["status"]
        next_name = clean_item[:200] if clean_item is not None else row["item_name"]
        conn.execute(
            """UPDATE shopping_items
               SET item_name=?,status=?,quantity=?,notes=?,updated_at_utc=? WHERE item_id=?""",
            (next_name, next_state,
             quantity if quantity is not None else row["quantity"],
             notes if notes is not None else row["notes"], utc_now(), item_id),
        )
        conn.commit()
        return {
            "status": "updated", "item_id": item_id, "state": next_state,
            "item": next_name,
            "quantity": quantity if quantity is not None else row["quantity"],
            "notes": notes if notes is not None else row["notes"],
        }
    finally:
        conn.close()


def calculate(expression: str) -> dict:
    if len(expression) > 200:
        raise ValueError("expression too long")
    value = _eval(ast.parse(expression, mode="eval"))
    if isinstance(value, complex) or not math.isfinite(float(value)):
        raise ValueError("invalid numeric result")
    return {"expression": expression, "result": value}


def list_pending_expenses(actor: ActorContext, limit: int = 10) -> dict:
    """Return unresolved money records so conversational clarifications can bind safely."""
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT event_id,event_type,category,amount_minor,currency,event_date_utc,
                       description,reference_text,space_id
                FROM financial_events
                WHERE status='PENDING_HUMAN_REVIEW' AND space_id IN ({marks})
                ORDER BY created_at_utc DESC LIMIT ?""",
            spaces + [max(1, min(25, int(limit)))],
        ).fetchall()
        return {"pending": [
            {
                "event_id": r["event_id"], "type": r["event_type"],
                "amount": r["amount_minor"]/100 if r["amount_minor"] is not None else None,
                "currency": r["currency"], "category": r["category"],
                "description": r["description"], "reference": r["reference_text"],
                "event_date_utc": r["event_date_utc"],
            }
            for r in rows
        ]}
    finally:
        conn.close()


def set_money_bucket(actor: ActorContext, name: str, amount: float,
                     currency: str = "MYR", notes: str | None = None,
                     shared: bool = False) -> dict:
    """Set an explicit bucket. Existing records keep their stored scope."""
    if amount < 0:
        raise ValueError("bucket amount cannot be negative")
    amount_minor = int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT * FROM money_buckets
                WHERE LOWER(bucket_name)=LOWER(?) AND space_id IN ({marks})
                ORDER BY updated_at_utc DESC""",
            [name] + spaces,
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
            space = row["space_id"]
        elif len(rows) > 1:
            intended = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
            matches = [row for row in rows if row["space_id"] == intended]
            if len(matches) != 1:
                raise ValueError("bucket name exists in more than one scope; specify which one")
            row = matches[0]
            space = row["space_id"]
        else:
            row = None
            space = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
        if row:
            bucket_id = row["bucket_id"]
            conn.execute(
                """UPDATE money_buckets SET amount_minor=?,currency=?,notes=?,updated_at_utc=?
                   WHERE bucket_id=?""",
                (amount_minor, currency.upper(), notes, utc_now(), bucket_id),
            )
        else:
            bucket_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO money_buckets(
                    bucket_id,owner_id,space_id,bucket_name,amount_minor,currency,notes
                   ) VALUES(?,?,?,?,?,?,?)""",
                (bucket_id, actor.user_id, space, name[:200], amount_minor, currency.upper(), notes),
            )
        conn.commit()
        return {
            "status": "saved", "bucket_id": bucket_id, "name": name,
            "amount": amount_minor / 100, "currency": currency.upper(), "space": space,
        }
    finally:
        conn.close()

def list_money_buckets(actor: ActorContext) -> dict:
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT bucket_id,bucket_name,amount_minor,currency,notes,space_id,updated_at_utc
                FROM money_buckets WHERE space_id IN ({marks})
                ORDER BY updated_at_utc DESC""", spaces,
        ).fetchall()
        return {"buckets": [
            {
                "bucket_id": r["bucket_id"], "name": r["bucket_name"],
                "amount": r["amount_minor"]/100, "currency": r["currency"],
                "notes": r["notes"], "space": r["space_id"],
            }
            for r in rows
        ]}
    finally:
        conn.close()
