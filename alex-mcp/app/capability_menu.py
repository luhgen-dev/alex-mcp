"""Stable menu numbers backed by the actual registered MCP capability surface."""
from __future__ import annotations

import asyncio

# Keep navigation IDs stable even when a deployment omits a domain.
SECTIONS = {
    1: ("Today & Work", (("Daily Brief", "get_daily_brief"), ("agenda", "get_agenda"), ("private roster", "work_schedule"), ("leave balances", "work_leave_balance"))),
    2: ("Money", (("expenses", "query_finances"), ("receipts", "find_receipts"), ("bills", "bills_list"), ("reports", "report_export"))),
    3: ("Reminders & Tasks", (("reminders", "create_reminder"), ("tasks", "list_tasks"), ("task history", "get_task_details"))),
    4: ("Diary & Plans", (("appointments", "add_diary_event"), ("availability", "check_my_availability"), ("plans", "list_plans"))),
    5: ("Shopping", (("shopping lists", "list_shopping_items"), ("add items", "add_shopping_item"), ("update items", "update_shopping_item"))),
    6: ("Saved Things", (("save notes and original media", "save_item"), ("search saved items", "search_saved_items"), ("retrieve originals", "get_saved_item"))),
    7: ("Home", (("device states", "ha_get_state"), ("permitted device controls", "ha_control"))),
    8: ("Goals & Savings", (("goals", "planning_list_goals"), ("contributions", "planning_record_goal_contribution"), ("cash pools", "planning_list_cash_pools"))),
    9: ("Ear Discussion Journal", (("discussion history", "list_discussions"), ("discussion details", "get_discussion"), ("decisions", "update_discussion"), ("actions and progress", "update_discussion_action"))),
    10: ("Ledgers & Trackers", (("custom trackers", "list_custom_ledgers"), ("tracker entries", "add_custom_ledger_entry"))),
}


async def _registered_names():
    # Lazy imports prevent a circular import at ingress startup.
    from mcp import Client
    from mcp_server import mcp
    async with Client(mcp) as client:
        result = await client.list_tools()
        return {tool.name for tool in result.tools}


def enabled_sections(registered_tools=None):
    """Return only capabilities actually exposed; never infer from old menu text.

    Call synchronously from ingress. Tests/deployment adapters may supply the
    exact registered name set rather than opening the local MCP client.
    """
    names = set(registered_tools) if registered_tools is not None else asyncio.run(_registered_names())
    result = {}
    for number, (title, capabilities) in SECTIONS.items():
        labels = [label for label, tool in capabilities if tool in names]
        if labels:
            result[number] = (title, ", ".join(labels) + ".")
    return result
