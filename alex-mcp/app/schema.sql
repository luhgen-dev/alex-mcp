PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inbound_messages (
    message_id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL DEFAULT 'WHATSAPP',
    conversation_id TEXT NOT NULL,
    conversation_type TEXT NOT NULL DEFAULT 'DIRECT_DM',
    sender_phone TEXT NOT NULL,
    sender_provider_jid TEXT,
    raw_text TEXT NOT NULL DEFAULT '',
    quoted_message_id TEXT,
    processing_state TEXT NOT NULL CHECK(processing_state IN ('RECEIVED','PROCESSING','COMPLETED','FAILED')) DEFAULT 'RECEIVED',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    processing_started_at_utc TEXT,
    cached_response TEXT,
    last_error TEXT,
    received_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_key TEXT PRIMARY KEY,
    applied_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_phone_history (
    history_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    valid_from_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to_utc TEXT,
    UNIQUE(phone_number, valid_to_utc),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_phone
ON user_phone_history(phone_number) WHERE valid_to_utc IS NULL;

CREATE TABLE IF NOT EXISTS spaces (
    space_id TEXT PRIMARY KEY,
    space_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    user_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    PRIMARY KEY(user_id, space_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE TABLE IF NOT EXISTS routing_rules (
    rule_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    category TEXT,
    force_space_id TEXT,
    high_value_threshold_minor INTEGER,
    UNIQUE(user_id, keyword),
    FOREIGN KEY(user_id) REFERENCES users(user_id),
    FOREIGN KEY(force_space_id) REFERENCES spaces(space_id)
);

CREATE TABLE IF NOT EXISTS financial_events (
    event_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    source_message_id TEXT,
    space_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('Expense','Income')),
    category TEXT,
    amount_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'MYR',
    event_date_utc TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    description TEXT NOT NULL,
    reference_text TEXT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUPERSEDED','PENDING_HUMAN_REVIEW','IGNORED')) DEFAULT 'ACTIVE',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(created_by) REFERENCES users(user_id),
    FOREIGN KEY(owner_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_finance_space_date ON financial_events(space_id, event_date_utc);
CREATE INDEX IF NOT EXISTS idx_finance_category ON financial_events(category, event_date_utc);
CREATE INDEX IF NOT EXISTS idx_finance_source ON financial_events(source_message_id);

CREATE TABLE IF NOT EXISTS financial_event_corrections (
    correction_id TEXT PRIMARY KEY,
    parent_event_id TEXT NOT NULL UNIQUE,
    child_event_id TEXT NOT NULL UNIQUE,
    reason TEXT,
    corrected_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(parent_event_id) REFERENCES financial_events(event_id),
    FOREIGN KEY(child_event_id) REFERENCES financial_events(event_id)
);

CREATE TABLE IF NOT EXISTS media_objects (
    media_id TEXT PRIMARY KEY,
    source_message_id TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('IMAGE','AUDIO','PDF','DOCUMENT')),
    mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    original_name TEXT,
    local_path TEXT NOT NULL,
    ocr_text TEXT,
    transcript_text TEXT,
    transcript_meta_json TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_message_id, media_type),
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_media_sha ON media_objects(sha256);

CREATE TABLE IF NOT EXISTS pending_items (
    item_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    media_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('PENDING','RESOLVED','CANCELLED')) DEFAULT 'PENDING',
    note TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at_utc TEXT,
    resolution_message_id TEXT,
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(media_id) REFERENCES media_objects(media_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_items_owner_status
ON pending_items(owner_id,status,created_at_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_items_source_kind
ON pending_items(source_message_id,kind);

CREATE TABLE IF NOT EXISTS event_media_links (
    event_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    PRIMARY KEY(event_id, media_id),
    FOREIGN KEY(event_id) REFERENCES financial_events(event_id),
    FOREIGN KEY(media_id) REFERENCES media_objects(media_id)
);

CREATE TABLE IF NOT EXISTS saved_items (
    item_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    source_message_id TEXT,
    space_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    media_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(media_id) REFERENCES media_objects(media_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_owner_date ON saved_items(owner_id, created_at_utc);


CREATE TABLE IF NOT EXISTS shopping_items (
    item_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity TEXT,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('OPEN','PURCHASED','REMOVED')) DEFAULT 'OPEN',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE INDEX IF NOT EXISTS idx_shopping_space_status
ON shopping_items(space_id, status, created_at_utc);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    source_message_id TEXT,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    task_text TEXT NOT NULL,
    due_at_utc TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    recurrence_rule TEXT,
    status TEXT NOT NULL CHECK(status IN ('OPEN','DUE','ACK','DEFERRED','COMP','CANC')) DEFAULT 'OPEN',
    presence_aware INTEGER NOT NULL DEFAULT 0 CHECK(presence_aware IN (0,1)),
    delivery_class TEXT NOT NULL DEFAULT 'routine' CHECK(delivery_class IN ('routine','time_critical')),
    follow_up_after_hours INTEGER NOT NULL DEFAULT 24 CHECK(follow_up_after_hours >= 0),
    acknowledged_at_utc TEXT,
    last_fired_at_utc TEXT,
    last_follow_up_at_utc TEXT,
    next_delivery_at_utc TEXT,
    defer_reason TEXT,
    claimable INTEGER NOT NULL DEFAULT 0 CHECK(claimable IN (0,1)),
    claimed_by_user_id TEXT,
    claimed_at_utc TEXT,
    claimant_follow_up_at_utc TEXT,
    family_resurfaced_at_utc TEXT,
    seen_at_utc TEXT,
    seen_by_user_id TEXT,
    nudged_at_utc TEXT,
    initiator_notified_at_utc TEXT,
    relinquished_at_utc TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at_utc);


CREATE TABLE IF NOT EXISTS selection_sets (
    selection_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    selection_kind TEXT NOT NULL CHECK(selection_kind IN ('RECEIPT','SAVED_ITEM')),
    items_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at_utc TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_selection_context
ON selection_sets(user_id,conversation_id,selection_kind,created_at_utc);

CREATE TABLE IF NOT EXISTS media_selection_sets (
    selection_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    media_type TEXT,
    items_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at_utc TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_media_selection_context
ON media_selection_sets(user_id,conversation_id,created_at_utc);

CREATE TABLE IF NOT EXISTS pending_selection_sets (
    selection_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at_utc TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_selection_context
ON pending_selection_sets(user_id,conversation_id,created_at_utc);

CREATE TABLE IF NOT EXISTS active_report_contexts (
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    report_kind TEXT NOT NULL,
    period TEXT,
    payload_json TEXT NOT NULL,
    spec_json TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id,conversation_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS reminder_events (
    event_id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'CREATED','DUE','DELIVERED','ACKNOWLEDGED','DEFERRED','RESCHEDULED',
        'FOLLOW_UP_DELIVERED','COMPLETED','CANCELLED'
    )),
    previous_state TEXT,
    new_state TEXT,
    previous_due_at_utc TEXT,
    new_due_at_utc TEXT,
    note TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(reminder_id) REFERENCES reminders(reminder_id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_events ON reminder_events(reminder_id,created_at_utc);

CREATE TABLE IF NOT EXISTS reminder_claim_events (
    claim_event_id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL,
    actor_user_id TEXT,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'CLAIMED','RELEASED','CLEARED_COMPLETED','CLEARED_CANCELLED'
    )),
    provider_message_id TEXT,
    reaction_text TEXT,
    note TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(reminder_id) REFERENCES reminders(reminder_id),
    FOREIGN KEY(actor_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_claim_events
ON reminder_claim_events(reminder_id,created_at_utc);

CREATE TABLE IF NOT EXISTS reminder_handoffs (
    handoff_id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL,
    from_user_id TEXT NOT NULL,
    to_user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','ACCEPTED','CANCELLED')) DEFAULT 'PENDING',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    accepted_at_utc TEXT,
    cancelled_at_utc TEXT,
    FOREIGN KEY(reminder_id) REFERENCES reminders(reminder_id),
    FOREIGN KEY(from_user_id) REFERENCES users(user_id),
    FOREIGN KEY(to_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_handoffs_pending
ON reminder_handoffs(reminder_id,status,created_at_utc);

CREATE TABLE IF NOT EXISTS savings_goals (
    goal_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    goal_name TEXT NOT NULL,
    target_amount_minor INTEGER,
    current_amount_minor INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'MYR',
    target_date TEXT,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','ACHIEVED','CANCELLED')) DEFAULT 'ACTIVE',
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(space_id, goal_name),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE TABLE IF NOT EXISTS money_buckets (
    bucket_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    bucket_name TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    currency TEXT NOT NULL DEFAULT 'MYR',
    notes TEXT,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(space_id, bucket_name),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE INDEX IF NOT EXISTS idx_money_buckets_owner ON money_buckets(owner_id, updated_at_utc);

CREATE TABLE IF NOT EXISTS leave_state (
    user_id TEXT PRIMARY KEY,
    balance_days REAL,
    as_of_date TEXT,
    notes TEXT,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);


CREATE TABLE IF NOT EXISTS work_roster (
    roster_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    work_date TEXT NOT NULL,
    shift_name TEXT NOT NULL,
    start_at_utc TEXT,
    end_at_utc TEXT,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('PLANNED','CONFIRMED','CANCELLED')) DEFAULT 'CONFIRMED',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_roster_owner_date ON work_roster(owner_id,work_date,status);

CREATE TABLE IF NOT EXISTS leave_records (
    leave_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    leave_date TEXT NOT NULL,
    end_date TEXT,
    leave_type TEXT NOT NULL DEFAULT 'ANNUAL_LEAVE'
        CHECK(leave_type IN ('ANNUAL_LEAVE','MEDICAL_LEAVE','OTHER_LEAVE')),
    portion TEXT NOT NULL DEFAULT 'FULL',
    status TEXT NOT NULL CHECK(status IN ('PLANNED','CONFIRMED','TAKEN','CANCELLED')) DEFAULT 'PLANNED',
    notes TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id,leave_date,portion),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);
CREATE INDEX IF NOT EXISTS idx_leave_owner_date ON leave_records(owner_id,leave_date,status);

CREATE TABLE IF NOT EXISTS diary_events (
    diary_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at_utc TEXT NOT NULL,
    end_at_utc TEXT,
    time_known INTEGER NOT NULL DEFAULT 1 CHECK(time_known IN (0,1)),
    timezone_name TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','CANCELLED')) DEFAULT 'ACTIVE',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);
CREATE INDEX IF NOT EXISTS idx_diary_space_time ON diary_events(space_id,start_at_utc,status);

CREATE TABLE IF NOT EXISTS diary_reminder_links (
    diary_id TEXT NOT NULL,
    reminder_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(diary_id,reminder_id),
    FOREIGN KEY(diary_id) REFERENCES diary_events(diary_id),
    FOREIGN KEY(reminder_id) REFERENCES reminders(reminder_id)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at_utc TEXT,
    end_at_utc TEXT,
    time_known INTEGER NOT NULL DEFAULT 0 CHECK(time_known IN (0,1)),
    timezone_name TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','LOCKED','CONFIRMED','CANCELLED')) DEFAULT 'DRAFT',
    source_plan_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(source_plan_id) REFERENCES plans(plan_id)
);
CREATE INDEX IF NOT EXISTS idx_plans_space_time ON plans(space_id,start_at_utc,status);

-- Tasks are first-class lifecycle objects, separate from plans and reminders.
-- Creation is idempotent by action_key; subsequent mutations are idempotent
-- through task_events. A task may reference a plan/reminder, but lifecycle
-- changes never mutate those linked objects implicitly.
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    source_message_id TEXT,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    title TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('OPEN','DONE','CANCELLED')) DEFAULT 'OPEN',
    assignee TEXT NOT NULL CHECK(assignee IN ('me','spouse','both','unassigned')) DEFAULT 'unassigned',
    due_at_utc TEXT,
    due_date_local TEXT,
    timezone_name TEXT NOT NULL,
    plan_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_space_status
ON tasks(space_id,status,updated_at_utc);
CREATE INDEX IF NOT EXISTS idx_tasks_plan
ON tasks(plan_id,status,updated_at_utc);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','UPDATED','COMPLETED','REOPENED','CANCELLED')),
    from_status TEXT,
    to_status TEXT NOT NULL CHECK(to_status IN ('OPEN','DONE','CANCELLED')),
    title_snapshot TEXT NOT NULL,
    notes_snapshot TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(actor_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_task_events_task
ON task_events(task_id,created_at_utc);

CREATE TABLE IF NOT EXISTS task_reminder_links (
    task_id TEXT NOT NULL,
    reminder_id TEXT NOT NULL UNIQUE,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(task_id,reminder_id),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(reminder_id) REFERENCES reminders(reminder_id)
);

CREATE TABLE IF NOT EXISTS plan_diary_links (
    plan_id TEXT PRIMARY KEY,
    diary_id TEXT NOT NULL UNIQUE,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id),
    FOREIGN KEY(diary_id) REFERENCES diary_events(diary_id)
);


CREATE TABLE IF NOT EXISTS schedule_conflicts (
    conflict_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at_utc TEXT NOT NULL,
    end_at_utc TEXT,
    timezone_name TEXT NOT NULL,
    notes TEXT,
    reminder_minutes_before INTEGER,
    roster_id TEXT,
    conflicting_diary_id TEXT,
    source_plan_id TEXT,
    conflict_kind TEXT NOT NULL DEFAULT 'WORK' CHECK(conflict_kind IN ('WORK','DIARY')),
    expires_at_utc TEXT,
    status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','CANCELLED')) DEFAULT 'OPEN',
    choice INTEGER,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(roster_id) REFERENCES work_roster(roster_id)
);

CREATE TABLE IF NOT EXISTS cashflow_baselines (
    user_id TEXT NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('MYR','SGD')),
    guaranteed_income_minor INTEGER NOT NULL DEFAULT 0,
    fixed_commitments_minor INTEGER NOT NULL DEFAULT 0,
    locked_allocations_minor INTEGER NOT NULL DEFAULT 0,
    reserves_minor INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id,currency),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_turns_conversation ON conversation_turns(conversation_id, created_at_utc);

CREATE TABLE IF NOT EXISTS outbound_messages (
    outbound_id TEXT PRIMARY KEY,
    source_message_id TEXT,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('TEXT','IMAGE','DOCUMENT')),
    text_body TEXT,
    local_path TEXT,
    mime_type TEXT,
    context_kind TEXT,
    context_id TEXT,
    provider_message_id TEXT,
    delivery_status TEXT NOT NULL CHECK(delivery_status IN ('PENDING','SENT','FAILED')) DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at_utc TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at_utc TEXT,
    job_reacted_at_utc TEXT,
    job_pinned_at_utc TEXT,
    job_pin_target TEXT,
    job_reaction_cleared_at_utc TEXT,
    job_unpinned_at_utc TEXT,
    job_failure_notice_at_utc TEXT,
    job_control_attempts INTEGER NOT NULL DEFAULT 0,
    job_control_next_attempt_at_utc TEXT,
    job_control_last_error TEXT,
    job_control_last_kind TEXT,
    job_control_failed_at_utc TEXT,
    job_unpin_failed_at_utc TEXT,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id)
);


CREATE INDEX IF NOT EXISTS idx_outbound_pending ON outbound_messages(delivery_status, created_at_utc);

CREATE TABLE IF NOT EXISTS ha_notification_outbox (
    notification_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    notify_service TEXT NOT NULL,
    reminder_id TEXT,
    handoff_id TEXT,
    title TEXT,
    message TEXT NOT NULL,
    data_json TEXT,
    delivery_status TEXT NOT NULL CHECK(delivery_status IN ('PENDING','SENT','FAILED')) DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at_utc TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at_utc TEXT,
    received_at_utc TEXT,
    received_device_id TEXT,
    FOREIGN KEY(user_id) REFERENCES users(user_id),
    FOREIGN KEY(reminder_id) REFERENCES reminders(reminder_id),
    FOREIGN KEY(handoff_id) REFERENCES reminder_handoffs(handoff_id)
);

CREATE INDEX IF NOT EXISTS idx_ha_notification_pending
ON ha_notification_outbox(delivery_status,next_attempt_at_utc,created_at_utc);

CREATE TABLE IF NOT EXISTS tool_execution_claims (
    action_key TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('STARTED','COMPLETED','UNCERTAIN')),
    result_json TEXT,
    attachments_json TEXT,
    started_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS tool_audit (
    audit_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL,
    source_message_id TEXT,
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('OK','ERROR')),
    latency_ms INTEGER,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_audit_message ON tool_audit(source_message_id, created_at_utc);

CREATE TABLE IF NOT EXISTS ai_usage (
    usage_id TEXT PRIMARY KEY,
    source_message_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_rounds INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    estimated_cost_usd REAL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id)
);


CREATE TABLE IF NOT EXISTS monitor_notifications (
    candidate_key TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    candidate_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    queued_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_owner
ON monitor_notifications(owner_id,queued_at_utc);

CREATE TABLE IF NOT EXISTS pending_error_reports (
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    target_outbound_id TEXT NOT NULL,
    target_provider_message_id TEXT,
    target_source_message_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id,conversation_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id),
    FOREIGN KEY(target_outbound_id) REFERENCES outbound_messages(outbound_id)
);

CREATE TABLE IF NOT EXISTS user_reported_errors (
    error_id TEXT PRIMARY KEY,
    reporter_user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    target_outbound_id TEXT NOT NULL,
    target_provider_message_id TEXT,
    target_source_message_id TEXT,
    user_explanation TEXT NOT NULL,
    detection_state TEXT NOT NULL
        CHECK(detection_state IN ('detected-by-Alex','reported-by-user','both')),
    bundle_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(reporter_user_id) REFERENCES users(user_id),
    FOREIGN KEY(target_outbound_id) REFERENCES outbound_messages(outbound_id)
);
CREATE INDEX IF NOT EXISTS idx_user_reported_errors_actor
ON user_reported_errors(reporter_user_id,created_at_utc);

CREATE TABLE IF NOT EXISTS diagnostic_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
