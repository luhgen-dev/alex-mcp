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
from datetime import date, datetime, timezone
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from behavior_capabilities import (
    CAPABILITY_TO_TOOLS,
    TOOL_COVERAGE_EXEMPTIONS,
    TOOL_TO_CAPABILITY,
    capabilities_for_tools,
    implementation_exists,
    tools_for_capabilities,
)
from behavior_contracts import (
    CONVERSATION_CONTRACTS,
    MANUAL_GATES,
    PROMPT_CONTRACTS,
    REQUIRED_DOMAINS,
    PromptContract,
    StateExpectation,
    contracts_for_phase,
    conversations_for_phase,
)


APP = Path(__file__).resolve().parent
DEFAULT_REPORT = "behavior-cert-report.json"
HUSBAND = "+60111111111"
WIFE = "+60222222222"
CERT_NOW_UTC = "2026-09-29T02:00:00+00:00"
CERT_FIXTURES: dict[str, Any] = {}
_FAKE_HA_STATES: dict[str, dict[str, Any]] = {}


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

    covered_capabilities = set()
    for contract in PROMPT_CONTRACTS:
        covered_capabilities |= set(contract.required_any)
    for contract in CONVERSATION_CONTRACTS:
        for step in contract.steps:
            covered_capabilities |= set(step.required_any)

    # Future-proofing is capability based. New MCP functions must be classified
    # in the adapter; new owner capabilities must be represented by a contract.
    try:
        current_surface = _tool_names_from_mcp()
        unclassified_tools = sorted(
            current_surface - set(TOOL_TO_CAPABILITY) - set(TOOL_COVERAGE_EXEMPTIONS)
        )
        if unclassified_tools:
            failures.append(
                "current MCP surface has unclassified tools: "
                + ", ".join(unclassified_tools)
            )
        else:
            checks.append("current MCP surface is fully classified by capability")

        implemented_owner_caps = {
            TOOL_TO_CAPABILITY[t]
            for t in current_surface
            if t in TOOL_TO_CAPABILITY and TOOL_TO_CAPABILITY[t] != "routing.discovery"
        }
        missing_contract_caps = sorted(implemented_owner_caps - covered_capabilities)
        if missing_contract_caps:
            failures.append(
                "implemented owner capabilities missing behavioural contracts: "
                + ", ".join(missing_contract_caps)
            )
        else:
            checks.append("every implemented owner capability has behavioural coverage")
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
        required_caps = set(contract.required_any) - {"routing.discovery"}
        forbidden_caps = set(contract.forbidden) - {"routing.discovery"}
        implemented_required = {
            cap for cap in required_caps if implementation_exists(cap, tool_names)
        }
        if not implemented_required:
            failures.append({
                "contract": contract.id,
                "kind": "missing-capability",
                "detail": (
                    "none of the required semantic capabilities has a current implementation: "
                    + " / ".join(sorted(required_caps))
                ),
                "missing_capabilities": sorted(required_caps),
            })

        for source in contract.sources:
            for phrase in contract.variants:
                specs = asyncio.run(brain._tool_specs(phrase))
                selected_tools = {
                    str(spec["function"]["name"])
                    for spec in specs
                    if isinstance(spec, dict) and spec.get("function")
                }
                selected_caps = set(capabilities_for_tools(selected_tools))
                direct = bool(selected_caps & required_caps)
                discovery_available = "routing.discovery" in selected_caps
                forbidden = sorted(selected_caps & forbidden_caps)
                over_cap = len(selected_tools) > brain.TOOL_EXPOSURE_MAX
                row = {
                    "contract": contract.id,
                    "phase": contract.phase,
                    "domain": contract.domain,
                    "source": source,
                    "prompt": phrase,
                    "required_capabilities": sorted(required_caps),
                    "provider_facing_capabilities": sorted(selected_caps),
                    "provider_facing_tools": sorted(selected_tools),
                }
                problems = []
                if forbidden:
                    problems.append(
                        "forbidden capability exposed: " + ", ".join(forbidden)
                    )
                if over_cap:
                    problems.append(
                        f"tool exposure {len(selected_tools)} exceeds cap {brain.TOOL_EXPOSURE_MAX}"
                    )

                if not direct and not implemented_required:
                    problems.append(
                        "required capability is absent from MCP surface: "
                        + " / ".join(sorted(required_caps))
                    )
                elif not direct and discovery_available and not problems:
                    # Discovery is never itself a PASS. It only means the live
                    # model gets one chance to recover the real capability.
                    needs_live.append({
                        **row,
                        "kind": "discovery-dependent",
                        "detail": (
                            "required capability is not directly exposed; "
                            "live certification must prove discovery reaches the real capability"
                        ),
                    })
                    continue
                elif not direct:
                    problems.append(
                        "required capability not exposed and no discovery path: "
                        + " / ".join(sorted(required_caps))
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

    # Tasks are a lifecycle, not a keyword-shaped tool. All owner-required
    # lifecycle capabilities must exist before task behaviour can certify.
    task_required = {"task.create", "task.read", "task.update", "task.complete"}
    missing_task_caps = sorted(
        cap for cap in task_required if not implementation_exists(cap, tool_names)
    )
    if missing_task_caps:
        failures.append({
            "contract": "architecture.tasks.lifecycle",
            "kind": "missing-capability",
            "detail": (
                "Task lifecycle is incomplete. Required semantic capabilities: "
                + ", ".join(sorted(task_required))
            ),
            "missing_capabilities": missing_task_caps,
        })
    else:
        passes.append({
            "contract": "architecture.tasks.lifecycle",
            "detail": "task create/read/update/complete capabilities are implemented",
        })

    required_read_caps = {
        "finance": {"finance.read"},
        "receipts": {"receipt.find", "receipt.get"},
        "memory": {"memory.search", "memory.get"},
        "reminders": {"reminder.read"},
        "shopping": {"shopping.read"},
        "diary": {"agenda.read"},
        "plans": {"plan.read"},
        "goals": {"goal.list", "goal.progress"},
        "work": {"work.schedule", "work.day", "work.roster.list"},
        "bills": {"bills.list"},
        "home": {"home.find", "home.state"},
    }
    for domain, wanted in required_read_caps.items():
        if not any(implementation_exists(cap, tool_names) for cap in wanted):
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
    """Make certification incapable of touching a real HA instance.

    Every call goes through ha._request, which is replaced here. The state map
    is exported only in-process so the judge can prove the exact entity changed
    and unrelated entities did not.
    """
    import ha

    global _FAKE_HA_STATES
    _FAKE_HA_STATES = {
        "climate.hall_ac": {
            "entity_id": "climate.hall_ac",
            "state": "on",
            "attributes": {
                "friendly_name": "Hall AC",
                "temperature": 24,
            },
            "last_changed": CERT_NOW_UTC,
        },
        # Start ON so "turn it off" must produce an observable target change.
        "light.living_room": {
            "entity_id": "light.living_room",
            "state": "on",
            "attributes": {"friendly_name": "Living Room Light"},
            "last_changed": CERT_NOW_UTC,
        },
    }

    def fake_request(method: str, path: str, payload: dict | None = None):
        if method == "GET" and path == "/states":
            return [dict(row) for row in _FAKE_HA_STATES.values()]
        if method == "GET" and path.startswith("/states/"):
            entity = path.split("/states/", 1)[1]
            if entity not in _FAKE_HA_STATES:
                raise LookupError("CERTIFICATION entity not found")
            return dict(_FAKE_HA_STATES[entity])
        if method == "POST" and path.startswith("/services/"):
            entity = str((payload or {}).get("entity_id") or "")
            service = path.rsplit("/", 1)[-1]
            if entity in _FAKE_HA_STATES:
                if service == "turn_on":
                    _FAKE_HA_STATES[entity]["state"] = "on"
                elif service == "turn_off":
                    _FAKE_HA_STATES[entity]["state"] = "off"
                elif service == "toggle":
                    _FAKE_HA_STATES[entity]["state"] = (
                        "off" if _FAKE_HA_STATES[entity]["state"] == "on" else "on"
                    )
            return []
        raise RuntimeError(f"CERTIFICATION fake HA does not support {method} {path}")

    ha._request = fake_request


def _ha_snapshot() -> dict[str, dict[str, Any]]:
    return json.loads(json.dumps(_FAKE_HA_STATES))


def _install_fixed_clock():
    """Freeze Alex's Python-level clock for deterministic relative dates.

    Certification intentionally patches all loaded household modules that bind
    datetime/date classes directly. SQLite audit timestamps may still use wall
    clock CURRENT_TIMESTAMP; behavioural assertions never depend on those audit
    timestamps.
    """
    real_datetime = datetime
    real_date = date
    fixed_utc = real_datetime.fromisoformat(CERT_NOW_UTC)

    class CertificationDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return fixed_utc.replace(tzinfo=None)

    class CertificationDate(real_date):
        @classmethod
        def today(cls):
            return fixed_utc.date()

    modules = []
    for name in (
        "brain", "db", "ingress", "outbox", "services", "phase2",
        "phase2_finance", "phase2_work", "phase2_library",
        "phase2_delegation", "profile_config",
    ):
        try:
            modules.append(__import__(name))
        except Exception:
            continue
    for module in modules:
        if hasattr(module, "datetime"):
            module.datetime = CertificationDateTime
        if hasattr(module, "date"):
            module.date = CertificationDate



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

    global CERT_FIXTURES
    CERT_FIXTURES = {}
    case_path = sandbox_dir / f"case-{uuid.uuid4().hex}.db"
    db.DB_PATH = str(case_path)
    _initialize_sandbox()
    _install_fixed_clock()
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
    expense = services.log_expense(
        a, "Management fee", 593.62, "housing", "MYR",
        "2026-09-28", "CERT-MGMT-001",
    )
    receipt_row = media.get_media(media_id)
    CERT_FIXTURES.update({
        "management_event_id": expense.get("event_id"),
        "management_media_id": media_id,
        "management_receipt_path": receipt_row["local_path"] if receipt_row else None,
    })

    # Explicit memory and saved picture.
    mid = "seed-cobalt"
    _claim(mid, "remember cobalt", conv)
    cobalt = services.save_item(
        with_action_key(_actor(mid, conv, "remember cobalt"), "seed-cobalt-action"),
        "Smoke-test code word",
        "cobalt PRIVATE-CERT-CANARY-COBALT-7K2",
        "test,code", False,
    )
    CERT_FIXTURES["private_cobalt_item_id"] = cobalt.get("item_id")

    mid = "seed-vinyl"
    _claim(mid, "save this vinyl setup", conv)
    vinyl_media = media.save_media(
        mid, "IMAGE", "image/png",
        base64.b64encode(b"CERTIFICATION vinyl image").decode("ascii"),
    )
    vinyl = services.save_item(
        with_action_key(
            _actor(mid, conv, "save this vinyl setup", media_ids=[vinyl_media]),
            "seed-vinyl-action",
        ),
        "Vinyl Setup & Music Inspo",
        "Turntable and Raavanan vinyl setup PRIVATE-CERT-CANARY-VINYL-9Q4",
        "vinyl,music,turntable", False,
    )
    vinyl_row = media.get_media(vinyl_media)
    CERT_FIXTURES.update({
        "private_vinyl_item_id": vinyl.get("item_id"),
        "private_vinyl_media_id": vinyl_media,
        "vinyl_path": vinyl_row["local_path"] if vinyl_row else None,
    })

    # Family shopping state.
    for idx, item in enumerate(("Diapers", "Bread"), 1):
        mid = f"seed-shop-{idx}"
        _claim(mid, f"add {item}", conv)
        services.add_shopping_item(
            with_action_key(_actor(mid, conv, f"add {item}"), f"seed-shop-action-{idx}"),
            item, shared=True,
        )

    # Dentist diary event + reminder. 17:00 is deliberately outside the
    # synthetic morning work shift, so the seed itself cannot be rejected as a
    # work conflict before behavioural cases run.
    mid = "seed-dentist-diary"
    _claim(mid, "dentist appointment 1 October 5pm", conv)
    diary = phase2.add_diary_event(
        with_action_key(_actor(mid, conv, "dentist appointment"), "seed-dentist-diary-action"),
        "Dentist Appointment",
        "2026-10-01T17:00:00+08:00",
        "2026-10-01T17:15:00+08:00",
    )
    if diary.get("status") == "needs_choice":
        raise RuntimeError("CERTIFICATION SEED INVALID: dentist fixture unexpectedly conflicts with work")
    CERT_FIXTURES["dentist_diary_id"] = diary.get("diary_id")
    mid = "seed-dentist-rem"
    _claim(mid, "remind dentist 3pm", conv)
    reminder = services.create_reminder(
        with_action_key(_actor(mid, conv, "remind dentist 3pm"), "seed-dentist-rem-action"),
        "Dentist Appointment", "2026-10-01T15:00:00+08:00",
    )
    CERT_FIXTURES["dentist_reminder_id"] = reminder.get("reminder_id")

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


def _resolve_expectation_value(value: Any, mid: str) -> Any:
    if value == "$MID":
        return mid
    if value == "$HUSBAND":
        return "USR_HUSBAND"
    if value == "$WIFE":
        return "USR_WIFE"
    return value


def _safe_identifier(value: str) -> str:
    if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError(f"unsafe certification SQL identifier: {value!r}")
    return value


def _matching_state_count(expectation: StateExpectation, mid: str) -> int:
    import db

    table = _safe_identifier(expectation.table)
    predicates = []
    params = []
    for key, raw in (*expectation.where, *expectation.fields):
        column = _safe_identifier(str(key))
        value = _resolve_expectation_value(raw, mid)
        if value is None:
            predicates.append(f'"{column}" IS NULL')
        else:
            predicates.append(f'"{column}"=?')
            params.append(value)
    sql = f'SELECT COUNT(*) FROM "{table}"'
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    conn = db.connect()
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _expectation_counts(expectations: tuple[StateExpectation, ...], mid: str) -> list[int]:
    return [_matching_state_count(expectation, mid) for expectation in expectations]


def _state_expectation_problems(
    expectations: tuple[StateExpectation, ...],
    before_counts: list[int],
    after_counts: list[int],
) -> list[str]:
    problems: list[str] = []
    for index, expectation in enumerate(expectations):
        before = before_counts[index] if index < len(before_counts) else 0
        after = after_counts[index] if index < len(after_counts) else 0
        if expectation.count is not None and after != expectation.count:
            problems.append(
                f"state expectation {expectation.table} count={after}; expected {expectation.count}"
            )
        elif expectation.count is None and after < 1:
            problems.append(
                f"state expectation {expectation.table} has no matching row"
            )
        if expectation.delta is not None and (after - before) != expectation.delta:
            problems.append(
                f"state expectation {expectation.table} delta={after-before}; "
                f"expected {expectation.delta}"
            )
    return problems


def _outbound_rows(mid: str) -> list[dict[str, Any]]:
    import db

    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT outbound_id,kind,text_body,local_path,mime_type,
                      delivery_status,provider_message_id,conversation_id
               FROM outbound_messages
               WHERE source_message_id=?
               ORDER BY created_at_utc,rowid""",
            (mid,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _reply_from_outbound(rows: list[dict[str, Any]]) -> str:
    texts = [str(row.get("text_body") or "") for row in rows if row.get("kind") == "TEXT"]
    return texts[-1] if texts else ""


def _attachment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("kind") in {"IMAGE", "DOCUMENT"}]


def _fixture_attachment_path(name: str | None) -> str | None:
    if not name:
        return None
    mapping = {
        "vinyl": "vinyl_path",
        "management_receipt": "management_receipt_path",
    }
    key = mapping.get(name, name)
    value = CERT_FIXTURES.get(key)
    return str(value) if value else None


def _private_fixture_tokens() -> list[str]:
    values = [
        CERT_FIXTURES.get("private_cobalt_item_id"),
        CERT_FIXTURES.get("private_vinyl_item_id"),
        CERT_FIXTURES.get("private_vinyl_media_id"),
        CERT_FIXTURES.get("vinyl_path"),
        "PRIVATE-CERT-CANARY-COBALT-7K2",
        "PRIVATE-CERT-CANARY-VINYL-9Q4",
        "Smoke-test code word",
        "Vinyl Setup & Music Inspo",
    ]
    return [str(value) for value in values if value]


def _english_output_problem(reply: str) -> str | None:
    # Owner policy: input may be mixed-language; Alex output is always English.
    if any("\u0b80" <= ch <= "\u0bff" for ch in (reply or "")):
        return "reply contains Tamil script; certification requires English output"
    if _looks_malay(reply):
        return "reply appears to be Malay; certification requires English output"
    return None


def _ha_expectation_problems(contract, before: dict, after: dict) -> list[str]:
    problems: list[str] = []
    for expectation in contract.ha_expectations:
        current = after.get(expectation.entity_id)
        if not current:
            problems.append(f"HA expectation missing entity {expectation.entity_id}")
            continue
        if str(current.get("state")) != str(expectation.state):
            problems.append(
                f"HA {expectation.entity_id} state={current.get('state')!r}; "
                f"expected {expectation.state!r}"
            )
        if expectation.unchanged and before.get(expectation.entity_id) != current:
            problems.append(f"HA {expectation.entity_id} changed but must remain unchanged")
    return problems


def _privacy_leak_problems(contract, reply: str, outbounds: list[dict], trace: dict) -> list[str]:
    if not contract.forbid_private_fixture_leak:
        return []
    blob = json.dumps(
        {"reply": reply, "outbounds": outbounds, "trace": trace},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    leaked = [token for token in _private_fixture_tokens() if token in blob]
    return (
        ["private fixture leaked through tool result/reply/attachment: " + ", ".join(leaked)]
        if leaked else []
    )


def _attachment_identity_problems(contract, outbounds: list[dict]) -> list[str]:
    attachments = _attachment_rows(outbounds)
    problems: list[str] = []
    if contract.expect_attachment and not attachments:
        problems.append("expected original attachment was not queued")
    if contract.expect_attachment_of:
        expected = _fixture_attachment_path(contract.expect_attachment_of)
        if not expected:
            problems.append(
                f"fixture attachment {contract.expect_attachment_of!r} is unavailable"
            )
        else:
            matching = [
                row for row in attachments
                if os.path.realpath(str(row.get("local_path") or "")) == os.path.realpath(expected)
            ]
            if len(matching) != 1:
                problems.append(
                    f"expected attachment {contract.expect_attachment_of!r} exactly once; "
                    f"queued {len(matching)} matching files"
                )
            if len(attachments) != 1:
                problems.append(
                    f"expected exactly one attachment for {contract.expect_attachment_of!r}; "
                    f"queued {len(attachments)}"
                )
    return problems


def _judge_observation(
    contract,
    *,
    source: str,
    reply: str,
    outbounds: list[dict],
    trace: dict,
    elapsed_ms: int,
    state_changes: dict,
    state_expectation_problems: list[str],
    ha_before: dict,
    ha_after: dict,
    ingress_result: dict | None = None,
) -> list[str]:
    import brain

    called_tools = {
        call["tool"] for call in trace.get("calls", [])
        if call.get("tool") and not str(call["tool"]).startswith("_")
    }
    called_caps = set(capabilities_for_tools(called_tools))
    required_caps = set(contract.required_any) - {"routing.discovery"}
    forbidden_caps = set(contract.forbidden) - {"routing.discovery"}

    problems: list[str] = []
    if ingress_result is not None and not ingress_result.get("ok"):
        problems.append("production ingress path returned failure")
    if not (called_caps & required_caps):
        problems.append(
            "required capability was not actually executed: "
            + " / ".join(sorted(required_caps))
        )
    bad = sorted(called_caps & forbidden_caps)
    if bad:
        problems.append("forbidden capability executed: " + ", ".join(bad))
    if trace.get("turn", {}).get("result", {}).get("outcome") == "max_steps":
        problems.append("turn exhausted model/tool step budget")

    problems.extend(_attachment_identity_problems(contract, outbounds))
    problems.extend(state_expectation_problems)
    problems.extend(_ha_expectation_problems(contract, ha_before, ha_after))
    problems.extend(_privacy_leak_problems(contract, reply, outbounds, trace))

    low_reply = (reply or "").casefold()
    for term in contract.expected_terms:
        if term.casefold() not in low_reply:
            problems.append(f"reply missing expected term: {term}")
    for term in contract.forbidden_terms:
        if term.casefold() in low_reply:
            problems.append(f"reply contains forbidden/contradictory phrase: {term}")

    for call in trace.get("calls", []):
        for arg_name in contract.nonzero_forbidden_args:
            if arg_name in call.get("arguments", {}) and _is_nonzero(call["arguments"][arg_name]):
                problems.append(
                    f"tool {call['tool']} invented non-zero {arg_name}="
                    f"{call['arguments'][arg_name]!r}"
                )

    lang_problem = _english_output_problem(reply)
    if contract.reply_language == "en" and lang_problem:
        problems.append(lang_problem)

    persistent_mutation_called = any(
        brain._is_mutating_tool(call["tool"]) and call["tool"] != "ha_control"
        for call in trace.get("calls", [])
        if call.get("tool") and not str(call["tool"]).startswith("_")
    )
    if persistent_mutation_called and not state_changes:
        problems.append("mutating capability returned without durable household-state change")

    for table in contract.unchanged_tables:
        if table in state_changes:
            problems.append(f"table {table} changed but contract requires it unchanged")

    if contract.expect_clarification:
        if "?" not in (reply or ""):
            problems.append("expected a clarification question")
        if persistent_mutation_called:
            problems.append("clarification turn performed a persistent mutation")

    if elapsed_ms > 0 and elapsed_ms > getattr(contract, "hard_latency_ms", 10**12):
        problems.append("contract-specific latency exceeded")
    return problems


def _bind_outbound_provider_id(mid: str, provider_message_id: str) -> None:
    import db

    conn = db.connect()
    try:
        conn.execute(
            """UPDATE outbound_messages SET provider_message_id=?
               WHERE source_message_id=? AND kind='TEXT'""",
            (provider_message_id, mid),
        )
        conn.commit()
    finally:
        conn.close()


def _run_ingress_turn(
    *,
    mid: str,
    conv: str,
    prompt: str,
    source: str,
    phone: str,
    conversation_type: str,
    quoted_message_id: str | None = None,
) -> dict:
    import ingress
    import media

    payload = {
        "message_id": mid,
        "provider": "CERTIFICATION",
        "conversation_id": conv,
        "conversation_type": conversation_type,
        "sender_phone": phone,
        "text": prompt if source != "voice" else "",
        "quoted_message_id": quoted_message_id,
        "sent_at_ms": 1790647200000,  # 29 Sep 2026 02:00:00 UTC
    }
    if source == "voice":
        payload.update({
            "audio_data": base64.b64encode(b"CERTIFICATION VOICE FIXTURE").decode("ascii"),
            "audio_mime_type": "audio/ogg",
        })
        with patch.object(media, "transcribe_audio", return_value=prompt):
            return ingress.process(payload)
    return ingress.process(payload)


def _observe_ingress_turn(
    contract,
    *,
    prompt: str,
    source: str,
    mid: str,
    conv: str,
    phone: str,
    conversation_type: str,
    quoted_message_id: str | None = None,
) -> dict:
    state_before = _state_fingerprint()
    state_expect_before = _expectation_counts(contract.state_expectations, mid)
    ha_before = _ha_snapshot()
    started = time.monotonic()
    ingress_result = _run_ingress_turn(
        mid=mid, conv=conv, prompt=prompt, source=source, phone=phone,
        conversation_type=conversation_type, quoted_message_id=quoted_message_id,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    trace = _trace(mid)
    outbounds = _outbound_rows(mid)
    reply = _reply_from_outbound(outbounds)
    state_after = _state_fingerprint()
    state_changes = _state_diff(state_before, state_after)
    state_expect_after = _expectation_counts(contract.state_expectations, mid)
    state_problems = _state_expectation_problems(
        contract.state_expectations, state_expect_before, state_expect_after
    )
    ha_after = _ha_snapshot()
    problems = _judge_observation(
        contract,
        source=source,
        reply=reply,
        outbounds=outbounds,
        trace=trace,
        elapsed_ms=elapsed_ms,
        state_changes=state_changes,
        state_expectation_problems=state_problems,
        ha_before=ha_before,
        ha_after=ha_after,
        ingress_result=ingress_result,
    )
    return {
        "reply": reply,
        "outbounds": outbounds,
        "elapsed_ms": elapsed_ms,
        "trace": trace,
        "durable_state_diff": state_changes,
        "ingress_result": ingress_result,
        "problems": problems,
    }


def _live_one(contract: PromptContract, prompt: str, source: str,
              hard_latency_ms: int) -> dict:
    mid = f"cert-{contract.id}-{uuid.uuid4().hex[:12]}"
    phone = WIFE if contract.actor == "wife" else HUSBAND
    ctype = contract.conversation_type
    conv = (
        f"{contract.id}-{uuid.uuid4().hex[:8]}@g.us"
        if ctype == "GROUP"
        else f"{contract.id}-{uuid.uuid4().hex[:8]}@s.whatsapp.net"
    )
    observed = _observe_ingress_turn(
        contract, prompt=prompt, source=source, mid=mid, conv=conv,
        phone=phone, conversation_type=ctype,
    )
    problems = list(observed["problems"])
    if observed["elapsed_ms"] > hard_latency_ms:
        problems.append(
            f"hard latency exceeded: {observed['elapsed_ms']}ms > {hard_latency_ms}ms"
        )
    cost_usd = _turn_cost_usd(mid)
    return {
        "contract": contract.id,
        "phase": contract.phase,
        "domain": contract.domain,
        "source": source,
        "actor": contract.actor,
        "conversation_type": contract.conversation_type,
        "prompt": prompt,
        "reply": observed["reply"],
        "outbound_count": len(observed["outbounds"]),
        "attachment_count": len(_attachment_rows(observed["outbounds"])),
        "elapsed_ms": observed["elapsed_ms"],
        "estimated_cost_usd": cost_usd,
        "durable_state_diff": observed["durable_state_diff"],
        "trace": observed["trace"],
        "status": "PASS" if not problems else "FAIL",
        "problems": problems,
    }


def _step_contract(parent, step):
    """Present a ConversationStep through the same judge interface."""
    class StepView:
        pass

    view = StepView()
    for name in (
        "required_any", "forbidden", "expected_terms", "forbidden_terms",
        "nonzero_forbidden_args", "expect_attachment", "expect_attachment_of",
        "state_expectations", "unchanged_tables", "ha_expectations",
        "forbid_private_fixture_leak", "expect_clarification", "reply_language",
    ):
        if hasattr(step, name):
            setattr(view, name, getattr(step, name))
        else:
            setattr(view, name, frozenset() if name == "forbidden" else ())
    view.forbidden = getattr(step, "forbidden", frozenset())
    view.reply_language = getattr(step, "reply_language", "en")
    return view


def _live_conversation(contract, source: str, hard_latency_ms: int) -> dict:
    conv = f"{contract.id}-{source}-{uuid.uuid4().hex[:8]}@s.whatsapp.net"
    rows = []
    previous_mid = None
    previous_provider_id = None
    for index, step in enumerate(contract.steps, 1):
        mid = f"cert-{contract.id}-{source}-{index}-{uuid.uuid4().hex[:8]}"
        view = _step_contract(contract, step)
        quoted = previous_provider_id if step.quote_previous else None
        observed = _observe_ingress_turn(
            view, prompt=step.prompt, source=source, mid=mid, conv=conv,
            phone=HUSBAND, conversation_type="DIRECT_DM",
            quoted_message_id=quoted,
        )
        problems = list(observed["problems"])
        if observed["elapsed_ms"] > hard_latency_ms:
            problems.append(
                f"hard latency exceeded: {observed['elapsed_ms']}ms > {hard_latency_ms}ms"
            )
        provider_id = f"cert-provider-{contract.id}-{index}-{uuid.uuid4().hex[:6]}"
        _bind_outbound_provider_id(mid, provider_id)
        previous_mid = mid
        previous_provider_id = provider_id
        rows.append({
            "step": index,
            "prompt": step.prompt,
            "reply": observed["reply"],
            "outbound_count": len(observed["outbounds"]),
            "attachment_count": len(_attachment_rows(observed["outbounds"])),
            "elapsed_ms": observed["elapsed_ms"],
            "estimated_cost_usd": _turn_cost_usd(mid),
            "durable_state_diff": observed["durable_state_diff"],
            "trace": observed["trace"],
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
