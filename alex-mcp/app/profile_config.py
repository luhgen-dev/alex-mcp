"""Phase 2 household profile configuration.

Home Assistant add-on options are the editable source for stable household facts.
This module normalizes them, applies the existing FAMILY_SHARED/private-space ACL,
and keeps a version history in SQLite so later edits do not silently rewrite the
past. Nothing here is wired into the live Alex conversation path yet.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import compat_tools as tools
from config import DATA_DIR
import scope_policy
from context import current_actor

ROLE_MAP = {
    "husband": ("USR_HUSBAND", "HUSBAND_PVT"),
    "wife": ("USR_WIFE", "WIFE_PVT"),
}
SUPPORTED_KINDS = (
    "income", "roster", "overtime_rule", "leave_balance",
    "recurring_payment", "account_alias", "reminder_preference",
    "presence_mapping",
)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alex_profile_config_versions (
    version_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN
        ('income','roster','overtime_rule','leave_balance',
         'recurring_payment','account_alias','reminder_preference',
         'presence_mapping')),
    record_key TEXT NOT NULL,
    owner_role TEXT NOT NULL CHECK(owner_role IN ('husband','wife','family')),
    owner_user_id TEXT,
    space_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility IN ('private','family')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'HA_OPTIONS',
    valid_from_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to_utc TEXT,
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
        ON DELETE RESTRICT ON UPDATE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_config_one_active
    ON alex_profile_config_versions(kind, record_key)
    WHERE valid_to_utc IS NULL;
CREATE INDEX IF NOT EXISTS idx_profile_config_acl
    ON alex_profile_config_versions(space_id, kind, valid_to_utc);
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


def _record_id(value):
    value = str(value or "").strip().lower()
    if not _ID_RE.fullmatch(value):
        raise ValueError("Profile ids must use 1-40 lowercase letters, numbers, _ or -")
    return value


def _role(value, allow_family=False):
    role = str(value or "").strip().lower()
    allowed = {"husband", "wife"} | ({"family"} if allow_family else set())
    if role not in allowed:
        raise ValueError("Profile owner must be husband/wife" + ("/family" if allow_family else ""))
    return role


def _privacy(owner_role, visibility):
    visibility = str(visibility or "").strip().lower()
    if visibility not in ("private", "family"):
        raise ValueError("Visibility must be private or family")
    if owner_role == "family":
        if visibility != "family":
            raise ValueError("A family-owned record cannot be private")
        return None, "FAMILY_SHARED", visibility
    user_id, private_space = ROLE_MAP[owner_role]
    return user_id, ("FAMILY_SHARED" if visibility == "family" else private_space), visibility


def _currency(value):
    code = str(value or "MYR").strip().upper()
    if code not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    return code


def _minor(value, allow_none=False):
    if value in (None, "") and allow_none:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be numeric")
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _bool(value, default=True):
    if value is None:
        return default
    return bool(value)


def _optional_day(value):
    if value in (None, ""):
        return None
    day = int(value)
    if not 1 <= day <= 31:
        raise ValueError("Day must be 1-31")
    return day


def _optional_float(value):
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("Value must be numeric")
    if result < 0:
        raise ValueError("Value cannot be negative")
    return result


def _clock(value, allow_none=True):
    if value in (None, "") and allow_none:
        return None
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("Time must use HH:MM 24-hour format")
    return text


def _csv_days(value):
    if value in (None, ""):
        return []
    allowed = {
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    }
    days = [x.strip().lower() for x in str(value).split(",") if x.strip()]
    if any(day not in allowed for day in days):
        raise ValueError("Weekdays must be comma-separated full day names")
    return days


def _base_record(kind, raw, allow_family_owner=False):
    owner = _role(raw.get("owner"), allow_family=allow_family_owner)
    visibility = str(raw.get("visibility") or "private").lower()
    owner_user_id, space_id, visibility = _privacy(owner, visibility)
    ident = _record_id(raw.get("id"))
    return {
        "kind": kind,
        "record_key": f"{owner}:{ident}",
        "owner_role": owner,
        "owner_user_id": owner_user_id,
        "space_id": space_id,
        "visibility": visibility,
    }


def _income(raw):
    rec = _base_record("income", raw)
    income_class = str(raw.get("income_class") or "fixed").lower()
    if income_class not in ("fixed", "variable"):
        raise ValueError("Income class must be fixed or variable")
    frequency = str(raw.get("frequency") or "monthly").lower()
    if frequency not in ("weekly", "fortnightly", "monthly", "annual"):
        raise ValueError("Unsupported income frequency")
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "amount_minor": _minor(raw.get("amount")),
        "currency": _currency(raw.get("currency")),
        "income_class": income_class,
        "frequency": frequency,
        "payday_day": _optional_day(raw.get("payday_day")),
        "effective_from": str(raw.get("effective_from") or "").strip() or None,
        "active": _bool(raw.get("active"), True),
    }
    if not payload["name"]:
        raise ValueError("Income name is required")
    rec["payload"] = payload
    return rec


def _recurring(raw):
    rec = _base_record("recurring_payment", raw, allow_family_owner=True)
    amount_type = str(raw.get("amount_type") or "fixed").lower()
    if amount_type not in ("fixed", "variable"):
        raise ValueError("Amount type must be fixed or variable")
    frequency = str(raw.get("frequency") or "monthly").lower()
    if frequency not in ("weekly", "monthly", "quarterly", "annual"):
        raise ValueError("Unsupported payment frequency")
    amount_minor = _minor(raw.get("amount"), allow_none=True)
    if amount_type == "fixed" and amount_minor is None:
        raise ValueError("Fixed recurring payments require an amount")
    effective_from = str(raw.get("effective_from") or "").strip() or None
    if frequency != "monthly" and not effective_from:
        raise ValueError(
            "Weekly, quarterly and annual recurring payments require effective_from"
        )
    if effective_from:
        from datetime import date as _date
        _date.fromisoformat(effective_from[:10])
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "amount_type": amount_type,
        "amount_minor": amount_minor,
        "currency": _currency(raw.get("currency")),
        "due_day": _optional_day(raw.get("due_day")),
        "frequency": frequency,
        "category": str(raw.get("category") or "").strip() or None,
        "auto_pay": _bool(raw.get("auto_pay"), False),
        "effective_from": effective_from,
        "active": _bool(raw.get("active"), True),
    }
    if not payload["name"]:
        raise ValueError("Recurring payment name is required")
    rec["payload"] = payload
    return rec


def _roster(raw):
    rec = _base_record("roster", raw)
    cycle_start = str(raw.get("cycle_start") or "").strip() or None
    if cycle_start:
        from datetime import date as _date
        _date.fromisoformat(cycle_start[:10])
    pattern = str(raw.get("pattern") or "").strip().lower()
    parts = [x.strip() for x in pattern.split(",") if x.strip()]
    if not parts or any(x not in ("morning", "evening", "off") for x in parts):
        raise ValueError("Roster pattern may contain morning, evening or off")
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "cycle_start": cycle_start,
        "pattern": ",".join(parts),
        "day_start": _clock(raw.get("day_start")),
        "day_end": _clock(raw.get("day_end")),
        "evening_start": _clock(
            raw.get("evening_start") or raw.get("night_start")
        ),
        "evening_end": _clock(
            raw.get("evening_end") or raw.get("night_end")
        ),
        "active": _bool(raw.get("active"), True),
    }
    if not payload["name"] or not payload["cycle_start"]:
        raise ValueError("Roster name and cycle_start are required")
    rec["payload"] = payload
    return rec


def _overtime_rule(raw):
    rec = _base_record("overtime_rule", raw)
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "currency": _currency(raw.get("currency") or "MYR"),
        "morning_pre_hours": _optional_float(raw.get("morning_pre_hours")),
        "morning_pre_start": _clock(raw.get("morning_pre_start")),
        "morning_weekend_standard_start": _clock(raw.get("morning_weekend_standard_start")),
        "morning_weekend_standard_end": _clock(raw.get("morning_weekend_standard_end")),
        "morning_weekend_low_start": _clock(raw.get("morning_weekend_low_start")),
        "morning_weekend_low_end": _clock(raw.get("morning_weekend_low_end")),
        "evening_post_hours": _optional_float(raw.get("evening_post_hours")),
        "evening_post_start": _clock(raw.get("evening_post_start")),
        "evening_post_end": _clock(raw.get("evening_post_end")),
        "evening_weekend_days": _csv_days(raw.get("evening_weekend_days")),
        "evening_weekend_standard_start": _clock(raw.get("evening_weekend_standard_start")),
        "evening_weekend_standard_end": _clock(raw.get("evening_weekend_standard_end")),
        "evening_weekend_low_start": _clock(raw.get("evening_weekend_low_start")),
        "evening_weekend_low_end": _clock(raw.get("evening_weekend_low_end")),
        "sunday_override_allowed": _bool(raw.get("sunday_override_allowed"), False),
        "payout_days": [
            int(x.strip()) for x in str(raw.get("payout_days") or "").split(",")
            if x.strip()
        ],
        "rate_formula": str(raw.get("rate_formula") or "").strip() or None,
        "active": _bool(raw.get("active"), True),
    }
    if not payload["name"]:
        raise ValueError("Overtime rule name is required")
    if any(day < 1 or day > 31 for day in payload["payout_days"]):
        raise ValueError("OT payout days must be 1-31")
    rec["payload"] = payload
    return rec


def _leave_balance(raw):
    rec = _base_record("leave_balance", raw)
    total = _optional_float(raw.get("entitlement_days"))
    remaining = _optional_float(raw.get("remaining_days"))
    if total is None or remaining is None:
        raise ValueError("Leave entitlement and remaining days are required")
    if remaining > total:
        raise ValueError("Leave remaining days cannot exceed entitlement")
    as_of_date = str(raw.get("as_of_date") or "").strip()
    if as_of_date:
        from datetime import date as _date
        _date.fromisoformat(as_of_date[:10])
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "entitlement_days": total,
        "remaining_days": remaining,
        "as_of_date": as_of_date,
        "active": _bool(raw.get("active"), True),
    }
    if not payload["name"] or not payload["as_of_date"]:
        raise ValueError("Leave name and as-of date are required")
    rec["payload"] = payload
    return rec


def _reminder_preference(raw):
    rec = _base_record("reminder_preference", raw)
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "bill_days_before": int(raw.get("bill_days_before") or 0),
        "follow_up_after_hours": int(raw.get("follow_up_after_hours") or 24),
        "quiet_start": _clock(raw.get("quiet_start")),
        "quiet_end": _clock(raw.get("quiet_end")),
        "presence_aware": _bool(raw.get("presence_aware"), False),
        "active": _bool(raw.get("active"), True),
    }
    if payload["bill_days_before"] < 0 or payload["follow_up_after_hours"] < 0:
        raise ValueError("Reminder offsets cannot be negative")
    if not payload["name"]:
        raise ValueError("Reminder preference name is required")
    rec["payload"] = payload
    return rec


def _presence_mapping(raw):
    rec = _base_record("presence_mapping", raw)
    payload = {
        "id": _record_id(raw.get("id")),
        "name": str(raw.get("name") or "").strip(),
        "person_entity": str(raw.get("person_entity") or "").strip() or None,
        "phone_tracker_entity": str(raw.get("phone_tracker_entity") or "").strip() or None,
        "home_zone": str(raw.get("home_zone") or "home").strip(),
        "active": _bool(raw.get("active"), True),
    }
    if not payload["name"]:
        raise ValueError("Presence mapping name is required")
    if not payload["person_entity"] and not payload["phone_tracker_entity"]:
        raise ValueError("Presence mapping needs at least one HA entity")
    rec["payload"] = payload
    return rec


def _account_alias(raw):
    rec = _base_record("account_alias", raw, allow_family_owner=True)
    payload = {
        "id": _record_id(raw.get("id")),
        "label": str(raw.get("label") or "").strip(),
        "purpose": str(raw.get("purpose") or "").strip() or None,
        "active": _bool(raw.get("active"), True),
    }
    if not payload["label"]:
        raise ValueError("Account alias label is required")
    rec["payload"] = payload
    return rec


def normalize_options(options):
    """Convert HA options into privacy-tagged, deterministic profile records."""
    options = options or {}
    builders = (
        ("income_profiles", _income),
        ("roster_profiles", _roster),
        ("overtime_profiles", _overtime_rule),
        ("leave_balances", _leave_balance),
        ("recurring_payments", _recurring),
        ("account_aliases", _account_alias),
        ("reminder_preferences", _reminder_preference),
        ("presence_mappings", _presence_mapping),
    )
    records = []
    seen = set()
    for option_name, builder in builders:
        values = options.get(option_name) or []
        if not isinstance(values, list):
            raise ValueError(f"{option_name} must be a list")
        for raw in values:
            if not isinstance(raw, dict):
                raise ValueError(f"{option_name} entries must be objects")
            rec = builder(raw)
            ident = (rec["kind"], rec["record_key"])
            if ident in seen:
                raise ValueError(f"Duplicate profile record: {rec['kind']} {rec['record_key']}")
            seen.add(ident)
            records.append(rec)
    return records


def _canonical_payload(record):
    payload = json.dumps(record["payload"], sort_keys=True, separators=(",", ":"))
    digest_source = json.dumps({
        "owner_role": record["owner_role"],
        "owner_user_id": record["owner_user_id"],
        "space_id": record["space_id"],
        "visibility": record["visibility"],
        "payload": record["payload"],
    }, sort_keys=True, separators=(",", ":"))
    return payload, hashlib.sha256(digest_source.encode("utf-8")).hexdigest()


def sync_options(options, conn=None):
    """Version HA profile options without logging or exposing their raw values."""
    own = conn is None
    if own:
        conn = tools.get_db()
    try:
        ensure_schema(conn)
        records = normalize_options(options)
        incoming = {(r["kind"], r["record_key"]): r for r in records}
        conn.execute("BEGIN IMMEDIATE")
        active = {
            (r["kind"], r["record_key"]): r
            for r in conn.execute("""
                SELECT * FROM alex_profile_config_versions
                WHERE valid_to_utc IS NULL
            """).fetchall()
        }
        added = changed = removed = unchanged = 0
        for key, record in incoming.items():
            payload_json, payload_hash = _canonical_payload(record)
            old = active.get(key)
            if old and old["payload_hash"] == payload_hash:
                unchanged += 1
                continue
            if old:
                conn.execute("""
                    UPDATE alex_profile_config_versions
                    SET valid_to_utc=CURRENT_TIMESTAMP
                    WHERE version_id=? AND valid_to_utc IS NULL
                """, (old["version_id"],))
                changed += 1
            else:
                added += 1
            conn.execute("""
                INSERT INTO alex_profile_config_versions(
                    version_id,kind,record_key,owner_role,owner_user_id,space_id,
                    visibility,payload_json,payload_hash,source
                ) VALUES (?,?,?,?,?,?,?,?,?,'HA_OPTIONS')
            """, (
                str(uuid.uuid4()), record["kind"], record["record_key"],
                record["owner_role"], record["owner_user_id"], record["space_id"],
                record["visibility"], payload_json, payload_hash,
            ))

        for key, old in active.items():
            if key not in incoming:
                conn.execute("""
                    UPDATE alex_profile_config_versions
                    SET valid_to_utc=CURRENT_TIMESTAMP
                    WHERE version_id=? AND valid_to_utc IS NULL
                """, (old["version_id"],))
                removed += 1
        conn.commit()
        return {
            "added": added, "changed": changed, "removed": removed,
            "unchanged": unchanged, "active": len(incoming),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()


def load_ha_options(path=None):
    path = Path(path or (Path(DATA_DIR) / "options.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Home Assistant options.json must contain an object")
    return data


def sync_from_ha(path=None):
    return sync_options(load_ha_options(path))


def authorized_records(sender_phone, conversation_type, kind=None,
                       requested_scope="all"):
    """Read only records authorized by the existing Jarvis privacy boundary."""
    if kind is not None and kind not in SUPPORTED_KINDS:
        raise ValueError("Unsupported profile kind")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space = tools.resolve_user_and_space(
            conn, sender_phone, conversation_type)
        try:
            scope = scope_policy.effective_read_scope(current_actor(), requested_scope)
        except RuntimeError:
            scope = str(requested_scope or "all").lower()
        clauses = ["valid_to_utc IS NULL"]
        args = []
        if kind:
            clauses.append("kind=?")
            args.append(kind)

        if conversation_type == "GROUP":
            if scope == "private":
                raise PermissionError("Private profile data cannot be shown in the family group")
            clauses.append("space_id='FAMILY_SHARED'")
        elif scope == "family":
            clauses.append("space_id='FAMILY_SHARED'")
        elif scope == "private":
            clauses.append("space_id=?")
            args.append(private_space)
        else:
            clauses.append("(space_id='FAMILY_SHARED' OR space_id=?)")
            args.append(private_space)

        rows = conn.execute(
            "SELECT * FROM alex_profile_config_versions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY kind,record_key",
            args,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item.pop("payload_hash", None)
            result.append(item)
        return result
    finally:
        conn.close()



def authorized_records_as_of(sender_phone, conversation_type, at_utc,
                             kind=None, requested_scope="all"):
    """Read the profile version that was effective at a historical instant."""
    if kind is not None and kind not in SUPPORTED_KINDS:
        raise ValueError("Unsupported profile kind")
    at_text = str(at_utc or "").strip()
    if not at_text:
        raise ValueError("Historical lookup requires an ISO timestamp")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space = tools.resolve_user_and_space(
            conn, sender_phone, conversation_type)
        try:
            scope = scope_policy.effective_read_scope(current_actor(), requested_scope)
        except RuntimeError:
            scope = str(requested_scope or "all").lower()
        clauses = [
            "datetime(valid_from_utc) <= datetime(?)",
            "(valid_to_utc IS NULL OR datetime(valid_to_utc) > datetime(?))",
        ]
        args = [at_text, at_text]
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if conversation_type == "GROUP":
            if scope == "private":
                raise PermissionError("Private profile data cannot be shown in the family group")
            clauses.append("space_id='FAMILY_SHARED'")
        elif scope == "family":
            clauses.append("space_id='FAMILY_SHARED'")
        elif scope == "private":
            clauses.append("space_id=?")
            args.append(private_space)
        else:
            clauses.append("(space_id='FAMILY_SHARED' OR space_id=?)")
            args.append(private_space)

        rows = conn.execute(
            "SELECT * FROM alex_profile_config_versions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY kind,record_key,valid_from_utc DESC",
            args,
        ).fetchall()
        result = []
        seen = set()
        for row in rows:
            key = (row["kind"], row["record_key"])
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item.pop("payload_hash", None)
            result.append(item)
        return result
    finally:
        conn.close()

def main():
    result = sync_from_ha()
    print(
        "[profile-config] synced "
        f"active={result['active']} added={result['added']} "
        f"changed={result['changed']} removed={result['removed']} "
        f"unchanged={result['unchanged']}"
    )


if __name__ == "__main__":
    main()
