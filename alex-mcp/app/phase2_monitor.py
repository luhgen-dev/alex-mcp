"""Phase 2 proactive candidate coordinator.

This module never sends a message. It produces candidates only when an explicit
active delegation exists, preserving Alex's quiet-by-default contract.
"""
from __future__ import annotations

from datetime import date

import phase2_delegation
import phase2_finance
import compat_tools as tools
import ha


def _norm(value):
    return " ".join(str(value or "").casefold().split())


def _subject_matches(subject, name, all_terms):
    subject = _norm(subject)
    name = _norm(name)
    if subject in all_terms:
        return True
    return bool(subject and name and (subject == name or subject in name or name in subject))


def home_state_candidates(sender_phone, conversation_type="DIRECT_DM"):
    """Return one-shot HA state matches for explicitly delegated monitors."""
    delegations = phase2_delegation.active_delegations(
        sender_phone, conversation_type, "CUSTOM"
    )
    candidates = []
    for delegation in delegations:
        params = delegation.get("parameters") or {}
        if str(params.get("kind") or "").upper() != "HA_STATE":
            continue
        entity_id = str(params.get("entity_id") or "").strip()
        target_state = str(params.get("target_state") or "").strip().casefold()
        if not entity_id or not target_state:
            continue
        try:
            snapshot = ha.get_state(entity_id)
        except Exception:
            continue
        current_state = str(snapshot.get("state") or "").strip().casefold()
        if current_state in {"", "unknown", "unavailable"}:
            continue
        if current_state != target_state:
            continue
        candidates.append({
            "kind": "HOME_STATE_MATCH",
            "entity_id": entity_id,
            "friendly_name": (
                (snapshot.get("attributes") or {}).get("friendly_name")
                or delegation["subject"]
                or entity_id
            ),
            "state": current_state,
            "target_state": target_state,
            "space": delegation["space_id"],
            "delegation_id": delegation["delegation_id"],
            "one_shot": True,
        })
    return candidates


def bill_candidates(as_of_date, sender_phone,
                    conversation_type="DIRECT_DM"):
    delegations = phase2_delegation.active_delegations(
        sender_phone, conversation_type, "BILL_MONITOR")
    if not delegations:
        return []

    d = date.fromisoformat(str(as_of_date)[:10])
    period = f"{d.year:04d}-{d.month:02d}"
    phase2_finance.ensure_obligation_instances(
        period, sender_phone, conversation_type, requested_scope="all")
    phase2_finance.refresh_obligation_states(
        d.isoformat(), sender_phone, conversation_type, requested_scope="all")
    obligations = phase2_finance.list_obligations(
        sender_phone, conversation_type, requested_scope="all", period=period)

    candidates = []
    for obligation in obligations:
        if obligation["state"] not in (
            "UNCONFIRMED_DUE", "PARTIAL", "CONFIRMED_UNPAID", "DEFERRED"
        ):
            continue
        matching = [
            d for d in delegations
            if _subject_matches(
                d["subject"], obligation["name"],
                {"all", "all bills", "bills", "household bills"},
            )
        ]
        if not matching:
            continue
        candidates.append({
            "kind": "BILL_FOLLOW_UP",
            "instance_id": obligation["instance_id"],
            "name": obligation["name"],
            "state": obligation["state"],
            "due_date": obligation["due_date"],
            "space": (
                matching[0]["space_id"]
                if matching[0]["space_id"] != "FAMILY_SHARED"
                else obligation["space_id"]
            ),
            "delegation_id": matching[0]["delegation_id"],
        })
    return candidates


def goal_candidates(period, sender_phone,
                    conversation_type="DIRECT_DM"):
    delegations = phase2_delegation.active_delegations(
        sender_phone, conversation_type, "GOAL_MONITOR")
    if not delegations:
        return []

    goals = phase2_finance.list_goals(
        sender_phone, conversation_type, requested_scope="all")
    conn = tools.get_db()
    try:
        candidates = []
        for goal in goals:
            if goal["status"] != "ACTIVE":
                continue
            matching = [
                d for d in delegations
                if _subject_matches(
                    d["subject"], goal["name"],
                    {"all", "all goals", "goals"},
                )
            ]
            if not matching:
                continue
            row = conn.execute(
                "SELECT * FROM alex_phase2_goals WHERE goal_id=?",
                (goal["goal_id"],),
            ).fetchone()
            expected = phase2_finance.goal_expected_for_period(
                conn, row, period)
            actual = conn.execute("""
                SELECT COALESCE(SUM(amount_minor),0) AS total
                FROM alex_phase2_goal_contributions
                WHERE goal_id=? AND period=?
            """, (goal["goal_id"], period)).fetchone()["total"]
            if actual >= expected:
                continue
            candidates.append({
                "kind": "GOAL_BELOW_PLAN",
                "goal_id": goal["goal_id"],
                "name": goal["name"],
                "period": period,
                "expected": phase2_finance._money(expected),
                "actual": phase2_finance._money(actual),
                "remaining_for_period": phase2_finance._money(expected - actual),
                "space": (
                    matching[0]["space_id"]
                    if matching[0]["space_id"] != "FAMILY_SHARED"
                    else goal["space"]
                ),
                "delegation_id": matching[0]["delegation_id"],
                "baseline_changed": False,
            })
        return candidates
    finally:
        conn.close()


def ot_allocation_candidates(sender_phone,
                             conversation_type="DIRECT_DM"):
    """Surface unallocated OT only when OT→goal tracking was explicitly delegated."""
    delegations = phase2_delegation.active_delegations(
        sender_phone, conversation_type, "OT_GOAL_TRACK")
    if not delegations:
        return []

    goals = phase2_finance.list_goals(
        sender_phone, conversation_type, requested_scope="all")
    by_name = {_norm(goal["name"]): goal for goal in goals}
    conn = tools.get_db()
    try:
        user_id, private_space = tools.resolve_user_and_space(
            conn, sender_phone, conversation_type)
        rows = conn.execute("""
            SELECT * FROM alex_phase2_cash_events
            WHERE owner_user_id=? AND event_type='OT'
              AND allocation_state IN ('UNALLOCATED','PARTIAL')
              AND (space_id=? OR space_id='FAMILY_SHARED')
            ORDER BY event_date,created_at_utc
        """, (user_id, private_space)).fetchall()
        candidates = []
        for delegation in delegations:
            goal = by_name.get(_norm(delegation["subject"]))
            if not goal:
                continue
            for row in rows:
                # Never bridge a private cash event into a shared goal, or vice versa.
                if row["space_id"] != goal["space"]:
                    continue
                status = phase2_finance.cash_event_status(
                    row["cash_event_id"], sender_phone, conversation_type)
                if status["unallocated"] <= 0:
                    continue
                candidates.append({
                    "kind": "OT_ALLOCATION_AVAILABLE",
                    "cash_event_id": row["cash_event_id"],
                    "goal_id": goal["goal_id"],
                    "goal_name": goal["name"],
                    "unallocated": status["unallocated"],
                    "currency": row["currency"],
                    "space": (
                        delegation["space_id"]
                        if delegation["space_id"] != "FAMILY_SHARED"
                        else row["space_id"]
                    ),
                    "delegation_id": delegation["delegation_id"],
                    "automatic_allocation": False,
                })
        return candidates
    finally:
        conn.close()
