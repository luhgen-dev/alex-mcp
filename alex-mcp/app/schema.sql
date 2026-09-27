PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inbound_messages (
    message_id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL DEFAULT 'WHATSAPP',
    conversation_id TEXT NOT NULL,
    conversation_type TEXT NOT NULL DEFAULT 'DIRECT_DM',
    sender_phone TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    processing_state TEXT NOT NULL CHECK(processing_state IN ('RECEIVED','PROCESSING','COMPLETED','FAILED')) DEFAULT 'RECEIVED',
    cached_response TEXT,
    last_error TEXT,
    received_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at_utc TEXT
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
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_message_id, media_type),
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_media_sha ON media_objects(sha256);

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
    last_fired_at_utc TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id),
    FOREIGN KEY(owner_id) REFERENCES users(user_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at_utc);

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
    UNIQUE(owner_id, goal_name),
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
    UNIQUE(owner_id, bucket_name),
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
    delivery_status TEXT NOT NULL CHECK(delivery_status IN ('PENDING','SENT','FAILED')) DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at_utc TEXT,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_outbound_pending ON outbound_messages(delivery_status, created_at_utc);

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
    output_tokens INTEGER NOT NULL DEFAULT 0,
    tool_rounds INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_message_id) REFERENCES inbound_messages(message_id)
);

CREATE TABLE IF NOT EXISTS diagnostic_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
