#!/usr/bin/env python3
from __future__ import annotations

"""Alex Phase-3 Tier-B behaviour certification.

Why this exists
---------------
stress_test.py proves deterministic service invariants.  It deliberately does
not prove that ordinary human wording reaches those services consistently.
This rig adds that missing layer without touching production /data.

Modes:
  catalog  - prove the rig itself covers every declared phase/domain.
  offline  - attack the deterministic router with every paraphrase; no provider.
  live     - drive the same contracts through brain.respond with a real provider
             against a disposable database, inspect tool traces, replies,
             attachments and latency.

The live run is intentionally opt-in because it consumes provider tokens.
Home Assistant physical effects, WhatsApp transport/rendering, group mention
metadata and real scheduled delivery remain explicit manual gates.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from behavior_contracts import (
    CONVERSATION_CONTRACTS,
    MANUAL_GATES,
    PROMPT_CONTRACTS,
    REQUIRED_DOMAINS,
    REQUIRED_OWNER_TOOL_COVERAGE,
    TOOL_COVERAGE_EXEMPTIONS,
    PromptContract,
    contracts_for_phase,
    conversations_for_phase,
)


APP = Path(__file__).resolve().parent
DEFAULT_REPORT = "behavior-cert-report.json"
HUSBAND = "+60111111111"
WIFE = "+60222222222"


def _emit(report: dict, report_path: str | None) -> dict:
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    print(payload)
    if report_path:
        Path(report_path).write_text(payload + "\n", encoding="utf-8")
    return report


def catalog_audit() -> dict:
    failures: list[str] = []
    checks: list[str] = []

    all_ids = [c.id for c in PROMPT_CONTRACTS]
    all_ids += [c.id for c in CONVERSATION_CONTRACTS]
    all_ids += [c.id for c in MANUAL_GATES]
    if len(all_ids) != len(set(all_ids)):
        failures.append("contract ids are not unique")
    else:
        checks.append("all contract ids unique")

    for contract in PROMPT_CONTRACTS:
        if len(contract.variants) < 2:
            failures.append(f"{contract.id}: fewer than two natural-language variants")
        if not contract.required_any:
            failures.append(f"{contract.id}: no required capability/tool")
        if contract.required_any & contract.forbidden:
            failures.append(f"{contract.id}: same tool is both required and forbidden")
    if not failures:
        checks.append("prompt contracts have multiple variants and coherent tool requirements")

    represented: dict[str, set[str]] = {"phase1": set(), "phase2": set(), "phase3": set()}
    for contract in PROMPT_CONTRACTS:
        represented.setdefault(contract.phase, set()).add(contract.domain)
    for contract in CONVERSATION_CONTRACTS:
        represented.setdefault(contract.phase, set()).add(contract.domain)
    for gate in MANUAL_GATES:
        represented.setdefault(gate.phase, set()).add(gate.domain)

    for phase, required in REQUIRED_DOMAINS.items():
        missing = sorted(required - represented.get(phase, set()))
        if missing:
            failures.append(f"{phase}: missing domains from certification catalog: {missing}")
        else:
            checks.append(f"{phase} declared domains covered")

    covered_tools = set()
    for contract in PROMPT_CONTRACTS:
        covered_tools |= set(contract.required_any)
    for contract in CONVERSATION_CONTRACTS:
        for step in contract.steps:
            covered_tools |= set(step.required_any)
    missing_owner_tools = sorted(REQUIRED_OWNER_TOOL_COVERAGE - covered_tools)
    if missing_owner_tools:
        failures.append(
            "owner-visible MCP tools missing behavioural contracts: "
            + ", ".join(missing_owner_tools)
        )
    else:
        checks.append("every declared owner-visible MCP tool is represented by behaviour contracts")

    # Future-proofing: if a new MCP tool is added later, catalog CI fails until
    # it is either given a human behaviour contract or explicitly documented as
    # a compatibility/internal exemption.
    try:
        current_surface = _tool_names_from_mcp()
        uncovered_surface = sorted(
            current_surface - covered_tools - set(TOOL_COVERAGE_EXEMPTIONS)
        )
        if uncovered_surface:
            failures.append(
                "current MCP surface has unclassified tools: "
                + ", ".join(uncovered_surface)
            )
        else:
            checks.append("current MCP surface has no unclassified user-facing tools")
    except Exception as exc:
        failures.append(f"unable to inspect MCP surface for coverage drift: {exc}")


    if not any(g.id == "manual.group.mention" for g in MANUAL_GATES):
        failures.append("real WhatsApp @mention gate is not represented")
    if not any(g.id == "manual.voice.transport" for g in MANUAL_GATES):
        failures.append("real voice transport gate is not represented")
    if not any(g.id == "manual.ha.physical" for g in MANUAL_GATES):
        failures.append("physical Home Assistant gate is not represented")
    if not failures:
        checks.append("non-simulatable production edges are explicit manual gates")

    variants = sum(len(c.variants) * len(c.sources) for c in PROMPT_CONTRACTS)
    conversation_steps = sum(len(c.steps) * len(c.sources) for c in CONVERSATION_CONTRACTS)
    return {
        "mode": "catalog",
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
        "failures": failures,
        "coverage": {
            "prompt_contracts": len(PROMPT_CONTRACTS),
            "prompt_source_variants": variants,
            "conversation_contracts": len(CONVERSATION_CONTRACTS),
            "conversation_steps": conversation_steps,
            "manual_gates": len(MANUAL_GATES),
            "phases": {k: sorted(v) for k, v in represented.items()},
        },
    }


def _tool_names_from_mcp() -> set[str]:
    from mcp import Client
    from mcp_server import mcp

    async def names():
        async with Client(mcp) as client:
            result = await client.list_tools()
            return {tool.name for tool in result.tools}

    return asyncio.run(names())


def _mcp_schemas() -> dict[str, dict]:
    from mcp import Client
    from mcp_server import mcp

    async def specs():
        async with Client(mcp) as client:
            result = await client.list_tools()
            return {
                t.name: {
                    "description": t.description or "",
                    "schema": t.input_schema or {},
                }
                for t in result.tools
            }

    return asyncio.run(specs())


def offline_certify(phase: str) -> dict:
    import brain

    tool_names = _tool_names_from_mcp()
    schemas = _mcp_schemas()
    failures: list[dict] = []
    passes: list[dict] = []
    needs_live: list[dict] = []

    for contract in contracts_for_phase(phase):
        surface_missing = sorted(contract.required_any - tool_names)
        required_surface_exists = bool(contract.required_any & tool_names)
        if not required_surface_exists:
            failures.append({
                "contract": contract.id,
                "kind": "missing-capability",
                "detail": (
                    "none of the required tools exist in MCP surface; "
                    f"expected one of {sorted(contract.required_any)}"
                ),
                "missing": surface_missing,
            })

        for source in contract.sources:
            for phrase in contract.variants:
                # Inspect the exact provider-facing tool surface, not only the
                # internal keyword selector. _tool_specs adds the read-only
                # fallback and the discovery valve that the model actually sees.
                specs = asyncio.run(brain._tool_specs(phrase))
                selected = {
                    str(spec["function"]["name"])
                    for spec in specs
                    if isinstance(spec, dict) and spec.get("function")
                }
                direct = bool(selected & contract.required_any)
                discovery_available = brain.DISCOVERY_TOOL_NAME in selected
                forbidden = sorted(selected & contract.forbidden)
                over_cap = len(selected) > brain.TOOL_EXPOSURE_MAX
                row = {
                    "contract": contract.id,
                    "phase": contract.phase,
                    "domain": contract.domain,
                    "source": source,
                    "prompt": phrase,
                    "provider_facing_tools": sorted(selected),
                }
                problems = []
                if forbidden:
                    problems.append("forbidden mutation exposed: " + ", ".join(forbidden))
                if over_cap:
                    problems.append(
                        f"tool exposure {len(selected)} exceeds cap {brain.TOOL_EXPOSURE_MAX}"
                    )

                if not direct and not required_surface_exists:
                    problems.append(
                        "required capability is absent from MCP surface: "
                        + " / ".join(sorted(contract.required_any))
                    )
                elif not direct and discovery_available and not problems:
                    # This is neither a pass nor a hard routing failure. The
                    # model may normalize the intent via discover_alex_tools;
                    # only live provider certification can prove it succeeds.
                    needs_live.append({
                        **row,
                        "kind": "discovery-dependent",
                        "detail": (
                            "required capability is not directly exposed; "
                            "discover_alex_tools must recover it in live certification"
                        ),
                    })
                    continue
                elif not direct:
                    problems.append(
                        "required capability not exposed and no discovery path: "
                        + " / ".join(sorted(contract.required_any))
                    )

                if problems:
                    failures.append({**row, "kind": "routing", "detail": "; ".join(problems)})
                else:
                    passes.append(row)

    # Architecture assertions that language routing alone cannot see.
    goal_schema = schemas.get("planning_create_goal", {}).get("schema", {})
    goal_required = set(goal_schema.get("required") or [])
    if "baseline_monthly" in goal_required:
        failures.append({
            "contract": "architecture.goal.owner-agency",
            "kind": "schema",
            "detail": (
                "planning_create_goal requires baseline_monthly. A user can ask for a goal "
                "without choosing a monthly contribution, so the model is structurally forced "
                "to invent one or fail. Draft goal creation must permit an unspecified baseline."
            ),
        })
    else:
        passes.append({
            "contract": "architecture.goal.owner-agency",
            "detail": "goal creation can omit monthly contribution",
        })

    task_tools = sorted(name for name in tool_names if "task" in name.casefold())
    if not task_tools:
        failures.append({
            "contract": "architecture.tasks.lifecycle",
            "kind": "missing-capability",
            "detail": (
                "No task lifecycle tool exists. A request for a task can only degrade into a "
                "plan note/reminder, so create/list/complete task behaviour cannot certify."
            ),
        })
    else:
        passes.append({
            "contract": "architecture.tasks.lifecycle",
            "detail": f"task tools present: {task_tools}",
        })

    # A read path must exist for every major user-owned object class.
    required_read_paths = {
        "finance": {"query_finances"},
        "receipts": {"find_receipts", "get_receipt"},
        "memory": {"search_saved_items", "get_saved_item"},
        "reminders": {"list_reminders"},
        "shopping": {"list_shopping_items"},
        "diary": {"get_agenda", "get_agenda_range"},
        "plans": {"list_plans"},
        "goals": {"planning_list_goals", "planning_goal_progress"},
        "work": {"work_schedule", "work_day", "list_work_roster"},
        "bills": {"bills_list"},
        "home": {"ha_find_entities", "ha_get_state"},
    }
    for domain, wanted in required_read_paths.items():
        if not (wanted & tool_names):
            failures.append({
                "contract": f"architecture.read-path.{domain}",
                "kind": "missing-capability",
                "detail": f"no read path exists; expected one of {sorted(wanted)}",
            })

    selected_contracts = list(contracts_for_phase(phase))
    status = "FAIL" if failures else ("LIVE_REQUIRED" if needs_live else "PASS")
    return {
        "mode": "offline",
        "phase": phase,
        "status": status,
        "summary": {
            "contracts": len(selected_contracts),
            "variant_checks_passed": len(passes),
            "discovery_dependent_checks": len(needs_live),
            "failures": len(failures),
            "mcp_tool_count": len(tool_names),
            "tool_exposure_cap": brain.TOOL_EXPOSURE_MAX,
        },
        "failures": failures,
        "needs_live": needs_live,
        "passes": passes,
        "manual_gates_not_claimed": [
            asdict(g) for g in MANUAL_GATES
            if phase == "all" or g.phase == phase
        ],
    }


def _load_source_options(path: str | None) -> dict:
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _credential_options(provider: str, source: dict) -> dict:
    # Never print these values. Environment variables are convenient for dev/CI;
    # /data/options.json allows an owner to run the rig inside the add-on without
    # copying keys into source control.
    gemini = (
        os.environ.get("ALEX_CERT_GEMINI_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or source.get("gemini_api_key")
        or ""
    )
    grok = (
        os.environ.get("ALEX_CERT_XAI_API_KEY")
        or os.environ.get("XAI_API_KEY")
        or source.get("xai_api_key")
        or ""
    )
    openai = (
        os.environ.get("ALEX_CERT_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or source.get("openai_api_key")
        or ""
    )
    available = {"gemini": bool(gemini), "grok": bool(grok), "openai": bool(openai)}
    if provider != "auto" and not available.get(provider, False):
        raise RuntimeError(
            f"No {provider} certification key is available. "
            "Use ALEX_CERT_* environment variables or --source-options /data/options.json."
        )
    if provider == "auto" and not any(available.values()):
        raise RuntimeError(
            "No provider key is available for live certification. "
            "Offline certification remains zero-cost."
        )
    return {
        "gemini_api_key": gemini,
        "xai_api_key": grok,
        "openai_api_key": openai,
    }


def _sandbox_options(provider: str, creds: dict) -> dict:
    return {
        "ai_provider": provider,
        **creds,
        "husband_phone": HUSBAND,
        "wife_phone": WIFE,
        "timezone": "Asia/Kuala_Lumpur",
        "context_turns": 8,
        "reasoning_effort": "low",
        "monthly_ai_budget_usd": 0,
        "auto_grok_fallback_budget_usd": 0.50,
        "budget_safety_multiplier": 2.0,
        "ocr_enabled": False,
        "income_profiles": [{
            "id": "salary", "owner": "husband", "visibility": "private",
            "name": "Base salary", "amount": 10000, "currency": "MYR",
            "income_class": "fixed", "frequency": "monthly",
            "payday_day": 25, "active": True,
        }],
        "roster_profiles": [{
            "id": "roster", "owner": "husband", "visibility": "private",
            "name": "Alternating", "cycle_start": "2026-09-21",
            "pattern": "evening,morning", "day_start": "07:45", "day_end": "16:15",
            "evening_start": "16:30", "evening_end": "01:00", "active": True,
        }],
        "overtime_profiles": [{
            "id": "ot", "owner": "husband", "visibility": "private",
            "name": "OT", "currency": "MYR", "morning_pre_hours": 2,
            "morning_pre_start": "05:45", "morning_weekend_standard_start": "04:45",
            "morning_weekend_standard_end": "16:45", "morning_weekend_low_start": "05:45",
            "morning_weekend_low_end": "15:45", "evening_post_hours": 2.75,
            "evening_post_start": "01:00", "evening_post_end": "04:15",
            "evening_weekend_days": "saturday", "evening_weekend_standard_start": "16:15",
            "evening_weekend_standard_end": "04:15", "evening_weekend_low_start": "16:15",
            "evening_weekend_low_end": "23:59", "sunday_override_allowed": True,
            "payout_days": "7,12", "rate_formula": "", "active": True,
        }],
        "leave_balances": [],
        "recurring_payments": [{
            "id": "tnb", "owner": "family", "visibility": "family",
            "name": "TNB electricity bill", "amount_type": "variable",
            "currency": "MYR", "due_day": 30, "frequency": "monthly", "active": True,
        }],
        "account_aliases": [],
        "reminder_preferences": [],
        "presence_mappings": [],
    }


def _install_fake_ha():
    """Make live certification incapable of touching a real HA instance."""
    import ha

    states = {
        "climate.hall_ac": {
            "entity_id": "climate.hall_ac",
            "state": "on",
            "attributes": {
                "friendly_name": "Hall AC",
                "temperature": 24,
            },
            "last_changed": "2026-09-29T01:00:00+00:00",
        },
        "light.living_room": {
            "entity_id": "light.living_room",
            "state": "off",
            "attributes": {"friendly_name": "Living Room Light"},
            "last_changed": "2026-09-29T01:00:00+00:00",
        },
    }

    def fake_request(method: str, path: str, payload: dict | None = None):
        if method == "GET" and path == "/states":
            return list(states.values())
        if method == "GET" and path.startswith("/states/"):
            entity = path.split("/states/", 1)[1]
            if entity not in states:
                raise LookupError("CERTIFICATION entity not found")
            return states[entity]
        if method == "POST" and path.startswith("/services/"):
            # Mutations are simulated in-memory only.
            entity = str((payload or {}).get("entity_id") or "")
            service = path.rsplit("/", 1)[-1]
            if entity in states:
                if service == "turn_on":
                    states[entity]["state"] = "on"
                elif service == "turn_off":
                    states[entity]["state"] = "off"
            return []
        raise RuntimeError(f"CERTIFICATION fake HA does not support {method} {path}")

    ha._request = fake_request


def _install_fixed_clock():
    """Freeze only the model-facing runtime clock for reproducible relative dates."""
    import brain

    real_datetime = datetime
    fixed_utc = real_datetime(2026, 9, 29, 2, 0, 0, tzinfo=timezone.utc)

    class CertificationDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    brain.datetime = CertificationDateTime


def _initialize_sandbox():
    import db
    import phase2_finance
    import phase2_library
    import phase2_delegation
    import phase2_work
    import profile_config

    db.initialize()
    profile_config.ensure_schema()
    profile_config.sync_from_ha()
    phase2_finance.ensure_schema()
    phase2_work.ensure_schema()
    phase2_library.ensure_schema()
    phase2_delegation.ensure_schema()


_STATE_FINGERPRINT_EXCLUDE = {
    # Transport/telemetry/focus artifacts are evidence about a turn, not the
    # user's durable household state.
    "inbound_messages", "outbound_messages", "conversation_turns",
    "tool_execution_claims", "tool_audit", "ai_usage", "selection_sets",
    "diagnostic_runs",
}


def _state_fingerprint() -> dict[str, dict[str, Any]]:
    """Privacy-safe durable-state fingerprint: counts + hashes, never row contents."""
    import db

    conn = db.connect()
    try:
        tables = [
            row["name"] for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            ).fetchall()
            if row["name"] not in _STATE_FINGERPRINT_EXCLUDE
        ]
        out: dict[str, dict[str, Any]] = {}
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            encoded = sorted(
                json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)
                for row in rows
            )
            digest = hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()[:16]
            out[table] = {"count": len(rows), "hash": digest}
        return out
    finally:
        conn.close()


def _state_diff(before: dict, after: dict) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    for table in sorted(set(before) | set(after)):
        b = before.get(table, {"count": 0, "hash": ""})
        a = after.get(table, {"count": 0, "hash": ""})
        if b != a:
            changed[table] = {
                "before_count": b.get("count", 0),
                "after_count": a.get("count", 0),
                "content_changed": b.get("hash") != a.get("hash"),
            }
    return changed


def _turn_cost_usd(mid: str) -> float:
    import db

    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(estimated_cost_usd),0) AS cost
               FROM ai_usage WHERE source_message_id=?""",
            (mid,),
        ).fetchone()
        return round(float(row["cost"] or 0.0), 10)
    finally:
        conn.close()


def _validate_seed(seed: str | None) -> None:
    if seed != "core":
        return
    import db

    conn = db.connect()
    try:
        expectations = {
            "financial_events": 1,
            "saved_items": 2,
            "shopping_items": 2,
            "diary_events": 1,
            "reminders": 1,
            "plans": 1,
            "alex_phase2_goals": 1,
        }
        bad = []
        for table, minimum in expectations.items():
            count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if count < minimum:
                bad.append(f"{table}={count} expected>={minimum}")
        if bad:
            raise RuntimeError("CERTIFICATION SEED INVALID: " + "; ".join(bad))
    finally:
        conn.close()


def _reset_case_database(sandbox_dir: Path, seed: str | None) -> str:
    """Give every prompt variant an independent DB so one wording cannot contaminate another."""
    import db

    case_path = sandbox_dir / f"case-{uuid.uuid4().hex}.db"
    db.DB_PATH = str(case_path)
    _initialize_sandbox()
    _install_fake_ha()
    if seed == "core":
        _seed_core()
    _validate_seed(seed)
    return str(case_path)


def _claim(mid: str, text: str, conv: str, phone: str = HUSBAND,
           conversation_type: str = "DIRECT_DM"):
    import db

    db.claim_inbound({
        "message_id": mid,
        "provider": "CERTIFICATION",
        "conversation_id": conv,
        "conversation_type": conversation_type,
        "sender_phone": phone,
        "text": text,
    })


def _actor(mid: str, conv: str, text: str = "", source: str = "text",
           phone: str = HUSBAND, media_ids: list[str] | None = None,
           conversation_type: str = "DIRECT_DM"):
    import db

    actor = db.resolve_actor(phone, conv, conversation_type, mid, media_ids or [])
    return replace(
        actor,
        source=source,
        trusted_text=text,
        received_at_utc="2026-09-29T02:00:00+00:00",
    )


def _seed_core():
    import db
    import media
    import phase2
    import phase2_finance
    import services
    from context import with_action_key

    conv = "cert-seed@s.whatsapp.net"

    # Management fee with original receipt.
    mid = "seed-management"
    _claim(mid, "management fee receipt", conv)
    media_id = media.save_media(
        mid, "IMAGE", "image/jpeg",
        base64.b64encode(b"CERTIFICATION management receipt RM593.62").decode("ascii"),
    )
    a = with_action_key(
        _actor(mid, conv, "management fee receipt", media_ids=[media_id]),
        "seed-management-action",
    )
    services.log_expense(
        a, "Management fee", 593.62, "housing", "MYR",
        "2026-09-28", "CERT-MGMT-001",
    )

    # Explicit memory and saved picture.
    mid = "seed-cobalt"
    _claim(mid, "remember cobalt", conv)
    services.save_item(
        with_action_key(_actor(mid, conv, "remember cobalt"), "seed-cobalt-action"),
        "Smoke-test code word", "cobalt", "test,code", False,
    )

    mid = "seed-vinyl"
    _claim(mid, "save this vinyl setup", conv)
    vinyl_media = media.save_media(
        mid, "IMAGE", "image/png",
        base64.b64encode(b"CERTIFICATION vinyl image").decode("ascii"),
    )
    services.save_item(
        with_action_key(
            _actor(mid, conv, "save this vinyl setup", media_ids=[vinyl_media]),
            "seed-vinyl-action",
        ),
        "Vinyl Setup & Music Inspo", "Turntable and Raavanan vinyl setup",
        "vinyl,music,turntable", False,
    )

    # Family shopping state.
    for idx, item in enumerate(("Diapers", "Bread"), 1):
        mid = f"seed-shop-{idx}"
        _claim(mid, f"add {item}", conv)
        services.add_shopping_item(
            with_action_key(_actor(mid, conv, f"add {item}"), f"seed-shop-action-{idx}"),
            item, shared=True,
        )

    # Dentist diary event + reminder.
    mid = "seed-dentist-diary"
    _claim(mid, "dentist appointment 1 October 4pm", conv)
    phase2.add_diary_event(
        with_action_key(_actor(mid, conv, "dentist appointment"), "seed-dentist-diary-action"),
        "Dentist Appointment",
        "2026-10-01T16:00:00+08:00",
        "2026-10-01T16:15:00+08:00",
    )
    mid = "seed-dentist-rem"
    _claim(mid, "remind dentist 2pm", conv)
    services.create_reminder(
        with_action_key(_actor(mid, conv, "remind dentist 2pm"), "seed-dentist-rem-action"),
        "Dentist Appointment", "2026-10-01T14:00:00+08:00",
    )

    # Draft Malacca plan.
    mid = "seed-malacca"
    _claim(mid, "Malacca family day trip", conv)
    phase2.create_plan(
        with_action_key(_actor(mid, conv, "Malacca family day trip"), "seed-malacca-action"),
        "Family Day Trip to Malacca",
        notes="Date undecided. Kid-friendly activities. Aim to be back home by around 9pm.",
        locked=False,
    )

    # Goal with a zero deterministic baseline, used only as read fixture.
    phase2_finance.create_goal(
        "Family Holiday Savings", 5000, 0, HUSBAND,
        visibility="private", status="DRAFT",
    )


def _trace(mid: str) -> dict:
    import db

    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT arguments_json,result_json
               FROM tool_audit
               WHERE source_message_id=? AND tool_name='_turn_trace'
               ORDER BY created_at_utc DESC,rowid DESC LIMIT 1""",
            (mid,),
        ).fetchone()
        calls = conn.execute(
            """SELECT tool_name,arguments_json,result_json,status,latency_ms
               FROM tool_audit
               WHERE source_message_id=? AND tool_name!='_turn_trace'
               ORDER BY created_at_utc,rowid""",
            (mid,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "turn": {
            "arguments": json.loads(row["arguments_json"]) if row else {},
            "result": json.loads(row["result_json"]) if row else {},
        },
        "calls": [
            {
                "tool": r["tool_name"],
                "arguments": json.loads(r["arguments_json"] or "{}"),
                "result": json.loads(r["result_json"] or "{}"),
                "status": r["status"],
                "latency_ms": r["latency_ms"],
            }
            for r in calls
        ],
    }


def _looks_malay(reply: str) -> bool:
    low = " " + (reply or "").casefold() + " "
    markers = (
        " anda ", " saya ", " boleh ", " tidak ", " maaf ", " adakah ",
        " sila ", " untuk ", " dengan ", " rancangan ", " peringatan ",
    )
    return sum(1 for marker in markers if marker in low) >= 2


def _is_nonzero(value: Any) -> bool:
    if value is None or value is False:
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return bool(str(value).strip())


def _live_one(contract: PromptContract, prompt: str, source: str,
              hard_latency_ms: int) -> dict:
    import brain

    mid = f"cert-{contract.id}-{uuid.uuid4().hex[:12]}"
    phone = WIFE if contract.actor == "wife" else HUSBAND
    ctype = contract.conversation_type
    conv = (
        f"{contract.id}-{uuid.uuid4().hex[:8]}@g.us"
        if ctype == "GROUP"
        else f"{contract.id}-{uuid.uuid4().hex[:8]}@s.whatsapp.net"
    )
    state_before = _state_fingerprint()
    _claim(mid, prompt, conv, phone=phone, conversation_type=ctype)
    actor = _actor(
        mid, conv, prompt, source=source, phone=phone,
        conversation_type=ctype,
    )
    started = time.monotonic()
    reply, attachments = asyncio.run(brain.respond(actor, prompt))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    trace = _trace(mid)
    state_after = _state_fingerprint()
    state_changes = _state_diff(state_before, state_after)
    cost_usd = _turn_cost_usd(mid)
    called = {
        c["tool"] for c in trace["calls"]
        if not c["tool"].startswith("_")
    }
    problems: list[str] = []
    if not (called & contract.required_any):
        problems.append(
            "required tool was not called: " + " / ".join(sorted(contract.required_any))
        )
    bad = sorted(called & contract.forbidden)
    if bad:
        problems.append("forbidden tool called: " + ", ".join(bad))
    if trace["turn"]["result"].get("outcome") == "max_steps":
        problems.append("turn exhausted model/tool step budget")
    if contract.expect_attachment and not attachments:
        problems.append("expected original attachment was not queued")
    low_reply = (reply or "").casefold()
    for term in contract.expected_terms:
        if term.casefold() not in low_reply:
            problems.append(f"reply missing expected term: {term}")
    for term in contract.forbidden_terms:
        if term.casefold() in low_reply:
            problems.append(f"reply contains forbidden/contradictory phrase: {term}")
    for call in trace["calls"]:
        for arg_name in contract.nonzero_forbidden_args:
            if arg_name in call["arguments"] and _is_nonzero(call["arguments"][arg_name]):
                problems.append(
                    f"tool {call['tool']} invented non-zero {arg_name}="
                    f"{call['arguments'][arg_name]!r}"
                )
    if source == "voice" and prompt.isascii() and _looks_malay(reply):
        problems.append("English voice transcript received an unsolicited Malay reply")
    persistent_mutation_called = any(
        brain._is_mutating_tool(call["tool"]) and call["tool"] != "ha_control"
        for call in trace["calls"]
        if not call["tool"].startswith("_")
    )
    if persistent_mutation_called and not state_changes:
        problems.append("mutating tool returned without any durable household-state change")
    if elapsed_ms > hard_latency_ms:
        problems.append(f"hard latency exceeded: {elapsed_ms}ms > {hard_latency_ms}ms")
    return {
        "contract": contract.id,
        "phase": contract.phase,
        "domain": contract.domain,
        "source": source,
        "actor": contract.actor,
        "conversation_type": contract.conversation_type,
        "prompt": prompt,
        "reply": reply,
        "attachment_count": len(attachments),
        "elapsed_ms": elapsed_ms,
        "estimated_cost_usd": cost_usd,
        "durable_state_diff": state_changes,
        "trace": trace,
        "status": "PASS" if not problems else "FAIL",
        "problems": problems,
    }


def _live_conversation(contract, source: str, hard_latency_ms: int) -> dict:
    import brain

    conv = f"{contract.id}-{source}-{uuid.uuid4().hex[:8]}@s.whatsapp.net"
    rows = []
    for index, step in enumerate(contract.steps, 1):
        mid = f"cert-{contract.id}-{source}-{index}-{uuid.uuid4().hex[:8]}"
        state_before = _state_fingerprint()
        _claim(mid, step.prompt, conv)
        actor = _actor(mid, conv, step.prompt, source=source)
        started = time.monotonic()
        reply, attachments = asyncio.run(brain.respond(actor, step.prompt))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        trace = _trace(mid)
        state_after = _state_fingerprint()
        state_changes = _state_diff(state_before, state_after)
        cost_usd = _turn_cost_usd(mid)
        called = {c["tool"] for c in trace["calls"] if not c["tool"].startswith("_")}
        problems = []
        if not (called & step.required_any):
            problems.append(
                "required tool was not called: " + " / ".join(sorted(step.required_any))
            )
        low_reply = (reply or "").casefold()
        for term in step.expected_terms:
            if term.casefold() not in low_reply:
                problems.append(f"reply missing expected term: {term}")
        for term in step.forbidden_terms:
            if term.casefold() in low_reply:
                problems.append(f"reply contains forbidden/contradictory phrase: {term}")
        for call in trace["calls"]:
            for arg_name in step.nonzero_forbidden_args:
                if arg_name in call["arguments"] and _is_nonzero(call["arguments"][arg_name]):
                    problems.append(
                        f"tool {call['tool']} invented non-zero {arg_name}="
                        f"{call['arguments'][arg_name]!r}"
                    )
        if step.expect_attachment and not attachments:
            problems.append("expected original attachment was not queued")
        if trace["turn"]["result"].get("outcome") == "max_steps":
            problems.append("turn exhausted model/tool step budget")
        if source == "voice" and step.prompt.isascii() and _looks_malay(reply):
            problems.append("English voice transcript received an unsolicited Malay reply")
        persistent_mutation_called = any(
            brain._is_mutating_tool(call["tool"]) and call["tool"] != "ha_control"
            for call in trace["calls"]
            if not call["tool"].startswith("_")
        )
        if persistent_mutation_called and not state_changes:
            problems.append("mutating tool returned without any durable household-state change")
        if elapsed_ms > hard_latency_ms:
            problems.append(f"hard latency exceeded: {elapsed_ms}ms > {hard_latency_ms}ms")
        rows.append({
            "step": index,
            "prompt": step.prompt,
            "reply": reply,
            "attachment_count": len(attachments),
            "elapsed_ms": elapsed_ms,
            "estimated_cost_usd": cost_usd,
            "durable_state_diff": state_changes,
            "trace": trace,
            "status": "PASS" if not problems else "FAIL",
            "problems": problems,
        })
    return {
        "contract": contract.id,
        "phase": contract.phase,
        "domain": contract.domain,
        "source": source,
        "status": "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL",
        "steps": rows,
    }


def live_certify(phase: str, provider: str, source_options: str | None,
                 hard_latency_ms: int, report_path: str | None,
                 max_live_cost_usd: float) -> dict:
    source = _load_source_options(source_options)
    creds = _credential_options(provider, source)
    sandbox = tempfile.TemporaryDirectory(prefix="alex-behavior-cert-")
    sandbox_dir = Path(sandbox.name)
    options_path = sandbox_dir / "options.json"
    options_path.write_text(
        json.dumps(_sandbox_options(provider, creds), ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        options_path.chmod(0o600)
    except OSError:
        pass
    os.environ["ALEX_DATA_DIR"] = str(sandbox_dir)
    os.environ["ALEX_OPTIONS_PATH"] = str(options_path)

    # Imports that bind DATA_DIR/OPTIONS_PATH happen only after the sandbox is set.
    import config
    import db

    if Path(config.DATA_DIR).resolve() != sandbox_dir.resolve():
        raise RuntimeError("CERTIFICATION SAFETY STOP: config.DATA_DIR did not bind to sandbox")
    if not Path(db.DB_PATH).resolve().is_relative_to(sandbox_dir.resolve()):
        raise RuntimeError("CERTIFICATION SAFETY STOP: database escaped disposable sandbox")

    _install_fake_ha()
    _install_fixed_clock()

    rows = []
    conversations = []
    accumulated_cost = 0.0
    budget_stopped = False

    for contract in contracts_for_phase(phase):
        if not contract.live:
            continue
        for source_kind in contract.sources:
            for prompt in contract.variants:
                if max_live_cost_usd > 0 and accumulated_cost >= max_live_cost_usd:
                    budget_stopped = True
                    break
                _reset_case_database(sandbox_dir, contract.seed)
                row = _live_one(contract, prompt, source_kind, hard_latency_ms)
                rows.append(row)
                accumulated_cost += float(row.get("estimated_cost_usd") or 0.0)
            if budget_stopped:
                break
        if budget_stopped:
            break

    if not budget_stopped:
        for contract in conversations_for_phase(phase):
            for source_kind in contract.sources:
                if max_live_cost_usd > 0 and accumulated_cost >= max_live_cost_usd:
                    budget_stopped = True
                    break
                _reset_case_database(sandbox_dir, contract.seed)
                conversation = _live_conversation(contract, source_kind, hard_latency_ms)
                conversations.append(conversation)
                accumulated_cost += sum(
                    float(step.get("estimated_cost_usd") or 0.0)
                    for step in conversation["steps"]
                )
            if budget_stopped:
                break

    elapsed = [r["elapsed_ms"] for r in rows]
    for conv in conversations:
        elapsed.extend(step["elapsed_ms"] for step in conv["steps"])
    failures = [r for r in rows if r["status"] == "FAIL"]
    failures += [r for r in conversations if r["status"] == "FAIL"]

    live_status = (
        "FAIL" if failures else
        ("INCOMPLETE_BUDGET" if budget_stopped else "PASS")
    )
    report = {
        "mode": "live",
        "phase": phase,
        "provider": provider,
        "status": live_status,
        "sandbox": True,
        "production_data_touched": False,
        "summary": {
            "prompt_runs": len(rows),
            "conversation_runs": len(conversations),
            "failures": len(failures),
            "budget_stopped": budget_stopped,
            "estimated_cost_usd": round(accumulated_cost, 8),
            "max_live_cost_usd": max_live_cost_usd,
            "latency_ms_p50": int(statistics.median(elapsed)) if elapsed else 0,
            "latency_ms_max": max(elapsed) if elapsed else 0,
            "hard_latency_ms": hard_latency_ms,
        },
        "prompt_results": rows,
        "conversation_results": conversations,
        "manual_gates_not_claimed": [
            asdict(g) for g in MANUAL_GATES
            if phase == "all" or g.phase == phase
        ],
    }
    # Keep TemporaryDirectory alive until report is serialized; it is deleted on return.
    return _emit(report, report_path)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Alex cross-phase behaviour certification")
    p.add_argument(
        "--mode", choices=("catalog", "offline", "live"), default="offline",
        help="catalog audits the rig, offline checks routing, live uses a real provider in a sandbox",
    )
    known_phases = sorted(
        {c.phase for c in PROMPT_CONTRACTS}
        | {c.phase for c in CONVERSATION_CONTRACTS}
        | {g.phase for g in MANUAL_GATES}
    )
    p.add_argument(
        "--phase", choices=("all", *known_phases), default="all",
    )
    p.add_argument(
        "--provider", choices=("auto", "gemini", "grok", "openai"), default="auto",
    )
    p.add_argument(
        "--source-options", default=os.environ.get("ALEX_CERT_SOURCE_OPTIONS", "/data/options.json"),
        help="optional existing options file used only to copy provider credentials into the sandbox",
    )
    p.add_argument("--report", default=None)
    p.add_argument("--hard-latency-ms", type=int, default=20000)
    p.add_argument(
        "--max-live-cost-usd", type=float, default=0.25,
        help="hard runner-level estimated provider spend cap for live certification; 0 disables",
    )
    p.add_argument(
        "--no-fail-exit", action="store_true",
        help="emit failures but exit 0; useful only while repairing a known-bad release",
    )
    return p


def main() -> dict:
    args = _parser().parse_args()
    if args.mode == "catalog":
        report = catalog_audit()
        _emit(report, args.report)
    elif args.mode == "offline":
        report = offline_certify(args.phase)
        _emit(report, args.report)
    else:
        report = live_certify(
            args.phase, args.provider, args.source_options,
            max(1000, args.hard_latency_ms), args.report,
            max(0.0, float(args.max_live_cost_usd)),
        )
    if report["status"] != "PASS" and not args.no_fail_exit:
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
