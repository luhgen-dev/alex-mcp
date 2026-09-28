#!/usr/bin/env python3
"""Release-gate audit for Alex MCP.

This is intentionally static/offline. It verifies that the architecture agreed
with the owner cannot silently regress while normal unit/stress tests still pass.
Live provider, WhatsApp and Home Assistant behavior belongs to MCP Check.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from mcp import Client

import brain
from mcp_server import mcp

ROOT = Path(__file__).resolve().parents[1]
APP = Path(__file__).resolve().parent


def main() -> dict:
    checks: list[str] = []
    failures: list[str] = []

    def require(condition: bool, label: str):
        if condition:
            checks.append(label)
        else:
            failures.append(label)

    # 1. Required deterministic domains exist.
    required_modules = {
        "db.py", "services.py", "media.py", "scheduler.py", "outbox.py",
        "phase2.py", "phase2_finance.py", "phase2_work.py", "phase2_library.py",
        "phase2_delegation.py", "phase2_monitor.py", "phase2_presence.py",
        "phase2_policy.py", "phase2_reports.py", "phase2_intent.py",
        "profile_config.py", "diagnostics.py", "ha.py", "connect.js",
    }
    require(all((APP / name).exists() for name in required_modules),
            "all required deterministic domain modules present")

    # 2. MCP surface contains every owner-approved responsibility.
    async def names():
        async with Client(mcp) as client:
            listed = await client.list_tools()
            return {t.name for t in listed.tools}
    tool_names = asyncio.run(names())
    required_tools = {
        # Core ledger / receipts / explicit memory
        "log_expense", "query_finances", "correct_expense",
        "find_receipts", "get_receipt",
        "save_item", "search_saved_items", "get_saved_item",
        "remove_saved_item", "resolve_numbered_choice",
        # Tasks, shopping, diary/plans/agenda/privacy
        "create_reminder", "update_reminder", "reminder_history",
        "add_shopping_item", "list_shopping_items",
        "add_diary_event", "update_diary_event", "resolve_latest_diary_conflict",
        "get_agenda_range", "create_plan", "confirm_plan", "share_plan",
        "check_my_availability", "check_spouse_availability",
        # Mature finance / work / obligations
        "planning_create_goal", "planning_set_period_target",
        "planning_record_goal_contribution", "planning_record_cash",
        "planning_allocate_cash_to_goal", "planning_allocate_cash_to_pool",
        "planning_baseline", "planning_cashflow",
        "bills_list", "bills_match_payment", "bills_record_payment",
        "work_schedule", "work_record_event", "work_ot_status",
        "work_leave_balance", "work_departure_plan",
        # Household library / delegation / HA / reports / diagnostics
        "asset_create", "asset_link_document", "warranty_expiring",
        "monitor_delegate", "monitor_cancel",
        "ha_find_entities", "ha_get_state", "ha_control", "ha_draft_automation",
        "report_snapshot", "report_export", "report_payload",
        "system_health", "recent_failures",
    }
    missing_tools = sorted(required_tools - tool_names)
    require(not missing_tools, "all agreed MCP responsibility tools present")

    # 3. Token budget is architectural, not aspirational.
    require(brain.TOOL_EXPOSURE_MAX <= 6, "provider-facing MCP schema cap is six or fewer")
    representative = [
        "how much did I spend this weekend",
        "remind me tomorrow 9 pay electricity",
        "turn off the living room light",
        "allocate RM300 extra cash to my Europe goal",
        "show my receipt reference 123",
        "what shift am I working next week",
        "move dentist appointment Friday 3pm",
        "save this photo for me",
        "monitor my Europe goal",
        "export my report as pdf",
        "நாளைக்கு 9 மணிக்கு பில் கட்ட நினைவூட்டு",
    ]
    require(
        all(len(brain._select_tool_names(q, ["attachment"] if "photo" in q else None))
            <= brain.TOOL_EXPOSURE_MAX for q in representative),
        "representative intents stay inside MCP token schema budget",
    )
    require(
        brain.DISCOVERY_TOOL_NAME == "discover_alex_tools",
        "AI intent-discovery fallback present for novel/typo-heavy wording",
    )
    require(
        brain._pure_chat("Hi Alex, are you working?"),
        "basic conversational health-check phrase stays tool-free",
    )
    require(
        callable(brain.provider_probe) and callable(brain.classify_runtime_error),
        "live provider probe and sanitized runtime error classifier present",
    )

    # 4. Plug-and-play HA configuration carries user-owned facts/secrets.
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    for key in (
        "ai_provider", "xai_api_key", "gemini_api_key", "openai_api_key",
        "husband_phone", "wife_phone", "timezone", "stt_provider",
        "income_profiles", "roster_profiles", "overtime_profiles",
        "leave_balances", "recurring_payments", "account_aliases",
        "reminder_preferences", "presence_mappings",
        "monthly_ai_budget_usd", "budget_safety_multiplier",
    ):
        require(re.search(rf"^\s*{re.escape(key)}:", config_text, re.M) is not None,
                f"HA configuration exposes {key}")
    require('xai_api_key: ""' in config_text
            and 'gemini_api_key: ""' in config_text
            and 'openai_api_key: ""' in config_text,
            "source ships with empty provider secrets")
    require('husband_phone: ""' in config_text and 'wife_phone: ""' in config_text,
            "source ships with empty household phone placeholders")

    # 5. Provider-neutral defaults and no Needle dependency.
    config_py = (APP / "config.py").read_text(encoding="utf-8")
    require('grok_model: str = "grok-4.7"' in config_py,
            "Grok default model configured")
    require('gemini_model: str = "gemini-3.8-flash"' in config_py,
            "Gemini default model configured")
    require('openai_model: str = "gpt-5.6-luna"' in config_py,
            "OpenAI default model configured")
    import_lines = []
    for p in APP.iterdir():
        if p.suffix != ".py" or p.name == "final_audit.py":
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("import ") or stripped.startswith("from "):
                import_lines.append(stripped)
    require(
        not any(
            re.match(r"^(?:import|from)\s+needle(?:\.|\s|$)", line)
            for line in import_lines
        ),
        "Needle is not an Alex MCP dependency",
    )

    # 6. WhatsApp UX: QR Web UI + explicit family-group binding.
    connect = (APP / "connect.js").read_text(encoding="utf-8")
    require(
        "qrcode.todataurl" in connect.lower()
        and "qrdataurl" in connect.lower()
        and "linked devices" in connect.lower(),
        "WhatsApp QR pairing Web UI present",
    )
    require("alex set family group" in connect.lower(),
            "Family Shared group pairing command present")

    # 7. Core trust rules are explicit in the brain and cannot rely on memory.
    prompt = brain.SYSTEM_PROMPT
    for phrase, label in (
        ("never guess MYR versus SGD", "currency no-guess contract"),
        ("automatic receipt retention", "automatic receipt retention contract"),
        ("never invent a clock time", "date-only no-invented-time contract"),
        ("never read or reveal the spouse's private roster/Diary", "spouse privacy contract"),
        ("OT, variable income and unexpected cash stay unallocated", "variable cash contract"),
        ("never raise an allowance", "owner-controlled allowance contract"),
        ("never invent an entity_id", "Home Assistant entity safety contract"),
    ):
        require(phrase.casefold() in prompt.casefold(), label)

    # 8. Local media dependencies and text-only answer architecture.
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for package in ("whisper.cpp", "tesseract-ocr", "ffmpeg", "poppler-utils"):
        require(package in docker, f"local media dependency {package} bundled")
    docs = (ROOT / "DOCS.md").read_text(encoding="utf-8")
    require("Alex replies in text only." in docs,
            "voice input/text output owner decision documented")

    # 9. No live external integration is falsely certified here.
    require("MCP Check" in (ROOT.parent / "PARITY_AUDIT.md").read_text(encoding="utf-8"),
            "live integration gate is explicitly named MCP Check")

    result = {
        "status": "PASS" if not failures else "FAIL",
        "passed": len(checks),
        "failed": len(failures),
        "checks": checks,
        "failures": failures,
        "tool_count": len(tool_names),
        "tool_exposure_max": brain.TOOL_EXPOSURE_MAX,
        "scope": "offline release architecture; live provider/WhatsApp/HA is MCP Check",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if failures:
        raise SystemExit(1)
    return result


if __name__ == "__main__":
    main()
