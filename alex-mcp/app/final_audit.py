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
    async def surface():
        async with Client(mcp) as client:
            listed = await client.list_tools()
            return {
                t.name: (t.input_schema or {})
                for t in listed.tools
            }
    tool_schemas = asyncio.run(surface())
    tool_names = set(tool_schemas)
    required_tools = {
        # Core ledger / receipts / explicit memory
        "log_expense", "query_finances", "correct_expense",
        "find_receipts", "get_receipt",
        "save_item", "search_saved_items", "get_saved_item",
        "remove_saved_item", "resolve_numbered_choice",
        # Tasks, shopping, diary/plans/agenda/privacy
        "create_reminder", "update_reminder", "reminder_history",
        "create_task", "list_tasks", "update_task", "complete_task",
        "reopen_task", "cancel_task",
        "add_shopping_item", "list_shopping_items", "update_shopping_item",
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

    # 2b. User-facing mutators must be groundable from natural household
    # language. A green capability route is not sufficient when the model would
    # otherwise have to invent an opaque SQLite UUID.
    def schema_parts(name):
        schema = tool_schemas.get(name, {})
        return set(schema.get("properties") or {}), set(schema.get("required") or [])

    natural_reference_contracts = {
        "planning_lock_goal": ({"goal_name"}, {"goal_id"}),
        "planning_reopen_goal": ({"goal_name"}, {"goal_id"}),
        "planning_set_period_target": ({"goal_name"}, {"goal_id", "period"}),
        "planning_change_goal_baseline": ({"goal_name"}, {"goal_id"}),
        "planning_record_goal_contribution": (
            {"goal_name"}, {"goal_id", "contribution_date"}
        ),
        "planning_goal_progress": ({"goal_name"}, {"goal_id"}),
        "planning_goal_deviation": (
            {"goal_name"}, {"goal_id", "actual_amount", "period"}
        ),
        "planning_compare_salary": (
            {"event_date", "amount"}, {"cash_event_id"}
        ),
        "planning_cash_status": (
            {"event_type", "event_date", "amount"}, {"cash_event_id"}
        ),
        "planning_allocate_cash_to_goal": (
            {"cash_event_type", "cash_event_date", "cash_event_amount", "goal_name"},
            {"cash_event_id", "goal_id"},
        ),
        "planning_cash_pool_balance": ({"pool_name"}, {"pool_id"}),
        "planning_allocate_cash_to_pool": (
            {"cash_event_type", "cash_event_date", "cash_event_amount", "pool_name"},
            {"cash_event_id", "pool_id"},
        ),
        "planning_update_reserve": ({"reserve_name"}, {"reserve_id"}),
        "planning_goal_projection": ({"goal_name"}, {"goal_id"}),
        "asset_link_document": ({"asset_name"}, {"asset_id", "evidence_ref"}),
    }
    for name, (natural_fields, opaque_optional) in natural_reference_contracts.items():
        props, required = schema_parts(name)
        require(
            natural_fields <= props and not (opaque_optional & required),
            f"{name} is groundable without inventing opaque ids",
        )

    # 3. Token budget is architectural, not aspirational.
    require(brain.TOOL_EXPOSURE_MAX <= 6, "provider-facing MCP schema cap is six or fewer")
    require(brain.MAX_MODEL_CALLS <= 4, "provider orchestration is capped at four model calls")
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
    async def exposed_for(text):
        return {
            spec["function"]["name"]
            for spec in await brain._tool_specs(text)
            if isinstance(spec, dict) and spec.get("function")
        }

    # A mutator that needs an opaque id must be exposed with a safe resolver/read
    # path on the same natural turn. This prevents a model from fabricating UUIDs.
    grounding_cases = {
        "Actually change that parking expense to RM8.50": {
            "correct_expense", "query_finances",
        },
        "Approve that pending expense as food": {
            "confirm_expense", "list_pending_expenses",
        },
        "Send me my management receipt": {
            "find_receipts", "get_receipt",
        },
        "Delete the cobalt note I asked you to remember": {
            "remove_saved_item", "search_saved_items",
        },
        "Cancel my dentist reminder": {
            "update_reminder", "list_reminders",
        },
        "Complete the Malacca passport task": {
            "complete_task", "list_tasks",
        },
        "Mark bread as bought": {
            "update_shopping_item", "list_shopping_items",
        },
        "Turn off the living room light": {
            "ha_control", "ha_find_entities",
        },
        "Confirm the Malacca plan": {
            "confirm_plan", "list_plans",
        },
        "Move my dentist appointment to 5pm": {
            "update_diary_event",
        },
        "Record the TNB bill as paid": {
            "bills_record_payment", "bills_list",
        },
        "Stop monitoring my holiday goal": {
            "monitor_cancel", "monitor_list",
        },
    }
    for phrase, expected in grounding_cases.items():
        exposed = asyncio.run(exposed_for(phrase))
        require(
            expected <= exposed,
            "opaque-id action is grounded: " + phrase,
        )

    diary_grounding = asyncio.run(exposed_for(
        "Move my dentist appointment to 5pm"
    ))
    require(
        bool(diary_grounding & {"get_agenda", "get_agenda_range"}),
        "diary update has an agenda resolver for its opaque diary id",
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
    require(
        callable(brain._provider_routes) and callable(brain._auto_needs_full_model),
        "automatic cheapest-capable provider router present",
    )
    require(
        brain._local_chat_reply("Hi Alex, are you working?") is not None,
        "tiny health-check chat can stay fully local and zero-token",
    )
    require(
        callable(brain._discover_tool_specs)
        and callable(brain._contextual_tool_hints)
        and callable(brain._looks_like_false_capability_denial)
        and callable(brain._looks_like_wrong_language_reply),
        "v0.5 semantic discovery, conversation focus, and bounded model self-repair present",
    )

    # 4. Plug-and-play HA configuration carries user-owned facts/secrets.
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    for key in (
        "ai_provider", "xai_api_key", "gemini_api_key", "openai_api_key",
        "gemini_lite_model", "husband_phone", "wife_phone", "timezone", "stt_provider",
        "income_profiles", "roster_profiles", "overtime_profiles",
        "leave_balances", "recurring_payments", "account_aliases",
        "reminder_preferences", "presence_mappings",
        "monthly_ai_budget_usd", "auto_grok_fallback_budget_usd", "budget_safety_multiplier",
    ):
        require(re.search(rf"^\s*{re.escape(key)}:", config_text, re.M) is not None,
                f"HA configuration exposes {key}")
    require('xai_api_key: ""' in config_text
            and 'gemini_api_key: ""' in config_text
            and 'openai_api_key: ""' in config_text,
            "source ships with empty provider secrets")
    require('husband_phone: ""' in config_text and 'wife_phone: ""' in config_text,
            "source ships with empty household phone placeholders")
    require('reasoning_effort: "low"' in config_text,
            "default reasoning is low for latency/cost-sensitive household calls")
    require('ai_provider: "auto"' in config_text
            and 'list(auto|grok|gemini|openai)' in config_text,
            "Auto Saver is the shipped provider-neutral default")
    require('auto_grok_fallback_budget_usd: 0.5' in config_text,
            "automatic Grok fallback has a conservative monthly spend cap")

    # 5. Provider-neutral defaults and no Needle dependency.
    config_py = (APP / "config.py").read_text(encoding="utf-8")
    require('grok_model: str = "grok-4.7"' in config_py,
            "Grok default model configured")
    require('gemini_model: str = "gemini-3.8-flash"' in config_py,
            "Gemini quality model configured")
    require('gemini_lite_model: str = "gemini-3.1-flash-lite"' in config_py,
            "Gemini low-cost agentic model configured")
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
    require(
        "isAlexMentioned(message)" in connect
        and "isReplyToAlex(message)" in connect
        and "if (!(await isAlexMentioned(message)) && !(await isReplyToAlex(message))) return;" in connect
        and "async function jidMatchesSelf" in connect,
        "Family Shared responds only to explicit mention or swipe reply",
    )
    require(
        "USAGE_URL" in connect and "AI usage — last 24h" in connect,
        "local no-provider-call usage telemetry is visible in Web UI",
    )
    require(
        "SLOW_ACK_MS" in connect
        and "SLOW_ACK_TEXT" in connect
        and "slowAckTimer" in connect
        and "if (!isGroup)" in connect,
        "slow private turns get a bounded working acknowledgement without weakening group reply binding",
    )
    db_text = (APP / "db.py").read_text(encoding="utf-8")
    require(
        "resolve_quoted_context" in db_text
        and "provider_message_id" in db_text
        and "quoted_message_id" in db_text,
        "WhatsApp swipe replies bind to durable same-conversation context",
    )

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
    media_text = (APP / "media.py").read_text(encoding="utf-8")
    schema_text = (APP / "schema.sql").read_text(encoding="utf-8")
    require(
        "_choose_voice_transcript" in media_text
        and '"en", "ta"' in media_text
        and "auto_cloud_rescue" in media_text,
        "voice pipeline locally verifies suspicious English/Tamil ASR before cloud rescue",
    )
    require(
        "transcript_meta_json TEXT" in schema_text
        and '"transcript_meta_json"' in db_text,
        "voice ASR provenance is durably migrated for diagnostics",
    )
    human_ai = APP / "human_ai_lab.py"
    require(human_ai.exists(), "ChatGPT/human reasoning bridge is part of the external certification rig")
    if human_ai.exists():
        human_text = human_ai.read_text(encoding="utf-8")
        require(
            "_opaque_packet_id" in human_text
            and "_public_packet" in human_text
            and '"_contract_id"' in human_text,
            "human-AI reasoning packets are blind to certification answer labels",
        )

    chatgpt_review = APP / "chatgpt_reasoning_review.py"
    require(
        chatgpt_review.exists(),
        "independent ChatGPT reasoning QC is shipped with the certification rig",
    )
    if chatgpt_review.exists():
        review_text = chatgpt_review.read_text(encoding="utf-8")
        require(
            "REVIEWED_CORPUS_FINGERPRINT" in review_text
            and "_public_packet" in review_text
            and "independent_public_packet_only" in review_text,
            "ChatGPT reasoning QC is blind and fingerprint-bound",
        )
    lab_text = (APP / "alex_lab.py").read_text(encoding="utf-8")
    require(
        "run_chatgpt_reasoning_review" in lab_text
        and '"chatgpt_reasoning_review"' in lab_text,
        "External Alex Lab gates on the independent ChatGPT reasoning review",
    )

    # Version metadata must never drift between the HA card, server and image.
    server_text = (APP / "mcp_server.py").read_text(encoding="utf-8")
    config_version = re.search(r'^version:\s*"([^"]+)"', config_text, re.M)
    server_version = re.search(r'version="([^"]+)"', server_text)
    docker_version = re.search(r'^ARG BUILD_VERSION=([^\s]+)', docker, re.M)
    require(
        bool(config_version and server_version and docker_version)
        and config_version.group(1) == server_version.group(1) == docker_version.group(1),
        "HA config, MCP server and Docker image versions are aligned",
    )

    # 9. Production safety: no test-state wipe script is shipped.
    reset_script = ROOT / "rootfs" / "etc" / "cont-init.d" / "05-reset-test-state"
    require(not reset_script.exists(), "production image contains no automatic test-state reset")

    # 10. No live external integration is falsely certified here.
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
