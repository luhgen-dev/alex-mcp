from __future__ import annotations

"""Stable provider-facing facade for Alex v0.5.

The conversational model sees a small, durable vocabulary.  Detailed MCP tools
remain the deterministic implementation surface and are still used for audits,
idempotency and rollback, but they are not advertised on ordinary turns.

The facade is deliberately thin: it never owns household truth.  It only
translates a stable request into one or more existing deterministic MCP calls.
"""

from collections.abc import Awaitable, Callable
from typing import Any


FACADE_NAMES = frozenset({
    "finance_query", "finance_log", "finance_correct",
    "library_find", "library_save",
    "reminders_view", "reminder_change",
    "shopping_view", "shopping_change",
    "home_state", "home_control",
    "agenda_view", "calculate", "load_pack",
})

PACKS = (
    "tasks", "plans", "diary", "planning", "work", "bills",
    "assets", "monitoring", "reports", "diagnostics",
)

# Detailed tools are retained for deterministic execution and targeted packs.
PACK_TOOLS: dict[str, frozenset[str]] = {
    "tasks": frozenset({
        "create_task", "list_tasks", "update_task",
        "complete_task", "reopen_task", "cancel_task",
    }),
    "plans": frozenset({
        "create_plan", "list_plans", "update_plan", "confirm_plan", "share_plan",
        "check_my_availability", "check_spouse_availability",
    }),
    "diary": frozenset({
        "add_diary_event", "update_diary_event",
        "resolve_diary_conflict", "resolve_latest_diary_conflict",
        "get_agenda", "get_agenda_range",
        "check_my_availability", "check_spouse_availability",
    }),
    "planning": frozenset({
        "planning_create_goal", "planning_lock_goal", "planning_reopen_goal",
        "planning_set_period_target", "planning_change_goal_baseline",
        "planning_record_goal_contribution", "planning_goal_progress",
        "planning_goal_deviation", "planning_goal_projection",
        "planning_record_cash", "planning_compare_salary",
        "planning_match_goal_alias", "planning_cash_status",
        "planning_allocate_cash_to_goal", "planning_create_cash_pool",
        "planning_cash_pool_balance", "planning_allocate_cash_to_pool",
        "planning_add_reserve", "planning_update_reserve",
        "planning_list_reserves", "planning_baseline",
        "planning_income_outlook", "planning_cashflow",
        "planning_brief", "planning_list_goals", "calculate",
    }),
    "work": frozenset({
        "work_schedule", "work_day", "work_record_event", "work_ot_status",
        "work_leave_balance", "work_departure_plan", "list_work_roster",
        "set_leave_record", "list_leave_records",
    }),
    "bills": frozenset({
        "bills_list", "bills_match_payment", "bills_record_payment",
        "bills_defer", "bills_confirm_unpaid", "query_finances", "find_receipts",
    }),
    "assets": frozenset({
        "asset_create", "asset_link_document", "asset_list", "warranty_expiring",
        "search_saved_items", "get_saved_item",
    }),
    "monitoring": frozenset({"monitor_delegate", "monitor_list", "monitor_cancel"}),
    "reports": frozenset({"report_snapshot", "report_export", "report_payload"}),
    "diagnostics": frozenset({"system_health", "recent_failures"}),
}

# Map the detailed capability surface into the ordinary facade.  Specialized
# writes intentionally route through load_pack.
UNDERLYING_TO_FACADE: dict[str, str] = {
    "query_finances": "finance_query",
    "find_receipts": "finance_query",
    "list_pending_expenses": "finance_query",
    "bills_list": "finance_query",
    "log_expense": "finance_log",
    "correct_expense": "finance_correct",
    "confirm_expense": "finance_correct",
    "search_saved_items": "library_find",
    "get_saved_item": "library_find",
    "resolve_numbered_choice": "library_find",
    "get_receipt": "library_find",
    "remove_saved_item": "library_save",
    "asset_list": "library_find",
    "warranty_expiring": "library_find",
    "save_item": "library_save",
    "list_reminders": "reminders_view",
    "reminder_history": "reminders_view",
    "create_reminder": "reminder_change",
    "update_reminder": "reminder_change",
    "list_shopping_items": "shopping_view",
    "add_shopping_item": "shopping_change",
    "update_shopping_item": "shopping_change",
    "ha_find_entities": "home_state",
    "ha_get_state": "home_state",
    "ha_home_summary": "home_state",
    "ha_home_report": "home_state",
    "ha_draft_automation": "home_state",
    "ha_control": "home_control",
    "get_agenda": "agenda_view",
    "get_agenda_range": "agenda_view",
    "list_plans": "agenda_view",
    "list_tasks": "agenda_view",
    "check_my_availability": "agenda_view",
    "check_spouse_availability": "agenda_view",
    "work_schedule": "agenda_view",
    "work_day": "agenda_view",
    "list_leave_records": "agenda_view",
    "calculate": "calculate",
}


def _fn(name: str, description: str, properties: dict[str, Any],
        required: tuple[str, ...] = ()) -> dict:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _enum(values: tuple[str, ...] | list[str]) -> dict:
    return {"type": "string", "enum": list(values)}


def all_specs() -> dict[str, dict]:
    """Return the 14 stable provider-facing tool schemas."""
    return {
        "finance_query": _fn(
            "finance_query",
            "Read authorized finances, receipts, pending expense clarifications or recurring bills. Never writes.",
            {
                "operation": _enum(("transactions", "receipts", "pending", "bills")),
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
                "search": {"type": ["string", "null"]},
                "currency": {"type": ["string", "null"], "enum": ["MYR", "SGD", None]},
                "scope": {"type": ["string", "null"], "enum": ["all", "family", "private", None]},
                "source": {"type": ["string", "null"], "enum": ["all", "voice", "receipt", "text", None]},
                "amount": {"type": ["number", "null"]},
                "period": {"type": ["string", "null"]},
                "as_of_date": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ("operation",),
        ),
        "finance_log": _fn(
            "finance_log",
            "Record one verified expense or income. Currency must be explicitly supported by user/receipt evidence.",
            {
                "description": {"type": "string"},
                "amount": {"type": ["number", "null"]},
                "category": {"type": ["string", "null"]},
                "currency": {"type": ["string", "null"], "enum": ["MYR", "SGD", None]},
                "event_date_local": {"type": ["string", "null"]},
                "reference": {"type": ["string", "null"]},
                "event_type": {"type": "string", "enum": ["Expense", "Income"]},
            },
            ("description",),
        ),
        "finance_correct": _fn(
            "finance_correct",
            "Correct an exact finance record or approve/reject an exact pending record. Never guess the target record.",
            {
                "operation": _enum(("correct", "confirm", "reject")),
                "event_id": {"type": "string"},
                "amount": {"type": ["number", "null"]},
                "description": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
                "event_date_local": {"type": ["string", "null"]},
                "reason": {"type": ["string", "null"]},
            },
            ("operation", "event_id"),
        ),
        "library_find": _fn(
            "library_find",
            "Find/browse authorized saved memories, original receipts, assets or warranties; can retrieve an exact original.",
            {
                "operation": _enum(("saved", "saved_item", "choice", "receipts", "receipt", "assets", "warranties")),
                "query": {"type": ["string", "null"]},
                "kind": {"type": ["string", "null"], "enum": ["picture", "document", "note", None]},
                "item_id": {"type": ["string", "null"]},
                "choice": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                "media_id": {"type": ["string", "null"]},
                "amount": {"type": ["number", "null"]},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "include_documents": {"type": "boolean"},
                "within_days": {"type": ["integer", "null"], "minimum": 0, "maximum": 3650},
                "as_of_date": {"type": ["string", "null"]},
            },
            ("operation",),
        ),
        "library_save": _fn(
            "library_save",
            "Explicitly save/remember an item or remove an exact saved-memory index. Automatic receipt retention is separate.",
            {
                "operation": _enum(("save", "remove")),
                "title": {"type": ["string", "null"]},
                "content": {"type": ["string", "null"]},
                "tags": {"type": ["string", "null"]},
                "shared": {"type": "boolean"},
                "item_id": {"type": ["string", "null"]},
            },
            ("operation",),
        ),
        "reminders_view": _fn(
            "reminders_view",
            "Read authorized reminders or one reminder's durable history.",
            {
                "operation": _enum(("list", "history")),
                "reminder_id": {"type": ["string", "null"]},
                "include_completed": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ("operation",),
        ),
        "reminder_change": _fn(
            "reminder_change",
            "Create or update an explicit reminder. A reminder is distinct from a task, diary event and plan.",
            {
                "operation": _enum(("create", "update")),
                "task": {"type": ["string", "null"]},
                "due_local": {"type": ["string", "null"]},
                "recurrence_rule": {"type": ["string", "null"]},
                "shared": {"type": "boolean"},
                "recipient": {"type": "string", "enum": ["me", "spouse", "husband", "wife", "both"]},
                "presence_aware": {"type": "boolean"},
                "delivery_class": {"type": "string", "enum": ["routine", "time_critical"]},
                "follow_up_after_hours": {"type": "integer", "minimum": 0, "maximum": 720},
                "reminder_id": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"]},
                "new_due_local": {"type": ["string", "null"]},
            },
            ("operation",),
        ),
        "shopping_view": _fn(
            "shopping_view",
            "Read the authorized shopping list.",
            {
                "include_purchased": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "scope": {"type": ["string", "null"], "enum": ["all", "family", "private", None]},
            },
        ),
        "shopping_change": _fn(
            "shopping_change",
            "Add, mark bought, reopen or remove a shopping item. For updates you may supply exact item_id or its visible item name; Alex resolves ambiguity safely.",
            {
                "operation": _enum(("add", "update")),
                "item": {"type": ["string", "null"]},
                "item_id": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"], "enum": ["purchased", "open", "removed", None]},
                "quantity": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
                "shared": {"type": "boolean"},
                "scope": {"type": ["string", "null"], "enum": ["all", "family", "private", None]},
            },
            ("operation",),
        ),
        "home_state": _fn(
            "home_state",
            "Read real Home Assistant entities/states or a privacy-safe whole-home summary/report. Never controls devices.",
            {
                "operation": _enum(("find", "get", "summary", "report", "draft_automation")),
                "query": {"type": ["string", "null"]},
                "domain": {"type": ["string", "null"]},
                "entity_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "name": {"type": ["string", "null"]},
                "trigger_yaml": {"type": ["string", "null"]},
                "action_yaml": {"type": ["string", "null"]},
                "condition_yaml": {"type": ["string", "null"]},
            },
            ("operation",),
        ),
        "home_control": _fn(
            "home_control",
            "Perform only the user's explicit low-risk HA action on an exact entity. Sensitive domains are rejected by the backend.",
            {
                "entity_id": {"type": "string"},
                "action": {"type": "string"},
                "value": {"type": ["number", "null"]},
            },
            ("entity_id", "action"),
        ),
        "agenda_view": _fn(
            "agenda_view",
            "Read agenda, plans, tasks, availability, work schedule/day or leave records. Never mutates them.",
            {
                "operation": _enum((
                    "range", "agenda", "plans", "tasks",
                    "availability_self", "availability_spouse",
                    "work_schedule", "work_day", "leave_records",
                )),
                "phrase": {"type": ["string", "null"]},
                "reference_date": {"type": ["string", "null"]},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "include_plans": {"type": "boolean"},
                "include_cancelled": {"type": "boolean"},
                "status": {"type": ["string", "null"]},
                "plan_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "start_local": {"type": ["string", "null"]},
                "end_local": {"type": ["string", "null"]},
                "on_date": {"type": ["string", "null"]},
            },
            ("operation",),
        ),
        "calculate": _fn(
            "calculate",
            "Perform exact local arithmetic.",
            {"expression": {"type": "string"}},
            ("expression",),
        ),
        "load_pack": _fn(
            "load_pack",
            "Load one narrow specialist capability pack only when the ordinary facade cannot complete the user's request.",
            {"pack": _enum(PACKS)},
            ("pack",),
        ),
    }


def facade_names_for_underlying(underlying: set[str]) -> set[str]:
    names = {
        UNDERLYING_TO_FACADE[name]
        for name in underlying
        if name in UNDERLYING_TO_FACADE
    }
    specialized = set(underlying) - set(UNDERLYING_TO_FACADE)
    # Discovery is superseded by the bounded enum-only pack loader.
    specialized.discard("discover_alex_tools")
    if specialized:
        names.add("load_pack")
    return names


def specs_for_underlying(underlying: set[str], max_tools: int = 6) -> list[dict]:
    catalog = all_specs()
    names = facade_names_for_underlying(underlying)
    if not names and underlying:
        names.add("load_pack")
    # Keep load_pack last; ordinary direct facades should be easiest to choose.
    order = (
        "finance_query", "finance_log", "finance_correct",
        "library_find", "library_save",
        "reminders_view", "reminder_change",
        "shopping_view", "shopping_change",
        "home_state", "home_control", "agenda_view", "calculate", "load_pack",
    )
    return [catalog[name] for name in order if name in names][:max_tools]


def pack_tools(pack: str) -> set[str]:
    key = str(pack or "").strip().casefold()
    if key not in PACK_TOOLS:
        raise ValueError("Unknown Alex capability pack")
    return set(PACK_TOOLS[key])


def _compact(args: dict, allowed: tuple[str, ...], defaults: dict[str, Any] | None = None) -> dict:
    out = dict(defaults or {})
    for key in allowed:
        if key in args and args[key] is not None:
            out[key] = args[key]
    return out


def simple_translation(name: str, args: dict) -> tuple[str, dict] | None:
    """Translate a one-step facade call to the existing deterministic MCP surface.

    Multi-step/name-resolution calls are handled by execute().
    """
    name = str(name)
    args = dict(args or {})
    op = str(args.get("operation") or "").casefold()

    if name == "finance_log":
        return "log_expense", _compact(args, (
            "description", "amount", "category", "currency",
            "event_date_local", "reference", "event_type",
        ))
    if name == "finance_correct":
        if op in {"confirm", "reject"}:
            payload = _compact(args, ("event_id", "amount", "category"))
            payload["approve"] = op == "confirm"
            return "confirm_expense", payload
        if op == "correct":
            return "correct_expense", _compact(args, (
                "event_id", "amount", "description", "category",
                "event_date_local", "reason",
            ))
    if name == "finance_query":
        if op == "transactions":
            return "query_finances", _compact(args, (
                "start_date", "end_date", "category", "search",
                "currency", "limit", "scope", "source",
            ))
        if op == "receipts":
            return "find_receipts", _compact(args, (
                "query", "amount", "start_date", "end_date", "limit",
            ))
        if op == "pending":
            return "list_pending_expenses", _compact(args, ("limit",))
        if op == "bills":
            return "bills_list", _compact(args, ("period", "as_of_date"))
    if name == "library_save":
        if op == "remove":
            return "remove_saved_item", _compact(args, ("item_id",))
        if op in {"", "save"}:
            return "save_item", _compact(args, ("title", "content", "tags", "shared"))
    if name == "library_find":
        if op == "saved":
            return "search_saved_items", _compact(args, ("query", "limit", "kind"))
        if op == "saved_item":
            return "get_saved_item", _compact(args, ("item_id",))
        if op == "choice":
            return "resolve_numbered_choice", _compact(args, ("choice",))
        if op == "receipts":
            return "find_receipts", _compact(args, (
                "query", "amount", "start_date", "end_date", "limit",
            ))
        if op == "receipt":
            return "get_receipt", _compact(args, ("media_id",))
        if op == "assets":
            return "asset_list", _compact(args, ("include_documents",))
        if op == "warranties":
            return "warranty_expiring", {
                "within_days": int(args.get("within_days") or 30),
                "as_of_date": str(args.get("as_of_date") or ""),
            }
    if name == "reminders_view":
        if op == "history":
            return "reminder_history", _compact(args, ("reminder_id",))
        if op == "list":
            return "list_reminders", _compact(args, ("include_completed", "limit"))
    if name == "reminder_change":
        if op == "create":
            return "create_reminder", _compact(args, (
                "task", "due_local", "recurrence_rule", "shared", "recipient",
                "presence_aware", "delivery_class", "follow_up_after_hours",
            ), defaults={"recipient": "me"})
        if op == "update":
            return "update_reminder", _compact(args, (
                "reminder_id", "status", "new_due_local",
            ))
    if name == "shopping_view":
        return "list_shopping_items", _compact(args, (
            "include_purchased", "limit", "scope",
        ))
    if name == "shopping_change" and op == "add":
        return "add_shopping_item", _compact(args, (
            "item", "quantity", "notes", "shared",
        ), defaults={"shared": True})
    if name == "shopping_change" and op == "update" and args.get("item_id"):
        return "update_shopping_item", _compact(args, (
            "item_id", "status", "quantity", "notes",
        ), defaults={"status": "purchased"})
    if name == "home_state":
        if op == "find":
            return "ha_find_entities", _compact(args, ("query", "domain", "limit"))
        if op == "get":
            return "ha_get_state", _compact(args, ("entity_id",))
        if op == "summary":
            return "ha_home_summary", {}
        if op == "report":
            return "ha_home_report", {}
        if op == "draft_automation":
            return "ha_draft_automation", _compact(
                args, ("name", "trigger_yaml", "action_yaml", "condition_yaml")
            )
    if name == "home_control":
        return "ha_control", _compact(args, ("entity_id", "action", "value"))
    if name == "agenda_view":
        mapping = {
            "range": ("get_agenda_range", ("phrase", "reference_date", "include_plans")),
            "agenda": ("get_agenda", ("start_date", "end_date", "include_plans")),
            "plans": ("list_plans", ("include_cancelled", "limit")),
            "tasks": ("list_tasks", ("status", "plan_id", "limit")),
            "availability_self": ("check_my_availability", ("start_local", "end_local")),
            "availability_spouse": ("check_spouse_availability", ("start_local", "end_local")),
            "work_schedule": ("work_schedule", ("start_date", "end_date")),
            "work_day": ("work_day", ("on_date",)),
            "leave_records": ("list_leave_records", ("start_date", "end_date", "include_cancelled")),
        }
        if op in mapping:
            target, allowed = mapping[op]
            return target, _compact(args, allowed)
    if name == "calculate":
        return "calculate", _compact(args, ("expression",))
    return None


async def execute(
    name: str,
    args: dict,
    call_mcp: Callable[[str, dict, str], Awaitable[tuple[dict, list[dict]]]],
    action_key: str,
) -> tuple[dict, list[dict]]:
    """Execute a facade request through real deterministic MCP calls.

    The callback is Alex's audited/idempotent MCP execution path.
    """
    translated = simple_translation(name, args)
    if translated is not None:
        tool, payload = translated
        return await call_mcp(tool, payload, action_key + ":facade")

    if name == "shopping_change" and str(args.get("operation") or "").casefold() == "update":
        item_name = str(args.get("item") or "").strip()
        if not item_name:
            return {
                "status": "clarification_required",
                "message": "Which shopping item should I update?",
            }, []
        scope = args.get("scope")
        listed, files = await call_mcp(
            "list_shopping_items",
            _compact({"scope": scope, "include_purchased": False, "limit": 100},
                     ("scope", "include_purchased", "limit")),
            action_key + ":resolve",
        )
        rows = list(listed.get("items") or [])
        exact = [
            row for row in rows
            if str(row.get("item") or row.get("item_name") or "").strip().casefold()
            == item_name.casefold()
        ]
        if len(exact) != 1:
            return {
                "status": "clarification_required",
                "message": (
                    "I found more than one matching shopping item; which list/item do you mean?"
                    if len(exact) > 1
                    else f"I couldn't find an open shopping item named {item_name!r}."
                ),
                "matches": exact[:10],
            }, files
        target_id = exact[0].get("item_id")
        if not target_id:
            return {"status": "error", "message": "Shopping item has no stable id."}, files
        payload = _compact(args, ("status", "quantity", "notes"),
                           defaults={"status": "purchased"})
        payload["item_id"] = target_id
        changed, more = await call_mcp(
            "update_shopping_item", payload, action_key + ":update"
        )
        return changed, files + more

    return {
        "status": "clarification_required",
        "message": "That facade request is incomplete or unsupported; ask one focused question.",
    }, []
