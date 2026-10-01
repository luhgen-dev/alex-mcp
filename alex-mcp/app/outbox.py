from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.request
from datetime import timedelta

import runtime_clock

from db import connect

EGRESS_URL = "http://127.0.0.1:5002/send"
DOCUMENT_NOTICE_ATTEMPTS = 3
_STARTUP_MANAGED_JOB_RECONCILED = False


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


def _row_value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _payload(row) -> dict:
    kind = row["kind"]
    message_id = _whatsapp_message_id(row["outbound_id"])
    reply_to = (
        row["source_message_id"]
        if row["source_message_id"] and not row["context_kind"]
        else None
    )
    common = {
        "to": row["conversation_id"],
        "message_id": message_id,
        "reply_to_message_id": reply_to,
        "reply_to_participant_jid": (
            _row_value(row, "source_sender_provider_jid")
            if reply_to and str(row["conversation_id"]).endswith("@g.us")
            else None
        ),
        "reply_to_text": _row_value(row, "source_raw_text") if reply_to else None,
    }
    if kind == "TEXT":
        return {**common, "kind": "text", "text": row["text_body"] or ""}

    path = row["local_path"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path or "missing attachment path")
    with open(path, "rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")
    mime = row["mime_type"] or (
        "image/jpeg" if kind == "IMAGE" else "application/octet-stream"
    )
    outbound_kind = (
        "image" if kind == "IMAGE"
        else ("audio" if str(mime).lower().startswith("audio/") else "document")
    )
    return {
        **common,
        "kind": outbound_kind,
        "file_b64": data,
        "mimetype": mime,
        "filename": os.path.basename(path),
        "caption": row["text_body"] or "",
    }


def _joined_row(conn, outbound_id: str):
    return conn.execute(
        """SELECT o.*,i.sender_provider_jid AS source_sender_provider_jid,
                  i.raw_text AS source_raw_text
           FROM outbound_messages o
           LEFT JOIN inbound_messages i ON i.message_id=o.source_message_id
           WHERE o.outbound_id=?""",
        (outbound_id,),
    ).fetchone()


def _control(row, kind: str, emoji: str | None = None) -> bool:
    if not row["source_message_id"]:
        return False
    payload = {
        "to": row["conversation_id"],
        "kind": kind,
        "target_message_id": row["source_message_id"],
    }
    if str(row["conversation_id"]).endswith("@g.us") and row["source_sender_provider_jid"]:
        payload["target_participant_jid"] = row["source_sender_provider_jid"]
    if kind == "reaction":
        payload["emoji"] = emoji or ""
    ok, _detail = _send(payload)
    return ok


def _control_outbound(row, kind: str) -> bool:
    """Pin/unpin a message Alex itself sent, using its provider or deterministic ID."""
    target = str(row["provider_message_id"] or "") or _whatsapp_message_id(row["outbound_id"])
    if not target:
        return False
    payload = {
        "to": row["conversation_id"],
        "kind": kind,
        "target_message_id": target,
    }
    ok, _detail = _send(payload)
    return ok


def _reconcile_pending_item_markers(conn) -> None:
    rows = conn.execute(
        """SELECT o.*,p.status AS pending_status
           FROM outbound_messages o
           JOIN pending_items p ON p.item_id=o.context_id
           WHERE o.context_kind='PENDING_ITEM'
             AND (
                p.status='PENDING'
                OR (
                    p.status IN ('RESOLVED','CANCELLED')
                    AND (
                        (o.job_reacted_at_utc IS NOT NULL AND o.job_reaction_cleared_at_utc IS NULL)
                        OR (o.job_pinned_at_utc IS NOT NULL AND o.job_unpinned_at_utc IS NULL)
                    )
                )
             )"""
    ).fetchall()
    for row in rows:
        if row["pending_status"] == "PENDING":
            _ensure_unresolved_markers(conn, row)
        else:
            _cleanup_resolved_markers(conn, row)


def _reconcile_reminder_pins(conn) -> None:
    """Keep only unresolved, unclaimed fired Family reminders pinned."""
    rows = conn.execute(
        """SELECT o.*,r.status AS reminder_status,r.claimable,r.claimed_by_user_id
           FROM outbound_messages o
           JOIN reminders r ON r.reminder_id=o.context_id
           WHERE o.context_kind='REMINDER_INITIAL'
             AND o.delivery_status='SENT'
             AND o.job_pinned_at_utc IS NOT NULL
             AND o.job_unpinned_at_utc IS NULL"""
    ).fetchall()
    for row in rows:
        unresolved = (
            str(row["conversation_id"]).endswith("@g.us")
            and int(row["claimable"] or 0) == 1
            and not row["claimed_by_user_id"]
            and row["reminder_status"] == "DUE"
        )
        if not unresolved and _control_outbound(row, "unpin"):
            conn.execute(
                "UPDATE outbound_messages SET job_unpinned_at_utc=? WHERE outbound_id=?",
                (_now(), row["outbound_id"]),
            )
            conn.commit()


def _ensure_unresolved_markers(conn, row) -> None:
    """Mark a managed unresolved source message with ⏳ + pin."""
    managed = row["kind"] == "DOCUMENT" or row["context_kind"] == "PENDING_ITEM"
    if not managed or not row["source_message_id"]:
        return
    now = _now()
    if not row["job_reacted_at_utc"]:
        if _control(row, "reaction", "⏳"):
            conn.execute(
                "UPDATE outbound_messages SET job_reacted_at_utc=? WHERE outbound_id=?",
                (now, row["outbound_id"]),
            )
            conn.commit()
            row = _joined_row(conn, row["outbound_id"])
    if not row["job_pinned_at_utc"]:
        if _control(row, "pin"):
            conn.execute(
                "UPDATE outbound_messages SET job_pinned_at_utc=? WHERE outbound_id=?",
                (now, row["outbound_id"]),
            )
            conn.commit()


def _cleanup_resolved_markers(conn, row) -> None:
    """Remove managed ⏳/pin only when the managed item is truly resolved."""
    managed = row["kind"] == "DOCUMENT" or row["context_kind"] == "PENDING_ITEM"
    if not managed or not row["source_message_id"]:
        return
    now = _now()
    if row["job_reacted_at_utc"] and not row["job_reaction_cleared_at_utc"]:
        if _control(row, "reaction", ""):
            conn.execute(
                "UPDATE outbound_messages SET job_reaction_cleared_at_utc=? WHERE outbound_id=?",
                (now, row["outbound_id"]),
            )
            conn.commit()
            row = _joined_row(conn, row["outbound_id"])
    if row["job_pinned_at_utc"] and not row["job_unpinned_at_utc"]:
        if _control(row, "unpin"):
            conn.execute(
                "UPDATE outbound_messages SET job_unpinned_at_utc=? WHERE outbound_id=?",
                (now, row["outbound_id"]),
            )
            conn.commit()


def _queue_document_notice(conn, row, permanent: bool = False) -> None:
    if row["job_failure_notice_at_utc"]:
        return
    attempts = int(row["attempt_count"] or 0)
    if not permanent and attempts < DOCUMENT_NOTICE_ATTEMPTS:
        return
    text = (
        "I couldn't deliver that document because the generated file is unavailable. "
        "The request is still unresolved."
        if permanent
        else "That document has not delivered yet. The original request remains marked as unresolved while delivery retries continue."
    )
    import uuid
    conn.execute(
        """INSERT INTO outbound_messages(
               outbound_id,source_message_id,conversation_id,kind,text_body
           ) VALUES(?,?,?,'TEXT',?)""",
        (str(uuid.uuid4()), row["source_message_id"], row["conversation_id"], text),
    )
    conn.execute(
        "UPDATE outbound_messages SET job_failure_notice_at_utc=? WHERE outbound_id=?",
        (_now(), row["outbound_id"]),
    )
    conn.commit()


def _reconcile_managed_jobs(conn) -> None:
    """Restore invariant after restart: unresolved marked; SENT cleaned up."""
    rows = conn.execute(
        """SELECT o.*,i.sender_provider_jid AS source_sender_provider_jid,
                  i.raw_text AS source_raw_text
           FROM outbound_messages o
           LEFT JOIN inbound_messages i ON i.message_id=o.source_message_id
           WHERE o.kind='DOCUMENT' AND o.source_message_id IS NOT NULL
             AND (
                (o.delivery_status IN ('PENDING','FAILED') AND o.attempt_count>0)
                OR (o.delivery_status='SENT' AND (
                    (o.job_reacted_at_utc IS NOT NULL AND o.job_reaction_cleared_at_utc IS NULL)
                    OR (o.job_pinned_at_utc IS NOT NULL AND o.job_unpinned_at_utc IS NULL)
                ))
             )
           ORDER BY o.created_at_utc"""
    ).fetchall()
    for row in rows:
        if row["delivery_status"] == "SENT":
            _cleanup_resolved_markers(conn, row)
        else:
            _ensure_unresolved_markers(conn, row)
            _queue_document_notice(
                conn, _joined_row(conn, row["outbound_id"]),
                permanent=row["delivery_status"] == "FAILED",
            )


def sweep():
    global _STARTUP_MANAGED_JOB_RECONCILED
    conn = connect()
    try:
        now_iso = _now()
        rows = conn.execute(
            """SELECT o.*,i.sender_provider_jid AS source_sender_provider_jid,
                      i.raw_text AS source_raw_text
               FROM outbound_messages o
               LEFT JOIN inbound_messages i ON i.message_id=o.source_message_id
               WHERE o.delivery_status='PENDING'
                 AND (o.next_attempt_at_utc IS NULL OR o.next_attempt_at_utc<=?)
               ORDER BY o.created_at_utc,o.rowid LIMIT 10""",
            (now_iso,),
        ).fetchall()

        for row in rows:
            permanent_error = False
            if row["context_kind"] in (
                "REMINDER_INITIAL", "REMINDER_FOLLOWUP",
                "REMINDER_CLAIMANT_FOLLOWUP", "REMINDER_FAMILY_RESURFACE",
            ) and row["context_id"]:
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
                    conn.commit()
                    continue

            if row["context_kind"] == "PENDING_ITEM":
                _ensure_unresolved_markers(conn, row)
                row = _joined_row(conn, row["outbound_id"])

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
                    (
                        attempts, delivered, _provider_message_id(detail),
                        row["outbound_id"],
                    ),
                )
                if row["context_kind"] in (
                    "REMINDER_INITIAL", "REMINDER_FOLLOWUP",
                    "REMINDER_CLAIMANT_FOLLOWUP", "REMINDER_FAMILY_RESURFACE",
                ) and row["context_id"]:
                    if row["context_kind"] == "REMINDER_INITIAL":
                        conn.execute(
                            "UPDATE reminders SET last_fired_at_utc=? WHERE reminder_id=?",
                            (delivered, row["context_id"]),
                        )
                        event_type = "DELIVERED"
                    elif row["context_kind"] == "REMINDER_FOLLOWUP":
                        conn.execute(
                            "UPDATE reminders SET last_follow_up_at_utc=? WHERE reminder_id=?",
                            (delivered, row["context_id"]),
                        )
                        event_type = "FOLLOW_UP_DELIVERED"
                    elif row["context_kind"] == "REMINDER_CLAIMANT_FOLLOWUP":
                        event_type = "CLAIMANT_FOLLOW_UP_DELIVERED"
                    else:
                        event_type = "FAMILY_RESURFACED_DELIVERED"
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
                conn.commit()
                refreshed = _joined_row(conn, row["outbound_id"])
                if refreshed and refreshed["kind"] == "DOCUMENT":
                    _cleanup_resolved_markers(conn, refreshed)
                if (
                    refreshed
                    and refreshed["context_kind"] == "REMINDER_INITIAL"
                    and str(refreshed["conversation_id"]).endswith("@g.us")
                    and refreshed["context_id"]
                ):
                    reminder = conn.execute(
                        """SELECT status,claimable,claimed_by_user_id FROM reminders
                           WHERE reminder_id=?""",
                        (refreshed["context_id"],),
                    ).fetchone()
                    if (
                        reminder
                        and reminder["status"] == "DUE"
                        and int(reminder["claimable"] or 0) == 1
                        and not reminder["claimed_by_user_id"]
                        and not refreshed["job_pinned_at_utc"]
                        and _control_outbound(refreshed, "pin")
                    ):
                        conn.execute(
                            "UPDATE outbound_messages SET job_pinned_at_utc=? WHERE outbound_id=?",
                            (_now(), refreshed["outbound_id"]),
                        )
                        conn.commit()

            elif permanent_error:
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status='FAILED',attempt_count=?,
                       last_error=?,next_attempt_at_utc=NULL WHERE outbound_id=?""",
                    (attempts, detail[:1000], row["outbound_id"]),
                )
                conn.commit()
                refreshed = _joined_row(conn, row["outbound_id"])
                if refreshed and refreshed["kind"] == "DOCUMENT":
                    _ensure_unresolved_markers(conn, refreshed)
                    _queue_document_notice(
                        conn, _joined_row(conn, row["outbound_id"]), permanent=True
                    )
            else:
                delay = min(300, 2 ** min(attempts, 8))
                next_try = (
                    runtime_clock.now_utc() + timedelta(seconds=delay)
                ).isoformat()
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status='PENDING',attempt_count=?,
                       last_error=?,next_attempt_at_utc=? WHERE outbound_id=?""",
                    (attempts, detail[:1000], next_try, row["outbound_id"]),
                )
                conn.commit()
                refreshed = _joined_row(conn, row["outbound_id"])
                if refreshed and refreshed["kind"] == "DOCUMENT":
                    _ensure_unresolved_markers(conn, refreshed)
                    _queue_document_notice(
                        conn, _joined_row(conn, row["outbound_id"]), permanent=False
                    )

        _reconcile_pending_item_markers(conn)
        _reconcile_reminder_pins(conn)

        # Restart reconciliation is exactly that: restart recovery. Normal
        # document attempts already apply/clean their own markers above. Running
        # this before every 0.5 s sweep can serially block reminders/text behind
        # control-call timeouts when WhatsApp is unavailable.
        if not _STARTUP_MANAGED_JOB_RECONCILED:
            try:
                _reconcile_managed_jobs(conn)
            finally:
                _STARTUP_MANAGED_JOB_RECONCILED = True
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
