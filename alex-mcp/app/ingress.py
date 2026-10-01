from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import runtime_clock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brain
import db
import media
import services
import scope_policy
import diagnostics
from config import DATA_DIR

PORT = 5001
RUNTIME_STATUS = os.path.join(DATA_DIR, "runtime_status.json")
CERT_DIR = os.path.join(DATA_DIR, "certification")
CERT_STATUS = os.path.join(CERT_DIR, "live_benchmark_status.json")
CERT_REPORT = os.path.join(CERT_DIR, "live_benchmark_report.json")
CERT_LOG = os.path.join(CERT_DIR, "live_benchmark.log")
CERT_SOURCE_OPTIONS = os.path.join(DATA_DIR, "options.json")
CERT_BUDGET_USD = 3.0
_CERT_LOCK = threading.Lock()
_CERT_PROCESS = None


def _read_runtime_status() -> dict:
    try:
        with open(RUNTIME_STATUS, "r", encoding="utf-8") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_runtime_status(**updates) -> None:
    current = _read_runtime_status()
    current.update(updates)
    current["updated_at"] = runtime_clock.utc_iso()
    tmp = RUNTIME_STATUS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RUNTIME_STATUS)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _record_processing_error(exc: Exception, payload: dict) -> dict:
    info = brain.classify_runtime_error(exc)
    safe = {
        **info,
        "message_id": str(payload.get("message_id") or "")[:120],
        "conversation_type": str(payload.get("conversation_type") or "DIRECT_DM")[:40],
        "at": runtime_clock.utc_iso(),
    }
    _write_runtime_status(last_processing_error=safe)
    return safe



def _write_cert_status(payload: dict) -> None:
    os.makedirs(CERT_DIR, exist_ok=True)
    tmp = CERT_STATUS + ".tmp"
    body = {
        **payload,
        "updated_at": runtime_clock.utc_iso(),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CERT_STATUS)


def _read_cert_status() -> dict:
    try:
        with open(CERT_STATUS, "r", encoding="utf-8") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except Exception:
        return {"state": "idle", "budget_usd": CERT_BUDGET_USD}


def _cert_report_summary() -> dict | None:
    try:
        with open(CERT_REPORT, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception:
        return None
    summary = report.get("summary") if isinstance(report, dict) else None
    if not isinstance(summary, dict):
        return None
    failures = []
    for row in report.get("prompt_results", []) if isinstance(report, dict) else []:
        if row.get("status") != "FAIL":
            continue
        failures.append({
            "contract": row.get("contract"),
            "domain": row.get("domain"),
            "source": row.get("source"),
            "problems": list(row.get("problems") or [])[:4],
        })
        if len(failures) >= 12:
            break
    if len(failures) < 12:
        for row in report.get("conversation_results", []) if isinstance(report, dict) else []:
            if row.get("status") != "FAIL":
                continue
            failures.append({
                "contract": row.get("contract"),
                "domain": row.get("domain"),
                "source": row.get("source"),
                "problems": [
                    problem
                    for step in row.get("steps", [])
                    for problem in (step.get("problems") or [])
                ][:4],
            })
            if len(failures) >= 12:
                break
    return {
        "result_status": report.get("status"),
        "summary": {
            "prompt_runs": summary.get("prompt_runs", 0),
            "conversation_runs": summary.get("conversation_runs", 0),
            "failures": summary.get("failures", 0),
            "budget_stopped": bool(summary.get("budget_stopped")),
            "estimated_cost_usd": summary.get("estimated_cost_usd", 0.0),
            "max_live_cost_usd": summary.get("max_live_cost_usd", CERT_BUDGET_USD),
            "planned_prompt_runs": summary.get("planned_prompt_runs", 0),
            "planned_conversation_runs": summary.get("planned_conversation_runs", 0),
            "skipped_paid_contracts": summary.get("skipped_paid_contracts", 0),
            "offline_failures": summary.get("offline_failures"),
            "latency_ms_p50": summary.get("latency_ms_p50", 0),
            "latency_ms_max": summary.get("latency_ms_max", 0),
        },
        "failures_preview": failures,
    }


def certification_status() -> dict:
    status = _read_cert_status()
    report = _cert_report_summary()
    if report:
        status = {**status, **report}
    status["report_available"] = os.path.exists(CERT_REPORT)
    return status


def _monitor_certification(proc: subprocess.Popen, log_handle) -> None:
    global _CERT_PROCESS
    returncode = proc.wait()
    try:
        log_handle.flush()
        log_handle.close()
    except Exception:
        pass
    report = _cert_report_summary()
    state = "completed" if report is not None else "error"
    _write_cert_status({
        "state": state,
        "pid": None,
        "returncode": returncode,
        "budget_usd": CERT_BUDGET_USD,
        "finished_at": runtime_clock.utc_iso(),
        "message": (
            "Live benchmark finished."
            if report is not None
            else "Live benchmark ended without a readable report. Check the benchmark log."
        ),
    })
    with _CERT_LOCK:
        if _CERT_PROCESS is proc:
            _CERT_PROCESS = None


def start_live_certification() -> dict:
    """Start one sandboxed paid benchmark using Alex's configured provider keys.

    The child reads only provider credentials from /data/options.json, then
    behavior_cert.py rebinds all Alex state to a disposable sandbox and mocks HA.
    """
    global _CERT_PROCESS
    with _CERT_LOCK:
        if _CERT_PROCESS is not None and _CERT_PROCESS.poll() is None:
            return {
                "ok": True,
                "already_running": True,
                **certification_status(),
            }

        os.makedirs(CERT_DIR, exist_ok=True)
        try:
            if os.path.exists(CERT_REPORT):
                os.unlink(CERT_REPORT)
        except OSError:
            pass

        log_handle = open(CERT_LOG, "w", encoding="utf-8")
        cmd = [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "behavior_cert.py"),
            "--mode", "live",
            "--phase", "all",
            "--provider", "auto",
            "--source-options", CERT_SOURCE_OPTIONS,
            "--live-strategy", "benchmark",
            "--max-live-cost-usd", str(CERT_BUDGET_USD),
            "--live-adversarial-per-contract", "1",
            "--hard-latency-ms", "20000",
            "--no-fail-exit",
            "--report", CERT_REPORT,
        ]
        env = os.environ.copy()
        # Defence in depth: the child itself also isolates state and HA. These
        # values make the intent explicit before it imports application modules.
        env["ALEX_CERT_SOURCE_OPTIONS"] = CERT_SOURCE_OPTIONS
        env["ALEX_HA_API_URL"] = "http://127.0.0.1:9/certification-no-ha"
        env.pop("SUPERVISOR_TOKEN", None)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(__file__),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception:
            log_handle.close()
            raise
        _CERT_PROCESS = proc
        _write_cert_status({
            "state": "running",
            "pid": proc.pid,
            "returncode": None,
            "budget_usd": CERT_BUDGET_USD,
            "started_at": runtime_clock.utc_iso(),
            "message": (
                "Live benchmark is running in a disposable sandbox. "
                "No WhatsApp messages or physical HA actions are sent."
            ),
        })
        threading.Thread(
            target=_monitor_certification,
            args=(proc, log_handle),
            daemon=True,
        ).start()
        return {"ok": True, **certification_status()}


def _received_at_utc(payload: dict) -> str:
    """Prefer WhatsApp's own send timestamp; fall back to arrival time.

    Guards against clock skew or bogus values: anything in the future or more
    than 7 days old falls back to now.
    """
    now = runtime_clock.now_utc()
    raw = payload.get("sent_at_ms")
    try:
        if raw is not None:
            sent = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
            if now - timedelta(days=7) <= sent <= now + timedelta(minutes=5):
                return min(sent, now).isoformat()
    except Exception:
        pass
    return now.isoformat()


def build_turn(payload: dict, media_lines: list[str]) -> dict:
    """Normalize one inbound WhatsApp message into a single Turn shape.

    trusted_text  = typed text and/or the user's own voice transcript
    document_lines = OCR/PDF text (untrusted content, never user intent)
    """
    typed = str(payload.get("text") or "").strip()
    transcript, document_lines = media.split_voice_transcript(media_lines)
    trusted_text = "\n".join(x for x in (typed, transcript) if x).strip()
    has_audio = bool(payload.get("audio_data"))
    has_image = bool(payload.get("image_data"))
    has_pdf = bool(payload.get("pdf_data"))
    if has_audio and not (has_image or has_pdf):
        source = "voice"
    elif has_image and not (has_audio or has_pdf):
        source = "image"
    elif has_pdf and not (has_audio or has_image):
        source = "document"
    elif has_audio or has_image or has_pdf:
        source = "mixed"
    else:
        source = "text"
    return {
        "trusted_text": trusted_text,
        "read_scope": scope_policy.resolve_read_scope(trusted_text),
        "document_lines": document_lines,
        "source": source,
        "has_document_media": has_image or has_pdf,
        "received_at_utc": _received_at_utc(payload),
    }


def _private_group_handoff_requested(text: str) -> bool:
    """Identify owner-private group requests that must execute in the owner's DM.

    Reads stay read-intent gated. For writes, explicit private wording or the
    owner's emoji shortcut is enough. The group actor itself never gains a
    private space.
    """
    low = str(text or "").casefold()
    if not low.strip():
        return False
    readish = bool(re.search(
        r"\b(?:show|list|find|get|what|when|where|how much|how many|tell me|"
        r"check|latest|recent|history|balance|did i|have i|do i|"
        r"spend|spent|spending|transactions?)\b",
        low,
    ))
    writeish = bool(re.search(
        r"\b(?:add|create|record|log|save|remember|set|update|change|edit|"
        r"correct|contribute|allocate|put|schedule|remind|monitor)\b",
        low,
    ))
    explicit_private = scope_policy.explicit_private(text)
    emoji_private = scope_policy.contains_emoji(text)
    # Do not infer that ordinary Family Shared finance/receipt reads are
    # private merely because they concern the authenticated sender. The group
    # actor is already structurally restricted to FAMILY_SHARED. Automatic DM
    # handoff is reserved for an explicit privacy signal or inherently private
    # profile facts that have no meaningful shared interpretation.
    sensitive_read = readish and bool(re.search(
        r"\b(?:salary|paycheck|take[- ]home|ot rate|overtime rate|"
        r"overtime pay|exact ot|bank balance|stash(?:es)?|cash\s+pool|cash\s+pools|"
        r"my private (?:notes?|memory|data))\b",
        low,
    ))
    return (
        ((explicit_private or emoji_private) and (readish or writeish))
        or sensitive_read
    )

def _dm_conversation_for_actor(actor) -> str:
    return actor.phone.replace("+", "") + "@s.whatsapp.net"


def _private_group_match_available(actor, text: str) -> bool:
    """Probe for a private-only receipt/note match without widening group ACLs.

    Family Shared is checked first. Only when the same read has no shared match
    do we probe the authenticated owner's private space, and only a boolean
    escapes this helper.
    """
    if actor.conversation_type != "GROUP":
        return False
    low = str(text or "").casefold()
    readish = bool(re.search(
        r"\b(?:show|find|get|send|open|what|which|where|latest|recent|list|"
        r"check|balance|how much|how many)\b",
        low,
    ))
    if not readish:
        return False
    receiptish = bool(re.search(r"\b(?:receipt|receipts|invoice|invoices)\b", low))
    savedish = bool(re.search(r"\b(?:note|notes|saved|memory|memories|remembered)\b", low))

    # A user may refer to an inherently-private stash only by its configured
    # name ("how much is in pocket cash?"). Recognize that name without
    # exposing its balance or existence to the group response.
    try:
        conn = db.connect()
        rows = conn.execute(
            """SELECT name FROM alex_phase2_cash_pools
               WHERE owner_user_id=? AND space_id=? AND status='ACTIVE'""",
            (actor.user_id, actor.private_space),
        ).fetchall()
        conn.close()
        for row in rows:
            name = str(row["name"] or "").strip().casefold()
            if name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", low):
                return True
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

    if not (receiptish or savedish):
        return False

    dm_actor = replace(
        actor,
        conversation_id=_dm_conversation_for_actor(actor),
        conversation_type="DIRECT_DM",
        allowed_spaces=(actor.private_space,),
    )
    try:
        if receiptish:
            shared = services.find_receipts(actor, query=text, limit=1)
            if shared.get("matches"):
                return False
            private = services.find_receipts(dm_actor, query=text, limit=1)
            return bool(private.get("matches"))
        shared = services.search_saved_items(actor, query=text, limit=1)
        if shared.get("matches"):
            return False
        private = services.search_saved_items(dm_actor, query=text, limit=1)
        return bool(private.get("matches"))
    except Exception:
        return False


def _error_report_command(text: str) -> tuple[bool, str]:
    raw = str(text or "").strip()
    match = re.match(
        r"(?is)^\s*mark\s+(?:this|that)\s+as\s+(?:an\s+)?error\b"
        r"(?:\s*[:,-]?\s*(?:because\s+)?(.*))?$",
        raw,
    )
    if not match:
        return False, ""
    explanation = str(match.group(1) or "").strip()
    return True, explanation



def _is_selection_followup(text: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:yes|yep|yeah|sure|ok|okay|please\s+do|show\s+it|send\s+it|open\s+it|get\s+it)\s*[.!]?\s*",
        str(text or ""),
        re.IGNORECASE,
    ))


def _selection_context_for_offer(actor, reply: str, attachments: list[dict]) -> dict | None:
    if attachments:
        return None
    if not re.search(
        r"\b(?:would\s+you\s+like\s+me\s+to|want\s+me\s+to|shall\s+i|should\s+i)\s+"
        r"(?:show|send|open|get)\b",
        str(reply or ""),
        re.IGNORECASE,
    ):
        return None
    return services.latest_single_selection_context(
        actor, created_after_utc=actor.received_at_utc
    )


def _selection_context_parts(context: dict | None) -> tuple[str, str] | None:
    if not context or context.get("context_kind") != "SELECTION":
        return None
    value = str(context.get("context_id") or "")
    if ":" not in value:
        return None
    kind, target_id = value.split(":", 1)
    if not kind or not target_id:
        return None
    return kind, target_id


def _deliver_selection_followup(actor, context: dict) -> dict:
    parts = _selection_context_parts(context)
    if not parts:
        raise ValueError("invalid selection continuation context")
    result = services.get_selection_target(actor, parts[0], parts[1])
    attachments = list(result.get("_attachments") or [])
    reply = "Here it is." if attachments else str(result.get("content") or result.get("title") or "Here it is.")
    db.queue_outbound(
        actor.conversation_id, "TEXT", text=reply,
        source_message_id=actor.source_message_id,
    )
    sent_paths = set()
    for item in attachments:
        local_path = item.get("path")
        if not local_path or local_path in sent_paths:
            continue
        sent_paths.add(local_path)
        kind = item.get("kind", "DOCUMENT")
        db.queue_outbound(
            actor.conversation_id,
            "IMAGE" if kind == "IMAGE" else "DOCUMENT",
            local_path=local_path,
            mime_type=item.get("mime_type"),
            source_message_id=actor.source_message_id,
        )
    db.finish_inbound(actor.source_message_id, reply)
    return {"ok": True, "selection_followup": True}


def _finish_simple_turn(actor, reply: str, **extra) -> dict:
    db.queue_outbound(
        actor.conversation_id, "TEXT", text=reply,
        source_message_id=actor.source_message_id,
    )
    db.finish_inbound(actor.source_message_id, reply)
    return {"ok": True, **extra}


def process(payload: dict) -> dict:
    required = ("message_id", "conversation_id", "sender_phone")
    if any(not payload.get(k) for k in required):
        return {"ok": False, "error": "missing required inbound fields"}

    claim = db.claim_inbound(payload)
    if claim == "DUPLICATE":
        return {"ok": True, "duplicate": True}

    try:
        if str(payload.get("event_kind") or "").upper() == "REACTION":
            actor = db.resolve_actor(
                payload["sender_phone"],
                payload["conversation_id"],
                payload.get("conversation_type", "GROUP"),
                payload["message_id"],
                [],
            )
            result = services.claim_reminder_from_reaction(
                actor,
                str(payload.get("reaction_target_message_id") or ""),
                str(payload.get("reaction_text") or ""),
            )
            # A successful first claim moves responsibility into the claimant's
            # DM immediately. The group stays quiet; the reaction itself remains
            # the visible family-level signal.
            if result.get("status") == "claimed":
                task = str(result.get("task") or "this reminder").strip()
                claim_text = (
                    f"Got it — you’ve claimed “{task}”. "
                    "I’ll follow up with you privately from here."
                )
                db.queue_outbound(
                    _dm_conversation_for_actor(actor), "TEXT", text=claim_text,
                    source_message_id=payload["message_id"],
                    context_kind="REMINDER_CLAIM_CONFIRMED",
                    context_id=result.get("reminder_id"),
                )
                try:
                    import ha_mobile
                    conn = db.connect()
                    try:
                        ha_mobile.queue_claim_confirmation(
                            conn, actor.user_id, result.get("reminder_id"), task,
                            event_key=f"wa-claim:{payload['message_id']}",
                        )
                        conn.commit()
                    finally:
                        conn.close()
                except Exception:
                    pass
                result["claimant_notified"] = True

            # A later claimant should not silently wonder whether they now own
            # the reminder, so tell them naturally that it is already being
            # handled without changing ownership.
            if (
                result.get("status") == "already_claimed"
                and result.get("claimed_by_user_id")
                and result.get("claimed_by_user_id") != actor.user_id
                and actor.conversation_type == "GROUP"
            ):
                winner = str(result.get("claimed_by_name") or "").strip()
                if not winner or winner.casefold() in {"husband", "wife"}:
                    winner = "your spouse"
                text = f"{winner} has already picked this one up, so you don’t need to worry about it 👍"
                db.queue_outbound(
                    actor.conversation_id, "TEXT", text=text,
                    source_message_id=payload["message_id"],
                )
                try:
                    import ha_mobile
                    ha_mobile.queue_info(
                        actor.user_id, f"wa-claim-collision:{payload['message_id']}",
                        text, result.get("reminder_id"),
                    )
                except Exception:
                    pass
                result["collision_reply"] = text
            db.finish_inbound(payload["message_id"], json.dumps(result, sort_keys=True))
            return {"ok": True, "reaction": result}

        if payload.get("media_failed"):
            kind = str(payload.get("media_failed_type") or "attachment").strip().lower()
            label = "document" if kind == "pdf" else kind
            reply = (
                f"I couldn't download that {label} from WhatsApp, so I didn't act on it. "
                "Please resend the attachment."
            )
            db.queue_outbound(
                payload["conversation_id"], "TEXT",
                text=reply,
                source_message_id=payload["message_id"],
            )
            db.finish_inbound(payload["message_id"], reply)
            return {"ok": True, "media_failed": True}
        db.touch_inbound_processing(payload["message_id"])
        media_ids, media_lines, vision_parts = media.process_payload_media(payload)
        db.touch_inbound_processing(payload["message_id"])
        turn = build_turn(payload, media_lines)
        actor = db.resolve_actor(
            payload["sender_phone"],
            payload["conversation_id"],
            payload.get("conversation_type", "DIRECT_DM"),
            payload["message_id"],
            media_ids,
        )
        # Turn metadata is attached by code; privacy spaces were already fixed
        # by resolve_actor and are never influenced by the model or the Turn.
        actor = replace(
            actor,
            source=turn["source"],
            trusted_text=turn["trusted_text"],
            received_at_utc=turn["received_at_utc"],
            read_scope=turn["read_scope"],
            private_handoff=(
                actor.conversation_type == "GROUP"
                and turn["read_scope"] == "private"
            ),
        )
        quoted_context = db.resolve_quoted_context(
            actor.conversation_id, payload.get("quoted_message_id"), actor.phone
        )
        # Orphan-attachment pairing applies ONLY to a genuinely captionless
        # image/PDF. Voice notes are the user's own words and must never
        # inherit an earlier, unrelated text instruction (v0.4.3 vinyl leak).
        if (
            not quoted_context
            and turn["has_document_media"]
            and not turn["trusted_text"]
        ):
            quoted_context = db.resolve_recent_instruction_context(
                actor.conversation_id, actor.phone, actor.source_message_id
            )
        db.touch_inbound_processing(payload["message_id"])

        # User-reported behavioural errors are captured deterministically from
        # a swipe-reply. This is diagnostics only: no action is retried or undone.
        is_error_command, inline_explanation = _error_report_command(
            turn["trusted_text"]
        )
        pending_error = diagnostics.pending_user_error_report(actor)
        if is_error_command:
            if not quoted_context or not quoted_context.get("outbound_id"):
                return _finish_simple_turn(
                    actor,
                    "Swipe-reply to the Alex message that was wrong, then say “Mark this as error.”",
                    error_report=True,
                )
            if inline_explanation:
                recorded = diagnostics.complete_user_error_report(
                    actor, inline_explanation, quoted_context
                )
                return _finish_simple_turn(
                    actor,
                    f"Marked as {recorded['error_id']}. I saved the diagnostic evidence only; I did not retry or undo anything.",
                    error_report=True,
                )
            diagnostics.begin_user_error_report(actor, quoted_context)
            return _finish_simple_turn(
                actor, "What was wrong?", error_report=True
            )

        if pending_error and turn["trusted_text"].strip():
            recorded = diagnostics.complete_user_error_report(
                actor, turn["trusted_text"]
            )
            return _finish_simple_turn(
                actor,
                f"Marked as {recorded['error_id']}. I saved the diagnostic evidence only; I did not retry or undo anything.",
                error_report=True,
            )

        if _is_selection_followup(turn["trusted_text"]):
            selection_context = quoted_context
            if not _selection_context_parts(selection_context):
                selection_context = db.resolve_recent_outbound_context(
                    actor.conversation_id, "SELECTION", max_age_seconds=600
                )
            if _selection_context_parts(selection_context):
                return _deliver_selection_followup(actor, selection_context)

        # Private reads asked from Family Shared are handed to the authenticated
        # owner's DM without ever widening the group actor's ACL. This is one
        # model/tool turn, not a group answer followed by a second private retry.
        if (
            actor.conversation_type == "GROUP"
            and (
                _private_group_handoff_requested(turn["trusted_text"])
            )
        ):
            dm_conversation = _dm_conversation_for_actor(actor)
            dm_actor = db.resolve_actor(
                actor.phone, dm_conversation, "DIRECT_DM",
                actor.source_message_id, media_ids,
            )
            dm_actor = replace(
                dm_actor,
                source=turn["source"],
                trusted_text=turn["trusted_text"],
                received_at_utc=turn["received_at_utc"],
                read_scope=turn["read_scope"],
                private_handoff=False,
            )
            private_reply, private_attachments = asyncio.run(
                brain.respond(
                    dm_actor, turn["trusted_text"], turn["document_lines"],
                    vision_parts, quoted_context=None,
                )
            )
            private_selection = _selection_context_for_offer(
                dm_actor, private_reply, private_attachments
            )
            if not private_attachments:
                db.queue_outbound(
                    dm_conversation, "TEXT", text=private_reply,
                    source_message_id=actor.source_message_id,
                    context_kind="SELECTION" if private_selection else None,
                    context_id=(
                        f"{private_selection['kind']}:{private_selection['id']}"
                        if private_selection else None
                    ),
                )
            sent_paths: set[str] = set()
            first_private_attachment = True
            for item in private_attachments:
                path = item.get("path")
                kind = item.get("kind", "DOCUMENT")
                if path and path not in sent_paths:
                    sent_paths.add(path)
                    db.queue_outbound(
                        dm_conversation,
                        "IMAGE" if kind == "IMAGE" else "DOCUMENT",
                        text=private_reply if first_private_attachment else None,
                        local_path=path,
                        mime_type=item.get("mime_type"),
                        source_message_id=actor.source_message_id,
                    )
                    first_private_attachment = False
            group_reply = (
                "I’m handling that in your private DM."
                if private_attachments else "I sent that to you privately."
            )
            db.queue_outbound(
                actor.conversation_id, "TEXT", text=group_reply,
                source_message_id=actor.source_message_id,
            )
            db.finish_inbound(actor.source_message_id, group_reply)
            return {"ok": True, "private_handoff": True}

        reply, attachments = asyncio.run(
            brain.respond(
                actor, turn["trusted_text"], turn["document_lines"], vision_parts,
                quoted_context=quoted_context,
            )
        )
        db.touch_inbound_processing(payload["message_id"])
        selection_context = _selection_context_for_offer(
            actor, reply, attachments
        )
        if not attachments:
            db.queue_outbound(
                actor.conversation_id, "TEXT", text=reply,
                source_message_id=actor.source_message_id,
                context_kind="SELECTION" if selection_context else None,
                context_id=(
                    f"{selection_context['kind']}:{selection_context['id']}"
                    if selection_context else None
                ),
            )
        sent_paths: set[str] = set()
        first_attachment = True
        for item in attachments:
            path = item.get("path")
            kind = item.get("kind", "DOCUMENT")
            if path and path not in sent_paths:
                sent_paths.add(path)
                db.queue_outbound(
                    actor.conversation_id,
                    "IMAGE" if kind == "IMAGE" else "DOCUMENT",
                    text=reply if first_attachment else None,
                    local_path=path,
                    mime_type=item.get("mime_type"),
                    source_message_id=actor.source_message_id,
                )
                first_attachment = False
        db.finish_inbound(actor.source_message_id, reply)
        return {"ok": True}
    except media.VoiceTranscriptionUncertain as exc:
        # Preserve the original audio but do not let Node retry a voice command
        # whose decoders disagree. Asking again is safer than a wrong mutation.
        reply = (
            "I couldn't understand that voice command confidently enough to act. "
            "Please resend the voice note or type the command."
        )
        try:
            db.queue_outbound(
                payload["conversation_id"], "TEXT",
                text=reply,
                source_message_id=payload["message_id"],
            )
            db.finish_inbound(payload["message_id"], reply)
        except Exception:
            db.fail_inbound(payload["message_id"], str(exc))
            raise
        return {"ok": True, "voice_uncertain": True}
    except PermissionError as exc:
        db.fail_inbound(payload["message_id"], str(exc))
        return {"ok": False, "unauthorized": True}
    except Exception as exc:
        db.fail_inbound(payload["message_id"], str(exc))
        _record_processing_error(exc, payload)
        # Reaction events are transport-level signals. A broken reaction must
        # never post a scary processing-error message into the family group.
        if str(payload.get("event_kind") or "").upper() == "REACTION":
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            return {"ok": False, "reaction_error": True, "error": str(exc)[:500]}
        try:
            db.queue_outbound(
                payload["conversation_id"], "TEXT",
                text="I hit a processing error and did not intentionally repeat the action. Please try that once more.",
                source_message_id=payload["message_id"],
            )
        except Exception:
            pass
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return {"ok": False, "error": str(exc)[:500]}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "alive"})
        elif self.path == "/runtime-status":
            self._json(200, _read_runtime_status())
        elif self.path == "/usage-summary":
            self._json(200, diagnostics.usage_summary(24))
        elif self.path == "/certification-status":
            self._json(200, certification_status())
        elif self.path == "/certification-report":
            try:
                with open(CERT_REPORT, "r", encoding="utf-8") as f:
                    report = json.load(f)
                self._json(200, report)
            except Exception:
                self._json(404, {"error": "certification report not available"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/provider-probe":
            result = brain.provider_probe()
            _write_runtime_status(provider_probe=result)
            self._json(200 if result.get("status") == "ok" else 503, result)
            return
        if self.path == "/certification-live":
            try:
                result = start_live_certification()
                self._json(202, result)
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)[:500]})
            return
        if self.path != "/ingress":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 50 * 1024 * 1024:
                self._json(413, {"error": "invalid payload size"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = process(payload)
            self._json(200 if result.get("ok") else 400, result)
        except Exception as exc:
            self._json(500, {"error": str(exc)[:500]})

    def log_message(self, fmt, *args):
        return


def main():
    db.initialize()
    if not os.path.exists(RUNTIME_STATUS):
        _write_runtime_status(provider_probe={"status": "untested"})
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[Alex MCP] Ingress ready on 127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
