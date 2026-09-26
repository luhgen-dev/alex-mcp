from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from config import DATA_DIR, get_settings, normalize_phone
from context import ActorContext

DB_PATH = os.path.join(DATA_DIR, "alex_mcp.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | None = None) -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


ROUTING_CATALOGUE = [
    (("mydin","lotus","tesco","aeon","giant","jaya grocer","grocery","groceries","barang dapur"), "groceries", "FAMILY_SHARED", None),
    (("tnb","electric","elektrik","bil elektrik","water bill","bil air","ranhill","indah water","sewerage"), "utilities", "FAMILY_SHARED", None),
    (("maxis","digi","hotlink","umobile","phone bill","top up","topup","reload"), "telco", "FAMILY_SHARED", None),
    (("parking","parkir","tng","touch n go","toll"), "transport", "FAMILY_SHARED", None),
    (("petrol","shell","petronas","caltex","bhp","minyak"), "fuel", "FAMILY_SHARED", None),
    (("school fee","yuran sekolah","tuition","sekolah"), "education", "FAMILY_SHARED", None),
    (("car installment","car instalment","kereta loan","ansuran kereta","motor installment","motosikal"), "vehicle_loan", "FAMILY_SHARED", None),
    (("car service","servis kereta","road tax","cukai jalan","insurance","insurans","tyre","tayar"), "vehicle_upkeep", "FAMILY_SHARED", 80000),
    (("netflix","spotify","astro","disney","subscription"), "subscriptions", "FAMILY_SHARED", None),
    (("doctor","clinic","klinik","hospital","doktor"), "medical", None, 150000),
    (("lunch","breakfast","dinner","mamak","kopitiam","makan","canteen"), "food", None, None),
]


def initialize() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = connect()
    try:
        conn.executescript(schema)
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
        spaces = tuple(r["space_id"] for r in conn.execute(
            "SELECT space_id FROM memberships WHERE user_id=? ORDER BY space_id", (user_id,)
        ).fetchall())
        private = "HUSBAND_PVT" if user_id == "USR_HUSBAND" else "WIFE_PVT"
        if private not in spaces:
            raise PermissionError("Private space membership is missing")
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
            "SELECT processing_state,cached_response FROM inbound_messages WHERE message_id=?",
            (payload["message_id"],),
        ).fetchone()
        if existing:
            return "DUPLICATE"
        conn.execute(
            """INSERT INTO inbound_messages(
                message_id,provider_name,conversation_id,conversation_type,sender_phone,raw_text,processing_state
               ) VALUES(?,?,?,?,?,?, 'PROCESSING')""",
            (
                payload["message_id"],
                payload.get("provider","WHATSAPP"),
                payload["conversation_id"],
                payload.get("conversation_type","DIRECT_DM"),
                normalize_phone(payload["sender_phone"]),
                payload.get("text","") or "",
            ),
        )
        conn.commit()
        return "CLAIMED"
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
                   source_message_id: str | None = None) -> str:
    oid = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO outbound_messages(
                outbound_id,source_message_id,conversation_id,kind,text_body,local_path,mime_type
               ) VALUES(?,?,?,?,?,?,?)""",
            (oid, source_message_id, conversation_id, kind, text, local_path, mime_type),
        )
        conn.commit()
        return oid
    finally:
        conn.close()


def record_usage(source_message_id: str, provider: str, model: str,
                 input_tokens: int, output_tokens: int, tool_rounds: int, latency_ms: int) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO ai_usage(
                usage_id,source_message_id,provider,model,input_tokens,output_tokens,tool_rounds,latency_ms
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), source_message_id, provider, model,
             input_tokens, output_tokens, tool_rounds, latency_ms),
        )
        conn.commit()
    finally:
        conn.close()
