from __future__ import annotations

import ast
import json
import math
import operator
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import runtime_clock

from dateutil.rrule import rrulestr

from context import ActorContext
from db import connect, utc_now


def _spaces_sql(actor: ActorContext, scope: str | None = None) -> tuple[str, list[str]]:
    resolved = str(scope or "all").strip().casefold()
    if resolved in {"", "all", "visible", "accessible"}:
        spaces = list(actor.allowed_spaces)
    elif resolved in {"family", "shared", "family_shared"}:
        if "FAMILY_SHARED" not in actor.allowed_spaces:
            raise PermissionError("family finance/list scope is not accessible in this conversation")
        spaces = ["FAMILY_SHARED"]
    elif resolved in {"private", "personal", "my"}:
        if actor.conversation_type == "GROUP" or actor.private_space not in actor.allowed_spaces:
            raise PermissionError("private scope is not accessible in the Family Shared group")
        spaces = [actor.private_space]
    else:
        raise ValueError("scope must be all, family, or private")
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


def _clean_category(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip().lower().replace(" ", "_")[:80]
    return None if cleaned in GENERIC_CATEGORIES else cleaned


def _find_rule(conn, actor: ActorContext, text: str):
    lowered = (text or "").lower()
    rows = conn.execute(
        "SELECT * FROM routing_rules WHERE user_id=? ORDER BY LENGTH(keyword) DESC",
        (actor.user_id,),
    ).fetchall()
    for row in rows:
        if row["keyword"].lower() in lowered:
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
    if actor.conversation_type == "GROUP":
        space = "FAMILY_SHARED"
    elif rule and rule["force_space_id"]:
        space = rule["force_space_id"]
    elif actor.media_ids:
        # Receipt/document media gets the established shared-finance default.
        # AUDIO is merely the user's input transport and must not change privacy scope.
        marks = ",".join("?" for _ in actor.media_ids)
        media_rows = conn.execute(
            f"SELECT media_type FROM media_objects WHERE media_id IN ({marks})",
            list(actor.media_ids),
        ).fetchall()
        has_financial_document = any(r["media_type"] in ("IMAGE", "PDF") for r in media_rows)
        space = "FAMILY_SHARED" if has_financial_document else actor.private_space
    else:
        space = actor.private_space
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
                   scope: str | None = None, source: str | None = None) -> dict:
    """Return exact aggregates over the full match set plus a bounded recent-record sample."""
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
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM financial_events WHERE {where}", params
        ).fetchone()
        rows = [dict(r) for r in conn.execute(
            f"""SELECT event_id,event_type,category,amount_minor,currency,event_date_utc,
                       description,reference_text,space_id
                FROM financial_events WHERE {where}
                ORDER BY event_date_utc DESC, created_at_utc DESC, rowid DESC LIMIT ?""",
            params + [max(1, min(100, int(limit)))],
        ).fetchall()]
    finally:
        conn.close()

    spending_totals: dict[str, float] = {}
    income_totals: dict[str, float] = {}
    for r in aggregate_rows:
        amount = (r["total_minor"] or 0) / 100
        target = spending_totals if r["event_type"] == "Expense" else income_totals
        target[r["currency"]] = round(amount, 2)

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
        "count": int(count_row["n"] if count_row else 0),
        "returned_records": len(records),
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
                  start_date: str | None = None, end_date: str | None = None, limit: int = 10) -> dict:
    """Find linked receipts plus the caller's own preserved-but-unlinked media."""
    marks, spaces = _spaces_sql(actor)
    bounded = max(1, min(25, int(limit)))
    sql = f"""SELECT DISTINCT m.media_id,m.media_type,m.mime_type,m.created_at_utc,
                     f.event_id,f.amount_minor,f.currency,f.event_date_utc,f.description,
                     f.reference_text,m.ocr_text
              FROM media_objects m
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
        needle = f"%{query.lower()}%"
        sql += """ AND (LOWER(f.description) LIKE ? OR LOWER(COALESCE(f.reference_text,'')) LIKE ?
                      OR LOWER(COALESCE(m.ocr_text,'')) LIKE ?)"""
        params.extend([needle, needle, needle])
    sql += " ORDER BY f.event_date_utc DESC LIMIT ?"
    params.append(bounded)

    conn = connect()
    try:
        linked = conn.execute(sql, params).fetchall()
        matches = [
            {"media_id": r["media_id"], "event_id": r["event_id"],
             "amount": (r["amount_minor"]/100 if r["amount_minor"] is not None else None),
             "currency": r["currency"], "description": r["description"],
             "event_date_utc": r["event_date_utc"], "reference": r["reference_text"],
             "linked": True}
            for r in linked
        ]

        remaining = bounded - len(matches)
        if remaining > 0:
            orphan_sql = """SELECT m.media_id,m.created_at_utc,m.ocr_text
                            FROM media_objects m
                            JOIN inbound_messages i ON i.message_id=m.source_message_id
                            WHERE i.sender_phone=? AND m.media_type IN ('IMAGE','PDF')
                              AND NOT EXISTS (
                                  SELECT 1 FROM event_media_links l WHERE l.media_id=m.media_id
                              )"""
            orphan_params: list = [actor.phone]
            if start_date:
                orphan_sql += " AND m.created_at_utc>=?"
                orphan_params.append(_local_bound(start_date, actor.timezone, False))
            if end_date:
                orphan_sql += " AND m.created_at_utc<=?"
                orphan_params.append(_local_bound(end_date, actor.timezone, True))
            if query:
                orphan_sql += " AND LOWER(COALESCE(m.ocr_text,'')) LIKE ?"
                orphan_params.append(f"%{query.lower()}%")
            if amount is not None:
                orphan_sql += " AND COALESCE(m.ocr_text,'') LIKE ?"
                orphan_params.append(f"%{float(amount):.2f}%")
            orphan_sql += " ORDER BY m.created_at_utc DESC LIMIT ?"
            orphan_params.append(remaining)
            for r in conn.execute(orphan_sql, orphan_params).fetchall():
                matches.append({
                    "media_id": r["media_id"], "event_id": None, "amount": None,
                    "currency": None, "description": "Saved receipt/media awaiting ledger linkage",
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
                "_attachments": [{
                    "path": row["local_path"], "mime_type": row["mime_type"],
                    "kind": "IMAGE" if row["media_type"] == "IMAGE" else "DOCUMENT",
                    "context_kind": "RECEIPT", "context_id": media_id,
                }],
            }

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
            "_attachments": [{
                "path": orphan["local_path"], "mime_type": orphan["mime_type"],
                "kind": "IMAGE" if orphan["media_type"] == "IMAGE" else "DOCUMENT",
                "context_kind": "RECEIPT", "context_id": media_id,
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
        space = "FAMILY_SHARED" if shared or actor.conversation_type == "GROUP" else actor.private_space
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
        conn.execute(
            """INSERT INTO saved_items(item_id,action_key,source_message_id,space_id,owner_id,title,content,tags,media_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (item_id, actor.action_key, actor.source_message_id, space, actor.user_id,
             title[:200], content[:20000], (tags or "")[:500] or None, media_id),
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
}
_SAVED_KIND_ALIASES = {
    "picture": "IMAGE", "pictures": "IMAGE", "photo": "IMAGE", "image": "IMAGE", "images": "IMAGE",
    "document": "DOCUMENT", "documents": "DOCUMENT", "pdf": "DOCUMENT", "file": "DOCUMENT",
    "note": "NOTE", "notes": "NOTE", "text": "NOTE",
}


def _saved_local_date(value: str | None, tz_name: str) -> str | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz_name)).strftime("%d %b %Y")
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
                matches = run(" AND (" + " OR ".join(clauses) + ")", extra)
                mode = "words"
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
                "context_kind": "SAVED_ITEM", "context_id": item_id,
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


def resolve_numbered_choice(actor: ActorContext, choice: int) -> dict:
    """Resolve the newest unexpired receipt/saved-memory numbered list."""
    index = int(choice)
    if index < 1:
        raise ValueError("choice must be 1 or greater")
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM selection_sets
               WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
               ORDER BY created_at_utc DESC LIMIT 10""",
            (actor.user_id, actor.conversation_id, utc_now()),
        ).fetchall()
        if not rows:
            raise ValueError("no numbered receipt or saved-memory list is waiting")
        latest = rows[0]
        ids = json.loads(latest["items_json"])
        if index > len(ids):
            raise ValueError("choice is outside the latest numbered list")
        target = ids[index - 1]
        kind = latest["selection_kind"]
    finally:
        conn.close()
    if kind == "RECEIPT":
        return get_receipt(actor, target)
    if kind == "SAVED_ITEM":
        return get_saved_item(actor, target)
    raise ValueError("unsupported numbered choice type")


def _active_user_phone(conn, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT phone_number FROM user_phone_history WHERE user_id=? AND valid_to_utc IS NULL LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["phone_number"] if row else None


def _reminder_targets(actor: ActorContext, recipient: str) -> list[str]:
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
    raise ValueError("recipient must be me, spouse, husband, wife, or both")


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


def create_reminder(actor: ActorContext, task: str, due_local: str,
                    recurrence_rule: str | None = None, shared: bool = False,
                    recipient: str = "me", presence_aware: bool = False,
                    delivery_class: str = "routine",
                    follow_up_after_hours: int = 24) -> dict:
    if not actor.action_key:
        raise RuntimeError("missing deterministic action key")
    due_utc = _parse_event_time(due_local, actor.timezone)
    delivery_class = (delivery_class or "routine").strip().lower()
    if delivery_class not in {"routine", "time_critical"}:
        raise ValueError("delivery_class must be routine or time_critical")
    follow_up_after_hours = max(0, min(24 * 30, int(follow_up_after_hours)))
    conn = connect()
    try:
        targets = _reminder_targets(actor, recipient)
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

            if target_user == actor.user_id:
                conversation_id = actor.conversation_id
            else:
                phone = _active_user_phone(conn, target_user)
                if not phone:
                    raise ValueError("target household member has no configured WhatsApp number")
                conversation_id = phone.replace("+", "") + "@s.whatsapp.net"

            space = "FAMILY_SHARED" if shared or actor.conversation_type == "GROUP" or target_user != actor.user_id or len(targets) > 1 else actor.private_space
            if space not in actor.allowed_spaces:
                raise PermissionError("requested reminder space is not accessible")

            rid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO reminders(
                    reminder_id,action_key,source_message_id,owner_id,space_id,conversation_id,
                    task_text,due_at_utc,timezone_name,recurrence_rule,
                    presence_aware,delivery_class,follow_up_after_hours
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, action_key, actor.source_message_id, target_user, space,
                 conversation_id, task[:500], due_utc, actor.timezone,
                 (recurrence_rule or "")[:500] or None,
                 1 if presence_aware else 0, delivery_class, follow_up_after_hours),
            )
            _record_reminder_event(
                conn, rid, "CREATED", None, "OPEN", None, due_utc,
                f"recipient={recipient}; delivery_class={delivery_class}"
            )
            created.append({
                "status": "created", "reminder_id": rid, "task": task, "due_at_utc": due_utc,
                "timezone": actor.timezone, "recurrence_rule": recurrence_rule,
                "recipient_user_id": target_user, "space": space,
                "presence_aware": bool(presence_aware), "delivery_class": delivery_class,
                "follow_up_after_hours": follow_up_after_hours,
            })
        conn.commit()
        return created[0] if len(created) == 1 else {"status": "created", "reminders": created}
    finally:
        conn.close()

def list_reminders(actor: ActorContext, include_completed: bool = False, limit: int = 20,
                   when: str | None = None) -> dict:
    marks, spaces = _spaces_sql(actor)
    states = "" if include_completed else " AND status IN ('OPEN','DUE','DEFERRED')"
    date_clause = ""
    params = list(spaces)
    resolved_window = None
    if when and str(when).strip():
        import phase2
        resolved_window = phase2.resolve_date_range(str(when), actor.timezone)
        date_clause = " AND due_at_utc BETWEEN ? AND ?"
        params.extend([
            _local_bound(resolved_window["start_date"], actor.timezone),
            _local_bound(resolved_window["end_date"], actor.timezone, end=True),
        ])
    params.append(max(1, min(50, int(limit))))
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT reminder_id,owner_id,task_text,due_at_utc,timezone_name,recurrence_rule,status
                FROM reminders WHERE space_id IN ({marks}) {states} {date_clause}
                ORDER BY due_at_utc ASC LIMIT ?""",
            params,
        ).fetchall()
        reminders = [dict(r) for r in rows]
        tz = ZoneInfo(actor.timezone)
        for row in reminders:
            row["due_local"] = datetime.fromisoformat(row["due_at_utc"]).astimezone(tz).isoformat()
        result = {"reminders": reminders, "timezone": actor.timezone}
        if resolved_window:
            result["resolved_window"] = resolved_window
            result["resolved_from"] = str(when)
        return result
    finally:
        conn.close()


def update_reminder(actor: ActorContext, reminder_id: str, status: str,
                    new_due_local: str | None = None) -> dict:
    status_map = {
        "ack": "ACK", "acknowledged": "ACK", "complete": "COMP", "completed": "COMP",
        "cancel": "CANC", "cancelled": "CANC", "defer": "DEFERRED", "deferred": "DEFERRED",
        "open": "OPEN",
    }
    event_map = {
        "ACK": "ACKNOWLEDGED", "COMP": "COMPLETED", "CANC": "CANCELLED",
        "DEFERRED": "DEFERRED", "OPEN": "RESCHEDULED", "DUE": "DUE",
    }
    resolved = status_map.get(status.lower(), status.upper())
    if resolved not in {"OPEN","DUE","ACK","DEFERRED","COMP","CANC"}:
        raise ValueError("unsupported reminder status")
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"""SELECT reminder_id,status,due_at_utc FROM reminders
                WHERE reminder_id=? AND space_id IN ({marks})""",
            [reminder_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("reminder not found in your accessible spaces")
        previous_state = row["status"]
        previous_due = row["due_at_utc"]
        new_due = _parse_event_time(new_due_local, actor.timezone) if new_due_local else previous_due
        acknowledged = utc_now() if resolved == "ACK" else None
        if resolved == "ACK":
            conn.execute(
                """UPDATE reminders SET status=?,due_at_utc=?,acknowledged_at_utc=?,
                   next_delivery_at_utc=NULL,defer_reason=NULL WHERE reminder_id=?""",
                (resolved, new_due, acknowledged, reminder_id),
            )
        else:
            conn.execute(
                """UPDATE reminders SET status=?,due_at_utc=?,next_delivery_at_utc=NULL,
                   defer_reason=NULL WHERE reminder_id=?""",
                (resolved, new_due, reminder_id),
            )
        _record_reminder_event(
            conn, reminder_id,
            "RESCHEDULED" if new_due_local and resolved in {"OPEN","DEFERRED"} else event_map[resolved],
            previous_state, resolved, previous_due, new_due,
        )
        conn.commit()
        return {"status": "updated", "reminder_id": reminder_id, "state": resolved,
                "due_at_utc": new_due}
    finally:
        conn.close()


def reminder_history(actor: ActorContext, reminder_id: str) -> dict:
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        owned = conn.execute(
            f"SELECT 1 FROM reminders WHERE reminder_id=? AND space_id IN ({marks})",
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
        return {"history": [dict(r) for r in rows]}
    finally:
        conn.close()

def set_goal(actor: ActorContext, name: str, target_amount: float | None = None,
             current_amount: float | None = None, currency: str = "MYR",
             target_date: str | None = None, notes: str | None = None,
             shared: bool = False) -> dict:
    space = "FAMILY_SHARED" if shared or actor.conversation_type == "GROUP" else actor.private_space
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM savings_goals WHERE space_id=? AND LOWER(goal_name)=LOWER(?)",
                           (space, name)).fetchone()
        target_minor = _minor(target_amount) if target_amount is not None else (row["target_amount_minor"] if row else None)
        current_minor = _minor(current_amount) if current_amount is not None and current_amount > 0 else (
            0 if current_amount == 0 else (row["current_amount_minor"] if row else 0)
        )
        if row:
            conn.execute(
                """UPDATE savings_goals SET target_amount_minor=?,current_amount_minor=?,currency=?,
                   target_date=?,notes=?,space_id=?,action_key=?,updated_at_utc=? WHERE goal_id=?""",
                (target_minor, current_minor, currency.upper(), target_date or row["target_date"],
                 notes if notes is not None else row["notes"], space, actor.action_key or row["action_key"],
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
                "current_amount": current_minor/100, "currency": currency.upper()}
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
    space = "FAMILY_SHARED" if shared else actor.private_space
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


def update_shopping_item(actor: ActorContext, item_id: str, status: str = "purchased",
                         quantity: str | None = None, notes: str | None = None) -> dict:
    resolved = {"open": "OPEN", "purchased": "PURCHASED", "bought": "PURCHASED",
                "removed": "REMOVED", "remove": "REMOVED"}.get((status or "").strip().lower())
    if not resolved:
        raise ValueError("shopping status must be open, purchased, or removed")
    marks, spaces = _spaces_sql(actor)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT * FROM shopping_items WHERE item_id=? AND space_id IN ({marks})",
            [item_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("shopping item not found in your accessible spaces")
        conn.execute(
            """UPDATE shopping_items SET status=?,quantity=?,notes=?,updated_at_utc=? WHERE item_id=?""",
            (resolved, quantity if quantity is not None else row["quantity"],
             notes if notes is not None else row["notes"], utc_now(), item_id),
        )
        conn.commit()
        return {"status": "updated", "item_id": item_id, "state": resolved}
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
    """Set an allocation/budget/stash bucket to an explicit amount supplied by the user."""
    if amount < 0:
        raise ValueError("bucket amount cannot be negative")
    space = "FAMILY_SHARED" if shared or actor.conversation_type == "GROUP" else actor.private_space
    if space not in actor.allowed_spaces:
        raise PermissionError("requested bucket space is not accessible")
    amount_minor = int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    conn = connect()
    try:
        row = conn.execute(
            "SELECT bucket_id FROM money_buckets WHERE space_id=? AND LOWER(bucket_name)=LOWER(?)",
            (space, name),
        ).fetchone()
        if row:
            bucket_id = row["bucket_id"]
            conn.execute(
                """UPDATE money_buckets SET amount_minor=?,currency=?,notes=?,space_id=?,updated_at_utc=?
                   WHERE bucket_id=?""",
                (amount_minor, currency.upper(), notes, space, utc_now(), bucket_id),
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
