#!/usr/bin/env python3
"""Deterministic reasoning regression oracle.

This module is deliberately NOT presented as an independent-model test. It is
a hand-authored, public-packet-only regression oracle derived from earlier QC
findings. Its value is fast repeatability: known language/routing cases should
not regress.

The genuinely fresh external-model judgment snapshot is stored separately in
real_ai_snapshot.json and scored by real_ai_snapshot_review.py.

This oracle still fails closed when its packet corpus fingerprint changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

import human_ai_lab


# Frozen deterministic regression corpus. Any prompt/tool-surface change
# invalidates this oracle until its regression expectations are reviewed.
REVIEWED_CORPUS_FINGERPRINT = "fb1f56c6dc24afd91e4ea2e3617c1e2c7b2d1e67aa75e3b5f665947acdae660f"


def _low(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _available(packet: dict[str, Any]) -> set[str]:
    return {
        str(tool.get("name"))
        for tool in packet.get("available_tools", [])
        if tool.get("name")
    }


def _pick(available: set[str], *names: str) -> list[str]:
    return [name for name in names if name in available]


def _one(available: set[str], *names: str) -> list[str]:
    for name in names:
        if name in available:
            return [name]
    return []


def _decision(packet: dict[str, Any]) -> dict[str, Any]:
    # Safety assertion: the reviewer is blind to contract/domain/expected labels.
    leaked = sorted(key for key in packet if str(key).startswith("_"))
    if leaked:
        raise RuntimeError(
            "external reasoning packet leaked private certification metadata: "
            + ", ".join(leaked)
        )

    prompt = str(packet.get("prompt") or "")
    text = _low(prompt)
    tools = _available(packet)
    actor = str(packet.get("actor") or "")
    history = " ".join(
        str(item.get("content") or "")
        for item in packet.get("conversation_history", [])
        if item.get("role") == "user"
    )
    prior = _low(history)

    def result(kind: str, selected: list[str] | None = None) -> dict[str, Any]:
        selected = selected or []
        selected = [
            name for i, name in enumerate(selected)
            if name in tools and name != "discover_alex_tools"
            and name not in selected[:i]
        ]
        return {
            "packet_id": packet["packet_id"],
            "decision": kind,
            "tools": selected,
            "reply_language": "en",
        }

    # Genuine ambiguity: possibility is not permission to mutate.
    if re.search(r"\b(?:might buy|maybe we need|thinking of getting) coffee\b", text):
        return result("clarify")

    # Cross-spouse private deletion is a privacy refusal, not a failed lookup.
    if (
        actor == "wife"
        and "cobalt" in text
        and re.search(r"\b(?:remove|delete)\b", text)
    ):
        return result("refuse")

    # High-value short follow-ups whose meaning comes from the preceding turn.
    if text == "confirm that one as food.":
        return result(
            "tools",
            _pick(tools, "list_pending_expenses", "confirm_expense")
            or _one(tools, "confirm_expense"),
        )
    if text == "list the amounts i have explicitly set aside each month.":
        return result(
            "tools", _one(tools, "planning_list_reserves", "planning_baseline")
        )
    if text == "compare this month's holiday contribution with the target.":
        return result("tools", _one(tools, "planning_goal_deviation"))
    if text == "show number 1.":
        return result("tools", _one(tools, "resolve_numbered_choice"))
    if text.startswith(
        "i don't know the date yet. make it kid-friendly"
    ):
        return result("tools", _one(tools, "update_plan"))
    if text.startswith("actually it was rm") and "parking" in prior:
        return result(
            "tools",
            _pick(tools, "query_finances", "correct_expense")
            or _one(tools, "correct_expense"),
        )
    if text == "send me that again." and "receipt" in prior:
        return result("tools", _one(tools, "get_receipt", "find_receipts"))

    # Contextual HA follow-up: prior user text supplies the device focus,
    # while the current imperative supplies write authority.
    if (
        re.search(r"\b(?:turn|switch)\s+(?:it|that)\s+(?:on|off)\b", text)
        and re.search(r"\b(?:ac|air conditioner|light|fan|switch|thermostat)\b", prior)
    ):
        return result(
            "tools",
            _pick(tools, "ha_find_entities", "ha_control")
            or _one(tools, "ha_control"),
        )

    # Explicitly denied saved-memory writes that request a browse instead.
    if (
        re.search(r"\b(?:don't|do not|dont|not asking(?: you)? to)\b", text)
        and re.search(r"\b(?:save|remember)\b", text)
        and re.search(r"\b(?:show|list|find|saved items?|already saved)\b", text)
    ):
        return result("tools", _one(tools, "search_saved_items"))

    # Compound roster + departure questions require both reads when exposed.
    if (
        re.search(r"\b(?:shift|work)\b", text)
        and (
            re.search(r"\b(?:what time|when)\s+(?:should|do)\s+i\s+leave\b", text)
            or "leave home" in text
            or "departure" in text
        )
    ):
        return result(
            "tools",
            _pick(tools, "work_schedule", "work_departure_plan")
            or _one(tools, "work_departure_plan", "work_schedule"),
        )

    # Original media provenance/replay. This is regression logic only; the
    # real-AI snapshot makes its own independent choices for these packets.
    if re.search(r"\b(?:voice\s*notes?|audio\s*notes?|recordings?)\b", text):
        if re.search(r"\b(?:send|open|get|original|preserved)\b", text):
            return result(
                "tools",
                _pick(tools, "find_media", "get_media_original")
                or _one(tools, "get_media_original", "find_media"),
            )
        return result("tools", _one(tools, "find_media"))
    if (
        re.search(r"\b(?:send|open|get)\b", text)
        and re.search(r"\b(?:voice\s*note|audio\s*note|recording)\b", text)
    ):
        return result(
            "tools",
            _pick(tools, "find_media", "get_media_original")
            or _one(tools, "get_media_original", "find_media"),
        )

    # Post-smoke lifecycle routes that previously produced false success or
    # wrong-domain answers. Keep them ahead of generic finance/monitor/reminder
    # keyword handling.
    if (
        (
            re.search(r"\b(?:push|hand|give|pass|transfer|ask)\b", text)
            and re.search(r"\b(?:priya|wife|husband|spouse|partner)\b", text)
        )
        or (
            re.search(r"\b(?:priya|wife|husband|spouse|partner)\b", text)
            and re.search(r"\b(?:take|claim|handle)\b", text)
        )
    ) and "handoff_reminder_claim" in tools:
        return result("tools", ["handoff_reminder_claim"])

    if (
        "reminder" in text
        and re.search(r"\b(?:release|unclaim|can'?t handle|cannot handle|can'?t do)\b", text)
    ):
        return result(
            "tools",
            _pick(tools, "list_reminders", "release_reminder_claim")
            or _one(tools, "release_reminder_claim"),
        )

    if re.search(r"\b(?:took|had)\s+(?:annual leave|medical leave|mc)\b", text):
        return result("tools", _one(tools, "set_leave_record", "work_record_event"))
    if re.search(r"\b(?:annual leave|medical leave|\bmc\b)\b", text) and (
        re.search(r"\b(?:tomorrow|next|planned|planning)\b", text)
        or re.search(r"\b(?:i'?m|i am|will be)\s+(?:on\s+)?(?:annual leave|medical leave|mc)\b", text)
    ):
        return result("tools", _one(tools, "set_leave_record", "work_record_event"))

    if re.search(
        r"\b(?:this month|just for this month)\b.*\btarget\b"
        r"|\btarget\b.*\b(?:this month|just for this month)\b",
        text,
    ) and "planning_set_period_target" in tools:
        return result("tools", ["planning_set_period_target"])

    if re.search(
        r"\b(?:change|update|edit|raise|lower)\b.*\b(?:goal|savings?)\b.*\btarget\b"
        r"|\b(?:goal|savings?)\b.*\btarget\b.*\b(?:to|=)\b",
        text,
    ):
        return result("tools", _one(tools, "planning_update_goal_target"))

    if re.search(
        r"\b(?:my\s+)?(?:stash|cash pool|buffer)\b.*\b(?:is|has|balance)\b.*\b(?:rm|myr|sgd|\d)",
        text,
    ) or re.search(
        r"\bset\b.*\b(?:stash|cash pool|buffer)\b.*\bbalance\b", text
    ):
        return result("tools", _one(tools, "planning_declare_cash_pool_balance"))

    if re.search(
        r"\b(?:spent|used|paid|record)\b.*\b(?:from|using)\b.*\b(?:stash|cash pool|buffer)\b"
        r"|\brecord\b.*\bspent\b.*\b(?:stash|cash pool|buffer)\b",
        text,
    ):
        return result("tools", _one(tools, "planning_record_cash_pool_spend"))

    if re.search(
        r"\bcash\s*outflow\b|\btotal\s*outflow\b"
        r"|\bmoney\b.*\b(?:went|goes?|going)\s+out\b",
        text,
    ):
        return result("tools", _one(tools, "planning_cash_outflow"))

    if re.search(
        r"\b(?:change|update|edit)\b.*\b(?:warranty|asset|appliance)\b"
        r"|\bwarranty\b.*\b(?:expiry|end date|expires)\b.*\b(?:to|on)\b",
        text,
    ):
        return result("tools", _one(tools, "asset_update"))

    if re.search(
        r"\b(?:monitor|watch|tell me when|let me know when)\b.*"
        r"\b(?:light|switch|fan|ac|aircon|air conditioner|thermostat|climate|tv|television|speaker)\b",
        text,
    ) and "monitor_home_state" in tools:
        return result(
            "tools",
            _pick(tools, "ha_find_entities", "ha_get_state", "monitor_home_state")
            or ["monitor_home_state"],
        )

    # Cross-domain semantic precedence discovered during the regression QC
    # pass. These rules resolve natural language where a keyword-only classifier
    # is especially likely to choose the wrong domain.

    # Media + an explicit financial write is a ledger action even when the word
    # "receipt" is omitted ("Add the payment shown in this PDF").
    if (
        packet.get("source") in {"image", "pdf"}
        and re.search(r"\b(?:add|log|record)\b", text)
        and re.search(r"\b(?:payment|amount|expense)\b", text)
    ):
        return result("tools", _one(tools, "log_expense"))

    # Plain spending/expense/transaction questions are ledger reads. Keep this
    # ahead of planning/report vocabulary so "spending today" cannot fall
    # through to an unrelated domain.
    if (
        re.search(r"\b(?:spending|expenses?|transactions?)\b", text)
        and "report" not in text
        and not re.search(
            r"\b(?:log|record|add|correct|fix|wrong|actually|change|"
            r"pending|waiting|clarif|approve|confirm)\b",
            text,
        )
    ):
        return result("tools", _one(tools, "query_finances"))

    # "Anything I need to remember later?" is naturally a reminder query, not
    # an explicit saved-memory browse.
    if text == "anything i need to remember later?":
        return result("tools", _one(tools, "list_reminders"))

    if (
        re.search(r"\bexpenses?\b.*\b(?:waiting|clarify|pending)\b", text)
        or re.search(r"\bpending expenses?\b", text)
    ):
        return result("tools", _one(tools, "list_pending_expenses"))

    if (
        "reminder" in text
        and (
            "history" in text
            or text.startswith("what happened to ")
        )
    ):
        return result("tools", _one(tools, "reminder_history"))

    # "Remind me what..." asks for recall of the plan, not a new reminder.
    if text.startswith("remind me what") and "plan" in text:
        return result("tools", _one(tools, "list_plans"))

    if (
        re.search(r"\b(?:list|show|what)\b.*\b(?:savings )?goals?\b", text)
        or "saving towards" in text
    ):
        return result("tools", _one(tools, "planning_list_goals", "planning_goal_progress"))

    if (
        re.search(r"\b(?:what|do i have|anything)\b.*\bbills?\b", text)
        or re.search(r"\bbill\b.*\bcoming up\b", text)
        or re.search(r"\bwhat'?s due\b", text)
    ):
        return result("tools", _one(tools, "bills_list"))

    # First-class task semantics outrank the surrounding plan name.
    if "left to do" in text and "list_tasks" in tools:
        return result("tools", ["list_tasks"])
    if (
        "task" in text
        and re.search(r"\b(?:remove|cancel)\b", text)
        and "cancel_task" in tools
    ):
        return result(
            "tools",
            _pick(tools, "list_tasks", "cancel_task")
            or ["cancel_task"],
        )

    # Cash arrival is not an expense and stays unallocated until instructed.
    if (
        re.search(r"\brm\s*\d+(?:\.\d+)?\b", text)
        and re.search(r"\b(?:ot|overtime|bonus|salary|extra cash)\b", text)
        and re.search(r"\b(?:got|came in|received|credited|record)\b", text)
    ):
        return result("tools", _one(tools, "planning_record_cash"))

    # Asset/warranty inventory is distinct from explicit saved-memory storage.
    if re.search(r"\b(?:appliances?|assets?|warrant(?:y|ies))\b", text):
        if re.search(r"\b(?:what|show|any|list)\b", text):
            return result(
                "tools", _one(tools, "asset_list", "warranty_expiring")
            )

    if re.search(r"\b(?:finance|finances)\b.*\b(?:summary|this month)\b", text):
        return result(
            "tools",
            _one(tools, "query_finances", "report_snapshot", "planning_brief"),
        )

    # Confirming a draft plan is the operation that creates its linked diary
    # event; calling add_diary_event separately risks duplication. Negated
    # locking ("don't lock it", "unlocked") is creation/refinement, not consent.
    plan_lock_negated = bool(
        re.search(r"\b(?:don'?t|do not|not)\s+lock\b|\bunlocked\b", text)
    )
    if (
        "plan" in text
        and not plan_lock_negated
        and re.search(r"\b(?:make .* real|lock|confirm)\b", text)
    ):
        return result("tools", _one(tools, "confirm_plan"))

    if (
        re.search(r"\b(?:wife|spouse|partner|husband)\b", text)
        and re.search(r"\b(?:free|available|availability)\b", text)
    ):
        return result("tools", _one(tools, "check_spouse_availability"))

    # Goal lifecycle and projections outrank the noun phrase "savings goal".
    if re.search(r"\b(?:lock|activate)\b.*\bgoal\b", text):
        return result(
            "tools",
            _pick(tools, "planning_list_goals", "planning_lock_goal")
            or _one(tools, "planning_lock_goal"),
        )
    if re.search(r"\b(?:reopen|resume)\b.*\bgoal\b", text):
        return result(
            "tools",
            _pick(tools, "planning_list_goals", "planning_reopen_goal")
            or _one(tools, "planning_reopen_goal"),
        )
    if "goal" in text and (
        "this month only" in text
        or re.search(r"\benough\b.*\bthis month\b", text)
    ):
        return result("tools", _one(tools, "planning_set_period_target"))
    if "goal" in text and re.search(
        r"\b(?:when will i reach|project how long)\b", text
    ):
        return result("tools", _one(tools, "planning_goal_projection"))

    if (
        "fixed income" in text
        and "locked commitments" in text
    ):
        return result("tools", _one(tools, "planning_baseline"))
    if re.search(r"\b(?:income outlook|income am i expecting|expecting this month)\b", text):
        return result("tools", _one(tools, "planning_income_outlook"))

    # Departure planning outranks the word "leave", which otherwise resembles
    # annual-leave records.
    if (
        "leave home" in text
        or "departure" in text
        or re.search(r"\bwhat time should i leave home\b", text)
    ):
        return result("tools", _one(tools, "work_departure_plan"))

    # Explicit monitor/track verbs are delegation semantics even when the
    # monitored subject is a goal.
    if re.search(r"\b(?:monitor|monitoring|track|tracking)\b", text):
        if re.search(r"\b(?:stop|cancel)\b", text):
            return result("tools", _one(tools, "monitor_cancel"))
        if re.search(r"\b(?:what|show|list)\b", text):
            return result("tools", _one(tools, "monitor_list"))
        return result("tools", _one(tools, "monitor_delegate"))

    # Google Sheets consumes structured payload; PDF is the exported file path.
    if "google sheets" in text:
        return result("tools", _one(tools, "report_payload", "report_export"))

    # Exact persisted numbered answers.
    if re.fullmatch(r"[123]", text):
        if any(word in prior for word in ("diary", "appointment", "conflict")):
            return result(
                "tools",
                _one(tools, "resolve_latest_diary_conflict", "resolve_numbered_choice"),
            )
        return result(
            "tools",
            _one(tools, "resolve_numbered_choice", "resolve_latest_diary_conflict"),
        )

    # Compound finance + reminder request.
    if (
        "parking" in text
        and re.search(r"\brm\s*6\b", text)
        and "remind" in text
    ):
        return result("tools", _pick(tools, "log_expense", "create_reminder"))

    # Receipts / preserved evidence.
    if "receipt" in text or (
        "payment" in text
        and any(word in text for word in ("management", "tnb", "electricity"))
    ):
        if (
            re.search(r"\b(?:log|add)\b", text)
            and packet.get("source") in {"image", "pdf"}
        ):
            return result("tools", _one(tools, "log_expense"))
        if (
            any(name in tools for name in ("bills_record_payment", "bills_match_payment"))
            and re.search(r"\b(?:record|paid|as paid|which bill|match|belong)\b", text)
        ):
            if re.search(r"\b(?:which bill|match|belong)\b", text):
                return result("tools", _one(tools, "bills_match_payment"))
            return result(
                "tools",
                _pick(tools, "bills_match_payment", "bills_record_payment")
                or _one(tools, "bills_record_payment"),
            )
        if re.search(
            r"\b(?:show|send|open|get|find|have|saved|receipt)\b", text
        ):
            return result(
                "tools",
                _pick(tools, "find_receipts", "get_receipt")
                or _one(tools, "find_receipts", "get_receipt"),
            )

    # Finance ledger.
    finance_signal = bool(
        re.search(
            r"\b(?:finance|finances|expenses?|transactions?|spending|paid|payment|management fee|salary)\b",
            text,
        )
        or re.search(r"\brm\s*\d", text)
    )
    if finance_signal:
        if (
            re.search(r"\b(?:paid|spent|log|record|add)\b", text)
            and re.search(r"\brm\s*\d", text)
            and not any(
                word in text
                for word in (
                    "contribution", "reserve", "goal", "ot", "overtime",
                    "salary", "bonus", "cash", "bill",
                )
            )
        ):
            return result("tools", _one(tools, "log_expense"))
        if re.search(
            r"\b(?:correct|fix|wrong|actually.*rm|change that parking)\b", text
        ):
            return result(
                "tools",
                _pick(tools, "query_finances", "correct_expense")
                or _one(tools, "correct_expense"),
            )
        if "pending" in text or "needs my confirmation" in text:
            return result("tools", _one(tools, "list_pending_expenses"))
        if re.search(r"\b(?:approve|confirm that one)\b", text):
            return result(
                "tools",
                _pick(tools, "list_pending_expenses", "confirm_expense")
                or _one(tools, "confirm_expense"),
            )
        if (
            "salary" in text
            and ("compare" in text or "configured salary" in text)
        ):
            return result("tools", _one(tools, "planning_compare_salary"))
        if "query_finances" in tools:
            return result("tools", ["query_finances"])

    # Shopping.
    if (
        any(
            phrase in text
            for phrase in (
                "shopping list", "grocery list", "groceries", "left to buy",
            )
        )
        or re.search(r"\b(?:bananas?|bread|diapers?|milk)\b", text)
    ):
        if re.search(r"\b(?:add|put|we need)\b", text):
            return result("tools", _one(tools, "add_shopping_item"))
        if re.search(
            r"\b(?:remove|mark|bought|done|already bought)\b", text
        ):
            return result("tools", _one(tools, "update_shopping_item"))
        return result("tools", _one(tools, "list_shopping_items"))

    # Explicit saved memory.
    if (
        any(
            phrase in text
            for phrase in (
                "remember", "saved", "save this", "asked you to keep",
                "asked you to save", "code word", "vinyl", "turntable",
            )
        )
        or re.search(r"\bkeep a note\b", text)
    ):
        if (
            re.search(r"\b(?:remember that|remember this|save this|keep a note)\b", text)
            and not re.search(
                r"\b(?:show|what|find|open|where|delete|remove)\b", text
            )
        ):
            return result("tools", _one(tools, "save_item"))
        if re.search(r"\b(?:delete|remove)\b", text):
            return result(
                "tools",
                _pick(tools, "search_saved_items", "remove_saved_item")
                or _one(tools, "remove_saved_item"),
            )
        if re.search(
            r"\b(?:show|open|where|find).*\b(?:picture|image|vinyl|turntable)\b",
            text,
        ):
            return result(
                "tools",
                _pick(tools, "search_saved_items", "get_saved_item")
                or _one(tools, "search_saved_items", "get_saved_item"),
            )
        return result("tools", _one(tools, "search_saved_items"))

    # Explicitly denied reminder mutations stay read-only.
    if (
        re.search(r"\b(?:don't|do not|dont|not asking(?: you)? to)\b", text)
        and re.search(r"\b(?:delete|remove|cancel|complete|reschedule|change|update)\b", text)
        and re.search(r"\b(?:reminder|remnder|remidn|remindn|remidr)\b", text)
    ):
        return result("tools", _one(tools, "list_reminders"))

    # Reminders.
    if (
        "remind" in text
        or "reminder" in text
        or "rember me" in text
        or "remidn" in text
        or "remnder" in text
        or "நினைவூட்டு" in prompt
    ):
        if re.search(
            r"\b(?:what|show|any|do i have|coming up)\b", text
        ):
            return result("tools", _one(tools, "list_reminders"))
        if re.search(r"\b(?:history|what happened)\b", text):
            return result("tools", _one(tools, "reminder_history"))
        if re.search(
            r"\b(?:mark|cancel|move|snooze|complete|reschedule)\b", text
        ):
            return result(
                "tools",
                _pick(tools, "list_reminders", "update_reminder")
                or _one(tools, "update_reminder"),
            )
        return result("tools", _one(tools, "create_reminder"))

    # Tamil explicit-memory browse.
    if "சேமித்த" in prompt or "சேமிக்க" in prompt:
        return result("tools", _one(tools, "search_saved_items"))

    # First-class tasks.
    if re.search(r"\b(?:tasks?|passport-check|passport task)\b", text):
        if re.search(r"\b(?:add|make|create|i need)\b", text):
            return result("tools", _one(tools, "create_task"))
        if re.search(
            r"\b(?:what|show|unfinished|left to do|active)\b", text
        ):
            return result("tools", _one(tools, "list_tasks"))
        if re.search(r"\b(?:change|update|edit)\b", text):
            return result(
                "tools",
                _pick(tools, "list_tasks", "update_task")
                or _one(tools, "update_task"),
            )
        if re.search(r"\b(?:mark|complete|done|finish)\b", text):
            return result(
                "tools",
                _pick(tools, "list_tasks", "complete_task")
                or _one(tools, "complete_task"),
            )
        if re.search(r"\b(?:reopen|back to open)\b", text):
            return result(
                "tools",
                _pick(tools, "list_tasks", "reopen_task")
                or _one(tools, "reopen_task"),
            )
        if re.search(r"\b(?:cancel|remove)\b", text):
            return result(
                "tools",
                _pick(tools, "list_tasks", "cancel_task")
                or _one(tools, "cancel_task"),
            )

    # Diary / agenda / availability.
    if (
        any(
            phrase in text
            for phrase in (
                "appointment", "agenda", "calendar", "diary", "am i free",
                "availability", "anything happening", "coming up",
            )
        )
        or re.search(r"\bwhat do i have on\b", text)
    ):
        if "wife" in text or "spouse" in text:
            return result("tools", _one(tools, "check_spouse_availability"))
        if "am i free" in text or "my availability" in text:
            return result("tools", _one(tools, "check_my_availability"))
        if (
            re.search(r"\b(?:add|put)\b.*\b(?:diary|appointment)\b", text)
            or re.match(r"i have a .*appointment", text)
        ):
            return result("tools", _one(tools, "add_diary_event"))
        if re.search(
            r"\b(?:move|reschedule|cancel)\b.*\b(?:appointment|meeting|event)\b",
            text,
        ):
            return result(
                "tools",
                _pick(tools, "get_agenda", "update_diary_event")
                or _one(tools, "update_diary_event"),
            )
        return result("tools", _one(tools, "get_agenda_range", "get_agenda"))

    # Draft plans, kept separate from money plans.
    if re.search(r"\bmalacca\b", text) or re.search(
        r"\b(?:day.trip|draft plan|family day trip)\b", text
    ):
        if (
            re.search(r"\b(?:start|create|planning)\b", text)
            and not re.search(r"\b(?:what|show|so far)\b", text)
        ):
            return result("tools", _one(tools, "create_plan"))
        if re.search(r"\b(?:lock|confirm|make .* real)\b", text):
            return result("tools", _one(tools, "confirm_plan"))
        if re.search(r"\b(?:share|publish)\b", text):
            return result("tools", _one(tools, "share_plan"))
        if re.search(
            r"\b(?:update|keep the date|kid-friendly|add to our .*draft|back home|return around)\b",
            text,
        ):
            return result("tools", _one(tools, "update_plan"))
        return result("tools", _one(tools, "list_plans"))

    # Goal / cash planning.
    planning_signal = any(
        phrase in text
        for phrase in (
            "goal", "savings", "saving", "extra cash", "cash pool", "stash",
            "reserve", "baseline", "cash flow", "cashflow", "income outlook",
            "money plan", "financial plan", "ot money", "overtime pay", "bonus",
        )
    )
    if planning_signal:
        if (
            re.search(r"\b(?:create|start|i want)\b.*\bgoal\b", text)
        ):
            return result("tools", _one(tools, "planning_create_goal"))
        if re.search(r"\b(?:lock|activate)\b.*\bgoal\b", text):
            return result(
                "tools",
                _pick(tools, "planning_list_goals", "planning_lock_goal")
                or _one(tools, "planning_lock_goal"),
            )
        if re.search(r"\b(?:reopen|resume)\b.*\bgoal\b", text):
            return result(
                "tools",
                _pick(tools, "planning_list_goals", "planning_reopen_goal")
                or _one(tools, "planning_reopen_goal"),
            )
        if (
            re.search(
                r"\b(?:change|make).*\b(?:contribution|baseline)\b.*\b(?:every month|monthly)\b",
                text,
            )
            or ("from now on" in text and "baseline" in text)
        ):
            return result(
                "tools",
                _pick(tools, "planning_list_goals", "planning_change_goal_baseline")
                or _one(tools, "planning_change_goal_baseline"),
            )
        if re.search(r"\b(?:this month|just for this month)\b.*\btarget\b", text):
            return result("tools", _one(tools, "planning_set_period_target"))
        if (
            re.search(r"\b(?:contribution|put rm\d+ into .*savings)\b", text)
            and "compare" not in text
        ):
            return result(
                "tools",
                _pick(tools, "planning_list_goals", "planning_record_goal_contribution")
                or _one(tools, "planning_record_goal_contribution"),
            )
        if re.search(
            r"\b(?:below plan|compare .*contribution.*target)\b", text
        ):
            return result("tools", _one(tools, "planning_goal_deviation"))
        if re.search(
            r"\b(?:which goal|match .*alias|alias .*goal|holiday account)\b", text
        ):
            return result("tools", _one(tools, "planning_match_goal_alias"))
        if re.search(r"\b(?:when will i reach|project how long)\b", text):
            return result("tools", _one(tools, "planning_goal_projection"))
        if (
            re.search(
                r"\b(?:got|record|came in|received|credited)\b.*\b(?:ot|overtime|bonus|salary|extra cash)\b",
                text,
            )
            or re.search(r"\brm\d+ ot\b", text)
        ):
            return result("tools", _one(tools, "planning_record_cash"))
        if re.search(r"\b(?:what.*left|unallocated|cash.*left)\b", text):
            return result("tools", _one(tools, "planning_cash_status"))
        if re.search(
            r"\b(?:put|allocate|channel)\b.*\b(?:goal|holiday savings)\b", text
        ):
            return result(
                "tools",
                _pick(tools, "planning_cash_status", "planning_allocate_cash_to_goal")
                or _one(tools, "planning_allocate_cash_to_goal"),
            )
        if re.search(r"\b(?:create|make)\b.*\b(?:stash|cash pool)\b", text):
            return result("tools", _one(tools, "planning_create_cash_pool"))
        if re.search(
            r"\b(?:balance|how much)\b.*\b(?:stash|cash pool|holiday buffer)\b",
            text,
        ):
            return result("tools", _one(tools, "planning_cash_pool_balance"))
        if re.search(
            r"\b(?:put|allocate)\b.*\b(?:stash|holiday buffer|cash pool)\b",
            text,
        ):
            return result(
                "tools",
                _pick(tools, "planning_cash_status", "planning_allocate_cash_to_pool")
                or _one(tools, "planning_allocate_cash_to_pool"),
            )
        if re.search(r"\b(?:set aside|add).*\breserve\b", text):
            return result("tools", _one(tools, "planning_add_reserve"))
        if re.search(r"\b(?:change|disable).*\breserve\b", text):
            return result(
                "tools",
                _pick(tools, "planning_list_reserves", "planning_update_reserve")
                or _one(tools, "planning_update_reserve"),
            )
        if "safe monthly baseline" in text or "fixed income" in text:
            return result("tools", _one(tools, "planning_baseline"))
        if "income outlook" in text or "income am i expecting" in text:
            return result("tools", _one(tools, "planning_income_outlook"))
        if "cash flow" in text or "cashflow" in text:
            return result("tools", _one(tools, "planning_cashflow"))
        if "money plan" in text or "financial plan" in text:
            return result("tools", _one(tools, "planning_brief"))
        if re.search(r"\b(?:reserves?|allowances?)\b", text):
            return result("tools", _one(tools, "planning_list_reserves"))
        if re.search(r"\b(?:what goals|show my goals|saving towards)\b", text):
            return result("tools", _one(tools, "planning_list_goals"))
        return result(
            "tools", _one(tools, "planning_goal_progress", "planning_list_goals")
        )

    # Recurring bills.
    if (
        any(word in text for word in ("bill", "tnb", "electricity"))
        and any(
            name in tools for name in (
                "bills_list", "bills_defer", "bills_confirm_unpaid",
                "bills_match_payment", "bills_record_payment",
            )
        )
    ):
        if re.search(r"\b(?:defer|move .*due date)\b", text):
            return result(
                "tools",
                _pick(tools, "bills_list", "bills_defer")
                or _one(tools, "bills_defer"),
            )
        if re.search(r"\b(?:unpaid|did not pay)\b", text):
            return result(
                "tools",
                _pick(tools, "bills_list", "bills_confirm_unpaid")
                or _one(tools, "bills_confirm_unpaid"),
            )
        if re.search(r"\b(?:match|which bill|belong to)\b", text):
            return result("tools", _one(tools, "bills_match_payment"))
        if re.search(
            r"\b(?:record .*paid|paid .*record|record the payment)\b", text
        ):
            return result(
                "tools",
                _pick(tools, "bills_match_payment", "bills_record_payment")
                or _one(tools, "bills_record_payment"),
            )
        return result("tools", _one(tools, "bills_list"))

    # Work / leave / OT.
    if any(
        phrase in text
        for phrase in (
            "shift", "work schedule", "working morning", "working evening",
            "leave", "mc", "overtime", " ot ", "departure", "leave home",
        )
    ):
        if re.search(
            r"\b(?:record|mark|took mc|worked .*ot|shift was swapped)\b", text
        ):
            if (
                "annual leave" in text
                or "medical leave" in text
                or re.search(r"\bmc\b", text)
            ):
                return result("tools", _one(tools, "set_leave_record"))
            return result("tools", _one(tools, "work_record_event"))
        if "leave balance" in text or "annual leave do i have left" in text:
            return result("tools", _one(tools, "work_leave_balance"))
        if "leave" in text and re.search(
            r"\b(?:show|list|recorded|what)\b", text
        ):
            return result("tools", _one(tools, "list_leave_records"))
        if re.search(
            r"\b(?:eligible for ot|what ot|overtime status)\b", text
        ):
            return result("tools", _one(tools, "work_ot_status"))
        if (
            "leave home" in text
            or "departure" in text
            or re.search(r"\b(?:what time|when)\s+(?:should|do)\s+i\s+leave\b", text)
        ):
            if re.search(r"\b(?:shift|work)\b", text):
                return result(
                    "tools",
                    _pick(tools, "work_schedule", "work_departure_plan")
                    or _one(tools, "work_departure_plan"),
                )
            return result("tools", _one(tools, "work_departure_plan"))
        return result("tools", _one(tools, "work_schedule", "work_day"))

    # Assets / manuals / warranties.
    if any(
        word in text for word in ("asset", "appliance", "warranty", "manual", "water dispenser")
    ):
        if re.search(
            r"\b(?:save|add)\b.*\b(?:asset|appliance|water dispenser)\b", text
        ):
            return result("tools", _one(tools, "asset_create"))
        if re.search(
            r"\b(?:link|attach)\b.*\b(?:manual|warranty|document)\b", text
        ):
            return result("tools", _one(tools, "asset_link_document"))
        if "warrant" in text:
            return result("tools", _one(tools, "warranty_expiring", "asset_list"))
        return result("tools", _one(tools, "asset_list"))

    # Explicit monitoring delegation.
    if any(word in text for word in ("monitor", "track", "tracking")):
        if re.search(r"\b(?:stop|cancel)\b", text):
            return result("tools", _one(tools, "monitor_cancel"))
        if re.search(r"\b(?:what|show|list)\b", text):
            return result("tools", _one(tools, "monitor_list"))
        return result("tools", _one(tools, "monitor_delegate"))

    # HA automation drafting is never a live control request.
    if (
        "ha_draft_automation" in tools
        and re.search(r"\b(?:draft|prepare|create|make|write)\b.*\bautomation\b", text)
    ):
        return result("tools", ["ha_draft_automation"])

    # Explicit live control supports both "switch off the AC" and
    # "turn the hall AC off" word orderings.
    if (
        "ha_control" in tools
        and re.search(
            r"\b(?:turn|switch)\s+(?:on|off)\b"
            r"|\b(?:turn|switch)\b.*\b(?:on|off)\b",
            text,
        )
        and not re.search(
            r"\b(?:don't|do not|dont|without actually|not asking|what would|how would|hypothetical)\b",
            text,
        )
    ):
        return result(
            "tools",
            _pick(tools, "ha_find_entities", "ha_control")
            or ["ha_control"],
        )

    # Home Assistant.
    if any(
        phrase in text
        for phrase in (
            "hall ac", "air conditioner", "living room light", "home status",
            "at home", "home assistant automation", "hall light",
        )
    ):
        if re.search(r"\b(?:draft|prepare).*\bautomation\b", text):
            return result("tools", _one(tools, "ha_draft_automation"))
        if (
            re.search(r"\b(?:turn off|switch .* off)\b", text)
            and not re.search(
                r"\b(?:don't|do not|without actually|not asking|what would)\b",
                text,
            )
        ):
            return result(
                "tools",
                _pick(tools, "ha_find_entities", "ha_control")
                or _one(tools, "ha_control"),
            )
        if "picture" in text or "status card" in text:
            return result(
                "tools", _one(tools, "ha_home_report", "ha_home_summary")
            )
        if "home status" in text or "what's on at home" in text:
            return result(
                "tools", _one(tools, "ha_home_summary", "ha_home_report")
            )
        return result(
            "tools",
            _pick(tools, "ha_find_entities", "ha_get_state")
            or _one(tools, "ha_get_state"),
        )

    if (
        re.search(
            r"\b(?:finance|financial|expense|spending)\s+report\b"
            r"|\breport\b.*\b(?:finance|financial|expenses?|spending)\b",
            text,
        )
        and not re.search(r"\b(?:pdf|csv|export|send .*file|google sheets)\b", text)
        and "finance_report" in tools
    ):
        return result("tools", ["finance_report"])

    # Reports / exports.
    if any(
        phrase in text
        for phrase in (
            "report as pdf", "pdf of my monthly", "google sheets",
            "dashboard payload", "planning snapshot",
        )
    ):
        if "pdf" in text:
            return result("tools", _one(tools, "report_export"))
        if "google sheets" in text or "payload" in text:
            return result("tools", _one(tools, "report_payload"))
        return result("tools", _one(tools, "report_snapshot"))

    # Diagnostics.
    if any(
        phrase in text for phrase in ("alex fail", "alex errors", "alex healthy")
    ):
        if "fail" in text or "error" in text:
            return result("tools", _one(tools, "recent_failures"))
        return result("tools", _one(tools, "system_health"))

    # Exact arithmetic.
    if "minus" in text or re.search(r"what's\s+\d", text):
        return result("tools", _one(tools, "calculate"))

    # Negated / hypothetical HA action still permits a safe read.
    if any(
        phrase in text
        for phrase in (
            "don't turn off", "what would happen", "without actually",
            "not asking you to switch",
        )
    ):
        selected = _pick(tools, "ha_find_entities", "ha_get_state")
        return result("tools" if selected else "answer", selected)

    # Conservative fallback: choose a read-only data source if one is clearly
    # present. Never guess a mutator in the deterministic regression oracle.
    for name in (
        "get_agenda_range", "query_finances", "list_reminders",
        "list_shopping_items", "search_saved_items", "planning_list_goals",
        "work_schedule", "bills_list", "asset_list", "monitor_list",
        "system_health", "report_snapshot", "calculate",
    ):
        if name in tools:
            return result("tools", [name])

    return result("answer")


async def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Run the deterministic reasoning regression oracle"
    )
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    private_packets = await human_ai_lab.build_packets("all")
    public_packets = [
        human_ai_lab._public_packet(packet)
        for packet in private_packets
    ]
    decisions = [{
        "_meta": {
            "corpus_fingerprint": REVIEWED_CORPUS_FINGERPRINT,
            "reviewer": "deterministic regression oracle",
        }
    }]
    decisions.extend(_decision(packet) for packet in public_packets)
    report = human_ai_lab.score_packets(
        private_packets, decisions, "all"
    )
    report["review"] = {
        "independent_public_packet_only": False,
        "classification": "deterministic regression oracle; not an independent model test",
        "reviewer": "deterministic regression oracle",
        "decision_counts": {
            kind: sum(
                1 for row in decisions if row.get("decision") == kind
            )
            for kind in ("tools", "clarify", "refuse", "answer")
        },
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if report["status"] != "PASS":
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    asyncio.run(main())
