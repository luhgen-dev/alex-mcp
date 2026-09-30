from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Resolve

from context import ActorContext, current_actor
import services
import phase2
import diagnostics
import phase2_finance
import phase2_work
import phase2_library
import phase2_delegation
import phase2_monitor
import phase2_home
import phase2_reports

mcp = MCPServer(
    "Alex Household Tools",
    version="0.5.0",
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
    """Record an expense or income. Currency must be explicitly known as MYR or SGD; never guess it. Use null category/amount when genuinely unclear. Omit event_date_local unless the user or the receipt states a date/time; Alex stamps the message time automatically and never needs an invented clock time."""
    return services.log_expense(actor, description, amount, category, currency, event_date_local, reference, event_type)


@mcp.tool()
def confirm_expense(event_id: str, actor: Actor, approve: bool = True,
                    category: str | None = None, amount: float | None = None) -> dict:
    """Confirm or reject a pending money record after the user supplies missing information or approval."""
    return services.confirm_expense(actor, event_id, approve, category, amount)


@mcp.tool()
def query_finances(actor: Actor, start_date: str | None = None, end_date: str | None = None,
                   category: str | None = None, search: str | None = None,
                   currency: str | None = None, limit: int = 20,
                   scope: str | None = None, source: str | None = None) -> dict:
    """Read true ledger totals and matching transactions. Use ISO dates YYYY-MM-DD. scope may be all/family/private. source may be all/voice/receipt/text. For today/tomorrow/yesterday, resolve the runtime date and set both start_date and end_date."""
    return services.query_finances(
        actor, start_date, end_date, category, search, currency, limit, scope, source
    )


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
def find_media(actor: Actor, media_type: str = "all", query: str | None = None,
               start_date: str | None = None, end_date: str | None = None,
               limit: int = 10) -> dict:
    """Browse preserved original voice notes, images or documents the authenticated user is allowed to retrieve. Results are numbered for follow-up retrieval."""
    return services.find_media(actor, media_type, query, start_date, end_date, limit)


@mcp.tool()
def get_media_original(media_id: str, actor: Actor) -> dict:
    """Retrieve one authorized original media object, including the original voice-note audio."""
    return services.get_media_original(actor, media_id)


@mcp.tool()
def save_item(title: str, content: str, actor: Actor, tags: str | None = None,
              shared: bool = False) -> dict:
    """Explicitly remember something the user asked Alex to save. This is separate from automatic receipt retention."""
    return services.save_item(actor, title, content, tags, shared)


@mcp.tool()
def search_saved_items(actor: Actor, query: str | None = None, limit: int = 10,
                       kind: str | None = None) -> dict:
    """Search or browse things the user explicitly asked Alex to save/remember. Leave query empty to list everything saved (newest first). kind may be picture, document or note (e.g. "what pictures did I save" -> kind=picture, no query). Results are numbered; the user can then say "show 2". Use get_saved_item to send an original."""
    return services.search_saved_items(actor, query, limit, kind)


@mcp.tool()
def get_saved_item(item_id: str, actor: Actor) -> dict:
    """Retrieve one explicitly saved item, including its original attachment when it had one."""
    return services.get_saved_item(actor, item_id)


@mcp.tool()
def remove_saved_item(item_id: str, actor: Actor) -> dict:
    """Soft-remove an explicit saved-memory index while retaining original archived media evidence."""
    return services.remove_saved_item(actor, item_id)


@mcp.tool()
def resolve_numbered_choice(choice: int, actor: Actor) -> dict:
    """Resolve the newest unexpired numbered receipt, saved-memory, or original-media list to the exact original item."""
    return services.resolve_numbered_choice(actor, choice)


@mcp.tool()
def create_reminder(task: str, due_local: str, actor: Actor,
                    recurrence_rule: str | None = None, shared: bool = False,
                    recipient: str = "me", presence_aware: bool = False,
                    delivery_class: str = "routine",
                    follow_up_after_hours: int = 24) -> dict:
    """Create a durable reminder. recipient is me/spouse/husband/wife/both; routine reminders may use quiet/presence policy, while time_critical bypasses those deferrals."""
    return services.create_reminder(
        actor, task, due_local, recurrence_rule, shared, recipient,
        presence_aware, delivery_class, follow_up_after_hours,
    )


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
def reminder_history(reminder_id: str, actor: Actor) -> dict:
    """Read the durable state-transition history for one authorized reminder."""
    return services.reminder_history(actor, reminder_id)


@mcp.tool()
def create_task(title: str, actor: Actor, notes: str | None = None,
                assignee: str = "unassigned", shared: bool = False,
                due_local: str | None = None, plan_id: str | None = None,
                reminder_id: str | None = None) -> dict:
    """Create a first-class task with OPEN lifecycle state. due_local and plan_id are optional. A reminder is never created implicitly; reminder_id only links an already-created authorized reminder. DM defaults private, while group/shared tasks are family-visible."""
    return phase2.create_task(
        actor, title, notes, assignee, shared, due_local, plan_id, reminder_id
    )


@mcp.tool()
def list_tasks(actor: Actor, status: str = "open",
               plan_id: str | None = None, limit: int = 50) -> dict:
    """List authorized tasks. status may be open, done, cancelled, or all; plan_id optionally narrows to one accessible plan."""
    return phase2.list_tasks(actor, status, plan_id, limit)


@mcp.tool()
def update_task(task_id: str, actor: Actor, title: str | None = None,
                notes: str | None = None, due_local: str | None = None,
                assignee: str | None = None, plan_id: str | None = None,
                reminder_id: str | None = None) -> dict:
    """Edit task fields without changing task lifecycle state. Empty due_local/plan_id/reminder_id clears that optional link/value. This never creates a reminder or rewrites the linked plan."""
    return phase2.update_task(
        actor, task_id, title, notes, due_local, assignee, plan_id, reminder_id
    )


@mcp.tool()
def complete_task(task_id: str, actor: Actor) -> dict:
    """Mark an OPEN task DONE while preserving its lifecycle history and linked plan/reminder state."""
    return phase2.complete_task(actor, task_id)


@mcp.tool()
def reopen_task(task_id: str, actor: Actor) -> dict:
    """Explicitly reopen a DONE task back to OPEN while preserving lifecycle history."""
    return phase2.reopen_task(actor, task_id)


@mcp.tool()
def cancel_task(task_id: str, actor: Actor) -> dict:
    """Cancel a task without deleting or changing any linked plan or reminder."""
    return phase2.cancel_task(actor, task_id)


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
def list_shopping_items(actor: Actor, include_purchased: bool = False, limit: int = 50,
                        scope: str | None = None) -> dict:
    """List shopping items visible to the authenticated household user. scope may be all, family, or private."""
    return services.list_shopping_items(actor, include_purchased, limit, scope)


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
def ha_home_summary(actor: Actor) -> dict:
    """Read Home Assistant states and produce a deterministic privacy-safe whole-home status summary."""
    import ha
    rows = ha.list_states()
    return phase2_home.summarize_home(rows)


@mcp.tool()
def ha_home_report(actor: Actor) -> dict:
    """Render a deterministic local PNG whole-home status card and return it as a WhatsApp image attachment."""
    from pathlib import Path
    from config import DATA_DIR
    import ha

    summary = phase2_home.summarize_home(ha.list_states())
    payload = phase2_home.render_home_report_png(summary)
    out_dir = Path(DATA_DIR) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "alex-home-status.png"
    path.write_bytes(payload)
    return {
        "status": "ready",
        "summary": summary,
        "_attachments": [{"path": str(path), "kind": "IMAGE", "mime_type": "image/png"}],
    }


@mcp.tool()
def ha_draft_automation(name: str, trigger_yaml: str, action_yaml: str, actor: Actor,
                        condition_yaml: str | None = None) -> dict:
    """Return a draft Home Assistant automation proposal only. It never deploys or edits HA configuration."""
    import ha
    return ha.draft_automation(name, trigger_yaml, action_yaml, condition_yaml)


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
                     portion: str = "FULL", notes: str | None = None,
                     end_date: str | None = None,
                     leave_type: str = "ANNUAL_LEAVE") -> dict:
    """Store owner-private leave lifecycle. PLANNED/CONFIRMED do not count as historical absence; only TAKEN materializes dated annual/medical leave into the work engine."""
    return phase2.set_leave_record(
        actor, leave_date, status, portion, notes, end_date, leave_type
    )


@mcp.tool()
def list_leave_records(actor: Actor, start_date: str | None = None,
                       end_date: str | None = None, include_cancelled: bool = False) -> dict:
    """Read the authenticated user's planned/confirmed/taken leave records."""
    return phase2.list_leave_records(actor, start_date, end_date, include_cancelled)


@mcp.tool()
def create_plan(title: str, actor: Actor, start_local: str | None = None,
                end_local: str | None = None, notes: str | None = None,
                shared: bool = False, locked: bool = False,
                time_known: bool | None = None) -> dict:
    """Create a draft life plan. If the user gave a date but no clock time, set time_known=false; never invent midnight. DM defaults private; group/shared is family."""
    return phase2.create_plan(
        actor, title, start_local, end_local, notes, shared, locked, time_known
    )


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
def confirm_plan(plan_id: str, actor: Actor, add_to_diary: bool = True,
                 reminder_minutes_before: int | None = None,
                 reminder_recipient: str = "me") -> dict:
    """Confirm a plan. A dated plan materializes a linked Diary event; conflicts are gated before confirmation."""
    return phase2.confirm_plan(
        actor, plan_id, add_to_diary, reminder_minutes_before, reminder_recipient
    )


@mcp.tool()
def share_plan(plan_id: str, actor: Actor, shared_notes: str | None = None) -> dict:
    """Publish a private plan as a separate FAMILY_SHARED copy. Private notes are never copied automatically; provide shared_notes only when the owner explicitly wants those family-safe notes shared."""
    return phase2.share_plan(actor, plan_id, shared_notes)


@mcp.tool()
def add_diary_event(title: str, start_local: str, actor: Actor,
                    end_local: str | None = None, notes: str | None = None,
                    shared: bool = False, reminder_minutes_before: int | None = None,
                    reminder_recipient: str = "me",
                    time_known: bool | None = None) -> dict:
    """Add a real-life diary commitment. If user supplied only a date, set time_known=false so same-day items become a heads-up rather than an invented midnight conflict. Exact work/Diary overlaps require user choice."""
    return phase2.add_diary_event(
        actor, title, start_local, end_local, notes, shared,
        reminder_minutes_before, reminder_recipient, time_known=time_known
    )


@mcp.tool()
def resolve_diary_conflict(conflict_id: str, choice: int, actor: Actor,
                           reminder_recipient: str = "me") -> dict:
    """Resolve a diary-vs-work conflict: 1=add event + PLANNED leave, 2=add event and keep clash, 3=cancel."""
    return phase2.resolve_diary_conflict(actor, conflict_id, choice, reminder_recipient)


@mcp.tool()
def resolve_latest_diary_conflict(choice: int, actor: Actor,
                                  reminder_recipient: str = "me") -> dict:
    """Resolve the latest unexpired owner-scoped diary conflict ticket. This is the safe handler for a later bare '1', '2' or '3' reply."""
    return phase2.resolve_latest_diary_conflict(actor, choice, reminder_recipient)


@mcp.tool()
def update_diary_event(diary_id: str, actor: Actor, status: str | None = None,
                       start_local: str | None = None, end_local: str | None = None,
                       title: str | None = None, notes: str | None = None,
                       linked_reminders: str = "ask") -> dict:
    """Reschedule/cancel a diary event. If linked reminders exist, keep/shift/cancel must be explicit."""
    return phase2.update_diary_event(
        actor, diary_id, status, start_local, end_local, title, notes, linked_reminders
    )


@mcp.tool()
def get_agenda(start_date: str, end_date: str, actor: Actor,
               include_plans: bool = True) -> dict:
    """Combined read-only agenda. Use returned start_local/end_local/due_local fields for user-facing times; stored *_utc fields are internal evidence."""
    return phase2.get_agenda(actor, start_date, end_date, include_plans)


@mcp.tool()
def get_agenda_range(phrase: str, actor: Actor, reference_date: str | None = None,
                     include_plans: bool = True) -> dict:
    """Resolve natural dates/ranges deterministically and return the combined agenda. Use returned local-time fields for replies."""
    return phase2.get_agenda_range(actor, phrase, reference_date, include_plans)


@mcp.tool()
def check_my_availability(start_local: str, actor: Actor,
                          end_local: str | None = None) -> dict:
    """Owner-only private availability check. Rejected in the family group and never publishable there."""
    return phase2.check_my_availability(actor, start_local, end_local)


@mcp.tool()
def check_spouse_availability(start_local: str, actor: Actor,
                              end_local: str | None = None) -> dict:
    """Check only shared spouse commitments. Private spouse roster/Diary is never read; if no shared clash exists, returns private_check_required."""
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
def planning_create_goal(name: str, target_amount: float, actor: Actor,
                         baseline_monthly: float = 0,
                         currency: str = "MYR", target_date: str | None = None,
                         status: str = "DRAFT", shared: bool = False) -> dict:
    """Create an unlocked draft goal. baseline_monthly is optional and defaults to zero; never invent a contribution."""
    return phase2_finance.create_goal(
        name, target_amount, baseline_monthly, actor.phone, actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        currency, target_date, status,
    )


@mcp.tool()
def planning_lock_goal(actor: Actor, goal_id: str | None = None,
                       goal_name: str | None = None) -> dict:
    """Lock/activate one draft goal after explicit owner approval. Provide either goal_id from a prior result or its natural goal_name; Alex resolves names conservatively."""
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.lock_goal(
        resolved, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_reopen_goal(actor: Actor, goal_id: str | None = None,
                         goal_name: str | None = None) -> dict:
    """Reopen one eligible goal only after explicit owner instruction. goal_name may be used instead of an opaque id."""
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.reopen_goal(
        resolved, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_set_period_target(amount: float, actor: Actor,
                               goal_id: str | None = None,
                               goal_name: str | None = None,
                               period: str | None = None,
                               reason: str | None = None) -> dict:
    """Set a one-period goal target such as 'RM100 is enough this month' without changing future recurring baseline. Natural goal_name is supported; omitted period means the current local month."""
    import runtime_clock
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    effective_period = period or runtime_clock.today(actor.timezone).strftime("%Y-%m")
    return phase2_finance.set_goal_period_target(
        resolved, effective_period, amount,
        actor.phone, actor.conversation_type, reason
    )


@mcp.tool()
def planning_change_goal_baseline(new_monthly_amount: float, actor: Actor,
                                  goal_id: str | None = None,
                                  goal_name: str | None = None,
                                  effective_from_period: str | None = None,
                                  reason: str | None = None) -> dict:
    """Change a recurring goal baseline prospectively. Natural goal_name is supported; earlier periods remain unchanged."""
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.set_goal_baseline(
        resolved, new_monthly_amount, actor.phone, actor.conversation_type,
        effective_from_period, reason,
    )


@mcp.tool()
def planning_record_goal_contribution(amount: float, actor: Actor,
                                      goal_id: str | None = None,
                                      goal_name: str | None = None,
                                      contribution_date: str | None = None,
                                      contribution_kind: str = "ONE_OFF",
                                      source_cash_event_id: str | None = None) -> dict:
    """Record an actual goal contribution without changing its recurring plan. goal_name is accepted and omitted contribution_date means today locally."""
    import runtime_clock
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    effective_date = (
        contribution_date
        or runtime_clock.today(actor.timezone).isoformat()
    )
    return phase2_finance.record_goal_contribution(
        resolved, amount, effective_date,
        actor.phone, actor.conversation_type,
        contribution_kind, source_cash_event_id, actor.source_message_id,
    )


@mcp.tool()
def planning_goal_progress(actor: Actor, goal_id: str | None = None,
                           goal_name: str | None = None) -> dict:
    """Read goal target, actual funding, remaining amount and recurring baseline. Natural goal_name is accepted."""
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.goal_progress(
        resolved, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_goal_deviation(actor: Actor, goal_id: str | None = None,
                            goal_name: str | None = None,
                            actual_amount: float | None = None,
                            period: str | None = None) -> dict:
    """Compare actual contribution with the approved period plan. If actual_amount is omitted Alex computes actual contributions from stored records; omitted period means current local month."""
    import runtime_clock
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    effective_period = period or runtime_clock.today(actor.timezone).strftime("%Y-%m")
    return phase2_finance.evaluate_goal_deviation(
        resolved, actual_amount, effective_period,
        actor.phone, actor.conversation_type,
    )


@mcp.tool()
def planning_record_cash(event_type: str, amount: float, actor: Actor,
                         event_date: str | None = None,
                         currency: str = "MYR", description: str | None = None,
                         shared: bool = False) -> dict:
    """Record salary/OT/bonus/refund/other cash. Variable cash starts UNALLOCATED. Omit event_date only when the user means the current local day."""
    import runtime_clock
    effective_date = event_date or runtime_clock.today(actor.timezone).isoformat()
    return phase2_finance.record_cash_event(
        event_type, amount, effective_date, actor.phone, actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        currency, description, actor.source_message_id,
    )


@mcp.tool()
def planning_compare_salary(actor: Actor, cash_event_id: str | None = None,
                            event_date: str | None = None,
                            amount: float | None = None) -> dict:
    """Compare an actual recorded salary payment with configured fixed salary. Omit cash_event_id to use the unique matching salary, or the latest salary when asking about the latest payment."""
    resolved = phase2_finance.resolve_cash_event_reference(
        cash_event_id, actor.phone, actor.conversation_type,
        event_type="SALARY", event_date=event_date, amount=amount,
        source_message_id=actor.source_message_id, latest=True,
    )
    return phase2_finance.compare_actual_salary(
        resolved, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_match_goal_alias(alias_text: str, actor: Actor) -> dict:
    """Conservatively resolve a configured non-sensitive account alias to one authorized goal."""
    return phase2_finance.resolve_goal_from_alias(
        alias_text, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_cash_status(actor: Actor, cash_event_id: str | None = None,
                         event_type: str | None = None,
                         event_date: str | None = None,
                         amount: float | None = None) -> dict:
    """Read how much of one cash event is still unallocated. Natural event type/date/amount may be used instead of an opaque cash_event_id; ambiguous matches are never guessed."""
    resolved = phase2_finance.resolve_cash_event_reference(
        cash_event_id, actor.phone, actor.conversation_type,
        event_type=event_type, event_date=event_date, amount=amount,
        source_message_id=actor.source_message_id,
        require_unallocated=True,
    )
    return phase2_finance.cash_event_status(
        resolved, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_allocate_cash_to_goal(amount: float, actor: Actor,
                                   cash_event_id: str | None = None,
                                   cash_event_type: str | None = None,
                                   cash_event_date: str | None = None,
                                   cash_event_amount: float | None = None,
                                   goal_id: str | None = None,
                                   goal_name: str | None = None,
                                   contribution_date: str | None = None) -> dict:
    """Allocate explicit extra cash to a goal only after the user instructs Alex. Natural cash-event and goal references are supported; ambiguous cash is never guessed."""
    resolved_cash = phase2_finance.resolve_cash_event_reference(
        cash_event_id, actor.phone, actor.conversation_type,
        event_type=cash_event_type, event_date=cash_event_date,
        amount=cash_event_amount, source_message_id=actor.source_message_id,
        require_unallocated=True,
    )
    resolved_goal = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.allocate_cash_to_goal(
        resolved_cash, resolved_goal, amount,
        actor.phone, actor.conversation_type, contribution_date,
    )


@mcp.tool()
def planning_create_cash_pool(name: str, actor: Actor, currency: str = "MYR",
                              shared: bool = False) -> dict:
    """Create a stash/cash pool without allocating any money into it."""
    return phase2_finance.create_cash_pool(
        name, actor.phone, actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        currency,
    )


@mcp.tool()
def planning_cash_pool_balance(actor: Actor, pool_id: str | None = None,
                               pool_name: str | None = None) -> dict:
    """Read the exact balance of one authorized stash/cash pool. Natural pool_name is accepted instead of an opaque id."""
    resolved = phase2_finance.resolve_cash_pool_reference(
        pool_id, pool_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.cash_pool_balance(
        resolved, actor.phone, actor.conversation_type
    )


@mcp.tool()
def planning_allocate_cash_to_pool(amount: float, actor: Actor,
                                   cash_event_id: str | None = None,
                                   cash_event_type: str | None = None,
                                   cash_event_date: str | None = None,
                                   cash_event_amount: float | None = None,
                                   pool_id: str | None = None,
                                   pool_name: str | None = None) -> dict:
    """Allocate explicit extra cash to a stash/pool after user instruction. Natural cash-event and pool references are supported; ambiguous matches are never guessed."""
    resolved_cash = phase2_finance.resolve_cash_event_reference(
        cash_event_id, actor.phone, actor.conversation_type,
        event_type=cash_event_type, event_date=cash_event_date,
        amount=cash_event_amount, source_message_id=actor.source_message_id,
        require_unallocated=True,
    )
    resolved_pool = phase2_finance.resolve_cash_pool_reference(
        pool_id, pool_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.allocate_cash_to_pool(
        resolved_cash, resolved_pool, amount,
        actor.phone, actor.conversation_type,
    )


@mcp.tool()
def planning_add_reserve(name: str, monthly_amount: float, actor: Actor,
                         currency: str = "MYR", shared: bool = False) -> dict:
    """Add an explicit monthly reserve/allowance to the baseline only because the user asked to reserve it."""
    return phase2_finance.add_plan_reserve(
        name, monthly_amount, actor.phone, actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        currency,
    )


@mcp.tool()
def planning_update_reserve(actor: Actor, reserve_id: str | None = None,
                            reserve_name: str | None = None,
                            monthly_amount: float | None = None,
                            name: str | None = None,
                            active: bool | None = None) -> dict:
    """Change, enable or disable a reserve only after explicit owner instruction. Natural reserve_name is accepted instead of an opaque id."""
    resolved = phase2_finance.resolve_reserve_reference(
        reserve_id, reserve_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.update_plan_reserve(
        resolved, actor.phone, actor.conversation_type,
        monthly_amount, name, active,
    )


@mcp.tool()
def planning_list_reserves(actor: Actor, include_inactive: bool = False) -> dict:
    """List authorized explicit planning reserves/allowances."""
    return {"reserves": phase2_finance.list_plan_reserves(
        actor.phone, actor.conversation_type, "all", include_inactive
    )}


@mcp.tool()
def planning_baseline(actor: Actor, currency: str = "MYR",
                      reveal_inputs: bool = False) -> dict:
    """Read deterministic baseline capacity. Raw private income is included in output only when the owner explicitly asks to reveal it."""
    return phase2_finance.baseline_plan(
        actor.phone, actor.conversation_type, "all", reveal_inputs, currency
    )


@mcp.tool()
def planning_income_outlook(actor: Actor, period: str | None = None,
                            currency: str = "MYR",
                            reveal_sources: bool = False) -> dict:
    """Read confirmed/expected/possible income. Omitted period means the current local month. OT remains possible/unknown until observed."""
    import runtime_clock
    effective_period = period or runtime_clock.today(actor.timezone).strftime("%Y-%m")
    return phase2_finance.income_outlook(
        effective_period, actor.phone, actor.conversation_type,
        "all", reveal_sources, currency
    )


@mcp.tool()
def planning_goal_projection(actor: Actor, goal_id: str | None = None,
                             goal_name: str | None = None,
                             from_period: str | None = None) -> dict:
    """Project a goal using its approved baseline only; natural goal_name is accepted and one-off extra contributions do not rewrite future baseline."""
    resolved = phase2_finance.resolve_goal_reference(
        goal_id, goal_name, actor.phone, actor.conversation_type
    )
    return phase2_finance.goal_projection(
        resolved, actor.phone, actor.conversation_type, from_period
    )


@mcp.tool()
def planning_cashflow(actor: Actor, period: str | None = None,
                      currency: str = "MYR") -> dict:
    """Read a dated monthly forecast. Omitted period means the current local month. OT/variable income remains separate from guaranteed baseline."""
    import runtime_clock
    effective_period = period or runtime_clock.today(actor.timezone).strftime("%Y-%m")
    return phase2_finance.cashflow_forecast(
        effective_period, actor.phone, actor.conversation_type, "all", currency,
    )


@mcp.tool()
def planning_brief(actor: Actor, currency: str = "MYR") -> dict:
    """Read a privacy-scoped baseline planning brief. Does not recommend allowance changes."""
    return phase2_finance.planning_brief(
        actor.phone, actor.conversation_type, "all", currency,
    )


@mcp.tool()
def planning_list_goals(actor: Actor) -> dict:
    """List authorized advanced goals and their current plan state."""
    return {"goals": phase2_finance.list_goals(
        actor.phone, actor.conversation_type, "all"
    )}


@mcp.tool()
def bills_list(actor: Actor, period: str | None = None,
               as_of_date: str | None = None) -> dict:
    """List recurring obligations. If period is omitted, materialize the current and next local month so 'coming up' does not falsely return empty."""
    from datetime import date, timedelta
    import runtime_clock

    effective_date = as_of_date or runtime_clock.today(actor.timezone).isoformat()
    effective_day = date.fromisoformat(effective_date)
    if period:
        periods = [period]
    else:
        next_month = (effective_day.replace(day=28) + timedelta(days=4)).replace(day=1)
        periods = [effective_day.strftime("%Y-%m"), next_month.strftime("%Y-%m")]
    for target_period in periods:
        phase2_finance.ensure_obligation_instances(
            target_period, actor.phone, actor.conversation_type, "all"
        )
    phase2_finance.refresh_obligation_states(
        effective_date, actor.phone, actor.conversation_type, "all"
    )
    return {"obligations": phase2_finance.list_obligations(
        actor.phone, actor.conversation_type, "all", period
    ), "as_of_date": effective_date}


@mcp.tool()
def bills_match_payment(label: str, amount: float, event_date: str, actor: Actor) -> dict:
    """Conservatively match payment/receipt facts to exactly one authorized recurring obligation. Ambiguous matches are returned, never guessed."""
    return phase2_finance.resolve_obligation_from_evidence(
        label, amount, event_date, actor.phone, actor.conversation_type, "all"
    )


@mcp.tool()
def bills_record_payment(instance_id: str, amount: float, actor: Actor,
                         note: str | None = None) -> dict:
    """Record an actual payment against one exact obligation instance; supports partial payment."""
    return phase2_finance.record_obligation_payment(
        instance_id, amount, actor.phone, actor.conversation_type, note,
    )


@mcp.tool()
def bills_defer(instance_id: str, new_due_date: str, actor: Actor,
                note: str | None = None) -> dict:
    """Defer one exact obligation; history remains intact."""
    return phase2_finance.defer_obligation(
        instance_id, new_due_date, actor.phone, actor.conversation_type, note,
    )


@mcp.tool()
def bills_confirm_unpaid(instance_id: str, actor: Actor, note: str | None = None) -> dict:
    """Mark one obligation explicitly confirmed unpaid. Missing evidence alone never means unpaid."""
    return phase2_finance.confirm_obligation_unpaid(
        instance_id, actor.phone, actor.conversation_type, note,
    )


@mcp.tool()
def work_schedule(start_date: str, end_date: str, actor: Actor) -> dict:
    """Read repeating roster, explicit shift exceptions and effective OT over a date range."""
    return phase2_work.work_summary(start_date, end_date, actor.phone, actor.conversation_type)


@mcp.tool()
def work_day(on_date: str, actor: Actor) -> dict:
    """Read a natural historical/current work-day brief without inventing attendance."""
    return phase2_work.historical_work_day(on_date, actor.phone, actor.conversation_type)


@mcp.tool()
def work_record_event(event_type: str, event_date: str, actor: Actor,
                      shift_code: str | None = None, start_time: str | None = None,
                      end_time: str | None = None, hours: float | None = None,
                      units_days: float | None = None, work_scope: str | None = None,
                      manager_override: bool = False, note: str | None = None,
                      shared: bool = False) -> dict:
    """Record leave/MC/shift-swap or OT offered/pending/planned/worked/unavailable as a dated fact."""
    return phase2_work.record_work_event(
        event_type, event_date, actor.phone, actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        shift_code, start_time, end_time, hours, units_days, work_scope,
        manager_override, note, actor.source_message_id,
    )


@mcp.tool()
def work_ot_status(on_date: str, actor: Actor) -> dict:
    """Read explicit/default OT state; offered/pending are never treated as worked income."""
    return phase2_work.ot_status_for_date(on_date, actor.phone, actor.conversation_type)


@mcp.tool()
def work_leave_balance(leave_id: str, actor: Actor, as_of_date: str | None = None) -> dict:
    """Read annual/medical leave from configured snapshot plus later TAKEN/recorded events."""
    return phase2_work.leave_balance(leave_id, actor.phone, actor.conversation_type, as_of_date)


@mcp.tool()
def work_departure_plan(on_date: str, actor: Actor, travel_minutes: int | None = None,
                        prep_minutes: int = 30, arrival_buffer_minutes: int = 10) -> dict:
    """Calculate leave-home/alarm candidates from real roster plus supplied travel duration. Never invent route time or create an alarm."""
    return phase2_home.shift_departure_plan(
        on_date, actor.phone, actor.conversation_type,
        travel_minutes, prep_minutes, arrival_buffer_minutes,
    )


@mcp.tool()
def asset_create(name: str, actor: Actor, category: str | None = None,
                 brand: str | None = None, model: str | None = None,
                 serial_number: str | None = None, purchase_date: str | None = None,
                 warranty_end: str | None = None, note: str | None = None,
                 shared: bool = True) -> dict:
    """Create household asset metadata such as appliance/warranty records."""
    return phase2_library.create_asset(
        name, actor.phone, actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        category, brand, model, serial_number, purchase_date, warranty_end, note,
    )


@mcp.tool()
def asset_link_document(document_type: str, actor: Actor,
                        asset_id: str | None = None,
                        asset_name: str | None = None,
                        evidence_ref: str | None = None,
                        note: str | None = None) -> dict:
    """Link preserved receipt/warranty/manual/photo evidence to an authorized asset. Use asset_name instead of inventing an asset UUID. When linking the attachment on this exact message, omit evidence_ref and Alex binds the single preserved current media object automatically."""
    resolved_asset = phase2_library.resolve_asset_reference(
        asset_id, asset_name, actor.phone, actor.conversation_type
    )
    resolved_evidence = str(evidence_ref or "").strip()
    if not resolved_evidence:
        media_ids = [str(value) for value in (actor.media_ids or []) if value]
        if len(media_ids) != 1:
            raise ValueError(
                "The current message must contain exactly one preserved attachment "
                "when evidence_ref is omitted."
            )
        resolved_evidence = media_ids[0]
    return phase2_library.link_document(
        resolved_asset, document_type, resolved_evidence,
        actor.phone, actor.conversation_type,
        actor.source_message_id, note,
    )


@mcp.tool()
def asset_list(actor: Actor, include_documents: bool = False) -> dict:
    """List authorized household/private assets without leaking another person's private assets."""
    return {"assets": phase2_library.list_assets(
        actor.phone, actor.conversation_type, "all", include_documents
    )}


@mcp.tool()
def warranty_expiring(actor: Actor, within_days: int = 90,
                      as_of_date: str | None = None) -> dict:
    """Find warranties expiring in a deterministic window. Omitted as_of_date means today locally; a vague 'warranties I should know about' uses the next 90 days."""
    import runtime_clock
    effective_date = as_of_date or runtime_clock.today(actor.timezone).isoformat()
    return {"warranties": phase2_library.warranties_expiring(
        within_days, effective_date, actor.phone, actor.conversation_type, "all"
    )}


@mcp.tool()
def monitor_delegate(delegation_type: str, subject: str, actor: Actor,
                     shared: bool = False) -> dict:
    """Enable one proactive monitor only after an explicit user instruction. Quiet by default otherwise."""
    return phase2_delegation.create_delegation(
        delegation_type, subject, actor.phone, actor.source_message_id,
        actor.conversation_type,
        "family" if shared or actor.conversation_type == "GROUP" else "private",
        None, True,
    )


@mcp.tool()
def monitor_list(actor: Actor, delegation_type: str | None = None) -> dict:
    """List active user-authorized proactive monitoring delegations."""
    return {"delegations": phase2_delegation.active_delegations(
        actor.phone, actor.conversation_type, delegation_type
    )}


@mcp.tool()
def monitor_cancel(delegation_id: str, actor: Actor) -> dict:
    """Cancel one explicit monitoring delegation."""
    return phase2_delegation.close_delegation(
        delegation_id, actor.phone, actor.conversation_type, "CANCELLED"
    )


@mcp.tool()
def report_snapshot(actor: Actor, period: str | None = None,
                    include_raw_income: bool = False) -> dict:
    """Build a privacy-scoped household/finance/work snapshot. Raw private income is never exposed in a family group."""
    if include_raw_income and actor.conversation_type == "GROUP":
        raise PermissionError("raw private income cannot be requested from the family group")
    return phase2_reports.build_snapshot(
        actor.phone, actor.conversation_type, "all", period,
        include_raw_income=include_raw_income, include_assets=True, include_leave=True,
    )


@mcp.tool()
def report_export(format: str, actor: Actor, period: str | None = None,
                  include_raw_income: bool = False) -> dict:
    """Create a local privacy-scoped PDF/CSV/JSON report and return it as a WhatsApp document attachment."""
    import os
    import uuid
    from pathlib import Path
    from config import DATA_DIR

    fmt = (format or "").strip().lower()
    if fmt not in {"pdf", "csv", "json"}:
        raise ValueError("format must be pdf, csv or json")
    if include_raw_income and actor.conversation_type == "GROUP":
        raise PermissionError("raw private income cannot be exported from the family group")

    snapshot = phase2_reports.build_snapshot(
        actor.phone, actor.conversation_type, "all", period,
        include_raw_income=include_raw_income, include_assets=True, include_leave=True,
    )
    out_dir = Path(DATA_DIR) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_period = (period or "current").replace("/", "-").replace("..", "-")
    # One immutable path per export prevents a queued private report from
    # being overwritten by another user's/group's export before outbox send.
    path = out_dir / f"alex-{safe_period}-{uuid.uuid4().hex}.{fmt}"
    if fmt == "pdf":
        payload = phase2_reports.minimal_pdf(snapshot)
        path.write_bytes(payload)
        mime = "application/pdf"
    elif fmt == "csv":
        payload = phase2_reports.finance_csv(snapshot)
        path.write_text(payload, encoding="utf-8")
        mime = "text/csv"
    else:
        payload = phase2_reports.snapshot_json(snapshot)
        path.write_text(payload, encoding="utf-8")
        mime = "application/json"
    return {
        "status": "ready",
        "format": fmt,
        "period": period,
        "_attachments": [{"path": os.fspath(path), "kind": "DOCUMENT", "mime_type": mime}],
    }


@mcp.tool()
def report_payload(target: str, actor: Actor, period: str | None = None) -> dict:
    """Return a privacy-safe structured payload for a future Google Sheets or TV handoff; this tool never uploads externally."""
    target_name = (target or "").strip().lower()
    snapshot = phase2_reports.build_snapshot(
        actor.phone, actor.conversation_type, "all", period,
        include_raw_income=False, include_assets=True, include_leave=True,
    )
    if target_name in {"sheets", "google_sheets", "google sheets"}:
        return {"target": "google_sheets", "rows": phase2_reports.google_sheets_rows(snapshot)}
    if target_name in {"tv", "dashboard"}:
        return {"target": "tv", "payload": phase2_reports.tv_payload(snapshot)}
    raise ValueError("target must be google_sheets or tv")


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
