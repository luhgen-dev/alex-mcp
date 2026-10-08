from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import runtime_clock

from config import DATA_DIR, get_settings, normalize_phone
from context import ActorContext

DB_PATH = os.path.join(DATA_DIR, "alex_mcp.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
INBOUND_PROCESSING_LEASE_SECONDS = 900
RESTART_INTERRUPTED_PREFIX = "restart_interrupted:"


def utc_now() -> str:
    return runtime_clock.utc_iso()


def connect(path: str | None = None) -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


ROUTING_CATALOGUE = [
    (("mydin","tesco","lotus","aeon","giant","jaya grocer","barang dapur","grocery","groceries"), "groceries", "FAMILY_SHARED", None),
    (("pasar","market","wet market","pasar malam"), "marketing_allowance", "FAMILY_SHARED", None),
    (("manjaku","diaper","lampin","susu","baby"), "kids_supplies", "FAMILY_SHARED", None),
    (("school fee","yuran sekolah","yuran","tuition","sekolah"), "education", "FAMILY_SHARED", None),
    (("petrol","minyak","shell","petronas","caltex","bhp"), "fuel", "FAMILY_SHARED", None),
    (("car installment","car instalment","kereta loan","ansuran kereta","motorbike installment","motor installment","motosikal","ansuran motor"), "vehicle_loan", "FAMILY_SHARED", None),
    (("toll","touch n go","tng","parking","parkir"), "transport", "FAMILY_SHARED", None),
    (("road tax","cukai jalan","insurance","insurans","car service","servis kereta","tyre","tayar"), "vehicle_upkeep", "FAMILY_SHARED", 80000),
    (("maintenance fee","management fee","yuran penyelenggaraan"), "housing", "FAMILY_SHARED", None),
    (("tnb","electric","elektrik","bil elektrik"), "utilities", "FAMILY_SHARED", None),
    (("water bill","syabas","bil air","ranhill","air bill","indah water","sewerage"), "utilities", "FAMILY_SHARED", None),
    (("unifi","wifi","internet","streamyx","time fibre"), "internet", "FAMILY_SHARED", None),
    (("phone bill","bil telefon","maxis","celcom","digi","umobile","hotlink","topup","top up","reload"), "telco", "FAMILY_SHARED", None),
    (("astro","netflix","spotify","disney","subscription","langganan"), "subscriptions", "FAMILY_SHARED", None),
    (("doctor","clinic","klinik","hospital","doktor"), "medical", None, 150000),
    (("pharmacy","farmasi","guardian","watsons","ubat","medicine"), "pharmacy", None, 30000),
    (("credit card","kad kredit","cc payment","card payment","pawn","pajak","pajak gadai","pawn shop","tebus"), "debt_payment", None, None),
    (("shopee","lazada","online order","bnpl","spaylater"), "online_shopping", None, 0),
    (("vep",), "sg_vehicle", "FAMILY_SHARED", None),
    (("saman","fine","summons","compound"), "fines", "FAMILY_SHARED", None),
    (("lunch sg","food sg","makan sg","canteen","makan","lunch","breakfast","dinner","kopitiam","mamak"), "food", None, None),
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _recover_interrupted_inbound(conn: sqlite3.Connection) -> int:
    """Quarantine turns left PROCESSING by an add-on restart.

    A previous mutation may already have committed, so these message ids are
    never replayed automatically. Instead Alex sends one durable recovery note
    asking the user to verify state before trying the action again.
    """
    rows = conn.execute(
        """SELECT message_id,conversation_id FROM inbound_messages
           WHERE processing_state='PROCESSING'"""
    ).fetchall()
    if not rows:
        return 0
    stamp = utc_now()
    for row in rows:
        message_id = row["message_id"]
        conn.execute(
            """UPDATE inbound_messages
               SET processing_state='FAILED',last_error=?
               WHERE message_id=? AND processing_state='PROCESSING'""",
            (RESTART_INTERRUPTED_PREFIX + stamp, message_id),
        )
        conn.execute(
            """INSERT INTO outbound_messages(
                outbound_id,source_message_id,conversation_id,kind,text_body,
                context_kind,context_id
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), message_id, row["conversation_id"], "TEXT",
                "Alex restarted while I was processing that message. I won't repeat the action automatically because it may already have happened. Ask me to check it, or resend only if you want me to try again.",
                "INBOUND_RECOVERY", message_id,
            ),
        )
    return len(rows)


def initialize() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = connect()
    try:
        conn.executescript(schema)
        # Small additive migrations keep persistent /data safe across app updates.
        _ensure_column(conn, "inbound_messages", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "inbound_messages", "processing_started_at_utc", "TEXT")
        _ensure_column(conn, "inbound_messages", "sender_provider_jid", "TEXT")
        _ensure_column(conn, "outbound_messages", "next_attempt_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_reacted_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_pinned_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_pin_target", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_reaction_cleared_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_unpinned_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_failure_notice_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_control_attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "outbound_messages", "job_control_next_attempt_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_control_last_error", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_control_last_kind", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_control_failed_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "job_unpin_failed_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "presence_aware", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "reminders", "delivery_class", "TEXT NOT NULL DEFAULT 'routine'")
        _ensure_column(conn, "reminders", "follow_up_after_hours", "INTEGER NOT NULL DEFAULT 24")
        _ensure_column(conn, "reminders", "acknowledged_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "last_follow_up_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "next_delivery_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "defer_reason", "TEXT")
        _ensure_column(conn, "reminders", "claimable", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "reminders", "claimed_by_user_id", "TEXT")
        _ensure_column(conn, "reminders", "claimed_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "claimant_follow_up_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "family_resurfaced_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "seen_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "seen_by_user_id", "TEXT")
        _ensure_column(conn, "reminders", "nudged_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "initiator_notified_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "relinquished_at_utc", "TEXT")
        _ensure_column(conn, "outbound_messages", "context_kind", "TEXT")
        _ensure_column(conn, "outbound_messages", "context_id", "TEXT")
        _ensure_column(conn, "outbound_messages", "provider_message_id", "TEXT")
        _ensure_column(conn, "inbound_messages", "quoted_message_id", "TEXT")
        _ensure_column(conn, "ai_usage", "estimated_cost_usd", "REAL")
        _ensure_column(conn, "ai_usage", "cached_input_tokens", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "ai_usage", "reasoning_tokens", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "ai_usage", "model_calls", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "media_objects", "transcript_meta_json", "TEXT")
        _ensure_column(conn, "pending_items", "accumulated_text", "TEXT")
        _ensure_column(conn, "pending_items", "routing_json", "TEXT")
        _ensure_column(conn, "selection_sets", "read_scope", "TEXT")
        # v0.5.37: quote-bound error reporting gives every unresolved draft a
        # durable identity so only replies to its pinned prompt can complete it.
        _ensure_column(conn, "pending_error_reports", "error_draft_id", "TEXT")
        conn.execute(
            """UPDATE pending_error_reports
               SET error_draft_id='ERRD-' || upper(hex(randomblob(8)))
               WHERE error_draft_id IS NULL OR TRIM(error_draft_id)=''"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_error_draft
               ON pending_error_reports(error_draft_id)
               WHERE error_draft_id IS NOT NULL"""
        )
        # v0.5.13 reminder drafts carry the user's trusted clarification
        # fragments as one durable request. Backfill existing drafts from their
        # original inbound text without touching other pending-item kinds.
        conn.execute(
            """UPDATE pending_items
               SET accumulated_text=(
                   SELECT i.raw_text FROM inbound_messages i
                   WHERE i.message_id=pending_items.source_message_id
               )
               WHERE kind='REMINDER_DRAFT'
                 AND (accumulated_text IS NULL OR TRIM(accumulated_text)='')"""
        )

        # v0.5.16 establishes one active reminder draft per owner/chat. Older
        # draft questions were previously allowed to remain PENDING together,
        # which made short replies and pins ambiguous. Keep the newest draft
        # and deterministically supersede the rest once on upgrade.
        draft_invariant_migration = "v0516_single_active_reminder_draft"
        draft_invariant_done = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key=?",
            (draft_invariant_migration,),
        ).fetchone()
        if not draft_invariant_done:
            rows = conn.execute(
                """SELECT rowid,item_id,owner_id,conversation_id
                   FROM pending_items
                   WHERE kind='REMINDER_DRAFT' AND status='PENDING'
                   ORDER BY owner_id,conversation_id,created_at_utc DESC,rowid DESC"""
            ).fetchall()
            seen_draft_chats: set[tuple[str, str]] = set()
            superseded: list[str] = []
            for row in rows:
                key = (str(row["owner_id"]), str(row["conversation_id"]))
                if key in seen_draft_chats:
                    superseded.append(str(row["item_id"]))
                else:
                    seen_draft_chats.add(key)
            if superseded:
                marks = ",".join("?" for _ in superseded)
                conn.execute(
                    f"""UPDATE pending_items
                        SET status='CANCELLED',resolved_at_utc=?,
                            resolution_message_id='superseded_v0516'
                        WHERE item_id IN ({marks}) AND status='PENDING'""",
                    [utc_now()] + superseded,
                )
            conn.execute(
                "INSERT INTO schema_migrations(migration_key) VALUES(?)",
                (draft_invariant_migration,),
            )

        # v0.5.6 used a malformed Baileys pin payload but still recorded
        # job_pinned_at_utc after the transport returned success. Reset those
        # false-positive flags exactly once so v0.5.7 reconciliation can issue
        # real pins for still-unresolved managed items/reminders.
        pin_migration = "v057_reset_false_pin_flags"
        already_reset = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key=?",
            (pin_migration,),
        ).fetchone()
        if not already_reset:
            conn.execute(
                """UPDATE outbound_messages
                   SET job_pinned_at_utc=NULL,job_unpinned_at_utc=NULL
                   WHERE job_pinned_at_utc IS NOT NULL"""
            )
            conn.execute(
                "INSERT INTO schema_migrations(migration_key) VALUES(?)",
                (pin_migration,),
            )

        # v0.5.6 treated post-due acknowledgement as a terminal ACK state.
        # v0.5.7 makes acknowledgement metadata-only, so reopen those legacy
        # non-terminal rows exactly once and preserve their acknowledgement time
        # as seen metadata. COMP/CANC are untouched.
        ack_migration = "v057_reopen_legacy_ack_reminders"
        already_reopened = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key=?",
            (ack_migration,),
        ).fetchone()
        if not already_reopened:
            conn.execute(
                """UPDATE reminders
                   SET status='DUE',
                       seen_at_utc=COALESCE(seen_at_utc,acknowledged_at_utc)
                   WHERE status='ACK'"""
            )
            conn.execute(
                "INSERT INTO schema_migrations(migration_key) VALUES(?)",
                (ack_migration,),
            )

        # Privacy is a scope, never an expense category. Clean historical rows
        # created by the old presenter bug without changing amount/date/scope.
        conn.execute(
            """UPDATE financial_events SET category=NULL
               WHERE category IS NOT NULL
                 AND LOWER(REPLACE(category,'-','_')) IN (
                   'private','privately','personal','family','shared',
                   'family_shared','just_for_me','only_for_me','my_private'
                 )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbound_provider_message "
            "ON outbound_messages(conversation_id,provider_message_id)"
        )
        _ensure_column(conn, "schedule_conflicts", "conflicting_diary_id", "TEXT")
        _ensure_column(conn, "schedule_conflicts", "conflict_kind", "TEXT NOT NULL DEFAULT 'WORK'")
        _ensure_column(conn, "schedule_conflicts", "expires_at_utc", "TEXT")
        _ensure_column(conn, "leave_records", "end_date", "TEXT")
        _ensure_column(conn, "leave_records", "leave_type", "TEXT NOT NULL DEFAULT 'ANNUAL_LEAVE'")
        _ensure_column(conn, "leave_records", "space_id", "TEXT")
        conn.execute(
            """UPDATE leave_records
               SET space_id=CASE owner_id
                   WHEN 'USR_HUSBAND' THEN 'HUSBAND_PVT'
                   WHEN 'USR_WIFE' THEN 'WIFE_PVT'
                   ELSE space_id END
               WHERE space_id IS NULL OR TRIM(space_id)=''"""
        )
        # This index depends on the additive space_id migration above. Keep it
        # out of schema.sql so upgrades from pre-0.5.2 databases cannot fail
        # before _ensure_column() has a chance to add the column.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leave_owner_space_date "
            "ON leave_records(owner_id,space_id,leave_date,status)"
        )
        _ensure_column(conn, "diary_events", "time_known", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "plans", "time_known", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "schedule_conflicts", "source_plan_id", "TEXT")
        _recover_interrupted_inbound(conn)
        # v0.4.4 Phase-0 trace retention: only the compact per-turn trace rows
        # are pruned. Real tool audit history is never deleted here.
        conn.execute(
            "DELETE FROM tool_audit WHERE tool_name='_turn_trace' "
            "AND created_at_utc < datetime('now','-14 days')"
        )
        conn.commit()
        conn.execute("BEGIN")
        conn.executemany("INSERT OR IGNORE INTO users(user_id,display_name) VALUES(?,?)", [
            ("USR_HUSBAND", "Husband"),
            ("USR_WIFE", "Wife"),
        ])
        conn.executemany("INSERT OR IGNORE INTO spaces(space_id,space_name) VALUES(?,?)", [
            ("FAMILY_SHARED", "Family Shared"),
            ("HUSBAND_PVT", "Husband Private"),
            ("WIFE_PVT", "Wife Private"),
        ])
        conn.executemany("INSERT OR IGNORE INTO memberships(user_id,space_id) VALUES(?,?)", [
            ("USR_HUSBAND","FAMILY_SHARED"), ("USR_HUSBAND","HUSBAND_PVT"),
            ("USR_WIFE","FAMILY_SHARED"), ("USR_WIFE","WIFE_PVT"),
        ])
        for user_id in ("USR_HUSBAND", "USR_WIFE"):
            for keywords, category, force_space, threshold in ROUTING_CATALOGUE:
                for keyword in keywords:
                    rule_id = f"{user_id}:{keyword}"
                    conn.execute(
                        """INSERT INTO routing_rules(rule_id,user_id,keyword,category,force_space_id,high_value_threshold_minor)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(user_id,keyword) DO UPDATE SET
                             category=excluded.category,
                             force_space_id=excluded.force_space_id,
                             high_value_threshold_minor=excluded.high_value_threshold_minor""",
                        (rule_id, user_id, keyword, category, force_space, threshold),
                    )
        conn.commit()
        apply_identity_options(conn)
    finally:
        conn.close()


def apply_identity_options(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    settings = get_settings()
    mapping = [
        ("USR_HUSBAND", normalize_phone(settings.husband_phone)),
        ("USR_WIFE", normalize_phone(settings.wife_phone)),
    ]
    try:
        for user_id, phone in mapping:
            if not phone:
                continue
            current = conn.execute(
                "SELECT history_id,phone_number FROM user_phone_history WHERE user_id=? AND valid_to_utc IS NULL",
                (user_id,),
            ).fetchone()
            if current and current["phone_number"] == phone:
                continue
            clash = conn.execute(
                "SELECT user_id FROM user_phone_history WHERE phone_number=? AND valid_to_utc IS NULL",
                (phone,),
            ).fetchone()
            if clash and clash["user_id"] != user_id:
                raise ValueError(f"Phone {phone} is already assigned to another Alex user")
            if current:
                conn.execute(
                    "UPDATE user_phone_history SET valid_to_utc=? WHERE history_id=?",
                    (utc_now(), current["history_id"]),
                )
            conn.execute(
                "INSERT INTO user_phone_history(history_id,user_id,phone_number) VALUES(?,?,?)",
                (str(uuid.uuid4()), user_id, phone),
            )
        conn.commit()
    finally:
        if own:
            conn.close()


def resolve_actor(phone: str, conversation_id: str, conversation_type: str,
                  source_message_id: str, media_ids: list[str] | None = None) -> ActorContext:
    clean = normalize_phone(phone)
    conn = connect()
    try:
        row = conn.execute(
            """SELECT u.user_id FROM users u
               JOIN user_phone_history p ON p.user_id=u.user_id
               WHERE p.phone_number=? AND p.valid_to_utc IS NULL LIMIT 1""",
            (clean,),
        ).fetchone()
        if not row:
            raise PermissionError("Sender is not configured as an Alex household user")
        user_id = row["user_id"]
        memberships = tuple(r["space_id"] for r in conn.execute(
            "SELECT space_id FROM memberships WHERE user_id=? ORDER BY space_id", (user_id,)
        ).fetchall())
        private = "HUSBAND_PVT" if user_id == "USR_HUSBAND" else "WIFE_PVT"
        if private not in memberships or "FAMILY_SHARED" not in memberships:
            raise PermissionError("Required Alex space membership is missing")
        # Channel origin is a structural read boundary, not an LLM choice.
        # Family-group turns may see only FAMILY_SHARED. Private DMs may see
        # the caller's private space plus FAMILY_SHARED.
        spaces = ("FAMILY_SHARED",) if conversation_type == "GROUP" else memberships
        return ActorContext(
            user_id=user_id,
            phone=clean,
            allowed_spaces=spaces,
            private_space=private,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            source_message_id=source_message_id,
            media_ids=tuple(media_ids or []),
            timezone=get_settings().timezone,
        )
    finally:
        conn.close()


def claim_inbound(payload: dict) -> str:
    conn = connect()
    try:
        existing = conn.execute(
            """SELECT processing_state,cached_response,attempt_count,processing_started_at_utc,last_error
               FROM inbound_messages WHERE message_id=?""",
            (payload["message_id"],),
        ).fetchone()
        now = runtime_clock.now_utc()
        if existing:
            state = existing["processing_state"]
            if state == "COMPLETED":
                return "DUPLICATE"
            if (
                state == "FAILED"
                and str(existing["last_error"] or "").startswith(RESTART_INTERRUPTED_PREFIX)
            ):
                # A prior process died mid-turn. The associated mutation may
                # already have completed, so transport redelivery must never
                # re-run this exact WhatsApp message automatically.
                return "DUPLICATE"
            if existing["processing_started_at_utc"]:
                try:
                    started = datetime.fromisoformat(existing["processing_started_at_utc"].replace("Z", "+00:00"))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    age = (now - started).total_seconds()
                    if state == "PROCESSING" and age < INBOUND_PROCESSING_LEASE_SECONDS:
                        return "DUPLICATE"
                    # The Node bridge retries non-2xx responses immediately. A
                    # short FAILED cooldown makes those transport retries no-op
                    # while still allowing a genuinely stale redelivery later.
                    if state == "FAILED" and age < 30:
                        return "DUPLICATE"
                except Exception:
                    if state in {"PROCESSING", "FAILED"}:
                        return "DUPLICATE"
            conn.execute(
                """UPDATE inbound_messages SET processing_state='PROCESSING',
                   attempt_count=attempt_count+1,processing_started_at_utc=?,last_error=NULL
                   WHERE message_id=?""",
                (now.isoformat(), payload["message_id"]),
            )
            conn.commit()
            return "CLAIMED"
        conn.execute(
            """INSERT INTO inbound_messages(
                message_id,provider_name,conversation_id,conversation_type,sender_phone,
                sender_provider_jid,raw_text,quoted_message_id,
                processing_state,attempt_count,processing_started_at_utc
               ) VALUES(?,?,?,?,?,?,?,?, 'PROCESSING',1,?)""",
            (
                payload["message_id"],
                payload.get("provider","WHATSAPP"),
                payload["conversation_id"],
                payload.get("conversation_type","DIRECT_DM"),
                normalize_phone(payload["sender_phone"]),
                payload.get("sender_provider_jid") or None,
                payload.get("text","") or "",
                payload.get("quoted_message_id") or None,
                now.isoformat(),
            ),
        )
        conn.commit()
        return "CLAIMED"
    finally:
        conn.close()


def touch_inbound_processing(message_id: str) -> None:
    """Refresh the processing lease around slow media/model stages.

    First-use Whisper download plus multiple local passes can take several
    minutes. Refreshing the lease prevents a concurrent transport retry from
    re-entering the same WhatsApp message while the original worker is alive.
    """
    conn = connect()
    try:
        conn.execute(
            """UPDATE inbound_messages
               SET processing_started_at_utc=?
               WHERE message_id=? AND processing_state='PROCESSING'""",
            (runtime_clock.now_utc().isoformat(), message_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_inbound(message_id: str, response: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE inbound_messages SET processing_state='COMPLETED',cached_response=?,completed_at_utc=? WHERE message_id=?",
            (response, utc_now(), message_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_inbound(message_id: str, error: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE inbound_messages SET processing_state='FAILED',last_error=? WHERE message_id=?",
            (error[:1000], message_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_turn(user_id: str, conversation_id: str, role: str, content: str) -> None:
    if not content:
        return
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO conversation_turns(turn_id,user_id,conversation_id,role,content) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), user_id, conversation_id, role, content[:12000]),
        )
        conn.commit()
    finally:
        conn.close()


def recent_turns(conversation_id: str, limit: int) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT role,content FROM conversation_turns
               WHERE conversation_id=? ORDER BY created_at_utc DESC,rowid DESC LIMIT ?""",
            (conversation_id, max(2, min(20, limit))),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def queue_outbound(conversation_id: str, kind: str, text: str | None = None,
                   local_path: str | None = None, mime_type: str | None = None,
                   source_message_id: str | None = None,
                   context_kind: str | None = None,
                   context_id: str | None = None) -> str:
    oid = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO outbound_messages(
                outbound_id,source_message_id,conversation_id,kind,text_body,local_path,mime_type,
                context_kind,context_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (oid, source_message_id, conversation_id, kind, text, local_path, mime_type,
             context_kind, context_id),
        )
        conn.commit()
        return oid
    finally:
        conn.close()


def has_completed_mutation(source_message_id: str) -> bool:
    """Whether this exact inbound turn has a verified completed mutation.

    tool_execution_claims contains only mutating tools. Joining it to the
    per-turn tool audit lets ingress close a deferred voice item only after the
    typed clarification actually changed state, never after a read, failed tool,
    empty model reply, or retrieval request.
    """
    message_id = str(source_message_id or "").strip()
    if not message_id:
        return False
    conn = connect()
    try:
        row = conn.execute(
            """SELECT 1
               FROM tool_audit a
               JOIN tool_execution_claims c ON c.action_key=a.action_key
               WHERE a.source_message_id=?
                 AND a.status='OK'
                 AND c.state='COMPLETED'
               LIMIT 1""",
            (message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def has_completed_non_pending_mutation(source_message_id: str) -> bool:
    """Verified mutation excluding pending-item lifecycle bookkeeping.

    A typed clarification of one deferred voice item must not auto-resolve that
    item merely because the same turn resolved/cancelled a different pending
    item. Explicit pending resolve/cancel tools already update their own target.
    """
    message_id = str(source_message_id or "").strip()
    if not message_id:
        return False
    conn = connect()
    try:
        row = conn.execute(
            """SELECT 1
               FROM tool_audit a
               JOIN tool_execution_claims c ON c.action_key=a.action_key
               WHERE a.source_message_id=?
                 AND a.status='OK'
                 AND c.state='COMPLETED'
                 AND c.tool_name NOT IN ('resolve_pending_item','cancel_pending_item')
               LIMIT 1""",
            (message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_usage(source_message_id: str, provider: str, model: str,
                 input_tokens: int, output_tokens: int, tool_rounds: int,
                 latency_ms: int, estimated_cost_usd: float | None = None,
                 cached_input_tokens: int = 0, reasoning_tokens: int = 0,
                 model_calls: int = 0) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO ai_usage(
                usage_id,source_message_id,provider,model,input_tokens,cached_input_tokens,
                output_tokens,reasoning_tokens,model_calls,tool_rounds,latency_ms,estimated_cost_usd
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), source_message_id, provider, model,
             input_tokens, cached_input_tokens, output_tokens, reasoning_tokens,
             model_calls, tool_rounds, latency_ms, estimated_cost_usd),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_quoted_context(
    conversation_id: str,
    quoted_message_id: str | None,
    sender_phone: str | None = None,
    *,
    allow_group_peer_quote: bool = False,
) -> dict | None:
    """Resolve a WhatsApp reply inside the same conversation without guessing.

    By default a quoted user message must belong to the same authenticated
    sender.  The one exception is an explicitly @mentioned Family Shared group
    turn: that user may intentionally hand Alex another household member's
    already-visible group message as context.  Callers must opt into that case.
    """
    if not quoted_message_id:
        return None
    conn = connect()
    try:
        # Baileys 7 may deliver the same authenticated DM as either a phone JID
        # or a LID JID. Alex historically stores its own DM outbounds on the
        # phone JID. Resolve quotes across only the current actor's own two DM
        # forms; group conversations remain strictly exact-chat scoped.
        conversation_ids = [str(conversation_id or "")]
        if sender_phone and str(conversation_id or "").endswith("@lid"):
            phone = normalize_phone(sender_phone).lstrip("+")
            phone_jid = f"{phone}@s.whatsapp.net" if phone else ""
            if phone_jid and phone_jid not in conversation_ids:
                conversation_ids.append(phone_jid)

        row = None
        for candidate_conversation in conversation_ids:
            row = conn.execute(
                """SELECT outbound_id,source_message_id,text_body,context_kind,context_id,
                          provider_message_id
                   FROM outbound_messages
                   WHERE conversation_id=? AND provider_message_id=?
                   ORDER BY delivered_at_utc DESC,created_at_utc DESC LIMIT 1""",
                (candidate_conversation, quoted_message_id),
            ).fetchone()
            if row:
                break
        if not row:
            # WhatsApp/Baileys can quote Alex's deterministic bridge ID rather
            # than the provider_message_id stored after delivery. Use the same
            # deterministic fallback as reaction binding, still confined to the
            # current actor's own eligible conversation identities.
            wanted = str(quoted_message_id or "")
            for candidate_conversation in conversation_ids:
                candidates = conn.execute(
                    """SELECT outbound_id,source_message_id,text_body,context_kind,context_id,
                              provider_message_id
                       FROM outbound_messages
                       WHERE conversation_id=? AND delivery_status='SENT'
                       ORDER BY delivered_at_utc DESC,created_at_utc DESC LIMIT 100""",
                    (candidate_conversation,),
                ).fetchall()
                for candidate in candidates:
                    expected = (
                        "ALEX"
                        + hashlib.sha256(str(candidate["outbound_id"]).encode("utf-8"))
                        .hexdigest().upper()[:28]
                    )
                    if wanted and wanted in {
                        str(candidate["provider_message_id"] or ""), expected
                    }:
                        row = candidate
                        break
                if row:
                    break
        if row:
            result = {
                "quoted_alex_text": row["text_body"] or "",
                "outbound_id": row["outbound_id"],
                "provider_message_id": row["provider_message_id"],
                "source_message_id": row["source_message_id"],
                "context_kind": row["context_kind"],
                "context_id": row["context_id"],
            }
            if row["context_kind"] == "REPORT" and row["context_id"]:
                try:
                    report_context = json.loads(row["context_id"])
                except (TypeError, json.JSONDecodeError):
                    report_context = None
                if isinstance(report_context, dict):
                    result["report_context"] = report_context
            if row["source_message_id"]:
                events = conn.execute(
                    """SELECT event_id,status,event_type,amount_minor,currency,description,category
                       FROM financial_events
                       WHERE source_message_id=?
                         AND status IN ('ACTIVE','PENDING_HUMAN_REVIEW')
                       ORDER BY created_at_utc DESC""",
                    (row["source_message_id"],),
                ).fetchall()
                if len(events) == 1:
                    event = events[0]
                    result["financial_event"] = {
                        "event_id": event["event_id"],
                        "status": event["status"],
                        "type": event["event_type"],
                        "amount": event["amount_minor"] / 100 if event["amount_minor"] is not None else None,
                        "currency": event["currency"],
                        "description": event["description"],
                        "category": event["category"],
                    }
            return result

        # A user may reply to their own earlier instruction while attaching a
        # file. Bind only the same authenticated sender in the same conversation.
        user_row = None
        quoted_group_peer = False
        if sender_phone:
            for candidate_conversation in conversation_ids:
                user_row = conn.execute(
                    """SELECT message_id,raw_text,sender_phone FROM inbound_messages
                       WHERE message_id=? AND conversation_id=? AND sender_phone=? LIMIT 1""",
                    (
                        quoted_message_id,
                        candidate_conversation,
                        normalize_phone(sender_phone),
                    ),
                ).fetchone()
                if user_row:
                    break

        # Explicit @mention in the configured Family Shared group may hand Alex
        # another household member's already-visible message.  Never enable
        # this for DMs or ordinary reply-to-Alex continuation.
        if (
            not user_row
            and allow_group_peer_quote
            and str(conversation_id or "").endswith("@g.us")
        ):
            user_row = conn.execute(
                """SELECT message_id,raw_text,sender_phone FROM inbound_messages
                   WHERE message_id=? AND conversation_id=? LIMIT 1""",
                (quoted_message_id, conversation_id),
            ).fetchone()
            quoted_group_peer = bool(user_row)

        if user_row:
            raw_text = str(user_row["raw_text"] or "").strip()
            if raw_text:
                result = {
                    "quoted_user_text": raw_text[:2000],
                    "source_message_id": user_row["message_id"],
                }
                if quoted_group_peer:
                    result["quoted_group_peer"] = True
                return result
            media_row = conn.execute(
                """SELECT media_id,media_type FROM media_objects
                   WHERE source_message_id=? ORDER BY created_at_utc DESC LIMIT 1""",
                (user_row["message_id"],),
            ).fetchone()
            if media_row:
                result = {
                    "quoted_user_text": "",
                    "source_message_id": user_row["message_id"],
                    "quoted_media_id": media_row["media_id"],
                    "quoted_media_type": media_row["media_type"],
                }
                if quoted_group_peer:
                    result["quoted_group_peer"] = True
                return result
        return None
    finally:
        conn.close()



def resolve_recent_outbound_context(conversation_id: str, context_kind: str,
                                    max_age_seconds: int = 600) -> dict | None:
    """Resolve the latest delivered/queued Alex context in this conversation."""
    bounded = max(15, min(3600, int(max_age_seconds)))
    conn = connect()
    try:
        row = conn.execute(
            """SELECT outbound_id,source_message_id,text_body,context_kind,context_id,
                      provider_message_id,created_at_utc
               FROM outbound_messages
               WHERE conversation_id=?
                 AND datetime(created_at_utc)>=datetime('now', ?)
               ORDER BY created_at_utc DESC,rowid DESC LIMIT 1""",
            (conversation_id, f"-{bounded} seconds"),
        ).fetchone()
        if not row or row["context_kind"] != context_kind:
            return None
        return dict(row)
    finally:
        conn.close()


def latest_outbound_for_context(
    conversation_id: str, context_kind: str, context_id: str,
    max_age_seconds: int = 600,
) -> dict | None:
    """Return the newest outbound for one exact durable context."""
    bounded = max(15, min(3600, int(max_age_seconds)))
    conn = connect()
    try:
        row = conn.execute(
            """SELECT outbound_id,source_message_id,text_body,context_kind,context_id,
                      provider_message_id,created_at_utc
               FROM outbound_messages
               WHERE conversation_id=? AND context_kind=? AND context_id=?
                 AND datetime(created_at_utc)>=datetime('now', ?)
               ORDER BY created_at_utc DESC,rowid DESC LIMIT 1""",
            (
                conversation_id, str(context_kind or ""), str(context_id or ""),
                f"-{bounded} seconds",
            ),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_recent_instruction_context(conversation_id: str, sender_phone: str,
                                       current_message_id: str,
                                       max_age_seconds: int = 120) -> dict | None:
    """Short same-sender pairing for a captionless attachment after an instruction."""
    bounded = max(15, min(300, int(max_age_seconds)))
    conn = connect()
    try:
        row = conn.execute(
            """SELECT message_id,raw_text FROM inbound_messages
               WHERE conversation_id=? AND sender_phone=? AND message_id<>?
                 AND processing_state='COMPLETED'
                 AND TRIM(raw_text)<>''
                 AND datetime(received_at_utc)>=datetime('now', ?)
               ORDER BY received_at_utc DESC,rowid DESC LIMIT 1""",
            (
                conversation_id, normalize_phone(sender_phone), current_message_id,
                f"-{bounded} seconds",
            ),
        ).fetchone()
        if not row:
            return None
        return {
            "recent_user_instruction": str(row["raw_text"]).strip()[:2000],
            "source_message_id": row["message_id"],
            "pairing": "same_sender_recent_instruction",
        }
    finally:
        conn.close()


def create_pending_item(actor: ActorContext, kind: str, media_id: str | None = None,
                        note: str | None = None) -> dict:
    item_kind = str(kind or "OTHER").strip().upper()[:40]
    conn = connect()
    try:
        existing = conn.execute(
            """SELECT * FROM pending_items WHERE source_message_id=? AND kind=? LIMIT 1""",
            (actor.source_message_id, item_kind),
        ).fetchone()
        if existing:
            return dict(existing)
        if item_kind == "REMINDER_DRAFT":
            # One unresolved reminder conversation per owner/chat. Starting a
            # new reminder request supersedes older unfinished drafts so a short
            # date/time answer has exactly one safe target.
            conn.execute(
                """UPDATE pending_items
                   SET status='CANCELLED',resolved_at_utc=?,resolution_message_id=?
                   WHERE owner_id=? AND conversation_id=?
                     AND kind='REMINDER_DRAFT' AND status='PENDING'""",
                (
                    utc_now(), actor.source_message_id,
                    actor.user_id, actor.conversation_id,
                ),
            )
        item_id = str(uuid.uuid4())
        accumulated_text = (
            str(
                getattr(actor, "reminder_context_text", "")
                or getattr(actor, "trusted_text", "")
                or ""
            ).strip()
            if item_kind == "REMINDER_DRAFT" else None
        )
        routing_json = (
            str(getattr(actor, "reminder_routing_json", "") or "").strip() or None
            if item_kind == "REMINDER_DRAFT" else None
        )
        conn.execute(
            """INSERT INTO pending_items(
                   item_id,kind,owner_id,conversation_id,source_message_id,media_id,
                   note,accumulated_text,routing_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                item_id, item_kind, actor.user_id, actor.conversation_id,
                actor.source_message_id, media_id, note, accumulated_text,
                routing_json,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM pending_items WHERE item_id=?", (item_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def append_pending_item_text(item_id: str, owner_id: str, text: str) -> dict:
    """Append one user-authored clarification fragment to a pending draft."""
    fragment = str(text or "").strip()
    conn = connect()
    try:
        row = conn.execute(
            """SELECT * FROM pending_items
               WHERE item_id=? AND owner_id=? AND status='PENDING' LIMIT 1""",
            (item_id, owner_id),
        ).fetchone()
        if not row:
            raise ValueError("pending item is no longer unresolved")
        if str(row["kind"] or "").upper() != "REMINDER_DRAFT":
            return dict(row)
        current = str(row["accumulated_text"] or "").strip()
        if fragment:
            combined = (current + "\n" + fragment).strip() if current else fragment
            # Household reminder requests are intentionally compact; cap the
            # durable context while preserving all normal clarification turns.
            combined = combined[-4000:]
            conn.execute(
                "UPDATE pending_items SET accumulated_text=? WHERE item_id=?",
                (combined, item_id),
            )
            conn.commit()
        updated = conn.execute(
            "SELECT * FROM pending_items WHERE item_id=?", (item_id,)
        ).fetchone()
        return dict(updated)
    finally:
        conn.close()


def latest_pending_item(actor: ActorContext, kind: str,
                        max_age_seconds: int = 600) -> dict | None:
    """Return the newest unresolved owner item in this conversation."""
    bounded = max(30, min(3600, int(max_age_seconds)))
    conn = connect()
    try:
        row = conn.execute(
            """SELECT p.*,i.raw_text AS original_text
               FROM pending_items p
               LEFT JOIN inbound_messages i ON i.message_id=p.source_message_id
               WHERE p.owner_id=? AND p.conversation_id=? AND p.status='PENDING'
                 AND p.kind=?
                 AND datetime(p.created_at_utc)>=datetime('now', ?)
               ORDER BY p.created_at_utc DESC,p.rowid DESC LIMIT 1""",
            (
                actor.user_id, actor.conversation_id, str(kind).strip().upper(),
                f"-{bounded} seconds",
            ),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def single_pending_item(actor: ActorContext, kind: str,
                        max_age_seconds: int = 600) -> dict | None:
    """Return one unresolved owner/chat item only when the target is unique."""
    bounded = max(30, min(3600, int(max_age_seconds)))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT p.*,i.raw_text AS original_text
               FROM pending_items p
               LEFT JOIN inbound_messages i ON i.message_id=p.source_message_id
               WHERE p.owner_id=? AND p.conversation_id=? AND p.status='PENDING'
                 AND p.kind=?
                 AND datetime(p.created_at_utc)>=datetime('now', ?)
               ORDER BY p.created_at_utc DESC,p.rowid DESC LIMIT 2""",
            (
                actor.user_id, actor.conversation_id, str(kind).strip().upper(),
                f"-{bounded} seconds",
            ),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None
    finally:
        conn.close()


def pending_item_for_reference(actor: ActorContext, quoted_context: dict | None) -> dict | None:
    if not quoted_context:
        return None
    item_id = None
    if quoted_context.get("context_kind") == "PENDING_ITEM":
        item_id = str(quoted_context.get("context_id") or "") or None
    source_message_id = str(quoted_context.get("source_message_id") or "") or None
    media_id = str(quoted_context.get("quoted_media_id") or "") or None
    conn = connect()
    try:
        clauses = ["owner_id=?", "status='PENDING'"]
        args: list = [actor.user_id]
        if item_id:
            clauses.append("item_id=?")
            args.append(item_id)
        elif media_id:
            clauses.append("media_id=?")
            args.append(media_id)
        elif source_message_id:
            clauses.append("source_message_id=?")
            args.append(source_message_id)
        else:
            return None
        row = conn.execute(
            "SELECT * FROM pending_items WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at_utc DESC LIMIT 1",
            args,
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def pending_item_history_for_reference(
    actor: ActorContext, quoted_context: dict | None
) -> dict | None:
    """Resolve the exact owner-bound pending-item reference regardless of state.

    Used only when the user explicitly quotes an old Alex prompt. This lets
    ingress reject an expired/cancelled offer deterministically instead of
    allowing an unrelated newer workflow to consume the reply.
    """
    if not quoted_context or quoted_context.get("context_kind") != "PENDING_ITEM":
        return None
    item_id = str(quoted_context.get("context_id") or "").strip()
    if not item_id:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """SELECT * FROM pending_items
               WHERE item_id=? AND owner_id=? LIMIT 1""",
            (item_id, actor.user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_pending_item_status(item_id: str, owner_id: str, status: str,
                            resolution_message_id: str) -> bool:
    target = str(status or "").strip().upper()
    if target not in {"RESOLVED", "CANCELLED"}:
        raise ValueError("pending item status must be RESOLVED or CANCELLED")
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE pending_items
               SET status=?,resolved_at_utc=?,resolution_message_id=?
               WHERE item_id=? AND owner_id=? AND status='PENDING'""",
            (target, utc_now(), resolution_message_id, item_id, owner_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def resolve_pending_item(item_id: str, owner_id: str, resolution_message_id: str) -> bool:
    return set_pending_item_status(
        item_id, owner_id, "RESOLVED", resolution_message_id
    )


def cancel_pending_item(item_id: str, owner_id: str, resolution_message_id: str) -> bool:
    return set_pending_item_status(
        item_id, owner_id, "CANCELLED", resolution_message_id
    )


def _pending_local_display(value: str, timezone_name: str) -> tuple[str | None, str | None]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(timezone_name))
        hour = local.strftime("%I").lstrip("0") or "12"
        return (
            local.isoformat(),
            f"{local.day} {local.strftime('%B %Y')}, {hour}:{local.strftime('%M')} {local.strftime('%p')}",
        )
    except Exception:
        return None, None


def _store_pending_selection(conn, actor: ActorContext, item_ids: list[str]) -> None:
    if not item_ids:
        return
    expires = (runtime_clock.now_utc() + timedelta(minutes=10)).isoformat()
    conn.execute(
        """INSERT INTO pending_selection_sets(
               selection_id,user_id,conversation_id,items_json,
               created_at_utc,expires_at_utc
           ) VALUES(?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), actor.user_id, actor.conversation_id,
            json.dumps(item_ids), utc_now(), expires,
        ),
    )


def pending_item_by_choice(actor: ActorContext, choice: int) -> dict:
    if actor.conversation_type == "GROUP":
        raise PermissionError("Pending personal items are available only in the owner's DM")
    index = int(choice)
    if index < 1:
        raise ValueError("choice must be 1 or greater")
    conn = connect()
    try:
        row = conn.execute(
            """SELECT items_json FROM pending_selection_sets
               WHERE user_id=? AND conversation_id=? AND expires_at_utc>?
               ORDER BY created_at_utc DESC,rowid DESC LIMIT 1""",
            (actor.user_id, actor.conversation_id, utc_now()),
        ).fetchone()
        if not row:
            raise ValueError("no pending-item numbered list is waiting")
        ids = json.loads(row["items_json"] or "[]")
        if index > len(ids):
            raise ValueError("choice is outside the latest pending-item list")
        item = conn.execute(
            """SELECT * FROM pending_items
               WHERE item_id=? AND owner_id=? AND status='PENDING'""",
            (ids[index - 1], actor.user_id),
        ).fetchone()
        if not item:
            raise ValueError("that pending item is no longer unresolved")
        return dict(item)
    finally:
        conn.close()


def list_pending_items(actor: ActorContext, kind: str | None = None, limit: int = 20) -> list[dict]:
    if actor.conversation_type == "GROUP":
        raise PermissionError("Pending personal items are available only in the owner's DM")
    conn = connect()
    try:
        where = ["p.owner_id=?", "p.status='PENDING'"]
        args: list = [actor.user_id]
        effective_kind = str(kind or "VOICE").strip().upper()
        where.append("p.kind=?")
        args.append(effective_kind)
        rows = conn.execute(
            """SELECT p.item_id,p.kind,p.source_message_id,p.media_id,p.created_at_utc,
                      m.media_type,m.mime_type
               FROM pending_items p
               LEFT JOIN media_objects m ON m.media_id=p.media_id
               WHERE """ + " AND ".join(where)
            + " ORDER BY p.created_at_utc DESC LIMIT ?",
            args + [max(1, min(100, int(limit)))],
        ).fetchall()
        items = []
        for index, row in enumerate(rows, 1):
            item = dict(row)
            local_iso, display_time = _pending_local_display(
                item.get("created_at_utc"), actor.timezone
            )
            item["choice"] = index
            item["created_local"] = local_iso
            item["display_time"] = display_time
            items.append(item)
        if items:
            _store_pending_selection(
                conn, actor, [str(item["item_id"]) for item in items]
            )
            conn.commit()
        return items
    finally:
        conn.close()


def current_month_ai_cost(provider: str | None = None) -> float:
    conn = connect()
    try:
        month = runtime_clock.now_utc().strftime("%Y-%m")
        sql = """SELECT COALESCE(SUM(estimated_cost_usd),0) AS cost
                 FROM ai_usage
                 WHERE substr(created_at_utc,1,7)=?"""
        params: list[str] = [month]
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        row = conn.execute(sql, params).fetchone()
        return float(row["cost"] or 0.0)
    finally:
        conn.close()
