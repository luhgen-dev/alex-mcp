"""Project Jarvis Phase 2 deterministic finance/goal engine.

This module is intentionally not imported by alex_orchestrator yet. It can be
fully tested in isolation while the certified Phase-1 conversation path remains
unchanged.
"""
from __future__ import annotations

import calendar

import runtime_clock
import json
import sqlite3
import uuid
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import profile_config
import compat_tools as tools
import scope_policy
from context import current_actor


SCHEMA = """
CREATE TABLE IF NOT EXISTS alex_phase2_goals (
    goal_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    name TEXT NOT NULL,
    target_minor INTEGER NOT NULL CHECK(target_minor > 0),
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    baseline_monthly_minor INTEGER NOT NULL DEFAULT 0 CHECK(baseline_monthly_minor >= 0),
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('DRAFT','ACTIVE','PAUSED','COMPLETED','CANCELLED')),
    target_date TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id),
    FOREIGN KEY(created_by) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_goals_owner
    ON alex_phase2_goals(owner_user_id,space_id,status);

CREATE TABLE IF NOT EXISTS alex_phase2_goal_baseline_versions (
    goal_id TEXT NOT NULL,
    effective_period TEXT NOT NULL,
    monthly_minor INTEGER NOT NULL CHECK(monthly_minor >= 0),
    reason TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(goal_id,effective_period),
    FOREIGN KEY(goal_id) REFERENCES alex_phase2_goals(goal_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_goal_baseline_history
    ON alex_phase2_goal_baseline_versions(goal_id,effective_period);

CREATE TABLE IF NOT EXISTS alex_phase2_goal_period_targets (
    goal_id TEXT NOT NULL,
    period TEXT NOT NULL,
    target_minor INTEGER NOT NULL CHECK(target_minor >= 0),
    reason TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(goal_id,period),
    FOREIGN KEY(goal_id) REFERENCES alex_phase2_goals(goal_id)
);

CREATE TABLE IF NOT EXISTS alex_phase2_goal_contributions (
    contribution_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    contribution_kind TEXT NOT NULL
        CHECK(contribution_kind IN ('REGULAR','EXTRA','ONE_OFF','EVIDENCE')),
    contribution_date TEXT NOT NULL,
    period TEXT NOT NULL,
    source_cash_event_id TEXT,
    source_message_id TEXT,
    evidence_ref TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(goal_id) REFERENCES alex_phase2_goals(goal_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_goal_contrib
    ON alex_phase2_goal_contributions(goal_id,contribution_date);

CREATE TABLE IF NOT EXISTS alex_phase2_cash_events (
    cash_event_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK(event_type IN ('SALARY','OT','BONUS','REFUND','OTHER')),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    event_date TEXT NOT NULL,
    description TEXT,
    allocation_state TEXT NOT NULL DEFAULT 'UNALLOCATED'
        CHECK(allocation_state IN ('UNALLOCATED','PARTIAL','ALLOCATED')),
    source_message_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_cash_owner
    ON alex_phase2_cash_events(owner_user_id,space_id,event_date);

CREATE TABLE IF NOT EXISTS alex_phase2_cash_allocations (
    allocation_id TEXT PRIMARY KEY,
    cash_event_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(cash_event_id) REFERENCES alex_phase2_cash_events(cash_event_id),
    FOREIGN KEY(goal_id) REFERENCES alex_phase2_goals(goal_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_cash_alloc
    ON alex_phase2_cash_allocations(cash_event_id);

CREATE TABLE IF NOT EXISTS alex_phase2_cash_pools (
    pool_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','CLOSED')),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_cash_pools
    ON alex_phase2_cash_pools(owner_user_id,space_id,status);

CREATE TABLE IF NOT EXISTS alex_phase2_cash_pool_allocations (
    allocation_id TEXT PRIMARY KEY,
    cash_event_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(cash_event_id) REFERENCES alex_phase2_cash_events(cash_event_id),
    FOREIGN KEY(pool_id) REFERENCES alex_phase2_cash_pools(pool_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_cash_pool_alloc
    ON alex_phase2_cash_pool_allocations(cash_event_id,pool_id);

CREATE TABLE IF NOT EXISTS alex_phase2_cash_pool_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor != 0),
    adjustment_kind TEXT NOT NULL
        CHECK(adjustment_kind IN ('OPENING_BALANCE','MANUAL_ADJUSTMENT','SPEND')),
    event_date TEXT NOT NULL,
    category TEXT,
    funding_source TEXT,
    note TEXT,
    source_message_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(pool_id) REFERENCES alex_phase2_cash_pools(pool_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_cash_pool_adjustments
    ON alex_phase2_cash_pool_adjustments(pool_id,event_date);

CREATE TABLE IF NOT EXISTS alex_phase2_plan_reserves (
    reserve_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    monthly_minor INTEGER NOT NULL CHECK(monthly_minor >= 0),
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS alex_phase2_obligation_instances (
    instance_id TEXT PRIMARY KEY,
    profile_record_key TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    space_id TEXT NOT NULL,
    owner_user_id TEXT,
    period TEXT NOT NULL,
    name TEXT NOT NULL,
    expected_minor INTEGER,
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    due_date TEXT,
    paid_minor INTEGER NOT NULL DEFAULT 0 CHECK(paid_minor >= 0),
    state TEXT NOT NULL DEFAULT 'EXPECTED'
        CHECK(state IN (
            'EXPECTED','UNCONFIRMED_DUE','PARTIAL','PAID',
            'DEFERRED','CONFIRMED_UNPAID','CANCELLED'
        )),
    deferred_to TEXT,
    note TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_record_key,occurrence_key),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_obligation_due
    ON alex_phase2_obligation_instances(space_id,state,due_date);

CREATE TABLE IF NOT EXISTS alex_phase2_evidence_facts (
    evidence_fact_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    fact_type TEXT NOT NULL
        CHECK(fact_type IN ('GOAL_DEPOSIT','OBLIGATION_PAYMENT','INCOME')),
    amount_minor INTEGER,
    currency TEXT CHECK(currency IN ('MYR','SGD')),
    destination_alias TEXT,
    event_date TEXT,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    applied INTEGER NOT NULL DEFAULT 0 CHECK(applied IN (0,1)),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
"""


def ensure_schema(conn=None):
    own = conn is None
    if own:
        conn = tools.get_db()
    try:
        conn.executescript(SCHEMA)
        # Backfill pre-versioning goals once. Existing cached baseline values
        # remain the starting historical truth for their creation month.
        conn.execute("""
            INSERT OR IGNORE INTO alex_phase2_goal_baseline_versions(
                goal_id,effective_period,monthly_minor,reason
            )
            SELECT goal_id,substr(created_at_utc,1,7),baseline_monthly_minor,
                   'baseline history backfill'
            FROM alex_phase2_goals
        """)

        # Cash-pool visibility is persisted data. Never rewrite an existing
        # pool's Family/private space merely because the runtime policy changed;
        # current trusted scope controls access and new writes choose their
        # space through the central scope policy.

        # Bridge only genuine legacy stash/cash-pool buckets into the Phase-2
        # pool engine. Allowances/reserves remain separate concepts. Matching
        # owner+space+name makes this migration idempotent.
        legacy_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='money_buckets'"
        ).fetchone()
        if legacy_table:
            legacy_rows = conn.execute(
                """SELECT bucket_id,owner_id,space_id,bucket_name,amount_minor,currency,notes
                   FROM money_buckets"""
            ).fetchall()
            for legacy in legacy_rows:
                legacy_name = str(legacy["bucket_name"] or "").strip()
                if not re.search(r"\b(?:stash|cash\s*pool|buffer)\b", legacy_name, re.I):
                    continue
                target_space = (
                    "FAMILY_SHARED"
                    if legacy["space_id"] == "FAMILY_SHARED"
                    else (
                        "HUSBAND_PVT"
                        if legacy["owner_id"] == "USR_HUSBAND"
                        else "WIFE_PVT"
                    )
                )
                legacy_source = "legacy-money-bucket:" + str(legacy["bucket_id"])

                # Upgrade identity is the legacy bucket itself, not the pool's
                # current name/space. Older releases may already have bridged a
                # Family bucket into a private pool. If that exact bucket marker
                # exists anywhere, never bridge its balance a second time.
                bridged = conn.execute(
                    """SELECT 1 FROM alex_phase2_cash_pool_adjustments
                       WHERE source_message_id=? LIMIT 1""",
                    (legacy_source,),
                ).fetchone()
                if bridged:
                    continue

                existing = conn.execute(
                    """SELECT pool_id FROM alex_phase2_cash_pools
                       WHERE owner_user_id=?
                         AND LOWER(TRIM(name))=LOWER(TRIM(?))
                         AND status='ACTIVE' LIMIT 1""",
                    (legacy["owner_id"], legacy_name),
                ).fetchone()
                if existing:
                    # Older builds could already have bridged a zero-balance
                    # legacy bucket without writing an adjustment marker. A
                    # same-owner/same-name active pool in any space is enough
                    # evidence that this legacy bucket was already represented.
                    continue
                pool_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO alex_phase2_cash_pools(
                           pool_id,space_id,owner_user_id,name,currency
                       ) VALUES(?,?,?,?,?)""",
                    (
                        pool_id, target_space, legacy["owner_id"],
                        legacy_name, str(legacy["currency"] or "MYR").upper(),
                    ),
                )
                amount_minor = int(legacy["amount_minor"] or 0)
                if amount_minor:
                    conn.execute(
                        """INSERT INTO alex_phase2_cash_pool_adjustments(
                               adjustment_id,pool_id,space_id,owner_user_id,amount_minor,
                               adjustment_kind,event_date,note,source_message_id
                           ) VALUES(?,?,?,?,?,'OPENING_BALANCE',?,?,?)""",
                        (
                            str(uuid.uuid4()), pool_id, target_space,
                            legacy["owner_id"], amount_minor,
                            runtime_clock.today().isoformat(),
                            "Migrated from legacy stash bucket",
                            legacy_source,
                        ),
                    )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _minor(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Amount must be numeric")
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(minor):
    return float(Decimal(minor) / Decimal(100))


def _period(value=None):
    if value is None:
        value = runtime_clock.today()
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return f"{value.year:04d}-{value.month:02d}"


def _context(conn, sender_phone, conversation_type):
    user_id, private_space = tools.resolve_user_and_space(
        conn, sender_phone, conversation_type)
    shared = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND space_id='FAMILY_SHARED'",
        (user_id,),
    ).fetchone() is not None
    return user_id, private_space, shared


def _space_for(conn, sender_phone, conversation_type, visibility):
    user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
    visibility = str(visibility or "private").lower()
    if visibility not in ("private", "family"):
        raise ValueError("Visibility must be private or family")
    if conversation_type == "GROUP":
        if visibility != "family":
            raise PermissionError("Private Phase-2 records cannot be created in the family group")
        if not shared:
            raise PermissionError("Sender is not a family-space member")
        return user_id, "FAMILY_SHARED"
    if visibility == "family":
        if not shared:
            raise PermissionError("Sender is not a family-space member")
        return user_id, "FAMILY_SHARED"
    return user_id, private_space


def _authorized_space_clause(conn, sender_phone, conversation_type, requested_scope="all"):
    user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
    try:
        scope = scope_policy.effective_read_scope(current_actor(), requested_scope)
    except RuntimeError:
        # Direct backend tests/admin calls have no authenticated MCP actor and
        # retain the explicit legacy scope argument.
        scope = str(requested_scope or "all").lower()
    if conversation_type == "GROUP":
        if scope == "private":
            raise PermissionError("Private Phase-2 data cannot be shown in the family group")
        return user_id, private_space, "space_id='FAMILY_SHARED'", ()
    if scope == "private":
        return user_id, private_space, "space_id=?", (private_space,)
    if scope == "family":
        if not shared:
            raise PermissionError("Sender is not a family-space member")
        return user_id, private_space, "space_id='FAMILY_SHARED'", ()
    if shared:
        return user_id, private_space, "(space_id=? OR space_id='FAMILY_SHARED')", (private_space,)
    return user_id, private_space, "space_id=?", (private_space,)


def create_goal(name, target_amount, baseline_monthly, sender_phone,
                conversation_type="DIRECT_DM", visibility="private",
                currency="MYR", target_date=None, status="ACTIVE",
                baseline_effective_period=None):
    if not str(name or "").strip():
        raise ValueError("Goal name is required")
    currency = str(currency or "MYR").upper()
    if currency not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    target_minor = _minor(target_amount)
    baseline_minor = _minor(baseline_monthly)
    if target_minor <= 0:
        raise ValueError("Goal target must be greater than zero")
    if status not in ("DRAFT", "ACTIVE", "PAUSED"):
        raise ValueError("New goal status must be DRAFT, ACTIVE or PAUSED")
    effective_period = baseline_effective_period or _period()
    if not re.fullmatch(r"\d{4}-\d{2}", effective_period):
        raise ValueError("Goal baseline effective period must be YYYY-MM")

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_goals(
                goal_id,space_id,owner_user_id,created_by,name,target_minor,
                currency,baseline_monthly_minor,status,target_date
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, user_id, str(name).strip(),
            target_minor, currency, baseline_minor, status, target_date,
        ))
        conn.execute("""
            INSERT INTO alex_phase2_goal_baseline_versions(
                goal_id,effective_period,monthly_minor,reason
            ) VALUES (?,?,?,'initial baseline')
        """, (ident, effective_period, baseline_minor))
        conn.commit()
        return {
            "goal_id": ident, "name": str(name).strip(), "space": space_id,
            "target": _money(target_minor), "baseline_monthly": _money(baseline_minor),
            "currency": currency, "status": status,
            "baseline_effective_period": effective_period,
        }
    finally:
        conn.close()


def update_goal_target(goal_id, new_target_amount, sender_phone,
                       conversation_type="DIRECT_DM"):
    """Change only the goal target; recurring baseline/deadline stay untouched."""
    ensure_schema()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        target_minor = _minor(new_target_amount)
        if target_minor <= 0:
            raise ValueError("Goal target must be greater than zero")
        conn.execute(
            """UPDATE alex_phase2_goals
               SET target_minor=?,updated_at_utc=CURRENT_TIMESTAMP
               WHERE goal_id=?""",
            (target_minor, goal_id),
        )
        funded = conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_goal_contributions WHERE goal_id=?""",
            (goal_id,),
        ).fetchone()["total"]
        conn.commit()
        return {
            "status": "updated",
            "goal_id": goal_id,
            "name": goal["name"],
            "target": _money(target_minor),
            "funded": _money(funded),
            "remaining": _money(max(0, target_minor - funded)),
            "baseline_monthly": _money(goal["baseline_monthly_minor"]),
            "target_date": goal["target_date"],
            "baseline_changed": False,
            "deadline_changed": False,
        }
    finally:
        conn.close()


def _get_authorized_goal(conn, goal_id, sender_phone, conversation_type):
    user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
    row = conn.execute(
        "SELECT * FROM alex_phase2_goals WHERE goal_id=?", (goal_id,)
    ).fetchone()
    if not row:
        raise ValueError("Goal not found")
    if row["space_id"] == "FAMILY_SHARED":
        if not shared:
            raise PermissionError("Goal is not authorized")
    elif row["space_id"] != private_space or row["owner_user_id"] != user_id:
        raise PermissionError("Goal is not authorized")
    if conversation_type == "GROUP" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("Private goal cannot be used in the family group")
    return row


def _normalized_ref(value):
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    while words and words[0] in {"my", "our", "the"}:
        words.pop(0)
    return " ".join(words)


def resolve_goal_reference(goal_id, goal_name, sender_phone,
                           conversation_type="DIRECT_DM"):
    """Resolve an authorized goal from an opaque id or a unique natural name.

    User-facing reasoning should not need to invent UUIDs. Fuzzy matching is
    deliberately conservative: one unique exact/containment match is required.
    """
    ensure_schema()
    conn = tools.get_db()
    try:
        if goal_id:
            return _get_authorized_goal(
                conn, goal_id, sender_phone, conversation_type
            )["goal_id"]
    finally:
        conn.close()

    wanted = _normalized_ref(goal_name)
    if not wanted:
        raise ValueError("Provide goal_id or goal_name.")
    goals = list_goals(sender_phone, conversation_type, requested_scope="all")
    exact = [
        row for row in goals
        if _normalized_ref(row.get("name")) == wanted
    ]
    candidates = exact or [
        row for row in goals
        if wanted in _normalized_ref(row.get("name"))
        or _normalized_ref(row.get("name")) in wanted
    ]
    if len(candidates) == 1:
        return candidates[0]["goal_id"]
    if not candidates:
        raise ValueError(f"No authorized goal uniquely matches {goal_name!r}.")
    names = ", ".join(str(row.get("name")) for row in candidates[:5])
    raise ValueError(
        f"Goal name is ambiguous; ask which one: {names}"
    )


def _get_authorized_cash_event(conn, cash_event_id, sender_phone,
                               conversation_type):
    user_id, private_space, shared = _context(
        conn, sender_phone, conversation_type
    )
    row = conn.execute(
        "SELECT * FROM alex_phase2_cash_events WHERE cash_event_id=?",
        (cash_event_id,),
    ).fetchone()
    if not row:
        raise ValueError("Cash event not found")
    if row["owner_user_id"] != user_id:
        raise PermissionError("Cash event not authorized")
    if conversation_type == "GROUP":
        if row["space_id"] != "FAMILY_SHARED":
            raise PermissionError("Private cash event cannot be used in group")
    elif row["space_id"] not in (
        private_space, "FAMILY_SHARED" if shared else private_space
    ):
        raise PermissionError("Cash event not authorized")
    return row


def resolve_cash_event_reference(
    cash_event_id, sender_phone, conversation_type="DIRECT_DM",
    *, event_type=None, event_date=None, amount=None,
    source_message_id=None, require_unallocated=False, latest=False,
):
    """Resolve one authorized cash event without asking the model to invent ids."""
    ensure_schema()
    conn = tools.get_db()
    try:
        if cash_event_id:
            return _get_authorized_cash_event(
                conn, cash_event_id, sender_phone, conversation_type
            )["cash_event_id"]

        user_id, private_space, shared = _context(
            conn, sender_phone, conversation_type
        )
        if conversation_type == "GROUP":
            space_sql, space_args = "space_id='FAMILY_SHARED'", []
        elif shared:
            space_sql, space_args = "(space_id=? OR space_id='FAMILY_SHARED')", [private_space]
        else:
            space_sql, space_args = "space_id=?", [private_space]

        where = [space_sql, "owner_user_id=?"]
        params = list(space_args) + [user_id]
        if event_type:
            where.append("event_type=?")
            params.append(str(event_type).upper())
        if event_date:
            where.append("event_date=?")
            params.append(str(event_date)[:10])
        if amount is not None:
            where.append("amount_minor=?")
            params.append(_minor(amount))
        if require_unallocated:
            where.append("allocation_state IN ('UNALLOCATED','PARTIAL')")

        rows = conn.execute(
            """SELECT * FROM alex_phase2_cash_events WHERE """
            + " AND ".join(where)
            + " ORDER BY event_date DESC,created_at_utc DESC",
            params,
        ).fetchall()

        # A cash record created by this exact inbound message is unambiguous.
        if source_message_id:
            source_matches = [
                row for row in rows
                if row["source_message_id"] == source_message_id
            ]
            if len(source_matches) == 1:
                return source_matches[0]["cash_event_id"]

        if len(rows) == 1 or (latest and rows):
            return rows[0]["cash_event_id"]
        if not rows:
            raise ValueError("No authorized cash event matches that description.")
        choices = "; ".join(
            f"{row['event_type']} {row['event_date']} "
            f"{_money(row['amount_minor']):.2f} {row['currency']}"
            for row in rows[:5]
        )
        raise ValueError(
            "More than one cash event matches. Ask the user which one: " + choices
        )
    finally:
        conn.close()


def resolve_cash_pool_reference(pool_id, pool_name, sender_phone,
                                conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        if pool_id:
            return _get_authorized_pool(
                conn, pool_id, sender_phone, conversation_type
            )["pool_id"]
        _user_id, _private_space, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, "all"
        )
        rows = conn.execute(
            "SELECT * FROM alex_phase2_cash_pools WHERE status='ACTIVE' AND "
            + clause + " ORDER BY name",
            args,
        ).fetchall()
        wanted = _normalized_ref(pool_name)
        if not wanted:
            if len(rows) == 1:
                return rows[0]["pool_id"]
            if not rows:
                raise ValueError("No matching stash or cash pool was found in the current authorized scope.")
            names = ", ".join(row["name"] for row in rows[:8])
            raise ValueError("More than one stash/cash pool exists; ask which one: " + names)
        exact = [
            row for row in rows if _normalized_ref(row["name"]) == wanted
        ]
        candidates = exact or [
            row for row in rows
            if wanted in _normalized_ref(row["name"])
            or _normalized_ref(row["name"]) in wanted
        ]
        if len(candidates) == 1:
            return candidates[0]["pool_id"]
        if not candidates:
            raise ValueError(f"No matching cash pool was found for {pool_name!r} in the current authorized scope.")
        names = ", ".join(row["name"] for row in candidates[:5])
        raise ValueError("Cash-pool name is ambiguous; ask which one: " + names)
    finally:
        conn.close()

def resolve_reserve_reference(reserve_id, reserve_name, sender_phone,
                              conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        if reserve_id:
            return _authorized_reserve(
                conn, reserve_id, sender_phone, conversation_type
            )["reserve_id"]
    finally:
        conn.close()
    wanted = _normalized_ref(reserve_name)
    if not wanted:
        raise ValueError("Provide reserve_id or reserve_name.")
    rows = list_plan_reserves(
        sender_phone, conversation_type, "all", include_inactive=True
    )
    exact = [row for row in rows if _normalized_ref(row["name"]) == wanted]
    candidates = exact or [
        row for row in rows
        if wanted in _normalized_ref(row["name"])
        or _normalized_ref(row["name"]) in wanted
    ]
    if len(candidates) == 1:
        return candidates[0]["reserve_id"]
    if not candidates:
        raise ValueError(f"No authorized reserve uniquely matches {reserve_name!r}.")
    names = ", ".join(row["name"] for row in candidates[:5])
    raise ValueError("Reserve name is ambiguous; ask which one: " + names)


def lock_goal(goal_id, sender_phone, conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        if goal["status"] not in ("DRAFT", "PAUSED", "ACTIVE"):
            raise ValueError("Goal cannot be locked from its current state")
        conn.execute("""
            UPDATE alex_phase2_goals
            SET status='ACTIVE',updated_at_utc=CURRENT_TIMESTAMP WHERE goal_id=?
        """, (goal_id,))
        conn.commit()
        return {"goal_id": goal_id, "status": "ACTIVE", "plan_locked": True}
    finally:
        conn.close()


def reopen_goal(goal_id, sender_phone, conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        if goal["status"] in ("COMPLETED", "CANCELLED"):
            raise ValueError("Closed goal cannot be reopened for planning")
        conn.execute("""
            UPDATE alex_phase2_goals
            SET status='DRAFT',updated_at_utc=CURRENT_TIMESTAMP WHERE goal_id=?
        """, (goal_id,))
        conn.commit()
        return {"goal_id": goal_id, "status": "DRAFT", "plan_locked": False}
    finally:
        conn.close()


def _goal_baseline_for_period(conn, goal_id, period, fallback_minor=0):
    row = conn.execute("""
        SELECT monthly_minor FROM alex_phase2_goal_baseline_versions
        WHERE goal_id=? AND effective_period<=?
        ORDER BY effective_period DESC LIMIT 1
    """, (goal_id, period)).fetchone()
    return row["monthly_minor"] if row else fallback_minor


def set_goal_baseline(goal_id, new_monthly_amount, sender_phone,
                      conversation_type="DIRECT_DM",
                      effective_from_period=None, reason=None):
    """Change the recurring plan from one month onward without rewriting history."""
    effective_period = effective_from_period or _period()
    if not re.fullmatch(r"\d{4}-\d{2}", effective_period):
        raise ValueError("Goal baseline effective period must be YYYY-MM")
    ensure_schema()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(
            conn, goal_id, sender_phone, conversation_type)
        value = _minor(new_monthly_amount)
        conn.execute("""
            INSERT INTO alex_phase2_goal_baseline_versions(
                goal_id,effective_period,monthly_minor,reason
            ) VALUES (?,?,?,?)
            ON CONFLICT(goal_id,effective_period) DO UPDATE SET
              monthly_minor=excluded.monthly_minor,
              reason=excluded.reason,
              created_at_utc=CURRENT_TIMESTAMP
        """, (goal_id, effective_period, value, reason))
        current_value = _goal_baseline_for_period(
            conn, goal_id, _period(), goal["baseline_monthly_minor"])
        conn.execute("""
            UPDATE alex_phase2_goals
            SET baseline_monthly_minor=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE goal_id=?
        """, (current_value, goal_id))
        conn.commit()
        return {
            "goal_id": goal_id,
            "baseline_monthly": _money(value),
            "effective_from_period": effective_period,
        }
    finally:
        conn.close()


def set_goal_period_target(goal_id, period, amount, sender_phone,
                           conversation_type="DIRECT_DM", reason=None):
    if not isinstance(period, str) or len(period) != 7:
        raise ValueError("Period must be YYYY-MM")
    ensure_schema()
    conn = tools.get_db()
    try:
        _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        value = _minor(amount)
        conn.execute("""
            INSERT INTO alex_phase2_goal_period_targets(goal_id,period,target_minor,reason)
            VALUES (?,?,?,?)
            ON CONFLICT(goal_id,period) DO UPDATE SET
              target_minor=excluded.target_minor,
              reason=excluded.reason,
              created_at_utc=CURRENT_TIMESTAMP
        """, (goal_id, period, value, reason))
        conn.commit()
        return {"goal_id": goal_id, "period": period, "target": _money(value)}
    finally:
        conn.close()


def goal_expected_for_period(conn, goal_row, period):
    row = conn.execute("""
        SELECT target_minor FROM alex_phase2_goal_period_targets
        WHERE goal_id=? AND period=?
    """, (goal_row["goal_id"], period)).fetchone()
    if row:
        return row["target_minor"]
    return _goal_baseline_for_period(
        conn, goal_row["goal_id"], period,
        goal_row["baseline_monthly_minor"])


def record_goal_contribution(goal_id, amount, contribution_date,
                             sender_phone, conversation_type="DIRECT_DM",
                             contribution_kind="ONE_OFF",
                             source_cash_event_id=None,
                             source_message_id=None, evidence_ref=None):
    if contribution_kind not in ("REGULAR", "EXTRA", "ONE_OFF", "EVIDENCE"):
        raise ValueError("Unsupported contribution kind")
    d = date.fromisoformat(str(contribution_date)[:10])
    ensure_schema()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        amount_minor = _minor(amount)
        if amount_minor <= 0:
            raise ValueError("Contribution must be greater than zero")
        if source_message_id:
            existing = conn.execute(
                """SELECT contribution_id,amount_minor,currency,period
                   FROM alex_phase2_goal_contributions
                   WHERE goal_id=? AND source_message_id=?
                   ORDER BY created_at_utc LIMIT 1""",
                (goal_id, source_message_id),
            ).fetchone()
            if existing:
                return {
                    "status": "already_applied",
                    "contribution_id": existing["contribution_id"],
                    "goal_id": goal_id,
                    "amount": _money(existing["amount_minor"]),
                    "currency": existing["currency"],
                    "period": existing["period"],
                    "baseline_changed": False,
                }
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_goal_contributions(
                contribution_id,goal_id,space_id,owner_user_id,amount_minor,
                currency,contribution_kind,contribution_date,period,
                source_cash_event_id,source_message_id,evidence_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, goal_id, goal["space_id"], goal["owner_user_id"],
            amount_minor, goal["currency"], contribution_kind,
            d.isoformat(), _period(d), source_cash_event_id,
            source_message_id, evidence_ref,
        ))
        conn.commit()
        return {
            "contribution_id": ident, "goal_id": goal_id,
            "amount": _money(amount_minor), "currency": goal["currency"],
            "period": _period(d), "baseline_changed": False,
        }
    finally:
        conn.close()


def goal_progress(goal_id, sender_phone, conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        funded = conn.execute("""
            SELECT COALESCE(SUM(amount_minor),0) AS total
            FROM alex_phase2_goal_contributions WHERE goal_id=?
        """, (goal_id,)).fetchone()["total"]
        remaining = max(0, goal["target_minor"] - funded)
        return {
            "goal_id": goal_id, "name": goal["name"],
            "target": _money(goal["target_minor"]),
            "funded": _money(funded), "remaining": _money(remaining),
            "baseline_monthly": _money(_goal_baseline_for_period(
                conn, goal_id, _period(), goal["baseline_monthly_minor"])),
            "currency": goal["currency"], "space": goal["space_id"],
        }
    finally:
        conn.close()


def evaluate_goal_deviation(goal_id, actual_amount, period, sender_phone,
                            conversation_type="DIRECT_DM"):
    ensure_schema()
    period = period or _period()
    conn = tools.get_db()
    try:
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        expected = goal_expected_for_period(conn, goal, period)
        if actual_amount is None:
            actual = conn.execute(
                """SELECT COALESCE(SUM(amount_minor),0) AS total
                   FROM alex_phase2_goal_contributions
                   WHERE goal_id=? AND period=?""",
                (goal_id, period),
            ).fetchone()["total"]
        else:
            actual = _minor(actual_amount)
        delta = actual - expected
        if delta == 0:
            status = "ON_PLAN"
            prompt = None
        elif delta < 0:
            status = "BELOW_PLAN"
            prompt = (
                f"Recorded {_money(actual):.2f} for {goal['name']}; "
                f"this period's plan is {_money(expected):.2f}. "
                "Is this a partial contribution, a one-period exception, "
                "or do you want to change the regular plan?"
            )
        else:
            status = "ABOVE_PLAN"
            prompt = None
        return {
            "goal_id": goal_id, "period": period, "status": status,
            "expected": _money(expected), "actual": _money(actual),
            "delta": _money(delta), "baseline_changed": False,
            "prompt": prompt,
        }
    finally:
        conn.close()


def record_cash_event(event_type, amount, event_date, sender_phone,
                      conversation_type="DIRECT_DM", visibility="private",
                      currency="MYR", description=None, source_message_id=None):
    event_type = str(event_type or "").upper()
    if event_type not in ("SALARY", "OT", "BONUS", "REFUND", "OTHER"):
        raise ValueError("Unsupported cash event type")
    currency = str(currency or "MYR").upper()
    if currency not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    d = date.fromisoformat(str(event_date)[:10])
    value = _minor(amount)
    if value <= 0:
        raise ValueError("Cash event must be greater than zero")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_cash_events(
                cash_event_id,space_id,owner_user_id,event_type,amount_minor,
                currency,event_date,description,source_message_id
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, event_type, value, currency,
            d.isoformat(), description, source_message_id,
        ))
        conn.commit()
        return {
            "cash_event_id": ident, "event_type": event_type,
            "amount": _money(value), "currency": currency,
            "allocation_state": "UNALLOCATED", "space": space_id,
        }
    finally:
        conn.close()


def _allocated_cash_minor(conn, cash_event_id):
    goal_total = conn.execute("""
        SELECT COALESCE(SUM(amount_minor),0) AS total
        FROM alex_phase2_cash_allocations WHERE cash_event_id=?
    """, (cash_event_id,)).fetchone()["total"]
    pool_total = conn.execute("""
        SELECT COALESCE(SUM(amount_minor),0) AS total
        FROM alex_phase2_cash_pool_allocations WHERE cash_event_id=?
    """, (cash_event_id,)).fetchone()["total"]
    return int(goal_total or 0) + int(pool_total or 0)


def create_cash_pool(name, sender_phone, conversation_type="DIRECT_DM",
                     visibility="private", currency="MYR",
                     opening_balance=None, event_date=None,
                     source_message_id=None):
    """Create one stash/cash pool, optionally with an atomic opening balance."""
    if not str(name or "").strip():
        raise ValueError("Cash pool name is required")
    currency = str(currency or "MYR").upper()
    if currency not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    target_minor = _minor(opening_balance) if opening_balance is not None else None
    effective_date = date.fromisoformat(
        str(event_date or runtime_clock.today().isoformat())[:10]
    )
    ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility
        )
        exact = conn.execute(
            """SELECT * FROM alex_phase2_cash_pools
               WHERE owner_user_id=? AND space_id=? AND status='ACTIVE'
                 AND LOWER(TRIM(name))=LOWER(TRIM(?))
               ORDER BY created_at_utc LIMIT 2""",
            (user_id, space_id, str(name).strip()),
        ).fetchall()
        if len(exact) > 1:
            raise ValueError("More than one active cash pool already has that name.")
        created = not exact
        if exact:
            ident = exact[0]["pool_id"]
        else:
            ident = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO alex_phase2_cash_pools(
                    pool_id,space_id,owner_user_id,name,currency
                ) VALUES (?,?,?,?,?)""",
                (ident, space_id, user_id, str(name).strip(), currency),
            )

        allocated = int(conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_cash_pool_allocations WHERE pool_id=?""",
            (ident,),
        ).fetchone()["total"] or 0)
        adjusted = int(conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_cash_pool_adjustments WHERE pool_id=?""",
            (ident,),
        ).fetchone()["total"] or 0)
        current = allocated + adjusted

        opening_applied = False
        if target_minor is not None:
            if current == target_minor:
                pass
            elif current == 0:
                if target_minor:
                    conn.execute(
                        """INSERT INTO alex_phase2_cash_pool_adjustments(
                               adjustment_id,pool_id,space_id,owner_user_id,amount_minor,
                               adjustment_kind,event_date,note,source_message_id
                           ) VALUES(?,?,?,?,?,'OPENING_BALANCE',?,'Opening balance',?)""",
                        (
                            str(uuid.uuid4()), ident, space_id, user_id, target_minor,
                            effective_date.isoformat(), source_message_id,
                        ),
                    )
                current = target_minor
                opening_applied = True
            else:
                # "Create X with RM100" must never silently overwrite an
                # already-funded pool of the same name.
                conn.rollback()
                return {
                    "status": "already_exists_with_balance",
                    "pool_id": ident, "name": str(name).strip(),
                    "balance": _money(current), "currency": exact[0]["currency"],
                    "space": space_id,
                }

        conn.commit()
        return {
            "status": "created" if created else "already_exists",
            "pool_id": ident, "name": str(name).strip(), "space": space_id,
            "balance": _money(current), "currency": currency,
            "opening_balance_applied": opening_applied,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_cash_pools(sender_phone, conversation_type="DIRECT_DM",
                    requested_scope="all"):
    """List authorized active stash/cash pools with deterministic balances."""
    ensure_schema()
    conn = tools.get_db()
    try:
        _user_id, _private_space, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope
        )
        rows = conn.execute(
            """SELECT * FROM alex_phase2_cash_pools
               WHERE status='ACTIVE' AND """ + clause + " ORDER BY name",
            args,
        ).fetchall()
        result = []
        for row in rows:
            allocated = int(conn.execute(
                """SELECT COALESCE(SUM(amount_minor),0) AS total
                   FROM alex_phase2_cash_pool_allocations WHERE pool_id=?""",
                (row["pool_id"],),
            ).fetchone()["total"] or 0)
            adjusted = int(conn.execute(
                """SELECT COALESCE(SUM(amount_minor),0) AS total
                   FROM alex_phase2_cash_pool_adjustments WHERE pool_id=?""",
                (row["pool_id"],),
            ).fetchone()["total"] or 0)
            result.append({
                "pool_id": row["pool_id"], "name": row["name"],
                "balance": _money(allocated + adjusted),
                "currency": row["currency"],
                "scope": "family" if row["space_id"] == "FAMILY_SHARED" else "private",
            })
        return result
    finally:
        conn.close()

def _get_authorized_pool(conn, pool_id, sender_phone, conversation_type):
    user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
    row = conn.execute(
        "SELECT * FROM alex_phase2_cash_pools WHERE pool_id=?", (pool_id,)
    ).fetchone()
    if not row:
        raise ValueError("Cash pool not found in the records available to the current scope")
    if row["space_id"] == "FAMILY_SHARED":
        if not shared:
            raise PermissionError("Cash pool is not authorized")
    elif row["space_id"] != private_space or row["owner_user_id"] != user_id:
        raise PermissionError("Cash pool is not authorized")
    if conversation_type == "GROUP" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("Private cash pool cannot be used in group")

    # A live MCP turn is also constrained by the current trusted read/privacy
    # scope. This prevents a remembered/private pool id from bypassing the
    # global Family-by-default rule. Direct backend/admin calls with no actor
    # retain the legacy owner authorization above.
    try:
        scope = scope_policy.effective_read_scope(current_actor(), "all")
    except RuntimeError:
        scope = None
    if scope == "family" and row["space_id"] != "FAMILY_SHARED":
        raise ValueError("Cash pool not found in the records available to the current scope")
    if scope == "private" and row["space_id"] != private_space:
        raise ValueError("Cash pool not found in the records available to the current scope")
    return row


def cash_pool_balance(pool_id, sender_phone, conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        pool = _get_authorized_pool(
            conn, pool_id, sender_phone, conversation_type)
        allocated = conn.execute("""
            SELECT COALESCE(SUM(amount_minor),0) AS total
            FROM alex_phase2_cash_pool_allocations WHERE pool_id=?
        """, (pool_id,)).fetchone()["total"]
        adjusted = conn.execute("""
            SELECT COALESCE(SUM(amount_minor),0) AS total
            FROM alex_phase2_cash_pool_adjustments WHERE pool_id=?
        """, (pool_id,)).fetchone()["total"]
        total = int(allocated or 0) + int(adjusted or 0)
        return {
            "pool_id": pool_id, "name": pool["name"],
            "balance": _money(total), "currency": pool["currency"],
            "space": pool["space_id"],
            "cash_event_allocations": _money(int(allocated or 0)),
            "manual_adjustments": _money(int(adjusted or 0)),
        }
    finally:
        conn.close()


def declare_cash_pool_balance(pool_id, amount, event_date, sender_phone,
                              conversation_type="DIRECT_DM", note=None,
                              source_message_id=None):
    """Set a user-declared current pool/stash balance without inventing income."""
    target = _minor(amount)
    if target < 0:
        raise ValueError("Declared cash-pool balance cannot be negative")
    d = date.fromisoformat(str(event_date)[:10])
    ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pool = _get_authorized_pool(conn, pool_id, sender_phone, conversation_type)
        if source_message_id:
            prior = conn.execute(
                """SELECT adjustment_id FROM alex_phase2_cash_pool_adjustments
                   WHERE pool_id=? AND source_message_id=? LIMIT 1""",
                (pool_id, source_message_id),
            ).fetchone()
            if prior:
                conn.rollback()
                current = cash_pool_balance(
                    pool_id, sender_phone, conversation_type
                )
                return {"status": "already_applied", **current}
        allocated = conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_cash_pool_allocations WHERE pool_id=?""",
            (pool_id,),
        ).fetchone()["total"]
        adjusted = conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_cash_pool_adjustments WHERE pool_id=?""",
            (pool_id,),
        ).fetchone()["total"]
        current = int(allocated or 0) + int(adjusted or 0)
        delta = target - current
        if delta == 0:
            conn.rollback()
            return {
                "status": "already_in_state", "pool_id": pool_id,
                "balance": _money(target), "currency": pool["currency"],
            }
        prior_adjustments = conn.execute(
            "SELECT COUNT(*) AS n FROM alex_phase2_cash_pool_adjustments WHERE pool_id=?",
            (pool_id,),
        ).fetchone()["n"]
        kind = "OPENING_BALANCE" if current == 0 and not prior_adjustments else "MANUAL_ADJUSTMENT"
        conn.execute(
            """INSERT INTO alex_phase2_cash_pool_adjustments(
                   adjustment_id,pool_id,space_id,owner_user_id,amount_minor,
                   adjustment_kind,event_date,note,source_message_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), pool_id, pool["space_id"], pool["owner_user_id"],
                delta, kind, d.isoformat(), note, source_message_id,
            ),
        )
        conn.commit()
        return {
            "status": "updated", "pool_id": pool_id,
            "balance": _money(target), "currency": pool["currency"],
            "adjustment": _money(delta), "adjustment_kind": kind,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_cash_pool_spend(pool_id, amount, event_date, sender_phone,
                           conversation_type="DIRECT_DM", category=None,
                           funding_source=None, note=None,
                           source_message_id=None):
    """Track spending from discretionary stash without double-counting income."""
    value = _minor(amount)
    if value <= 0:
        raise ValueError("Stash spending must be greater than zero")
    d = date.fromisoformat(str(event_date)[:10])
    ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pool = _get_authorized_pool(conn, pool_id, sender_phone, conversation_type)
        if source_message_id:
            prior = conn.execute(
                """SELECT adjustment_id,amount_minor FROM alex_phase2_cash_pool_adjustments
                   WHERE pool_id=? AND source_message_id=? LIMIT 1""",
                (pool_id, source_message_id),
            ).fetchone()
            if prior:
                conn.rollback()
                return {
                    "status": "already_applied", "pool_id": pool_id,
                    "spent": _money(abs(prior["amount_minor"])),
                }
        allocated = conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS total FROM alex_phase2_cash_pool_allocations WHERE pool_id=?",
            (pool_id,),
        ).fetchone()["total"]
        adjusted = conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS total FROM alex_phase2_cash_pool_adjustments WHERE pool_id=?",
            (pool_id,),
        ).fetchone()["total"]
        balance = int(allocated or 0) + int(adjusted or 0)
        if value > balance:
            raise ValueError("Stash spending exceeds the recorded pool balance")
        conn.execute(
            """INSERT INTO alex_phase2_cash_pool_adjustments(
                   adjustment_id,pool_id,space_id,owner_user_id,amount_minor,
                   adjustment_kind,event_date,category,funding_source,note,source_message_id
               ) VALUES(?,?,?,?,?,'SPEND',?,?,?,?,?)""",
            (
                str(uuid.uuid4()), pool_id, pool["space_id"], pool["owner_user_id"],
                -value, d.isoformat(), category, funding_source, note,
                source_message_id,
            ),
        )
        conn.commit()
        return {
            "status": "recorded", "pool_id": pool_id,
            "spent": _money(value), "balance": _money(balance - value),
            "category": category, "funding_source": funding_source,
            "currency": pool["currency"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cash_outflow_components(period, sender_phone,
                            conversation_type="DIRECT_DM",
                            requested_scope="all", currency="MYR"):
    """Separate actual spending, goal savings and internal stash movements."""
    if not re.fullmatch(r"\d{4}-\d{2}", str(period or "")):
        raise ValueError("period must be YYYY-MM")
    currency = str(currency or "MYR").upper()
    ensure_schema()
    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope
        )
        goal = conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_goal_contributions
               WHERE period=? AND currency=? AND """ + clause,
            (period, currency, *args),
        ).fetchone()["total"]
        stash_in = conn.execute(
            """SELECT COALESCE(SUM(a.amount_minor),0) AS total
               FROM alex_phase2_cash_pool_allocations a
               JOIN alex_phase2_cash_events e ON e.cash_event_id=a.cash_event_id
               WHERE substr(a.created_at_utc,1,7)=? AND e.currency=? AND """
            + clause.replace("space_id", "e.space_id"),
            (period, currency, *args),
        ).fetchone()["total"]
        stash_adjust = conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) AS total
               FROM alex_phase2_cash_pool_adjustments
               WHERE substr(event_date,1,7)=? AND amount_minor>0 AND """
            + clause,
            (period, *args),
        ).fetchone()["total"]
        stash_spend = conn.execute(
            """SELECT COALESCE(SUM(-amount_minor),0) AS total
               FROM alex_phase2_cash_pool_adjustments
               WHERE substr(event_date,1,7)=? AND adjustment_kind='SPEND' AND """
            + clause,
            (period, *args),
        ).fetchone()["total"]
        return {
            "period": period, "currency": currency,
            "goal_savings_contributions": _money(int(goal or 0)),
            "internal_stash_allocations": _money(int(stash_in or 0) + int(stash_adjust or 0)),
            "stash_spending": _money(int(stash_spend or 0)),
            "internal_allocations_included_in_cash_outflow": False,
        }
    finally:
        conn.close()


def cash_event_status(cash_event_id, sender_phone, conversation_type="DIRECT_DM"):
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
        row = conn.execute(
            "SELECT * FROM alex_phase2_cash_events WHERE cash_event_id=?",
            (cash_event_id,),
        ).fetchone()
        if not row:
            raise ValueError("Cash event not found")
        if row["space_id"] == "FAMILY_SHARED":
            if not shared:
                raise PermissionError("Cash event not authorized")
        elif row["space_id"] != private_space or row["owner_user_id"] != user_id:
            raise PermissionError("Cash event not authorized")
        allocated = _allocated_cash_minor(conn, cash_event_id)
        return {
            "cash_event_id": cash_event_id,
            "amount": _money(row["amount_minor"]),
            "allocated": _money(allocated),
            "unallocated": _money(row["amount_minor"] - allocated),
            "allocation_state": row["allocation_state"],
            "event_type": row["event_type"],
        }
    finally:
        conn.close()


def allocate_cash_to_goal(cash_event_id, goal_id, amount, sender_phone,
                          conversation_type="DIRECT_DM", contribution_date=None):
    contribution_date = contribution_date or runtime_clock.today().isoformat()
    value = _minor(amount)
    if value <= 0:
        raise ValueError("Allocation must be greater than zero")
    ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
        cash = conn.execute(
            "SELECT * FROM alex_phase2_cash_events WHERE cash_event_id=?",
            (cash_event_id,),
        ).fetchone()
        if not cash:
            raise ValueError("Cash event not found")
        if cash["owner_user_id"] != user_id:
            raise PermissionError("Cash event not authorized")
        if cash["space_id"] != goal["space_id"]:
            raise PermissionError(
                "Private/shared cash cannot silently cross a privacy boundary")
        if cash["currency"] != goal["currency"]:
            raise ValueError(
                "Cash and goal currencies differ; explicit conversion is required")
        allocated = _allocated_cash_minor(conn, cash_event_id)
        if allocated + value > cash["amount_minor"]:
            raise ValueError("Allocation exceeds unallocated cash")
        allocation_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_cash_allocations(
                allocation_id,cash_event_id,goal_id,amount_minor
            ) VALUES (?,?,?,?)
        """, (allocation_id, cash_event_id, goal_id, value))
        contribution_id = str(uuid.uuid4())
        d = date.fromisoformat(str(contribution_date)[:10])
        conn.execute("""
            INSERT INTO alex_phase2_goal_contributions(
                contribution_id,goal_id,space_id,owner_user_id,amount_minor,
                currency,contribution_kind,contribution_date,period,
                source_cash_event_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            contribution_id, goal_id, goal["space_id"], goal["owner_user_id"],
            value, goal["currency"], "EXTRA", d.isoformat(), _period(d),
            cash_event_id,
        ))
        new_allocated = allocated + value
        state = (
            "ALLOCATED" if new_allocated == cash["amount_minor"] else "PARTIAL"
        )
        conn.execute("""
            UPDATE alex_phase2_cash_events SET allocation_state=?
            WHERE cash_event_id=?
        """, (state, cash_event_id))
        conn.commit()
        return {
            "cash_event_id": cash_event_id, "goal_id": goal_id,
            "allocated": _money(value),
            "remaining_unallocated": _money(cash["amount_minor"] - new_allocated),
            "allocation_state": state, "baseline_changed": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def allocate_cash_to_pool(cash_event_id, pool_id, amount, sender_phone,
                          conversation_type="DIRECT_DM"):
    value = _minor(amount)
    if value <= 0:
        raise ValueError("Allocation must be greater than zero")
    ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pool = _get_authorized_pool(
            conn, pool_id, sender_phone, conversation_type)
        user_id, private_space, shared = _context(
            conn, sender_phone, conversation_type)
        cash = conn.execute(
            "SELECT * FROM alex_phase2_cash_events WHERE cash_event_id=?",
            (cash_event_id,),
        ).fetchone()
        if not cash or cash["owner_user_id"] != user_id:
            raise PermissionError("Cash event not authorized")
        if cash["space_id"] != pool["space_id"]:
            raise PermissionError(
                "Private/shared cash cannot silently cross a privacy boundary")
        if cash["currency"] != pool["currency"]:
            raise ValueError(
                "Cash and pool currencies differ; explicit conversion is required")
        allocated = _allocated_cash_minor(conn, cash_event_id)
        if allocated + value > cash["amount_minor"]:
            raise ValueError("Allocation exceeds unallocated cash")
        conn.execute("""
            INSERT INTO alex_phase2_cash_pool_allocations(
                allocation_id,cash_event_id,pool_id,amount_minor
            ) VALUES (?,?,?,?)
        """, (str(uuid.uuid4()), cash_event_id, pool_id, value))
        new_allocated = allocated + value
        state = "ALLOCATED" if new_allocated == cash["amount_minor"] else "PARTIAL"
        conn.execute("""
            UPDATE alex_phase2_cash_events SET allocation_state=?
            WHERE cash_event_id=?
        """, (state, cash_event_id))
        conn.commit()
        return {
            "cash_event_id": cash_event_id, "pool_id": pool_id,
            "allocated": _money(value),
            "remaining_unallocated": _money(cash["amount_minor"] - new_allocated),
            "allocation_state": state,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_plan_reserve(name, monthly_amount, sender_phone,
                     conversation_type="DIRECT_DM", visibility="private",
                     currency="MYR"):
    if not str(name or "").strip():
        raise ValueError("Reserve name is required")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        value = _minor(monthly_amount)
        conn.execute("""
            INSERT INTO alex_phase2_plan_reserves(
                reserve_id,space_id,owner_user_id,name,monthly_minor,currency
            ) VALUES (?,?,?,?,?,?)
        """, (ident, space_id, user_id, str(name).strip(), value, currency.upper()))
        conn.commit()
        return {"reserve_id": ident, "monthly": _money(value), "space": space_id}
    finally:
        conn.close()


def _authorized_reserve(conn, reserve_id, sender_phone, conversation_type):
    user_id, private_space, shared = _context(
        conn, sender_phone, conversation_type)
    row = conn.execute(
        "SELECT * FROM alex_phase2_plan_reserves WHERE reserve_id=?",
        (reserve_id,),
    ).fetchone()
    if not row:
        raise ValueError("Reserve not found")
    if row["space_id"] == "FAMILY_SHARED":
        if not shared:
            raise PermissionError("Reserve not authorized")
    elif row["space_id"] != private_space or row["owner_user_id"] != user_id:
        raise PermissionError("Reserve not authorized")
    if conversation_type == "GROUP" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("Private reserve cannot be changed in group")
    return row


def update_plan_reserve(reserve_id, sender_phone,
                        conversation_type="DIRECT_DM",
                        monthly_amount=None, name=None, active=None):
    """Edit a reserve/allowance without changing unrelated plan data."""
    ensure_schema()
    conn = tools.get_db()
    try:
        row = _authorized_reserve(
            conn, reserve_id, sender_phone, conversation_type)
        next_name = row["name"] if name is None else str(name).strip()
        if not next_name:
            raise ValueError("Reserve name cannot be empty")
        next_minor = (
            row["monthly_minor"] if monthly_amount is None
            else _minor(monthly_amount)
        )
        next_active = row["active"] if active is None else (1 if active else 0)
        conn.execute("""
            UPDATE alex_phase2_plan_reserves
            SET name=?,monthly_minor=?,active=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE reserve_id=?
        """, (next_name, next_minor, next_active, reserve_id))
        conn.commit()
        return {
            "reserve_id": reserve_id, "name": next_name,
            "monthly": _money(next_minor), "active": bool(next_active),
            "space": row["space_id"],
        }
    finally:
        conn.close()


def list_plan_reserves(sender_phone, conversation_type="DIRECT_DM",
                       requested_scope="all", include_inactive=False):
    ensure_schema()
    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        active_clause = "" if include_inactive else " AND active=1"
        rows = conn.execute(
            "SELECT * FROM alex_phase2_plan_reserves WHERE "
            + clause + active_clause + " ORDER BY name",
            args,
        ).fetchall()
        return [{
            "reserve_id": row["reserve_id"], "name": row["name"],
            "monthly": _money(row["monthly_minor"]),
            "currency": row["currency"], "active": bool(row["active"]),
            "space": row["space_id"],
        } for row in rows]
    finally:
        conn.close()


def _monthly_income_minor(payload):
    amount = payload.get("amount_minor") or 0
    freq = payload.get("frequency")
    if freq == "monthly":
        return amount
    if freq == "annual":
        return int(Decimal(amount) / Decimal(12))
    if freq == "weekly":
        return int((Decimal(amount) * Decimal("52")) / Decimal("12"))
    if freq == "fortnightly":
        return int((Decimal(amount) * Decimal("26")) / Decimal("12"))
    return 0


def baseline_plan(sender_phone, conversation_type="DIRECT_DM",
                  requested_scope="all", reveal_inputs=False,
                  currency="MYR"):
    """Calculate recurring affordability from fixed configured facts only.

    Variable income (including OT) is deliberately excluded.
    """
    currency = str(currency or "MYR").upper()
    if currency not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    ensure_schema()
    profile_config.ensure_schema()
    records = profile_config.authorized_records(
        sender_phone, conversation_type, requested_scope=requested_scope)
    fixed_income = []
    commitments = []
    private_input_used = False
    for record in records:
        payload = record["payload"]
        if not payload.get("active", True):
            continue
        if (
            record["kind"] == "income"
            and payload.get("income_class") == "fixed"
            and payload.get("currency", "MYR") == currency
        ):
            fixed_income.append((record, _monthly_income_minor(payload)))
        elif record["kind"] == "recurring_payment":
            if (
                payload.get("amount_type") == "fixed"
                and payload.get("amount_minor") is not None
                and payload.get("currency", "MYR") == currency
            ):
                freq = payload.get("frequency")
                amt = payload["amount_minor"]
                if freq == "monthly":
                    monthly = amt
                elif freq == "annual":
                    monthly = int(Decimal(amt) / Decimal(12))
                elif freq == "quarterly":
                    monthly = int(Decimal(amt) / Decimal(3))
                elif freq == "weekly":
                    monthly = int((Decimal(amt) * Decimal("52")) / Decimal("12"))
                else:
                    monthly = 0
                commitments.append((record, monthly))
        if record["space_id"] != "FAMILY_SHARED":
            private_input_used = True

    conn = tools.get_db()
    try:
        user_id, private_space, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        goals = conn.execute("""
            SELECT * FROM alex_phase2_goals
            WHERE status='ACTIVE' AND currency=? AND """ + clause,
            (currency, *args)).fetchall()
        reserves = conn.execute("""
            SELECT * FROM alex_phase2_plan_reserves
            WHERE active=1 AND currency=? AND """ + clause,
            (currency, *args)).fetchall()
        if any(r["space_id"] != "FAMILY_SHARED" for r in goals):
            private_input_used = True
        if any(r["space_id"] != "FAMILY_SHARED" for r in reserves):
            private_input_used = True

        income_total = sum(v for _, v in fixed_income)
        commitment_total = sum(v for _, v in commitments)
        goal_total = sum(
            _goal_baseline_for_period(
                conn, r["goal_id"], _period(), r["baseline_monthly_minor"])
            for r in goals
        )
        reserve_total = sum(r["monthly_minor"] for r in reserves)
        available = income_total - commitment_total - goal_total - reserve_total
        output_space = (
            private_space if private_input_used and conversation_type != "GROUP"
            else "FAMILY_SHARED"
        )
        result = {
            "currency": currency,
            "fixed_income_monthly": _money(income_total) if reveal_inputs else None,
            "fixed_commitments_monthly": _money(commitment_total),
            "locked_goal_allocations_monthly": _money(goal_total),
            "reserves_monthly": _money(reserve_total),
            "available_baseline_monthly": _money(available),
            "variable_income_included": False,
            "output_space": output_space,
            "private_input_used": private_input_used,
        }
        if reveal_inputs:
            result["income_sources"] = [
                {
                    "name": rec["payload"].get("name"),
                    "monthly": _money(value),
                    "space": rec["space_id"],
                }
                for rec, value in fixed_income
            ]
        return result
    finally:
        conn.close()


def income_outlook(period, sender_phone,
                   conversation_type="DIRECT_DM",
                   requested_scope="all", reveal_sources=False,
                   currency="MYR"):
    """Classify income as confirmed, expected or possible.

    Confirmed = observed cash events.
    Expected = configured fixed income.
    Possible = configured variable income only. Possible income is never fed
    into baseline affordability.
    """
    if not isinstance(period, str) or len(period) != 7:
        raise ValueError("Period must be YYYY-MM")
    currency = str(currency or "MYR").upper()
    if currency not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    profile_config.ensure_schema()
    ensure_schema()
    records = profile_config.authorized_records(
        sender_phone, conversation_type, kind="income",
        requested_scope=requested_scope)
    expected = []
    possible = []
    private_input_used = False
    for record in records:
        payload = record["payload"]
        if not payload.get("active", True):
            continue
        if payload.get("currency", "MYR") != currency:
            continue
        monthly = _monthly_income_minor(payload)
        entry = {
            "name": payload.get("name"),
            "amount": _money(monthly),
            "currency": payload.get("currency", "MYR"),
            "space": record["space_id"],
        }
        if payload.get("income_class") == "fixed":
            expected.append(entry)
        else:
            possible.append(entry)
        if record["space_id"] != "FAMILY_SHARED":
            private_input_used = True

    conn = tools.get_db()
    try:
        _, private_space, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        rows = conn.execute("""
            SELECT event_type,amount_minor,currency,description,space_id,event_date
            FROM alex_phase2_cash_events
            WHERE substr(event_date,1,7)=? AND currency=? AND """ + clause
            + " ORDER BY event_date,created_at_utc",
            (period, currency, *args),
        ).fetchall()
        confirmed = [{
            "type": row["event_type"],
            "amount": _money(row["amount_minor"]),
            "currency": row["currency"],
            "description": row["description"],
            "space": row["space_id"],
            "date": row["event_date"],
        } for row in rows]
        if any(row["space_id"] != "FAMILY_SHARED" for row in rows):
            private_input_used = True

        expected_total = sum(_minor(x["amount"]) for x in expected)
        possible_total = sum(_minor(x["amount"]) for x in possible)
        confirmed_total = sum(row["amount_minor"] for row in rows)
        confirmed_salary = sum(
            row["amount_minor"] for row in rows if row["event_type"] == "SALARY"
        )
        expected_remaining = max(0, expected_total - confirmed_salary)
        output_space = (
            private_space if private_input_used and conversation_type != "GROUP"
            else "FAMILY_SHARED"
        )
        result = {
            "period": period,
            "confirmed_total": _money(confirmed_total),
            "expected_fixed_total": _money(expected_total),
            "expected_fixed_remaining": _money(expected_remaining),
            "possible_variable_total": _money(possible_total),
            "possible_is_guaranteed": False,
            "baseline_uses_possible": False,
            "private_input_used": private_input_used,
            "output_space": output_space,
        }
        if reveal_sources:
            result["confirmed"] = confirmed
            result["expected"] = expected
            result["possible"] = possible
        return result
    finally:
        conn.close()


def _income_dates_for_period(payload, year, month):
    frequency = payload.get("frequency")
    payday = payload.get("payday_day")
    anchor_text = payload.get("effective_from")
    month_start = date(year, month, 1)
    month_end = date(year, month, _days_in_month(year, month))

    if frequency == "monthly":
        day = int(payday or month_end.day)
        return [date(year, month, min(day, month_end.day))]

    if frequency in ("weekly", "fortnightly"):
        if not anchor_text:
            return [None]
        anchor = date.fromisoformat(str(anchor_text)[:10])
        step = 7 if frequency == "weekly" else 14
        if anchor > month_end:
            return []
        current = anchor
        if current < month_start:
            delta = (month_start - current).days
            jumps = (delta + step - 1) // step
            current = current + timedelta(days=jumps * step)
        out = []
        while current <= month_end:
            if current >= month_start:
                out.append(current)
            current += timedelta(days=step)
        return out

    if frequency == "annual":
        if not anchor_text:
            return [None]
        anchor = date.fromisoformat(str(anchor_text)[:10])
        if year < anchor.year or month != anchor.month:
            return []
        day = int(payday or anchor.day)
        return [date(year, month, min(day, month_end.day))]

    return [None]


def cashflow_forecast(period, sender_phone,
                      conversation_type="DIRECT_DM",
                      requested_scope="all", currency="MYR"):
    """Build a dated, privacy-scoped cash-flow view for one month.

    Possible variable income and OT payout windows are informational only and
    are excluded from guaranteed/baseline capacity.
    """
    if not re.fullmatch(r"\d{4}-\d{2}", str(period or "")):
        raise ValueError("Period must be YYYY-MM")
    currency = str(currency or "MYR").upper()
    if currency not in ("MYR", "SGD"):
        raise ValueError("Currency must be MYR or SGD")
    year, month = (int(x) for x in period.split("-"))

    ensure_schema()
    profile_config.ensure_schema()
    ensure_obligation_instances(
        period, sender_phone, conversation_type,
        requested_scope=requested_scope)

    income_records = profile_config.authorized_records(
        sender_phone, conversation_type, kind="income",
        requested_scope=requested_scope)
    income_records = [
        row for row in income_records
        if row["payload"].get("active", True)
        and row["payload"].get("currency", "MYR") == currency
    ]

    outlook = income_outlook(
        period, sender_phone, conversation_type,
        requested_scope=requested_scope,
        reveal_sources=True, currency=currency)
    baseline = baseline_plan(
        sender_phone, conversation_type,
        requested_scope=requested_scope,
        reveal_inputs=False, currency=currency)

    entries = []
    expected_remaining_entries_minor = 0
    possible_entries_minor = 0
    confirmed_salary_present = any(
        item["type"] == "SALARY"
        for item in outlook.get("confirmed", [])
    )
    for item in outlook.get("confirmed", []):
        entries.append({
            "date": item["date"],
            "kind": "CONFIRMED_INCOME",
            "name": item.get("description") or item["type"].title(),
            "amount": item["amount"],
            "currency": currency,
            "certainty": "CONFIRMED",
            "direction": "IN",
        })

    for record in income_records:
        payload = record["payload"]
        amount_minor = int(payload.get("amount_minor") or 0)
        fixed = payload.get("income_class") == "fixed"
        dates = _income_dates_for_period(payload, year, month)
        # For the common monthly salary case, once an actual salary event is
        # recorded, do not display the configured salary again as money still
        # expected this month.
        suppress = (
            fixed and payload.get("frequency") == "monthly"
            and confirmed_salary_present
        )
        if suppress:
            continue
        for d in dates:
            if fixed:
                expected_remaining_entries_minor += amount_minor
            else:
                possible_entries_minor += amount_minor
            entries.append({
                "date": d.isoformat() if d else None,
                "kind": "EXPECTED_INCOME" if fixed else "POSSIBLE_INCOME",
                "name": payload.get("name"),
                "amount": _money(amount_minor),
                "currency": currency,
                "certainty": "EXPECTED" if fixed else "POSSIBLE",
                "direction": "IN",
            })

    obligations = list_obligations(
        sender_phone, conversation_type,
        requested_scope=requested_scope, period=period)
    remaining_obligation_minor = 0
    for item in obligations:
        if item["currency"] != currency or item["state"] == "CANCELLED":
            continue
        expected_minor = item["expected_minor"]
        remaining_minor = (
            max(0, int(expected_minor or 0) - int(item["paid_minor"] or 0))
            if expected_minor is not None else None
        )
        if remaining_minor is not None:
            remaining_obligation_minor += remaining_minor
        entries.append({
            "date": item["due_date"],
            "kind": "OBLIGATION",
            "name": item["name"],
            "amount": (
                _money(remaining_minor)
                if remaining_minor is not None else None
            ),
            "currency": currency,
            "certainty": (
                "SETTLED" if item["state"] == "PAID"
                else "KNOWN" if expected_minor is not None
                else "VARIABLE"
            ),
            "direction": "OUT",
            "state": item["state"],
        })

    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        goals = conn.execute("""
            SELECT * FROM alex_phase2_goals
            WHERE status='ACTIVE' AND currency=? AND """ + clause,
            (currency, *args)).fetchall()
        remaining_goal_minor = 0
        for goal in goals:
            expected = goal_expected_for_period(conn, goal, period)
            actual = conn.execute("""
                SELECT COALESCE(SUM(amount_minor),0) AS total
                FROM alex_phase2_goal_contributions
                WHERE goal_id=? AND period=?
            """, (goal["goal_id"], period)).fetchone()["total"]
            remaining = max(0, expected - actual)
            remaining_goal_minor += remaining
            entries.append({
                "date": None,
                "kind": "GOAL_ALLOCATION",
                "name": goal["name"],
                "amount": _money(remaining),
                "currency": currency,
                "certainty": "LOCKED_PLAN",
                "direction": "OUT",
            })

        reserves = conn.execute("""
            SELECT * FROM alex_phase2_plan_reserves
            WHERE active=1 AND currency=? AND """ + clause,
            (currency, *args)).fetchall()
        reserve_minor = sum(int(row["monthly_minor"]) for row in reserves)
        for row in reserves:
            entries.append({
                "date": None,
                "kind": "RESERVE",
                "name": row["name"],
                "amount": _money(row["monthly_minor"]),
                "currency": currency,
                "certainty": "RESERVED",
                "direction": "HOLD",
            })
    finally:
        conn.close()

    # OT payout dates are useful schedule information even before the exact
    # formula exists. They carry no amount and never enter totals.
    try:
        overtime = profile_config.authorized_records(
            sender_phone, conversation_type, kind="overtime_rule",
            requested_scope="private")
    except PermissionError:
        overtime = []
    for record in overtime:
        payload = record["payload"]
        if not payload.get("active", True):
            continue
        if payload.get("currency", "MYR") != currency:
            continue
        for day in payload.get("payout_days") or []:
            try:
                payout = date(year, month, min(int(day), _days_in_month(year, month)))
            except (TypeError, ValueError):
                continue
            entries.append({
                "date": payout.isoformat(),
                "kind": "OT_PAYOUT_WINDOW",
                "name": "OT payout window",
                "amount": None,
                "currency": currency,
                "certainty": "POSSIBLE_AMOUNT_UNKNOWN",
                "direction": "IN",
            })

    entries.sort(key=lambda item: (
        item["date"] is None,
        item["date"] or "9999-99-99",
        item["kind"],
        item["name"] or "",
    ))
    expected_remaining_minor = expected_remaining_entries_minor
    future_net = (
        expected_remaining_minor
        - remaining_obligation_minor
        - remaining_goal_minor
        - reserve_minor
    )
    return {
        "period": period,
        "currency": currency,
        "entries": entries,
        "confirmed_income": outlook["confirmed_total"],
        "expected_fixed_remaining": _money(expected_remaining_minor),
        "possible_variable_total": _money(possible_entries_minor),
        "remaining_obligations": _money(remaining_obligation_minor),
        "remaining_goal_allocations": _money(remaining_goal_minor),
        "reserves": _money(reserve_minor),
        "future_net_from_remaining_guaranteed_flows": _money(future_net),
        "baseline_monthly_capacity": baseline["available_baseline_monthly"],
        "possible_income_in_guaranteed_totals": False,
        "output_space": baseline["output_space"],
    }


def goal_projection(goal_id, sender_phone,
                    conversation_type="DIRECT_DM", from_period=None):
    """Project baseline-only progress; one-off/OT top-ups can only improve it."""
    progress = goal_progress(goal_id, sender_phone, conversation_type)
    remaining_minor = _minor(progress["remaining"])
    baseline_minor = _minor(progress["baseline_monthly"])
    if remaining_minor <= 0:
        return {
            **progress, "months_remaining_baseline": 0,
            "projection_basis": "BASELINE_ONLY",
        }
    if baseline_minor <= 0:
        return {
            **progress, "months_remaining_baseline": None,
            "projection_basis": "NO_BASELINE_ALLOCATION",
        }
    months = (remaining_minor + baseline_minor - 1) // baseline_minor
    return {
        **progress,
        "months_remaining_baseline": int(months),
        "projection_basis": "BASELINE_ONLY",
        "variable_income_assumed": False,
    }


def planning_brief(sender_phone, conversation_type="DIRECT_DM",
                   requested_scope="all", currency="MYR"):
    plan = baseline_plan(
        sender_phone, conversation_type, requested_scope,
        reveal_inputs=False, currency=currency)
    available = plan["available_baseline_monthly"]
    if available >= 0:
        text = (
            "Based on the fixed commitments and planned payments currently "
            f"recorded, {available:.2f} per month remains in the baseline plan. "
            "Variable income such as overtime is not included."
        )
    else:
        text = (
            "The current fixed commitments and planned payments exceed the "
            f"configured fixed-income baseline by {abs(available):.2f} per month. "
            "Variable income such as overtime is not being used to cover the gap."
        )
    return {"text": text, **plan}


def list_goals(sender_phone, conversation_type="DIRECT_DM",
               requested_scope="all"):
    ensure_schema()
    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        rows = conn.execute("""
            SELECT * FROM alex_phase2_goals
            WHERE status NOT IN ('CANCELLED') AND """ + clause
            + " ORDER BY created_at_utc,name", args).fetchall()
        result = []
        for row in rows:
            funded = conn.execute("""
                SELECT COALESCE(SUM(amount_minor),0) AS total
                FROM alex_phase2_goal_contributions WHERE goal_id=?
            """, (row["goal_id"],)).fetchone()["total"]
            result.append({
                "goal_id": row["goal_id"], "name": row["name"],
                "target": _money(row["target_minor"]),
                "funded": _money(funded),
                "remaining": _money(max(0, row["target_minor"] - funded)),
                "baseline_monthly": _money(_goal_baseline_for_period(
                    conn, row["goal_id"], _period(),
                    row["baseline_monthly_minor"])),
                "currency": row["currency"], "status": row["status"],
                "space": row["space_id"], "target_date": row["target_date"],
            })
        return result
    finally:
        conn.close()


def list_obligations(sender_phone, conversation_type="DIRECT_DM",
                     requested_scope="all", period=None):
    ensure_schema()
    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        where = [clause]
        values = list(args)
        if period:
            where.append("period=?")
            values.append(period)
        rows = conn.execute(
            "SELECT * FROM alex_phase2_obligation_instances WHERE "
            + " AND ".join(where)
            + " ORDER BY due_date,name",
            values,
        ).fetchall()
        return [
            {
                **dict(row),
                "expected": (
                    _money(row["expected_minor"])
                    if row["expected_minor"] is not None else None
                ),
                "paid": _money(row["paid_minor"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _recurrence_dates_for_period(payload, year, month):
    """Return concrete due dates for one configured recurring payment."""
    frequency = payload.get("frequency")
    due_day = payload.get("due_day")
    anchor_text = payload.get("effective_from")
    month_start = date(year, month, 1)
    month_end = date(year, month, _days_in_month(year, month))

    if frequency == "monthly":
        if due_day:
            return [
                date(year, month, min(int(due_day), month_end.day))
            ]
        # A monthly rule without a due day is still a valid expected
        # obligation, but it has no precise due date.
        return [None]

    if not anchor_text:
        return []
    anchor = date.fromisoformat(str(anchor_text)[:10])

    if frequency == "weekly":
        if anchor > month_end:
            return []
        if anchor < month_start:
            delta = (month_start - anchor).days
            jumps = (delta + 6) // 7
            current = anchor + timedelta(days=jumps * 7)
        else:
            current = anchor
        out = []
        while current <= month_end:
            if current >= month_start:
                out.append(current)
            current += timedelta(days=7)
        return out

    if frequency == "quarterly":
        months = (year - anchor.year) * 12 + (month - anchor.month)
        if months < 0 or months % 3 != 0:
            return []
        day = int(due_day or anchor.day)
        return [date(year, month, min(day, month_end.day))]

    if frequency == "annual":
        if year < anchor.year or month != anchor.month:
            return []
        day = int(due_day or anchor.day)
        return [date(year, month, min(day, month_end.day))]

    return []


def ensure_obligation_instances(period, sender_phone,
                                conversation_type="DIRECT_DM",
                                requested_scope="all"):
    """Create expected obligation occurrences from configured recurrence rules."""
    year, month = (int(x) for x in period.split("-"))
    profile_config.ensure_schema()
    ensure_schema()
    records = profile_config.authorized_records(
        sender_phone, conversation_type,
        kind="recurring_payment", requested_scope=requested_scope)
    conn = tools.get_db()
    created = 0
    try:
        for record in records:
            p = record["payload"]
            if not p.get("active", True):
                continue
            # Once a period has been instantiated, later HA configuration
            # edits must not silently rewrite/regenerate that period. The new
            # rule takes effect on the next not-yet-instantiated period unless
            # the user explicitly edits the occurrence.
            existing_period = conn.execute("""
                SELECT 1 FROM alex_phase2_obligation_instances
                WHERE profile_record_key=? AND period=? LIMIT 1
            """, (record["record_key"], period)).fetchone()
            if existing_period:
                continue
            due_dates = _recurrence_dates_for_period(p, year, month)
            for occurrence_index, due in enumerate(due_dates):
                due_text = due.isoformat() if due else None
                occurrence_key = (
                    due_text if due_text
                    else f"{period}:undated:{occurrence_index}"
                )
                existing = conn.execute("""
                    SELECT 1 FROM alex_phase2_obligation_instances
                    WHERE profile_record_key=? AND occurrence_key=?
                """, (record["record_key"], occurrence_key)).fetchone()
                if existing:
                    continue
                conn.execute("""
                    INSERT INTO alex_phase2_obligation_instances(
                        instance_id,profile_record_key,occurrence_key,space_id,
                        owner_user_id,period,name,expected_minor,currency,due_date
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    str(uuid.uuid4()), record["record_key"], occurrence_key,
                    record["space_id"], record["owner_user_id"], period,
                    p.get("name"), p.get("amount_minor"),
                    p.get("currency", "MYR"), due_text,
                ))
                created += 1
        conn.commit()
        return {"period": period, "created": created}
    finally:
        conn.close()


def refresh_obligation_states(as_of_date, sender_phone,
                              conversation_type="DIRECT_DM",
                              requested_scope="all"):
    """Past due with no evidence becomes UNCONFIRMED_DUE, never 'unpaid'."""
    d = date.fromisoformat(str(as_of_date)[:10])
    ensure_schema()
    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        cur = conn.execute("""
            UPDATE alex_phase2_obligation_instances
            SET state='UNCONFIRMED_DUE',updated_at_utc=CURRENT_TIMESTAMP
            WHERE state='EXPECTED' AND due_date IS NOT NULL AND due_date < ?
              AND """ + clause, (d.isoformat(), *args))
        conn.commit()
        return {"changed": cur.rowcount}
    finally:
        conn.close()


def _authorized_obligation(conn, instance_id, sender_phone, conversation_type):
    user_id, private_space, shared = _context(conn, sender_phone, conversation_type)
    row = conn.execute("""
        SELECT * FROM alex_phase2_obligation_instances WHERE instance_id=?
    """, (instance_id,)).fetchone()
    if not row:
        raise ValueError("Obligation instance not found")
    if row["space_id"] == "FAMILY_SHARED":
        if not shared:
            raise PermissionError("Obligation not authorized")
    elif row["space_id"] != private_space or row["owner_user_id"] != user_id:
        raise PermissionError("Obligation not authorized")
    if conversation_type == "GROUP" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("Private obligation cannot be used in the family group")
    return row


def record_obligation_payment(instance_id, amount, sender_phone,
                              conversation_type="DIRECT_DM", note=None):
    ensure_schema()
    conn = tools.get_db()
    try:
        row = _authorized_obligation(
            conn, instance_id, sender_phone, conversation_type)
        value = _minor(amount)
        if value <= 0:
            raise ValueError("Payment must be greater than zero")
        new_paid = row["paid_minor"] + value
        expected = row["expected_minor"]
        state = "PAID" if expected is not None and new_paid >= expected else "PARTIAL"
        conn.execute("""
            UPDATE alex_phase2_obligation_instances
            SET paid_minor=?,state=?,note=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE instance_id=?
        """, (new_paid, state, note, instance_id))
        conn.commit()
        return {
            "instance_id": instance_id, "state": state,
            "paid": _money(new_paid),
            "expected": _money(expected) if expected is not None else None,
        }
    finally:
        conn.close()


def defer_obligation(instance_id, new_due_date, sender_phone,
                     conversation_type="DIRECT_DM", note=None):
    date.fromisoformat(str(new_due_date)[:10])
    ensure_schema()
    conn = tools.get_db()
    try:
        _authorized_obligation(conn, instance_id, sender_phone, conversation_type)
        conn.execute("""
            UPDATE alex_phase2_obligation_instances
            SET state='DEFERRED',deferred_to=?,note=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE instance_id=?
        """, (str(new_due_date)[:10], note, instance_id))
        conn.commit()
        return {"instance_id": instance_id, "state": "DEFERRED",
                "deferred_to": str(new_due_date)[:10]}
    finally:
        conn.close()


def confirm_obligation_unpaid(instance_id, sender_phone,
                              conversation_type="DIRECT_DM", note=None):
    """Only an explicit owner/user confirmation may produce CONFIRMED_UNPAID."""
    ensure_schema()
    conn = tools.get_db()
    try:
        _authorized_obligation(conn, instance_id, sender_phone, conversation_type)
        conn.execute("""
            UPDATE alex_phase2_obligation_instances
            SET state='CONFIRMED_UNPAID',note=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE instance_id=?
        """, (note, instance_id))
        conn.commit()
        return {"instance_id": instance_id, "state": "CONFIRMED_UNPAID"}
    finally:
        conn.close()


def record_evidence_fact(evidence_ref, fact_type, sender_phone,
                         conversation_type="DIRECT_DM", visibility="private",
                         amount=None, currency="MYR", destination_alias=None,
                         event_date=None, confidence=1.0):
    if fact_type not in ("GOAL_DEPOSIT", "OBLIGATION_PAYMENT", "INCOME"):
        raise ValueError("Unsupported evidence fact type")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("Confidence must be 0-1")
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_evidence_facts(
                evidence_fact_id,space_id,owner_user_id,evidence_ref,fact_type,
                amount_minor,currency,destination_alias,event_date,confidence
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, evidence_ref, fact_type,
            _minor(amount) if amount is not None else None,
            currency.upper() if currency else None,
            destination_alias, str(event_date)[:10] if event_date else None,
            confidence,
        ))
        conn.commit()
        return {"evidence_fact_id": ident, "applied": False}
    finally:
        conn.close()


def resolve_obligation_from_evidence(label, amount, event_date,
                                     sender_phone,
                                     conversation_type="DIRECT_DM",
                                     requested_scope="all"):
    """Conservatively resolve a receipt/payment fact to one obligation.

    Exact/normalized name plus amount and period are used when available.
    Multiple plausible obligations are never guessed.
    """
    ensure_schema()
    d = date.fromisoformat(str(event_date)[:10])
    period = _period(d)
    value = _minor(amount) if amount is not None else None
    wanted = _norm(label)
    conn = tools.get_db()
    try:
        _, _, clause, args = _authorized_space_clause(
            conn, sender_phone, conversation_type, requested_scope)
        rows = conn.execute("""
            SELECT * FROM alex_phase2_obligation_instances
            WHERE period=? AND state NOT IN ('PAID','CANCELLED')
              AND """ + clause + " ORDER BY due_date,name",
            (period, *args),
        ).fetchall()
        candidates = []
        for row in rows:
            name = _norm(row["name"])
            name_match = bool(
                wanted and (wanted == name or wanted in name or name in wanted)
            )
            amount_match = (
                value is not None and row["expected_minor"] is not None
                and value == row["expected_minor"]
            )
            # If a label is supplied, require it to match. Amount then
            # strengthens/disambiguates the match rather than overriding name.
            if wanted:
                if not name_match:
                    continue
                score = 2 + (1 if amount_match else 0)
            else:
                if not amount_match:
                    continue
                score = 1
            candidates.append((score, row))

        if not candidates:
            return {"status": "NOT_FOUND", "instance_id": None}
        best_score = max(score for score, _ in candidates)
        best = [row for score, row in candidates if score == best_score]
        if len(best) != 1:
            return {
                "status": "AMBIGUOUS", "instance_id": None,
                "matches": [
                    {
                        "instance_id": row["instance_id"],
                        "name": row["name"],
                        "expected": (
                            _money(row["expected_minor"])
                            if row["expected_minor"] is not None else None
                        ),
                        "due_date": row["due_date"],
                    }
                    for row in best
                ],
            }
        return {
            "status": "MATCH", "instance_id": best[0]["instance_id"],
            "name": best[0]["name"],
        }
    finally:
        conn.close()


def apply_obligation_payment_evidence(evidence_fact_id, sender_phone,
                                      conversation_type="DIRECT_DM",
                                      minimum_confidence=0.90,
                                      requested_scope="all"):
    """Apply one strong payment fact only when one obligation matches."""
    ensure_schema()
    conn = tools.get_db()
    try:
        fact = conn.execute("""
            SELECT * FROM alex_phase2_evidence_facts WHERE evidence_fact_id=?
        """, (evidence_fact_id,)).fetchone()
        if not fact:
            raise ValueError("Evidence fact not found")
        if fact["applied"]:
            raise ValueError("Evidence fact already applied")
        if fact["fact_type"] != "OBLIGATION_PAYMENT":
            raise ValueError("Evidence is not an obligation payment")
        user_id, private_space, shared = _context(
            conn, sender_phone, conversation_type)
        if fact["space_id"] == "FAMILY_SHARED":
            if not shared:
                raise PermissionError("Evidence not authorized")
        elif fact["space_id"] != private_space or fact["owner_user_id"] != user_id:
            raise PermissionError("Evidence not authorized")
        if conversation_type == "GROUP" and fact["space_id"] != "FAMILY_SHARED":
            raise PermissionError("Private evidence cannot be applied in group")
        if (
            fact["confidence"] < minimum_confidence
            or fact["amount_minor"] is None
            or not fact["event_date"]
        ):
            return {
                "status": "NEEDS_CONFIRMATION", "applied": False,
                "reason": "EVIDENCE_INCOMPLETE_OR_LOW_CONFIDENCE",
            }

        match = resolve_obligation_from_evidence(
            fact["destination_alias"], _money(fact["amount_minor"]),
            fact["event_date"], sender_phone, conversation_type,
            requested_scope=requested_scope)
        if match["status"] != "MATCH":
            return {
                "status": "NEEDS_CONFIRMATION", "applied": False,
                "reason": "OBLIGATION_NOT_UNIQUELY_MATCHED",
                "match_status": match["status"],
                "matches": match.get("matches", []),
            }

        row = _authorized_obligation(
            conn, match["instance_id"], sender_phone, conversation_type)
        if row["space_id"] != fact["space_id"]:
            raise PermissionError(
                "Receipt evidence cannot cross a privacy boundary")
        if fact["currency"] and fact["currency"] != row["currency"]:
            raise ValueError(
                "Payment evidence currency differs from the obligation currency")
        new_paid = row["paid_minor"] + fact["amount_minor"]
        expected = row["expected_minor"]
        state = (
            "PAID"
            if expected is not None and new_paid >= expected
            else "PARTIAL"
        )
        conn.execute("""
            UPDATE alex_phase2_obligation_instances
            SET paid_minor=?,state=?,note=?,updated_at_utc=CURRENT_TIMESTAMP
            WHERE instance_id=?
        """, (
            new_paid, state,
            "Payment recorded from verified evidence " + fact["evidence_ref"],
            row["instance_id"],
        ))
        conn.execute("""
            UPDATE alex_phase2_evidence_facts SET applied=1
            WHERE evidence_fact_id=?
        """, (evidence_fact_id,))
        conn.commit()
        return {
            "status": state, "applied": True,
            "instance_id": row["instance_id"],
            "paid": _money(new_paid),
            "expected": (
                _money(expected) if expected is not None else None
            ),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def compare_actual_salary(cash_event_id, sender_phone,
                          conversation_type="DIRECT_DM"):
    """Compare an observed salary deposit with configured fixed salary.

    A mismatch is only a prompt-worthy fact. It never edits the configured
    salary automatically.
    """
    status = cash_event_status(cash_event_id, sender_phone, conversation_type)
    if status["event_type"] != "SALARY":
        raise ValueError("Cash event is not a salary payment")
    configured = [
        r for r in profile_config.authorized_records(
            sender_phone, conversation_type, kind="income",
            requested_scope="private")
        if r["payload"].get("active", True)
        and r["payload"].get("income_class") == "fixed"
        and r["payload"].get("frequency") == "monthly"
    ]
    if len(configured) != 1:
        return {
            "status": "CONFIG_AMBIGUOUS", "baseline_changed": False,
            "prompt": "I recorded the salary payment, but the fixed salary profile is missing or ambiguous.",
        }
    expected = configured[0]["payload"]["amount_minor"]
    actual = _minor(status["amount"])
    if actual == expected:
        return {
            "status": "MATCH", "expected": _money(expected),
            "actual": _money(actual), "baseline_changed": False, "prompt": None,
        }
    return {
        "status": "DIFFERENT", "expected": _money(expected),
        "actual": _money(actual), "delta": _money(actual - expected),
        "baseline_changed": False,
        "prompt": (
            "I recorded the salary payment, but it differs from the configured "
            "monthly salary. Is this a one-off difference, or do you want to "
            "update the fixed salary profile?"
        ),
    }


def _norm(value):
    return " ".join(str(value or "").casefold().split())


def resolve_goal_from_alias(alias_text, sender_phone,
                            conversation_type="DIRECT_DM"):
    """Conservatively match one authorized account alias to one goal."""
    aliases = match_account_alias(alias_text, sender_phone, conversation_type)
    if len(aliases) != 1:
        return {"status": "AMBIGUOUS" if aliases else "NOT_FOUND", "goal_id": None}
    alias = aliases[0]["payload"]
    candidates = []
    alias_terms = {_norm(alias.get("label")), _norm(alias.get("purpose"))}
    for goal in list_goals(sender_phone, conversation_type, requested_scope="all"):
        goal_name = _norm(goal.get("name"))
        if goal_name and goal_name in alias_terms:
            candidates.append(goal)
    if len(candidates) != 1:
        return {
            "status": "AMBIGUOUS" if candidates else "NOT_FOUND",
            "goal_id": None,
        }
    return {"status": "MATCH", "goal_id": candidates[0]["goal_id"]}


def auto_apply_goal_deposit(evidence_fact_id, alias_text, sender_phone,
                            conversation_type="DIRECT_DM",
                            minimum_confidence=0.90):
    match = resolve_goal_from_alias(
        alias_text, sender_phone, conversation_type)
    if match["status"] != "MATCH":
        return {
            "status": "NEEDS_CONFIRMATION", "applied": False,
            "reason": "GOAL_NOT_UNIQUELY_MATCHED",
        }
    return apply_goal_deposit_evidence(
        evidence_fact_id, match["goal_id"], sender_phone,
        conversation_type, minimum_confidence=minimum_confidence)


def match_account_alias(alias_text, sender_phone, conversation_type="DIRECT_DM"):
    records = profile_config.authorized_records(
        sender_phone, conversation_type, kind="account_alias")
    wanted = " ".join(str(alias_text or "").casefold().split())
    matches = []
    for record in records:
        payload = record["payload"]
        label = " ".join(str(payload.get("label") or "").casefold().split())
        purpose = " ".join(str(payload.get("purpose") or "").casefold().split())
        if wanted and (wanted == label or wanted == purpose):
            matches.append(record)
    return matches


def apply_goal_deposit_evidence(evidence_fact_id, goal_id, sender_phone,
                                conversation_type="DIRECT_DM",
                                minimum_confidence=0.90):
    """Apply only a strong, explicit evidence fact; never change goal baseline."""
    ensure_schema()
    conn = tools.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        goal = _get_authorized_goal(conn, goal_id, sender_phone, conversation_type)
        fact = conn.execute("""
            SELECT * FROM alex_phase2_evidence_facts WHERE evidence_fact_id=?
        """, (evidence_fact_id,)).fetchone()
        if not fact:
            raise ValueError("Evidence fact not found")
        if fact["applied"]:
            raise ValueError("Evidence fact already applied")
        if fact["fact_type"] != "GOAL_DEPOSIT":
            raise ValueError("Evidence is not a goal deposit")
        if fact["space_id"] != goal["space_id"]:
            raise PermissionError("Evidence cannot cross a privacy boundary")
        if fact["currency"] and fact["currency"] != goal["currency"]:
            raise ValueError(
                "Evidence and goal currencies differ; explicit conversion is required")
        if fact["confidence"] < minimum_confidence:
            return {"status": "NEEDS_CONFIRMATION", "applied": False}
        if fact["amount_minor"] is None or not fact["event_date"]:
            return {"status": "NEEDS_CONFIRMATION", "applied": False}
        contribution_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_goal_contributions(
                contribution_id,goal_id,space_id,owner_user_id,amount_minor,
                currency,contribution_kind,contribution_date,period,evidence_ref
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            contribution_id, goal_id, goal["space_id"], goal["owner_user_id"],
            fact["amount_minor"], fact["currency"] or goal["currency"],
            "EVIDENCE", fact["event_date"], _period(fact["event_date"]),
            fact["evidence_ref"],
        ))
        conn.execute("""
            UPDATE alex_phase2_evidence_facts SET applied=1
            WHERE evidence_fact_id=?
        """, (evidence_fact_id,))
        expected = goal_expected_for_period(conn, goal, _period(fact["event_date"]))
        status = (
            "ON_PLAN" if fact["amount_minor"] == expected
            else "BELOW_PLAN" if fact["amount_minor"] < expected
            else "ABOVE_PLAN"
        )
        conn.commit()
        return {
            "status": status, "applied": True,
            "amount": _money(fact["amount_minor"]),
            "expected": _money(expected), "baseline_changed": False,
            "needs_plan_question": status == "BELOW_PLAN",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
