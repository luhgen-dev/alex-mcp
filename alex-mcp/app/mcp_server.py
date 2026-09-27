from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Resolve

from context import ActorContext, current_actor
import services

mcp = MCPServer(
    "Alex Household Tools",
    version="0.2.0",
    instructions="Deterministic household tools. Identity and permissions are injected by Alex and are never model-controlled.",
)


async def authenticated_actor() -> ActorContext:
    return current_actor()


Actor = Annotated[ActorContext, Resolve(authenticated_actor)]


@mcp.tool()
def log_expense(description: str, actor: Actor, amount: float | None = None,
                category: str | None = None, currency: str = "MYR",
                event_date_local: str | None = None, reference: str | None = None,
                event_type: str = "Expense") -> dict:
    """Record an expense or income. Use null category/amount when genuinely unclear; Alex will request confirmation."""
    return services.log_expense(actor, description, amount, category, currency, event_date_local, reference, event_type)


@mcp.tool()
def confirm_expense(event_id: str, actor: Actor, approve: bool = True,
                    category: str | None = None, amount: float | None = None) -> dict:
    """Confirm or reject a pending money record after the user supplies missing information or approval."""
    return services.confirm_expense(actor, event_id, approve, category, amount)


@mcp.tool()
def query_finances(actor: Actor, start_date: str | None = None, end_date: str | None = None,
                   category: str | None = None, search: str | None = None,
                   currency: str | None = None, limit: int = 20) -> dict:
    """Read true ledger totals and matching transactions. Use ISO dates YYYY-MM-DD for date filters."""
    return services.query_finances(actor, start_date, end_date, category, search, currency, limit)


@mcp.tool()
def list_pending_expenses(actor: Actor, limit: int = 10) -> dict:
    """List unresolved money records when the user is answering a previous clarification."""
    return services.list_pending_expenses(actor, limit)


@mcp.tool()
def correct_expense(event_id: str, actor: Actor, amount: float | None = None,
                    description: str | None = None, category: str | None = None,
                    event_date_local: str | None = None, reason: str | None = None) -> dict:
    """Correct an existing transaction append-only; the original remains auditable and is superseded."""
    return services.correct_expense(actor, event_id, amount, description, category, event_date_local, reason)


@mcp.tool()
def find_receipts(actor: Actor, query: str | None = None, amount: float | None = None,
                  start_date: str | None = None, end_date: str | None = None, limit: int = 10) -> dict:
    """Find saved original receipts by merchant/bank/reference text, amount or date. Similar recurring receipts remain distinct."""
    return services.find_receipts(actor, query, amount, start_date, end_date, limit)


@mcp.tool()
def get_receipt(media_id: str, actor: Actor) -> dict:
    """Retrieve one original receipt previously found by find_receipts so Alex can send the actual image/document back."""
    return services.get_receipt(actor, media_id)


@mcp.tool()
def save_item(title: str, content: str, actor: Actor, tags: str | None = None,
              shared: bool = False) -> dict:
    """Explicitly remember something the user asked Alex to save. This is separate from automatic receipt retention."""
    return services.save_item(actor, title, content, tags, shared)


@mcp.tool()
def search_saved_items(query: str, actor: Actor, limit: int = 10) -> dict:
    """Search things the user explicitly asked Alex to remember."""
    return services.search_saved_items(actor, query, limit)


@mcp.tool()
def get_saved_item(item_id: str, actor: Actor) -> dict:
    """Retrieve one explicitly saved item, including its original attachment when it had one."""
    return services.get_saved_item(actor, item_id)


@mcp.tool()
def create_reminder(task: str, due_local: str, actor: Actor,
                    recurrence_rule: str | None = None, shared: bool = False,
                    recipient: str = "me") -> dict:
    """Create a durable reminder. recipient is me/spouse/husband/wife/both. due_local is ISO local datetime; recurrence_rule is an RFC 5545 RRULE such as FREQ=WEEKLY."""
    return services.create_reminder(actor, task, due_local, recurrence_rule, shared, recipient)


@mcp.tool()
def list_reminders(actor: Actor, include_completed: bool = False, limit: int = 20) -> dict:
    """List upcoming/open reminders from spaces this user is allowed to see."""
    return services.list_reminders(actor, include_completed, limit)


@mcp.tool()
def update_reminder(reminder_id: str, status: str, actor: Actor,
                    new_due_local: str | None = None) -> dict:
    """Complete, cancel, acknowledge, reopen or defer a reminder. Provide new_due_local when rescheduling."""
    return services.update_reminder(actor, reminder_id, status, new_due_local)


@mcp.tool()
def set_goal(name: str, actor: Actor, target_amount: float | None = None,
             current_amount: float | None = None, currency: str = "MYR",
             target_date: str | None = None, notes: str | None = None,
             shared: bool = False) -> dict:
    """Create or update a savings/planning goal using only values the user actually supplied."""
    return services.set_goal(actor, name, target_amount, current_amount, currency, target_date, notes, shared)


@mcp.tool()
def list_goals(actor: Actor) -> dict:
    """Read active goals available to the authenticated user."""
    return services.list_goals(actor)


@mcp.tool()
def get_leave_balance(actor: Actor) -> dict:
    """Read the user's stored leave balance and as-of date. Never invent a balance when unknown."""
    return services.get_leave(actor)


@mcp.tool()
def set_leave_balance(balance_days: float, actor: Actor, as_of_date: str | None = None,
                      notes: str | None = None) -> dict:
    """Store/update leave balance only when the user explicitly provides the value."""
    return services.set_leave(actor, balance_days, as_of_date, notes)


@mcp.tool()
def add_shopping_item(item: str, actor: Actor, quantity: str | None = None,
                      notes: str | None = None, shared: bool = True) -> dict:
    """Add an item to the shopping list. Shared household list is the default; use shared=false only when the user clearly asks for a private list."""
    return services.add_shopping_item(actor, item, quantity, notes, shared)


@mcp.tool()
def list_shopping_items(actor: Actor, include_purchased: bool = False, limit: int = 50) -> dict:
    """List shopping items visible to the authenticated household user."""
    return services.list_shopping_items(actor, include_purchased, limit)


@mcp.tool()
def update_shopping_item(item_id: str, actor: Actor, status: str = "purchased",
                         quantity: str | None = None, notes: str | None = None) -> dict:
    """Mark a shopping item purchased/open/removed or update its quantity/notes. Do not mark purchased unless the user indicates it."""
    return services.update_shopping_item(actor, item_id, status, quantity, notes)


@mcp.tool()
def ha_find_entities(query: str, actor: Actor, domain: str | None = None, limit: int = 20) -> dict:
    """Find actual Home Assistant entity IDs by friendly name/entity id before answering or acting. Read-only."""
    import ha
    return ha.find_entities(query, domain, limit)


@mcp.tool()
def ha_get_state(entity_id: str, actor: Actor) -> dict:
    """Read the current state and attributes of one exact Home Assistant entity."""
    import ha
    return ha.get_state(entity_id)


@mcp.tool()
def ha_control(entity_id: str, action: str, actor: Actor, value: float | None = None) -> dict:
    """Perform an explicitly requested low-risk Home Assistant action on lights, switches, fans, climate or media players, then verify state. Sensitive domains are rejected by the backend."""
    import ha
    return ha.control(entity_id, action, value)


@mcp.tool()
def calculate(expression: str) -> dict:
    """Perform exact local arithmetic instead of estimating in language."""
    return services.calculate(expression)


@mcp.tool()
def set_money_bucket(name: str, amount: float, actor: Actor, currency: str = "MYR",
                     notes: str | None = None, shared: bool = False) -> dict:
    """Set a named allowance, allocation, stash or planning bucket to the exact amount the user supplied. Never invent or increase it without instruction."""
    return services.set_money_bucket(actor, name, amount, currency, notes, shared)


@mcp.tool()
def list_money_buckets(actor: Actor) -> dict:
    """Read the user's current named allowances, allocations and stash amounts for planning."""
    return services.list_money_buckets(actor)
