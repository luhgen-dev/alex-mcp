from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Resolve

from context import ActorContext, current_actor
import services
import phase2
import diagnostics

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
                category: str | None = None, currency: str | None = None,
                event_date_local: str | None = None, reference: str | None = None,
                event_type: str = "Expense") -> dict:
    """Record an expense or income. Currency must be explicitly known as MYR or SGD; never guess it. Use null category/amount when genuinely unclear."""
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
def set_work_roster(work_date: str, shift_name: str, actor: Actor,
                    start_local: str | None = None, end_local: str | None = None,
                    notes: str | None = None, status: str = "CONFIRMED") -> dict:
    """Store the user's work roster. Roster means work schedule, not personal diary."""
    return phase2.set_work_roster(actor, work_date, shift_name, start_local, end_local, notes, status)


@mcp.tool()
def list_work_roster(actor: Actor, start_date: str | None = None,
                     end_date: str | None = None, limit: int = 60) -> dict:
    """Read only the authenticated user's work roster."""
    return phase2.list_work_roster(actor, start_date, end_date, limit)


@mcp.tool()
def set_leave_record(leave_date: str, actor: Actor, status: str = "PLANNED",
                     portion: str = "FULL", notes: str | None = None) -> dict:
    """Store leave lifecycle: PLANNED -> CONFIRMED -> TAKEN. Never infer confirmed leave from a plan."""
    return phase2.set_leave_record(actor, leave_date, status, portion, notes)


@mcp.tool()
def list_leave_records(actor: Actor, start_date: str | None = None,
                       end_date: str | None = None, include_cancelled: bool = False) -> dict:
    """Read the authenticated user's planned/confirmed/taken leave records."""
    return phase2.list_leave_records(actor, start_date, end_date, include_cancelled)


@mcp.tool()
def create_plan(title: str, actor: Actor, start_local: str | None = None,
                end_local: str | None = None, notes: str | None = None,
                shared: bool = False, locked: bool = False) -> dict:
    """Create a draft life plan. DM defaults private; group/shared is family. Plans are not diary commitments until explicitly made so."""
    return phase2.create_plan(actor, title, start_local, end_local, notes, shared, locked)


@mcp.tool()
def list_plans(actor: Actor, include_cancelled: bool = False, limit: int = 50) -> dict:
    """List accessible draft/locked plans."""
    return phase2.list_plans(actor, include_cancelled, limit)


@mcp.tool()
def update_plan(plan_id: str, actor: Actor, status: str | None = None,
                title: str | None = None, start_local: str | None = None,
                end_local: str | None = None, notes: str | None = None) -> dict:
    """Update a plan or mark it DRAFT, LOCKED or CANCELLED. Do not silently rewrite the user's baseline intent."""
    return phase2.update_plan(actor, plan_id, status, title, start_local, end_local, notes)


@mcp.tool()
def share_plan(plan_id: str, actor: Actor) -> dict:
    """Explicitly publish a private plan as a separate FAMILY_SHARED copy; the private source remains intact."""
    return phase2.share_plan(actor, plan_id)


@mcp.tool()
def add_diary_event(title: str, start_local: str, actor: Actor,
                    end_local: str | None = None, notes: str | None = None,
                    shared: bool = False, reminder_minutes_before: int | None = None,
                    reminder_recipient: str = "me") -> dict:
    """Add a real-life diary commitment. If it clashes with the user's work roster, no event is written until the user chooses 1/2/3."""
    return phase2.add_diary_event(actor, title, start_local, end_local, notes, shared,
                                  reminder_minutes_before, reminder_recipient)


@mcp.tool()
def resolve_diary_conflict(conflict_id: str, choice: int, actor: Actor,
                           reminder_recipient: str = "me") -> dict:
    """Resolve a diary-vs-work conflict: 1=add event + PLANNED leave, 2=add event and keep clash, 3=cancel."""
    return phase2.resolve_diary_conflict(actor, conflict_id, choice, reminder_recipient)


@mcp.tool()
def update_diary_event(diary_id: str, actor: Actor, status: str | None = None,
                       start_local: str | None = None, end_local: str | None = None,
                       title: str | None = None, notes: str | None = None) -> dict:
    """Reschedule/cancel a diary event. Linked reminders move or cancel with it."""
    return phase2.update_diary_event(actor, diary_id, status, start_local, end_local, title, notes)


@mcp.tool()
def get_agenda(start_date: str, end_date: str, actor: Actor,
               include_plans: bool = True) -> dict:
    """Combined read-only agenda: diary + reminders + own work roster + own leave + accessible plans."""
    return phase2.get_agenda(actor, start_date, end_date, include_plans)


@mcp.tool()
def check_spouse_availability(start_local: str, actor: Actor,
                              end_local: str | None = None) -> dict:
    """Privacy-preserving spouse availability check. Returns busy/no conflict only; never exposes spouse private schedule details."""
    return phase2.check_spouse_availability(actor, start_local, end_local)


@mcp.tool()
def set_cashflow_baseline(currency: str, actor: Actor, guaranteed_income: float = 0,
                          fixed_commitments: float = 0, locked_allocations: float = 0,
                          reserves: float = 0, notes: str | None = None) -> dict:
    """Store only the user's explicit guaranteed-income baseline. OT/variable/extra cash is excluded and stays unallocated unless instructed."""
    return phase2.set_cashflow_baseline(actor, currency, guaranteed_income, fixed_commitments,
                                        locked_allocations, reserves, notes)


@mcp.tool()
def get_cashflow_baseline(currency: str, actor: Actor) -> dict:
    """Read the user's deterministic cash-flow baseline without recommending allowance changes."""
    return phase2.get_cashflow_baseline(actor, currency)


@mcp.tool()
def system_health(actor: Actor, hours: int = 24) -> dict:
    """Read sanitized Alex health/diagnostic facts: DB, failed messages, outbound queue, tool errors and usage. No secrets."""
    return diagnostics.system_health(actor, hours)


@mcp.tool()
def recent_failures(actor: Actor, hours: int = 24, limit: int = 20) -> dict:
    """Explain recent observed Alex failures from durable logs/audits. Return observed facts only, not invented causes."""
    return diagnostics.recent_failures(actor, hours, limit)


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
