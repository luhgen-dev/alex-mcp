from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

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
        _ensure_column(conn, "outbound_messages", "next_attempt_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "presence_aware", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "reminders", "delivery_class", "TEXT NOT NULL DEFAULT 'routine'")
        _ensure_column(conn, "reminders", "follow_up_after_hours", "INTEGER NOT NULL DEFAULT 24")
        _ensure_column(conn, "reminders", "acknowledged_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "last_follow_up_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "next_delivery_at_utc", "TEXT")
        _ensure_column(conn, "reminders", "defer_reason", "TEXT")
        _ensure_column(conn, "outbound_messages", "context_kind", "TEXT")
        _ensure_column(conn, "outbound_messages", "context_id", "TEXT")
        _ensure_column(conn, "outbound_messages", "provider_message_id", "TEXT")
        _ensure_column(conn, "inbound_messages", "quoted_message_id", "TEXT")
        _ensure_column(conn, "ai_usage", "estimated_cost_usd", "REAL")
        _ensure_column(conn, "ai_usage", "cached_input_tokens", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "ai_usage", "reasoning_tokens", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "ai_usage", "model_calls", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "media_objects", "transcript_meta_json", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbound_provider_message "
            "ON outbound_messages(conversation_id,provider_message_id)"
        )
        _ensure_column(conn, "schedule_conflicts", "conflicting_diary_id", "TEXT")
        _ensure_column(conn, "schedule_conflicts", "conflict_kind", "TEXT NOT NULL DEFAULT 'WORK'")
        _ensure_column(conn, "schedule_conflicts", "expires_at_utc", "TEXT")
        _ensure_column(conn, "leave_records", "end_date", "TEXT")
        _ensure_column(conn, "leave_records", "leave_type", "TEXT NOT NULL DEFAULT 'ANNUAL_LEAVE'")
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
                message_id,provider_name,conversation_id,conversation_type,sender_phone,raw_text,
                quoted_message_id,processing_state,attempt_count,processing_started_at_utc
               ) VALUES(?,?,?,?,?,?,?, 'PROCESSING',1,?)""",
            (
                payload["message_id"],
                payload.get("provider","WHATSAPP"),
                payload["conversation_id"],
                payload.get("conversation_type","DIRECT_DM"),
                normalize_phone(payload["sender_phone"]),
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


def resolve_quoted_context(conversation_id: str, quoted_message_id: str | None,
                           sender_phone: str | None = None) -> dict | None:
    """Resolve a WhatsApp reply inside the same conversation without model guessing."""
    if not quoted_message_id:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """SELECT outbound_id,source_message_id,text_body,context_kind,context_id,
                      provider_message_id
               FROM outbound_messages
               WHERE conversation_id=? AND provider_message_id=?
               ORDER BY delivered_at_utc DESC,created_at_utc DESC LIMIT 1""",
            (conversation_id, quoted_message_id),
        ).fetchone()
        if row:
            result = {
                "quoted_alex_text": row["text_body"] or "",
                "source_message_id": row["source_message_id"],
                "context_kind": row["context_kind"],
                "context_id": row["context_id"],
            }
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
        if sender_phone:
            user_row = conn.execute(
                """SELECT message_id,raw_text FROM inbound_messages
                   WHERE message_id=? AND conversation_id=? AND sender_phone=? LIMIT 1""",
                (quoted_message_id, conversation_id, normalize_phone(sender_phone)),
            ).fetchone()
            if user_row and str(user_row["raw_text"] or "").strip():
                return {
                    "quoted_user_text": str(user_row["raw_text"]).strip()[:2000],
                    "source_message_id": user_row["message_id"],
                }
        return None
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
