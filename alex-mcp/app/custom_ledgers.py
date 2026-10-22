from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import db
import runtime_clock
import scope_policy
from context import ActorContext


SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_ledgers (
    ledger_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL,
    purpose TEXT,
    fields_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','CLOSED')) DEFAULT 'ACTIVE',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(space_id,name),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE TABLE IF NOT EXISTS custom_ledger_entries (
    entry_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    ledger_id TEXT NOT NULL,
    source_message_id TEXT,
    owner_id TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    description TEXT NOT NULL,
    entry_kind TEXT NOT NULL CHECK(entry_kind IN ('NOTE','SPENDING','SAVINGS','TRANSFER','REFERENCE_ONLY')),
    amount_minor INTEGER,
    currency TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    media_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUPERSEDED')) DEFAULT 'ACTIVE',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(ledger_id) REFERENCES custom_ledgers(ledger_id),
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(media_id) REFERENCES media_objects(media_id)
);
CREATE INDEX IF NOT EXISTS idx_custom_ledger_entries
ON custom_ledger_entries(ledger_id,status,entry_date,created_at_utc);

CREATE TABLE IF NOT EXISTS custom_ledger_selection_sets (
    selection_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    items_json TEXT NOT NULL,
    read_scope TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at_utc TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_custom_ledger_selection
ON custom_ledger_selection_sets(user_id,conversation_id,created_at_utc);
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


def _minor(value):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("amount must be numeric")
    if amount < 0:
        raise ValueError("amount cannot be negative")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _norm(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _ledger_row(actor: ActorContext, ledger_id: str):
    spaces = scope_policy.read_spaces(actor)
    marks = ",".join("?" for _ in spaces)
    conn = db.connect()
    try:
        row = conn.execute(
            f"""SELECT * FROM custom_ledgers
                WHERE ledger_id=? AND space_id IN ({marks}) LIMIT 1""",
            [ledger_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("ledger not found in the current authorized scope")
        return dict(row)
    finally:
        conn.close()


def resolve_ledger(actor: ActorContext, ledger_id: str | None = None,
                   ledger_name: str | None = None, include_closed: bool = False) -> str:
    ensure_schema()
    if ledger_id:
        return str(_ledger_row(actor, str(ledger_id))["ledger_id"])
    wanted = _norm(ledger_name)
    if not wanted:
        raise ValueError("provide ledger_id or ledger_name")
    spaces = scope_policy.read_spaces(actor)
    marks = ",".join("?" for _ in spaces)
    status = "" if include_closed else " AND status='ACTIVE'"
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""SELECT ledger_id,name,status FROM custom_ledgers
                WHERE space_id IN ({marks}){status}
                ORDER BY updated_at_utc DESC""",
            spaces,
        ).fetchall()
    finally:
        conn.close()
    exact = [row for row in rows if _norm(row["name"]) == wanted]
    candidates = exact or [
        row for row in rows
        if wanted in _norm(row["name"]) or _norm(row["name"]) in wanted
    ]
    if len(candidates) == 1:
        return str(candidates[0]["ledger_id"])
    if not candidates:
        raise ValueError(f"no ledger uniquely matches {ledger_name!r}")
    names = ", ".join(str(row["name"]) for row in candidates[:8])
    raise ValueError("ledger name is ambiguous; ask which one: " + names)


def create_ledger(actor: ActorContext, name: str, purpose: str | None = None,
                  fields: list[str] | None = None, shared: bool = False) -> dict:
    ensure_schema()
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("ledger name is required")
    space = scope_policy.resolve_new_write_space(actor, requested_shared=shared)
    if space not in actor.allowed_spaces:
        raise PermissionError("requested ledger scope is not accessible")
    field_list = [
        str(value).strip()[:80] for value in (fields or [])
        if str(value or "").strip()
    ][:20]
    if not field_list:
        field_list = ["date", "description", "amount", "notes"]
    conn = db.connect()
    try:
        existing = conn.execute(
            """SELECT * FROM custom_ledgers
               WHERE space_id=? AND LOWER(TRIM(name))=LOWER(TRIM(?)) LIMIT 1""",
            (space, clean),
        ).fetchone()
        if existing:
            return {
                "status": "already_exists",
                "ledger_id": existing["ledger_id"],
                "name": existing["name"],
                "scope": "family" if space == "FAMILY_SHARED" else "private",
            }
        ident = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO custom_ledgers(
                   ledger_id,owner_id,space_id,name,purpose,fields_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                ident, actor.user_id, space, clean[:200],
                str(purpose or "").strip()[:1000] or None,
                json.dumps(field_list, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {
            "status": "created", "ledger_id": ident, "name": clean[:200],
            "purpose": str(purpose or "").strip() or None,
            "fields": field_list,
            "scope": "family" if space == "FAMILY_SHARED" else "private",
        }
    finally:
        conn.close()


def list_ledgers(actor: ActorContext, include_closed: bool = False) -> dict:
    ensure_schema()
    spaces = scope_policy.read_spaces(actor)
    marks = ",".join("?" for _ in spaces)
    clause = "" if include_closed else " AND status='ACTIVE'"
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""SELECT ledger_id,name,purpose,fields_json,status,space_id,
                       created_at_utc,updated_at_utc
                FROM custom_ledgers WHERE space_id IN ({marks}){clause}
                ORDER BY name""",
            spaces,
        ).fetchall()
        return {
            "ledgers": [
                {
                    "ledger_id": row["ledger_id"],
                    "name": row["name"], "purpose": row["purpose"],
                    "fields": json.loads(row["fields_json"] or "[]"),
                    "status": row["status"],
                    "scope": "family" if row["space_id"] == "FAMILY_SHARED" else "private",
                }
                for row in rows
            ],
            "presentation_rule": "Never expose ledger_id. Present name, purpose and scope.",
        }
    finally:
        conn.close()


def add_entry(actor: ActorContext, ledger_id: str, description: str,
              entry_date: str | None = None, entry_kind: str | None = None,
              amount: float | None = None, currency: str | None = None,
              data: dict | None = None) -> dict:
    ensure_schema()
    ledger = _ledger_row(actor, ledger_id)
    if ledger["status"] != "ACTIVE":
        raise ValueError("ledger is closed")
    desc = str(description or "").strip()
    if not desc:
        raise ValueError("entry description is required")
    kind = str(entry_kind or "").strip().upper().replace("-", "_")
    if amount is not None and kind not in {"SPENDING","SAVINGS","TRANSFER","REFERENCE_ONLY"}:
        return {
            "status": "needs_clarification",
            "clarification_question": (
                "Should I record this money as spending, savings, an internal transfer, "
                "or reference-only tracking?"
            ),
            "choices": ["SPENDING", "SAVINGS", "TRANSFER", "REFERENCE_ONLY"],
            "ledger": ledger["name"],
            "nothing_written": True,
        }
    if amount is None:
        kind = kind or "NOTE"
        if kind not in {"NOTE","REFERENCE_ONLY"}:
            raise ValueError("money entry kinds require an amount")
    if kind not in {"NOTE","SPENDING","SAVINGS","TRANSFER","REFERENCE_ONLY"}:
        raise ValueError("unsupported ledger entry kind")

    raw_currency = str(currency or "").strip().upper()
    cur = None
    if amount is not None:
        cur = {"": "MYR", "RM": "MYR", "MYR": "MYR", "SGD": "SGD", "S$": "SGD"}.get(raw_currency)
        if cur not in {"MYR","SGD"}:
            raise ValueError("currency must be MYR or SGD")
    date_text = str(entry_date or runtime_clock.today(actor.timezone).isoformat())[:10]
    datetime.fromisoformat(date_text)
    action_key = str(actor.action_key or "")
    if not action_key:
        raise RuntimeError("missing deterministic action key")
    media_id = str(actor.media_ids[0]) if actor.media_ids else None
    conn = db.connect()
    try:
        existing = conn.execute(
            "SELECT * FROM custom_ledger_entries WHERE action_key=?",
            (action_key,),
        ).fetchone()
        if existing:
            return {
                "status": "already_applied",
                "entry_id": existing["entry_id"],
                "ledger": ledger["name"],
            }
        ident = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO custom_ledger_entries(
                   entry_id,action_key,ledger_id,source_message_id,owner_id,
                   entry_date,description,entry_kind,amount_minor,currency,
                   data_json,media_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ident, action_key, ledger_id, actor.source_message_id, actor.user_id,
                date_text, desc[:500], kind, _minor(amount), cur,
                json.dumps(data or {}, ensure_ascii=False, sort_keys=True)[:20000],
                media_id,
            ),
        )
        conn.commit()
        return {
            "status": "recorded", "entry_id": ident, "ledger": ledger["name"],
            "date": date_text, "description": desc[:500], "entry_kind": kind,
            "amount": float(amount) if amount is not None else None,
            "currency": cur, "attachment_saved": bool(media_id),
            "accounting_note": (
                "Custom ledger entries are tracked separately from the main expense/goal ledgers "
                "unless the user explicitly uses a specialist Alex finance tool."
            ),
        }
    finally:
        conn.close()


def _store_selection(actor: ActorContext, ids: list[str]) -> str | None:
    if not ids:
        return None
    ident = str(uuid.uuid4())
    expiry = (runtime_clock.now_utc() + timedelta(hours=48)).isoformat()
    scope = str(actor.read_scope or "all")
    conn = db.connect()
    try:
        conn.execute(
            """INSERT INTO custom_ledger_selection_sets(
                   selection_id,user_id,conversation_id,items_json,read_scope,
                   created_at_utc,expires_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                ident, actor.user_id, actor.conversation_id,
                json.dumps(ids), scope, db.utc_now(), expiry,
            ),
        )
        conn.commit()
        return ident
    finally:
        conn.close()


def query_entries(actor: ActorContext, ledger_id: str,
                  start_date: str | None = None, end_date: str | None = None,
                  entry_kind: str | None = None, limit: int = 100) -> dict:
    ledger = _ledger_row(actor, ledger_id)
    where = ["ledger_id=?", "status='ACTIVE'"]
    args = [ledger_id]
    if start_date:
        where.append("entry_date>=?")
        args.append(str(start_date)[:10])
    if end_date:
        where.append("entry_date<=?")
        args.append(str(end_date)[:10])
    if entry_kind:
        where.append("entry_kind=?")
        args.append(str(entry_kind).upper())
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT * FROM custom_ledger_entries WHERE """
            + " AND ".join(where)
            + " ORDER BY entry_date DESC,created_at_utc DESC LIMIT ?",
            args + [max(1, min(500, int(limit)))],
        ).fetchall()
    finally:
        conn.close()
    values = []
    totals = {}
    for index, row in enumerate(rows, 1):
        amount = row["amount_minor"] / 100 if row["amount_minor"] is not None else None
        if amount is not None and row["currency"]:
            key = f"{row['entry_kind']}:{row['currency']}"
            totals[key] = round(totals.get(key, 0.0) + amount, 2)
        values.append({
            "choice": index, "entry_id": row["entry_id"],
            "date": row["entry_date"], "description": row["description"],
            "entry_kind": row["entry_kind"], "amount": amount,
            "currency": row["currency"],
            "has_attachment": bool(row["media_id"]),
        })
    selection = _store_selection(actor, [str(row["entry_id"]) for row in rows])
    return {
        "ledger": ledger["name"], "entries": values, "totals": totals,
        "selection_id": selection,
        "presentation_rule": (
            "Present numbered entries in this exact order. Never expose entry_id or selection_id. "
            "Custom ledger totals stay separate from main household expense/goal totals."
        ),
    }


def get_entry(actor: ActorContext, entry_id: str) -> dict:
    ensure_schema()
    spaces = scope_policy.read_spaces(actor)
    marks = ",".join("?" for _ in spaces)
    conn = db.connect()
    try:
        row = conn.execute(
            f"""SELECT e.*,l.name AS ledger_name,l.space_id,i.raw_text AS source_text,
                       i.received_at_utc AS source_received
                FROM custom_ledger_entries e
                JOIN custom_ledgers l ON l.ledger_id=e.ledger_id
                LEFT JOIN inbound_messages i ON i.message_id=e.source_message_id
                WHERE e.entry_id=? AND e.status='ACTIVE'
                  AND l.space_id IN ({marks}) LIMIT 1""",
            [entry_id] + spaces,
        ).fetchone()
        if not row:
            raise PermissionError("ledger entry not found in current authorized scope")
        tz = ZoneInfo(actor.timezone)
        try:
            stamp = datetime.fromisoformat(str(row["source_received"] or row["created_at_utc"]).replace("Z","+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            logged = stamp.astimezone(tz).isoformat()
        except Exception:
            logged = row["source_received"] or row["created_at_utc"]
        result = {
            "ledger": row["ledger_name"], "date": row["entry_date"],
            "description": row["description"], "entry_kind": row["entry_kind"],
            "amount": row["amount_minor"] / 100 if row["amount_minor"] is not None else None,
            "currency": row["currency"], "data": json.loads(row["data_json"] or "{}"),
            "logged_at_local": logged, "source_text": str(row["source_text"] or "")[:500] or None,
            "scope": "family" if row["space_id"] == "FAMILY_SHARED" else "private",
            "attachment_available": bool(row["media_id"]),
            "presentation_rule": "Never expose internal IDs.",
        }
        if row["media_id"]:
            media = conn.execute(
                "SELECT local_path,mime_type,media_type FROM media_objects WHERE media_id=?",
                (row["media_id"],),
            ).fetchone()
            if media:
                result["_attachments"] = [{
                    "path": media["local_path"], "mime_type": media["mime_type"],
                    "kind": (
                        "IMAGE" if media["media_type"] == "IMAGE"
                        else "AUDIO" if media["media_type"] == "AUDIO"
                        else "VIDEO" if media["media_type"] == "VIDEO"
                        else "DOCUMENT"
                    ),
                }]
        return result
    finally:
        conn.close()


def close_ledger(actor: ActorContext, ledger_id: str) -> dict:
    ledger = _ledger_row(actor, ledger_id)
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE custom_ledgers SET status='CLOSED',updated_at_utc=CURRENT_TIMESTAMP WHERE ledger_id=?",
            (ledger_id,),
        )
        conn.commit()
        return {"status": "closed", "name": ledger["name"], "history_preserved": True}
    finally:
        conn.close()
