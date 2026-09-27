from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Resolve

from context import ActorContext, current_actor
import services

mcp = MCPServer(
    "Alex Household Tools",
    version="0.1.0",
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
def create_reminder(task: str, due_local: str, actor: Actor,
                    recurrence_rule: str | None = None, shared: bool = False) -> dict:
    """Create a durable reminder. due_local must be an ISO local datetime with offset or a local ISO datetime."""
    return services.create_reminder(actor, task, due_local, recurrence_rule, shared)


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
