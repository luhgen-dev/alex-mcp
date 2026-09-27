"""Project Jarvis Phase 2 routing/cost policy.

No provider call is made here. The policy only says when a caller is allowed to
escalate from deterministic local logic to an AI worker.
"""
from __future__ import annotations

from decimal import Decimal


LOCAL_OPERATIONS = {
    "baseline_plan",
    "goal_progress",
    "goal_contribution",
    "cash_allocation",
    "obligation_state",
    "roster_lookup",
    "ot_default",
    "leave_balance",
    "asset_lookup",
    "report_build",
    "presence_delivery",
}


def route_operation(operation, *, ambiguity=False, media=False,
                    explicit_analysis=False):
    operation = str(operation or "").strip().lower()
    if operation in LOCAL_OPERATIONS and not ambiguity and not media and not explicit_analysis:
        return {
            "route": "LOCAL",
            "reason": "DETERMINISTIC",
            "provider_allowed": False,
        }
    if media:
        return {
            "route": "SPECIALIST",
            "reason": "MEDIA_UNDERSTANDING_REQUIRED",
            "provider_allowed": True,
        }
    if ambiguity or explicit_analysis:
        return {
            "route": "AI",
            "reason": "REASONING_REQUIRED",
            "provider_allowed": True,
        }
    return {
        "route": "LOCAL",
        "reason": "NO_AI_NEEDED",
        "provider_allowed": False,
    }


def project_cost(input_tokens, output_tokens, calls,
                 input_usd_per_million, output_usd_per_million,
                 safety_multiplier=2):
    """Project provider cost with the user's 2x expected-usage safety margin."""
    input_tokens = Decimal(str(input_tokens or 0))
    output_tokens = Decimal(str(output_tokens or 0))
    calls = Decimal(str(calls or 0))
    in_rate = Decimal(str(input_usd_per_million or 0))
    out_rate = Decimal(str(output_usd_per_million or 0))
    multiplier = Decimal(str(safety_multiplier or 1))
    if min(input_tokens, output_tokens, calls, in_rate, out_rate, multiplier) < 0:
        raise ValueError("Cost inputs cannot be negative")
    per_call = (
        input_tokens / Decimal(1_000_000) * in_rate
        + output_tokens / Decimal(1_000_000) * out_rate
    )
    expected = per_call * calls
    guarded = expected * multiplier
    return {
        "expected_usd": float(expected),
        "guarded_usd": float(guarded),
        "safety_multiplier": float(multiplier),
    }


def budget_gate(projection, monthly_cap_usd=5):
    guarded = Decimal(str(projection.get("guarded_usd", 0)))
    cap = Decimal(str(monthly_cap_usd))
    return {
        "within_budget": guarded <= cap,
        "guarded_usd": float(guarded),
        "cap_usd": float(cap),
        "headroom_usd": float(cap - guarded),
    }


def minimal_handoff(payload, allowed_fields):
    """Return only explicitly permitted fields for an external worker."""
    allowed = set(allowed_fields or [])
    return {key: value for key, value in (payload or {}).items() if key in allowed}
