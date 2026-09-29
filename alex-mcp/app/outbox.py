from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import runtime_clock

from db import connect

EGRESS_URL = "http://127.0.0.1:5002/send"


def _now():
    return runtime_clock.utc_iso()


def _send(payload: dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        EGRESS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, r.read().decode("utf-8")
    except Exception as exc:
        return False, str(exc)


def _provider_message_id(detail: str) -> str | None:
    try:
        payload = json.loads(detail or "{}")
    except Exception:
        return None
    value = payload.get("message_id") if isinstance(payload, dict) else None
    return str(value)[:200] if value else None


def _whatsapp_message_id(outbound_id: str) -> str:
    """Stable Baileys message id so transport retries reuse the same WA key."""
    digest = hashlib.sha256(str(outbound_id).encode("utf-8")).hexdigest().upper()
    return "ALEX" + digest[:28]


def _payload(row) -> dict:
    kind = row["kind"]
    message_id = _whatsapp_message_id(row["outbound_id"])
    if kind == "TEXT":
        return {
            "to": row["conversation_id"], "kind": "text",
            "text": row["text_body"] or "", "message_id": message_id,
        }
    path = row["local_path"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path or "missing attachment path")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    mime = row["mime_type"] or ("image/jpeg" if kind == "IMAGE" else "application/octet-stream")
    outbound_kind = (
        "image" if kind == "IMAGE"
        else ("audio" if str(mime).lower().startswith("audio/") else "document")
    )
    return {
        "to": row["conversation_id"],
        "kind": outbound_kind,
        "file_b64": data,
        "mimetype": mime,
        "filename": os.path.basename(path),
        "caption": row["text_body"] or "",
        "message_id": message_id,
    }


def sweep():
    conn = connect()
    try:
        now_iso = _now()
        rows = conn.execute(
            """SELECT * FROM outbound_messages
               WHERE delivery_status='PENDING'
                 AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc<=?)
               ORDER BY created_at_utc,rowid LIMIT 10""",
            (now_iso,),
        ).fetchall()
        for row in rows:
            permanent_error = False
            # Reminder rows are cancellable while still queued. Never deliver a
            # stale reminder after the user completed/cancelled it.
            if row["context_kind"] in ("REMINDER_INITIAL", "REMINDER_FOLLOWUP") and row["context_id"]:
                reminder = conn.execute(
                    "SELECT status FROM reminders WHERE reminder_id=?",
                    (row["context_id"],),
                ).fetchone()
                if not reminder or reminder["status"] in ("COMP", "CANC"):
                    conn.execute(
                        """UPDATE outbound_messages SET delivery_status='FAILED',
                           attempt_count=attempt_count+1,last_error='cancelled_before_delivery',
                           next_attempt_at_utc=NULL WHERE outbound_id=?""",
                        (row["outbound_id"],),
                    )
                    continue
            try:
                payload = _payload(row)
                ok, detail = _send(payload)
            except FileNotFoundError as exc:
                ok, detail, permanent_error = False, str(exc), True
            except Exception as exc:
                ok, detail = False, str(exc)
            attempts = int(row["attempt_count"] or 0) + 1
            if ok:
                delivered = _now()
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status='SENT',attempt_count=?,
                       delivered_at_utc=?,last_error=NULL,next_attempt_at_utc=NULL,
                       provider_message_id=? WHERE outbound_id=?""",
                    (attempts, delivered, _provider_message_id(detail), row["outbound_id"]),
                )
                if row["context_kind"] in ("REMINDER_INITIAL", "REMINDER_FOLLOWUP") and row["context_id"]:
                    if row["context_kind"] == "REMINDER_INITIAL":
                        conn.execute(
                            "UPDATE reminders SET last_fired_at_utc=? WHERE reminder_id=?",
                            (delivered, row["context_id"]),
                        )
                        event_type = "DELIVERED"
                    else:
                        conn.execute(
                            "UPDATE reminders SET last_follow_up_at_utc=? WHERE reminder_id=?",
                            (delivered, row["context_id"]),
                        )
                        event_type = "FOLLOW_UP_DELIVERED"
                    conn.execute(
                        """INSERT INTO reminder_events(
                            event_id,reminder_id,event_type,note
                           ) VALUES(lower(hex(randomblob(16))),?,?,?)""",
                        (row["context_id"], event_type, "confirmed by WhatsApp egress"),
                    )
                elif row["context_kind"] == "MONITOR" and row["context_id"]:
                    conn.execute(
                        "UPDATE monitor_notifications SET delivered_at_utc=? WHERE candidate_key=?",
                        (delivered, row["context_id"]),
                    )
            elif permanent_error:
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status='FAILED',attempt_count=?,
                       last_error=?,next_attempt_at_utc=NULL WHERE outbound_id=?""",
                    (attempts, detail[:1000], row["outbound_id"]),
                )
            else:
                # Transport/network outages must not silently consume a reminder.
                # Keep the durable row pending and back off locally without AI/token use.
                delay = min(300, 2 ** min(attempts, 8))
                next_try = (runtime_clock.now_utc() + timedelta(seconds=delay)).isoformat()
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status='PENDING',attempt_count=?,
                       last_error=?,next_attempt_at_utc=? WHERE outbound_id=?""",
                    (attempts, detail[:1000], next_try, row["outbound_id"]),
                )
        conn.commit()
    finally:
        conn.close()


def main():
    while True:
        try:
            sweep()
        except Exception as exc:
            print(f"[Alex MCP outbox] {exc}", flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
