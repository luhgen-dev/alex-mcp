from __future__ import annotations

"""Stable behaviour capabilities for Alex certification.

Contracts describe *what the owner expects*, not the name of today's MCP tool.
Only this adapter knows how a stable capability maps to the current tool surface.
A future facade migration should mostly change this file, not the contract catalog.
"""

from collections.abc import Iterable


CAPABILITY_TO_TOOLS: dict[str, frozenset[str]] = {
    # Finance / receipts
    "finance.write": frozenset({"log_expense"}),
    "finance.read": frozenset({"query_finances"}),
    "finance.pending.read": frozenset({"list_pending_expenses"}),
    "finance.pending.confirm": frozenset({"confirm_expense"}),
    "finance.correct": frozenset({"correct_expense"}),
    "receipt.find": frozenset({"find_receipts"}),
    "receipt.get": frozenset({"get_receipt"}),

    # Saved memory / selection
    "memory.save": frozenset({"save_item"}),
    "memory.search": frozenset({"search_saved_items"}),
    "memory.get": frozenset({"get_saved_item"}),
    "memory.remove": frozenset({"remove_saved_item"}),
    "selection.resolve": frozenset({"resolve_numbered_choice"}),

    # Reminders / shopping
    "reminder.create": frozenset({"create_reminder"}),
    "reminder.read": frozenset({"list_reminders"}),
    "reminder.update": frozenset({"update_reminder"}),
    "reminder.history": frozenset({"reminder_history"}),
    "shopping.add": frozenset({"add_shopping_item"}),
    "shopping.read": frozenset({"list_shopping_items"}),
    "shopping.update": frozenset({"update_shopping_item"}),

    # Diary / plans / availability
    "diary.create": frozenset({"add_diary_event"}),
    "diary.update": frozenset({"update_diary_event"}),
    "diary.conflict.resolve": frozenset({"resolve_diary_conflict", "resolve_latest_diary_conflict"}),
    "agenda.read": frozenset({"get_agenda", "get_agenda_range"}),
    "plan.create": frozenset({"create_plan"}),
    "plan.read": frozenset({"list_plans"}),
    "plan.update": frozenset({"update_plan"}),
    "plan.confirm": frozenset({"confirm_plan"}),
    "plan.share": frozenset({"share_plan"}),
    "availability.self": frozenset({"check_my_availability"}),
    "availability.spouse": frozenset({"check_spouse_availability"}),

    # Tasks are first-class and independently lifecycle-certified.
    "task.create": frozenset({"create_task", "add_task", "task_create"}),
    "task.read": frozenset({"list_tasks", "task_list"}),
    "task.update": frozenset({"update_task", "task_update", "task_change"}),
    "task.complete": frozenset({"complete_task", "task_complete", "task_change"}),
    "task.reopen": frozenset({"reopen_task", "task_reopen", "task_change"}),
    "task.cancel": frozenset({"cancel_task", "task_cancel", "task_change"}),

    # Goals / planning / cash
    "goal.create": frozenset({"planning_create_goal"}),
    "goal.list": frozenset({"planning_list_goals"}),
    "goal.progress": frozenset({"planning_goal_progress"}),
    "goal.lock": frozenset({"planning_lock_goal"}),
    "goal.reopen": frozenset({"planning_reopen_goal"}),
    "goal.period_target": frozenset({"planning_set_period_target"}),
    "goal.baseline.change": frozenset({"planning_change_goal_baseline"}),
    "goal.contribution": frozenset({"planning_record_goal_contribution"}),
    "goal.deviation": frozenset({"planning_goal_deviation"}),
    "goal.projection": frozenset({"planning_goal_projection"}),
    "goal.alias.match": frozenset({"planning_match_goal_alias"}),
    "cash.record": frozenset({"planning_record_cash"}),
    "cash.salary.compare": frozenset({"planning_compare_salary"}),
    "cash.status": frozenset({"planning_cash_status"}),
    "cash.allocate.goal": frozenset({"planning_allocate_cash_to_goal"}),
    "cash.pool.create": frozenset({"planning_create_cash_pool"}),
    "cash.pool.balance": frozenset({"planning_cash_pool_balance"}),
    "cash.pool.allocate": frozenset({"planning_allocate_cash_to_pool"}),
    "reserve.add": frozenset({"planning_add_reserve"}),
    "reserve.update": frozenset({"planning_update_reserve"}),
    "reserve.list": frozenset({"planning_list_reserves"}),
    "planning.baseline": frozenset({"planning_baseline"}),
    "planning.income": frozenset({"planning_income_outlook"}),
    "planning.cashflow": frozenset({"planning_cashflow"}),
    "planning.brief": frozenset({"planning_brief"}),

    # Bills
    "bills.list": frozenset({"bills_list"}),
    "bills.match": frozenset({"bills_match_payment"}),
    "bills.record": frozenset({"bills_record_payment"}),
    "bills.defer": frozenset({"bills_defer"}),
    "bills.confirm_unpaid": frozenset({"bills_confirm_unpaid"}),

    # Work / leave
    "work.schedule": frozenset({"work_schedule"}),
    "work.day": frozenset({"work_day"}),
    "work.roster.list": frozenset({"list_work_roster"}),
    "work.leave.record": frozenset({"set_leave_record"}),
    "work.leave.list": frozenset({"list_leave_records"}),
    "work.record": frozenset({"work_record_event"}),
    "work.ot": frozenset({"work_ot_status"}),
    "work.leave.balance": frozenset({"work_leave_balance"}),
    "work.departure": frozenset({"work_departure_plan"}),

    # Assets / monitoring
    "asset.create": frozenset({"asset_create"}),
    "asset.document.link": frozenset({"asset_link_document"}),
    "asset.list": frozenset({"asset_list"}),
    "asset.warranty": frozenset({"warranty_expiring"}),
    "monitor.delegate": frozenset({"monitor_delegate"}),
    "monitor.list": frozenset({"monitor_list"}),
    "monitor.cancel": frozenset({"monitor_cancel"}),

    # Home Assistant
    "home.find": frozenset({"ha_find_entities"}),
    "home.state": frozenset({"ha_get_state"}),
    "home.summary": frozenset({"ha_home_summary"}),
    "home.report": frozenset({"ha_home_report"}),
    "home.automation.draft": frozenset({"ha_draft_automation"}),
    "home.control": frozenset({"ha_control"}),

    # Reports / diagnostics / utility
    "report.snapshot": frozenset({"report_snapshot"}),
    "report.export": frozenset({"report_export"}),
    "report.payload": frozenset({"report_payload"}),
    "diagnostics.health": frozenset({"system_health"}),
    "diagnostics.failures": frozenset({"recent_failures"}),
    "calculate": frozenset({"calculate"}),

    # Discovery is a routing mechanism, never a successful owner capability.
    "routing.discovery": frozenset({"discover_alex_tools"}),
}


# Compatibility-only MCP tools intentionally superseded by the advanced surface.
# They remain classified so catalog drift is explicit, but behavioural contracts
# do not need to route users through them.
TASK_LIFECYCLE_SPEC = {
    "states": frozenset({"OPEN", "DONE", "CANCELLED"}),
    "required_actions": frozenset({
        "task.create", "task.read", "task.update",
        "task.complete", "task.reopen", "task.cancel",
    }),
    # These are behavioural fields, not necessarily today's MCP argument names.
    # due_at, assignee and plan_id are optional at creation, but the lifecycle
    # must preserve them when present. A reminder is a separate object/action.
    "fields": {
        "title": "required",
        "status": "required",
        "assignee": "optional:user|spouse|both|unassigned",
        "visibility": "required:private|family",
        "due_at": "optional",
        "plan_id": "optional",
        "notes": "optional",
        "reminder_id": "optional-separate-link",
    },
    "rules": (
        "task stays a task; never degrade to plan note",
        "due date is optional",
        "reminder is separate and only created when explicitly requested",
        "complete preserves history",
        "reopen is explicit",
        "cancel does not delete unrelated plan/reminder state",
    ),
}


TOOL_COVERAGE_EXEMPTIONS = frozenset({
    "set_goal", "list_goals",
    "get_leave_balance", "set_leave_balance", "set_work_roster",
    "set_cashflow_baseline", "get_cashflow_baseline",
    "set_money_bucket", "list_money_buckets",
})


TOOL_TO_CAPABILITIES: dict[str, frozenset[str]] = {}
_tool_caps: dict[str, set[str]] = {}
for _capability, _tools in CAPABILITY_TO_TOOLS.items():
    for _tool in _tools:
        _tool_caps.setdefault(_tool, set()).add(_capability)
TOOL_TO_CAPABILITIES = {
    tool: frozenset(capabilities)
    for tool, capabilities in _tool_caps.items()
}
# Backward-compatible primary classification for simple callers. Behaviour
# evaluation uses TOOL_TO_CAPABILITIES so one facade tool may implement several
# stable owner capabilities.
TOOL_TO_CAPABILITY: dict[str, str] = {
    tool: sorted(capabilities)[0]
    for tool, capabilities in TOOL_TO_CAPABILITIES.items()
}


def normalize_capability(token: str) -> str:
    """Accept a stable capability or a legacy/current tool alias.

    This lets the existing catalog migrate incrementally while execution is
    decoupled from tool names today. New contracts should use capability names.
    """
    value = str(token or "").strip()
    if value in CAPABILITY_TO_TOOLS:
        return value
    if value in TOOL_TO_CAPABILITIES:
        capabilities = TOOL_TO_CAPABILITIES[value]
        if len(capabilities) == 1:
            return next(iter(capabilities))
        raise ValueError(
            f"Tool alias {value!r} maps to multiple capabilities; "
            "new contracts must use a semantic capability name"
        )
    # Unknown task aliases are intentionally mapped to the owner-required
    # lifecycle instead of being treated as a mysterious tool name.
    if value in {"create_task", "add_task", "task_create"}:
        return "task.create"
    if value in {"list_tasks", "task_list"}:
        return "task.read"
    if value in {"update_task", "task_update"}:
        return "task.update"
    if value in {"complete_task", "task_complete"}:
        return "task.complete"
    if value in {"reopen_task", "task_reopen"}:
        return "task.reopen"
    if value in {"cancel_task", "task_cancel"}:
        return "task.cancel"
    raise ValueError(f"Unclassified behaviour capability/tool token: {value}")


def normalize_capabilities(tokens: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_capability(token) for token in tokens)


def tools_for_capabilities(capabilities: Iterable[str]) -> frozenset[str]:
    tools: set[str] = set()
    for capability in capabilities:
        tools.update(CAPABILITY_TO_TOOLS.get(normalize_capability(capability), ()))
    return frozenset(tools)


def capabilities_for_tools(tools: Iterable[str]) -> frozenset[str]:
    capabilities: set[str] = set()
    for tool in tools:
        capabilities.update(TOOL_TO_CAPABILITIES.get(tool, ()))
    return frozenset(capabilities)


def implementation_exists(capability: str, available_tools: set[str]) -> bool:
    return bool(CAPABILITY_TO_TOOLS.get(normalize_capability(capability), frozenset()) & available_tools)
