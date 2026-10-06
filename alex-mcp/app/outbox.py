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
DOCUMENT_NOTICE_ATTEMPTS = 3
CONTROL_MAX_ATTEMPTS = 5
CONTROL_BACKOFF_SECONDS = (5, 15, 60, 300, 900)
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


def _control_result(row, kind: str,
                    emoji: str | None = None) -> tuple[bool, str]:
    if not row["source_message_id"]:
        return False, "missing source_message_id"
    payload = {
        "to": row["conversation_id"],
        "kind": kind,
        "target_message_id": row["source_message_id"],
    }
    if (
        str(row["conversation_id"]).endswith("@g.us")
        and _row_value(row, "source_sender_provider_jid")
    ):
        payload["target_participant_jid"] = row["source_sender_provider_jid"]
    if kind == "reaction":
        payload["emoji"] = emoji or ""
    return _send(payload)


def _control(row, kind: str, emoji: str | None = None) -> bool:
    """Compatibility wrapper for direct tests/callers."""
    return _control_result(row, kind, emoji)[0]


def _control_outbound_result(
    row, kind: str, emoji: str | None = None
) -> tuple[bool, str]:
    """Control a message Alex itself sent, preserving transport detail."""
    target = str(row["provider_message_id"] or "") or _whatsapp_message_id(row["outbound_id"])
    if not target:
        return False, "missing outbound target message id"
    payload = {
        "to": row["conversation_id"],
        "kind": kind,
        "target_message_id": target,
        "target_from_me": True,
    }
    if kind == "reaction":
        payload["emoji"] = emoji or ""
    return _send(payload)


def _control_outbound(row, kind: str, emoji: str | None = None) -> bool:
    """Compatibility wrapper for direct tests/callers."""
    return _control_outbound_result(row, kind, emoji)[0]


def _control_retry_ready(row) -> bool:
    """Whether this row may make another WhatsApp marker control attempt now."""
    if _row_value(row, "job_control_failed_at_utc"):
        return False
    next_at = _row_value(row, "job_control_next_attempt_at_utc")
    if not next_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(next_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= runtime_clock.now_utc().astimezone(timezone.utc)
    except Exception:
        return True


def _record_control_attempt(conn, row, kind: str,
                            ok: bool, detail: str) -> bool:
    """Persist control outcome so a bridge failure can never hot-loop silently."""
    if ok:
        conn.execute(
            """UPDATE outbound_messages
               SET job_control_attempts=0,
                   job_control_next_attempt_at_utc=NULL,
                   job_control_last_error=NULL,
                   job_control_last_kind=?,
                   job_control_failed_at_utc=NULL,
                   job_unpin_failed_at_utc=CASE
                       WHEN ?='unpin' THEN NULL ELSE job_unpin_failed_at_utc END
               WHERE outbound_id=?""",
            (kind, kind, row["outbound_id"]),
        )
        conn.commit()
        return True

    attempts = int(_row_value(row, "job_control_attempts", 0) or 0) + 1
    error = str(detail or "WhatsApp control failed")[:1000]
    failed_at = _now() if attempts >= CONTROL_MAX_ATTEMPTS else None
    if failed_at:
        next_try = None
    else:
        delay = CONTROL_BACKOFF_SECONDS[min(attempts - 1, len(CONTROL_BACKOFF_SECONDS) - 1)]
        next_try = (runtime_clock.now_utc() + timedelta(seconds=delay)).isoformat()
    conn.execute(
        """UPDATE outbound_messages
           SET job_control_attempts=?,
               job_control_next_attempt_at_utc=?,
               job_control_last_error=?,
               job_control_last_kind=?,
               job_control_failed_at_utc=?,
               job_unpin_failed_at_utc=CASE
                   WHEN ?='unpin' AND ? IS NOT NULL THEN ?
                   ELSE job_unpin_failed_at_utc END
           WHERE outbound_id=?""",
        (
            attempts, next_try, error, kind, failed_at,
            kind, failed_at, failed_at, row["outbound_id"],
        ),
    )
    conn.commit()
    print(
        f"[Alex MCP] WhatsApp control {kind} failed "
        f"outbound={row['outbound_id']} attempt={attempts}/{CONTROL_MAX_ATTEMPTS} "
        f"error={error}",
        flush=True,
    )
    return False


def _attempt_control(conn, row, kind: str, emoji: str | None = None,
                     *, outbound: bool = False) -> bool:
    if not _control_retry_ready(row):
        return False
    ok, detail = (
        _control_outbound_result(row, kind, emoji)
        if outbound else _control_result(row, kind, emoji)
    )
    return _record_control_attempt(conn, row, kind, ok, detail)


def _expire_transient_pending_items(conn) -> None:
    """Expire prompt continuations; only VOICE is a durable pending inbox item."""
    now = _now()
    expired = conn.execute(
        """SELECT item_id FROM pending_items
           WHERE status='PENDING'
             AND (
               (kind='PRIVATE_SEARCH_OFFER'
                AND datetime(created_at_utc)<=datetime('now','-10 minutes'))
               OR
               (kind='REMINDER_DRAFT'
                AND datetime(created_at_utc)<=datetime('now','-30 minutes'))
             )"""
    ).fetchall()
    if not expired:
        return
    ids = [str(row["item_id"]) for row in expired]
    marks = ",".join("?" for _ in ids)
    conn.execute(
        f"""UPDATE pending_items
            SET status='CANCELLED',resolved_at_utc=?,resolution_message_id='expired'
            WHERE item_id IN ({marks}) AND status='PENDING'""",
        [now] + ids,
    )
    # An expired prompt should never be delivered late after transport recovery.
    conn.execute(
        f"""UPDATE outbound_messages
            SET delivery_status='FAILED',last_error='pending_prompt_expired',
                next_attempt_at_utc=NULL
            WHERE context_kind='PENDING_ITEM' AND context_id IN ({marks})
              AND delivery_status='PENDING'""",
        ids,
    )
    conn.commit()


def _reconcile_pending_item_markers(conn) -> None:
    rows = conn.execute(
        """SELECT o.rowid AS outbound_rowid,o.*,
                  p.status AS pending_status,p.kind AS pending_kind
           FROM outbound_messages o
           JOIN pending_items p ON p.item_id=o.context_id
           WHERE o.context_kind='PENDING_ITEM'
             AND (
                (p.status='PENDING' AND p.kind IN ('VOICE','REMINDER_DRAFT'))
                OR (
                    (p.status IN ('RESOLVED','CANCELLED') OR p.kind NOT IN ('VOICE','REMINDER_DRAFT'))
                    AND (
                        (o.job_reacted_at_utc IS NOT NULL AND o.job_reaction_cleared_at_utc IS NULL)
                        OR (o.job_pinned_at_utc IS NOT NULL AND o.job_unpinned_at_utc IS NULL)
                    )
                )
             )
           ORDER BY o.rowid DESC"""
    ).fetchall()

    # A multi-step reminder draft may ask more than one clarification
    # ("Tomorrow" -> "What time tomorrow?"). Keep exactly the newest delivered
    # clarification pinned; move the pin forward instead of accumulating old
    # pinned questions for the same unresolved draft.
    latest_draft_sent: dict[str, str] = {}
    for row in rows:
        if (
            row["pending_status"] == "PENDING"
            and row["pending_kind"] == "REMINDER_DRAFT"
            and row["delivery_status"] == "SENT"
            and row["provider_message_id"]
        ):
            latest_draft_sent.setdefault(
                str(row["context_id"]), str(row["outbound_id"])
            )

    for row in rows:
        if row["pending_status"] == "PENDING" and row["pending_kind"] == "VOICE":
            _ensure_unresolved_markers(conn, row)
        elif (
            row["pending_status"] == "PENDING"
            and row["pending_kind"] == "REMINDER_DRAFT"
        ):
            latest_id = latest_draft_sent.get(str(row["context_id"]))
            if latest_id and str(row["outbound_id"]) == latest_id:
                _ensure_reminder_draft_pin(conn, row)
            elif (
                row["job_pinned_at_utc"] is not None
                and row["job_unpinned_at_utc"] is None
            ):
                _cleanup_resolved_markers(conn, row)
        else:
            # Also cleans stale markers from transient offer/draft prompts.
            _cleanup_resolved_markers(conn, row)


def _ensure_claim_setup_markers(conn, row) -> None:
    """Mark an unclaimed pre-due family reminder as unresolved in the group."""
    if (
        row["delivery_status"] != "SENT"
        or not row["provider_message_id"]
    ):
        return

    now = _now()
    if not row["job_reacted_at_utc"]:
        if _attempt_control(conn, row, "reaction", "⏳", outbound=True):
            conn.execute(
                """UPDATE outbound_messages
                   SET job_reacted_at_utc=? WHERE outbound_id=?""",
                (now, row["outbound_id"]),
            )
            conn.commit()
            row = _joined_row(conn, row["outbound_id"])
        else:
            return

    if not row["job_pinned_at_utc"]:
        if _attempt_control(conn, row, "pin", outbound=True):
            conn.execute(
                """UPDATE outbound_messages
                   SET job_pinned_at_utc=?,job_pin_target='OUTBOUND'
                   WHERE outbound_id=?""",
                (now, row["outbound_id"]),
            )
            conn.commit()


def _cleanup_claim_setup_markers(conn, row) -> None:
    """Clear the pre-due claim marker once claimed, due, cancelled, or complete."""
    now = _now()
    if row["job_reacted_at_utc"] and not row["job_reaction_cleared_at_utc"]:
        if _attempt_control(conn, row, "reaction", "", outbound=True):
            conn.execute(
                """UPDATE outbound_messages
                   SET job_reaction_cleared_at_utc=? WHERE outbound_id=?""",
                (now, row["outbound_id"]),
            )
            conn.commit()
            row = _joined_row(conn, row["outbound_id"])
        else:
            return

    if row["job_pinned_at_utc"] and not row["job_unpinned_at_utc"]:
        if _attempt_control(conn, row, "unpin", outbound=True):
            conn.execute(
                """UPDATE outbound_messages
                   SET job_unpinned_at_utc=?,job_unpin_failed_at_utc=NULL
                   WHERE outbound_id=?""",
                (now, row["outbound_id"]),
            )
            conn.commit()


def _reconcile_claim_setup_markers(conn) -> None:
    """Keep only the newest OPEN, unclaimed family setup card active."""
    rows = conn.execute(
        """SELECT o.rowid AS outbound_rowid,o.*,
                  r.status AS reminder_status,r.claimable,
                  r.claimed_by_user_id
           FROM outbound_messages o
           JOIN reminders r ON r.reminder_id=o.context_id
           WHERE o.context_kind='REMINDER_SETUP'
             AND o.delivery_status='SENT'
             AND (
                (r.status='OPEN' AND r.claimable=1
                 AND r.claimed_by_user_id IS NULL)
                OR (
                    (o.job_reacted_at_utc IS NOT NULL
                     AND o.job_reaction_cleared_at_utc IS NULL)
                    OR (o.job_pinned_at_utc IS NOT NULL
                        AND o.job_unpinned_at_utc IS NULL)
                )
             )
           ORDER BY o.rowid DESC"""
    ).fetchall()

    # Releasing/reopening a family reminder deliberately creates a fresh card.
    # Only that newest card may become the active ⏳+pin claim surface; older
    # cards stay as history and are cleaned if they still carry markers.
    latest_sent: dict[str, str] = {}
    for row in rows:
        latest_sent.setdefault(str(row["context_id"]), str(row["outbound_id"]))

    for row in rows:
        is_latest = (
            latest_sent.get(str(row["context_id"])) == str(row["outbound_id"])
        )
        unresolved_claim = (
            is_latest
            and row["reminder_status"] == "OPEN"
            and int(row["claimable"] or 0) == 1
            and not row["claimed_by_user_id"]
        )
        if unresolved_claim:
            _ensure_claim_setup_markers(conn, row)
        else:
            _cleanup_claim_setup_markers(conn, row)


# Every Alex message that represents responsibility for one unresolved
# reminder. Exactly one of them (the newest sent one, in the chat where the
# responsibility currently lives) carries the unresolved ⏳ + pin.
_REMINDER_MARKER_KINDS = (
    "REMINDER_CREATED", "REMINDER_ASSIGNED", "REMINDER_CLAIM_CONFIRMED",
    "REMINDER_INITIAL", "REMINDER_FOLLOWUP", "REMINDER_CLAIMANT_FOLLOWUP",
)
_UNRESOLVED_REMINDER_STATES = ("OPEN", "DUE", "DEFERRED")


def _reminder_marker_should_be_active(row, is_latest: bool) -> bool:
    if not is_latest:
        return False
    if row["reminder_status"] not in _UNRESOLVED_REMINDER_STATES:
        return False
    if row["recurrence_rule"]:
        # A recurring reminder never finishes; pinning it forever would only
        # crowd out real unresolved items.
        return False
    if int(row["claimable"] or 0) == 1:
        home = str(row["reminder_conversation_id"] or "")
        here = str(row["conversation_id"] or "")
        if not row["claimed_by_user_id"]:
            # Unclaimed pre-due family work is marked on the group claim card
            # (REMINDER_SETUP). Once due, the fired group message carries it.
            return row["reminder_status"] != "OPEN" and here == home
        # Claimed: responsibility lives in the claimant's DM, never the group.
        return here != home
    return True


def _reconcile_reminder_pins(conn) -> None:
    """Keep ⏳ + pin on the one current message of every unresolved reminder.

    Covers personal reminders from creation (REMINDER_CREATED), assigned and
    claimed reminders (REMINDER_ASSIGNED / REMINDER_CLAIM_CONFIRMED) and fired
    reminders/follow-ups. 👍/seen keeps the reminder DUE, so markers stay;
    completion or cancellation clears them. Older carriers of the same
    reminder are cleaned when a newer one (e.g. the fired reminder) is sent.
    """
    marks = ",".join("?" for _ in _REMINDER_MARKER_KINDS)
    rows = conn.execute(
        f"""SELECT o.rowid AS outbound_rowid,o.*,
                  r.status AS reminder_status,r.claimable,r.claimed_by_user_id,
                  r.recurrence_rule,r.conversation_id AS reminder_conversation_id
           FROM outbound_messages o
           JOIN reminders r ON r.reminder_id=o.context_id
           WHERE o.context_kind IN ({marks})
             AND o.delivery_status='SENT'
             AND (
                r.status IN ('OPEN','DUE','DEFERRED')
                OR (o.job_reacted_at_utc IS NOT NULL
                    AND o.job_reaction_cleared_at_utc IS NULL)
                OR (o.job_pinned_at_utc IS NOT NULL
                    AND o.job_unpinned_at_utc IS NULL)
             )
           ORDER BY o.rowid DESC""",
        _REMINDER_MARKER_KINDS,
    ).fetchall()
    latest: dict[str, str] = {}
    for row in rows:
        latest.setdefault(str(row["context_id"]), str(row["outbound_id"]))
    for row in rows:
        is_latest = latest.get(str(row["context_id"])) == str(row["outbound_id"])
        if _reminder_marker_should_be_active(row, is_latest):
            _ensure_claim_setup_markers(conn, row)
        elif (
            (row["job_reacted_at_utc"] and not row["job_reaction_cleared_at_utc"])
            or (row["job_pinned_at_utc"] and not row["job_unpinned_at_utc"])
        ):
            _cleanup_claim_setup_markers(conn, row)


def _ensure_reminder_draft_pin(conn, row) -> None:
    """Pin Alex's unanswered reminder clarification without adding ⏳."""
    if (
        row["delivery_status"] != "SENT"
        or row["job_pinned_at_utc"]
        or not row["provider_message_id"]
    ):
        return
    if _attempt_control(conn, row, "pin", outbound=True):
        conn.execute(
            """UPDATE outbound_messages
               SET job_pinned_at_utc=?,job_pin_target='OUTBOUND'
               WHERE outbound_id=?""",
            (_now(), row["outbound_id"]),
        )
        conn.commit()


def _ensure_unresolved_markers(conn, row) -> None:
    """Mark only durable managed work: documents and unresolved VOICE items."""
    managed = row["kind"] == "DOCUMENT"
    if row["context_kind"] == "PENDING_ITEM":
        pending_kind = _row_value(row, "pending_kind")
        if pending_kind is None and row["context_id"]:
            pending = conn.execute(
                "SELECT kind FROM pending_items WHERE item_id=?",
                (row["context_id"],),
            ).fetchone()
            pending_kind = pending["kind"] if pending else None
        managed = str(pending_kind or "").upper() == "VOICE"
    if not managed or not row["source_message_id"]:
        return
    now = _now()
    if not row["job_reacted_at_utc"]:
        if _attempt_control(conn, row, "reaction", "⏳"):
            conn.execute(
                "UPDATE outbound_messages SET job_reacted_at_utc=? WHERE outbound_id=?",
                (now, row["outbound_id"]),
            )
            conn.commit()
            row = _joined_row(conn, row["outbound_id"])
        else:
            return
    if not row["job_pinned_at_utc"]:
        if _attempt_control(conn, row, "pin"):
            conn.execute(
                """UPDATE outbound_messages
                   SET job_pinned_at_utc=?,job_pin_target='SOURCE'
                   WHERE outbound_id=?""",
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
        if _attempt_control(conn, row, "reaction", ""):
            conn.execute(
                "UPDATE outbound_messages SET job_reaction_cleared_at_utc=? WHERE outbound_id=?",
                (now, row["outbound_id"]),
            )
            conn.commit()
            row = _joined_row(conn, row["outbound_id"])
        else:
            return
    if row["job_pinned_at_utc"] and not row["job_unpinned_at_utc"]:
        outbound_pin = str(_row_value(row, "job_pin_target") or "").upper() == "OUTBOUND"
        if _attempt_control(conn, row, "unpin", outbound=outbound_pin):
            conn.execute(
                """UPDATE outbound_messages
                   SET job_unpinned_at_utc=?,job_unpin_failed_at_utc=NULL
                   WHERE outbound_id=?""",
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
        _expire_transient_pending_items(conn)
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
                "REMINDER_INITIATOR_ESCALATION",
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
                    "REMINDER_INITIATOR_ESCALATION",
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
                    else:
                        # reminder_events has one durable follow-up event type;
                        # the outbound context records which follow-up surface
                        # was delivered without violating the table CHECK.
                        event_type = "FOLLOW_UP_DELIVERED"
                    conn.execute(
                        """INSERT INTO reminder_events(
                               event_id,reminder_id,event_type,note
                           ) VALUES(lower(hex(randomblob(16))),?,?,?)""",
                        (
                            row["context_id"], event_type,
                            f"confirmed by WhatsApp egress: {row['context_kind']}",
                        ),
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
                        and not refreshed["job_pinned_at_utc"]
                        and _attempt_control(
                            conn, refreshed, "pin", outbound=True
                        )
                    ):
                        conn.execute(
                            """UPDATE outbound_messages
                               SET job_pinned_at_utc=?,job_pin_target='OUTBOUND'
                               WHERE outbound_id=?""",
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
        _reconcile_claim_setup_markers(conn)
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
