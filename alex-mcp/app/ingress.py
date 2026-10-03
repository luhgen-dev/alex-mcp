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
import phase2_reports
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

    trusted_text  = typed text only
    document_lines = OCR/PDF text (untrusted content, never user intent)

    Voice transcripts are deliberately non-authoritative in v0.5.6. Original
    audio is saved to the pending-review inbox and only later typed text may
    become executable intent.
    """
    typed = str(payload.get("text") or "").strip()
    _transcript, document_lines = media.split_voice_transcript(media_lines)
    trusted_text = typed
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
    # Global scope rule: ordinary group wording remains Family Shared.
    # No domain (salary, stash, asset, receipt, etc.) may silently infer private
    # intent. Private DM handoff requires the current command's explicit
    # private wording or emoji shortcut.
    return (explicit_private or emoji_private) and (readish or writeish)

def _dm_conversation_for_actor(actor) -> str:
    return actor.phone.replace("+", "") + "@s.whatsapp.net"


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


def _typed_reply_completes_pending_item(text: str) -> bool:
    """A typed clarification may resolve a deferred item; retrieval never does.

    Voice/media provenance is immutable. Commands such as play/listen/show/details
    inspect the pending item and must leave it pending. A real typed clarification
    or an explicit resolve/cancel command may close it after the turn succeeds.
    """
    value = str(text or "").strip()
    if not value:
        return False
    if re.fullmatch(
        r"(?is)\s*(?:(?:play|listen(?:\s+to)?|show|open|send|get|details?|info)\s+)?\d+\s*[.!]?\s*",
        value,
    ):
        return False
    if re.search(
        r"(?i)\b(?:play|listen(?:\s+to)?|show|open|send|get|details?|information)\b"
        r".*\b(?:voice|audio|recording|note|it|this|that)\b",
        value,
    ):
        return False
    return True


def _resolve_voice_pending_after_success(actor, pending: dict | None,
                                         typed_text: str) -> bool:
    """Close a quoted voice inbox item only after a verified typed action.

    Retrieval/detail questions never resolve it. A narration-only or failed
    model turn has no completed mutating tool claim, so it also stays pending.
    """
    if not pending or str(pending.get("kind") or "").upper() != "VOICE":
        return False
    if not _typed_reply_completes_pending_item(typed_text):
        return False
    if not db.has_completed_non_pending_mutation(actor.source_message_id):
        return False
    return db.resolve_pending_item(
        pending["item_id"], actor.user_id, actor.source_message_id
    )


def _yes_no_answer(text: str) -> str | None:
    value = str(text or "").strip().casefold()
    if re.fullmatch(r"(?:yes|yep|yeah|sure|ok|okay|please|please do)[.!]?", value):
        return "yes"
    if re.fullmatch(r"(?:no|nope|nah|not now|cancel)[.!]?", value):
        return "no"
    return None


def _reminder_draft_cancel_command(text: str) -> bool:
    value = str(text or "").strip().casefold().rstrip(".!")
    return value in {
        "cancel", "cancel it", "cancel that", "cancel the reminder",
        "never mind", "nevermind", "forget it",
    }


def _private_search_offer_candidate(actor, query: str, reply: str,
                                    attachments: list[dict]) -> bool:
    """Offer one private retry after a Family-scope miss without probing private data."""
    if attachments or getattr(actor, "read_scope", None) != "family":
        return False
    text = str(query or "").strip()
    if not text or not re.search(
        r"(?i)\b(?:show|find|search|list|what|where|when|which|how much|"
        r"do you remember|remember me|have i|do i have|any)\b",
        text,
    ):
        return False
    value = str(reply or "")
    return bool(re.search(
        r"(?i)\b(?:could(?:n't| not) (?:find|locate)|can(?:'t|not) (?:find|locate)|"
        r"unable to (?:find|locate)|don(?:'t| not) (?:see|have|find)|no (?:matching|saved|record|records|"
        r"asset|assets|receipt|receipts|note|notes|pool|pools|stash|stashes|"
        r"reminder|reminders)|not found|nothing (?:matching|found))\b",
        value,
    ))


def _turn_structured_scope_miss(actor) -> dict | None:
    """Read a privacy-safe scoped-miss result from this exact tool turn."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT result_json FROM tool_audit
               WHERE source_message_id=? AND status='OK'
               ORDER BY created_at_utc DESC,rowid DESC""",
            (actor.source_message_id,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            payload = json.loads(row["result_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("status") == "not_found_in_current_scope"
            and payload.get("private_search_available") is True
        ):
            return payload
    return None


def _maybe_create_private_search_offer(actor, query: str, reply: str,
                                       attachments: list[dict]) -> tuple[dict | None, str]:
    structured_miss = (
        _turn_structured_scope_miss(actor)
        if not attachments and getattr(actor, "read_scope", None) == "family"
        else None
    )
    if not structured_miss and not _private_search_offer_candidate(
        actor, query, reply, attachments
    ):
        return None, reply
    pending = db.create_pending_item(
        actor,
        "PRIVATE_SEARCH_OFFER",
        note=json.dumps(
            {"query": str(query or "").strip()},
            ensure_ascii=False, separators=(",", ":"),
        )[:4000],
    )
    if structured_miss:
        value = (
            "I couldn't find that in your shared records.\n\n"
            "I can also check your private records if you want."
        )
    else:
        value = str(reply or "").rstrip()
        if not re.search(r"(?i)\bcheck\b.{0,35}\bprivate\b", value):
            value += (
                "\n\nI only checked your shared records. "
                "I can also check your private records if you want."
            )
    return pending, value


def _private_offer_query(pending: dict) -> str | None:
    try:
        payload = json.loads(str(pending.get("note") or "{}"))
    except Exception:
        return None
    query = str(payload.get("query") or "").strip() if isinstance(payload, dict) else ""
    return query or None


def _pending_item_recent(pending: dict, max_age_seconds: int) -> bool:
    raw = str(pending.get("created_at_utc") or "").strip()
    if not raw:
        return False
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (
            runtime_clock.now_utc() - created.astimezone(timezone.utc)
            <= timedelta(seconds=max_age_seconds)
        )
    except Exception:
        return False


def _private_offer_is_current_reference(actor, pending: dict,
                                        quoted_context: dict | None) -> bool:
    """A bare Yes belongs only to the offer it visibly follows.

    An explicit quote may select that offer directly. Otherwise the offer must
    still be Alex's newest outbound in the conversation. This prevents an older
    private-search offer from stealing a Yes meant for a later prompt.
    """
    if not _pending_item_recent(pending, 600):
        return False
    item_id = str(pending.get("item_id") or "")
    if (
        quoted_context
        and quoted_context.get("context_kind") == "PENDING_ITEM"
        and str(quoted_context.get("context_id") or "") == item_id
    ):
        return True
    latest = db.resolve_recent_outbound_context(
        actor.conversation_id, "PENDING_ITEM", max_age_seconds=600
    )
    return bool(latest and str(latest.get("context_id") or "") == item_id)


def _fulfill_private_search_offer(actor, pending: dict) -> dict:
    query = _private_offer_query(pending)
    if not query:
        raise ValueError("private-search offer has no recoverable query")

    dm_conversation = _dm_conversation_for_actor(actor)
    private_actor = db.resolve_actor(
        actor.phone, dm_conversation, "DIRECT_DM",
        actor.source_message_id, [],
    )
    private_actor = replace(
        private_actor,
        source="text",
        trusted_text=query,
        received_at_utc=getattr(actor, "received_at_utc", ""),
        read_scope="private",
        private_handoff=False,
    )
    reply, attachments = asyncio.run(
        brain.respond(private_actor, query, quoted_context=None)
    )
    report_context = _report_context_for_turn(private_actor)
    if not attachments:
        db.queue_outbound(
            dm_conversation, "TEXT", text=reply,
            source_message_id=actor.source_message_id,
            context_kind="REPORT" if report_context else None,
            context_id=report_context,
        )
    sent_paths = set()
    first = True
    for item in attachments:
        path = item.get("path")
        if not path or path in sent_paths:
            continue
        sent_paths.add(path)
        kind = item.get("kind", "DOCUMENT")
        db.queue_outbound(
            dm_conversation,
            "IMAGE" if kind == "IMAGE" else "DOCUMENT",
            text=reply if first else None,
            local_path=path,
            mime_type=item.get("mime_type"),
            source_message_id=actor.source_message_id,
            context_kind="REPORT" if first and report_context else None,
            context_id=report_context if first and report_context else None,
        )
        first = False

    db.resolve_pending_item(
        pending["item_id"], actor.user_id, actor.source_message_id
    )
    if actor.conversation_type == "GROUP":
        group_reply = "I checked that in your private DM."
        db.queue_outbound(
            actor.conversation_id, "TEXT", text=group_reply,
            source_message_id=actor.source_message_id,
        )
        db.finish_inbound(actor.source_message_id, group_reply)
        return {"ok": True, "private_search_handoff": True}

    db.finish_inbound(actor.source_message_id, reply)
    return {"ok": True, "private_search": True}


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


def _report_context_for_turn(actor) -> str | None:
    """Snapshot the report spec produced by this exact inbound turn for replies."""
    active = phase2_reports.load_active_report(actor.user_id, actor.conversation_id)
    if not active:
        return None
    spec = dict(active.get("spec") or {})
    if str(spec.get("source_message_id") or "") != str(actor.source_message_id or ""):
        return None
    payload = {
        "kind": active.get("kind"),
        "period": active.get("period"),
        "spec": spec,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:4000]


def _reminder_created_by_turn(actor) -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT 1 FROM reminders
               WHERE source_message_id=? LIMIT 1""",
            (actor.source_message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _is_reminder_request(text: str) -> bool:
    return bool(re.search(
        r"(?i)\b(?:remind|reminder|rember|remnder|remidn|remindn|remidr)\b",
        str(text or ""),
    ))


def _reply_is_reminder_clarification(reply: str) -> bool:
    value = str(reply or "")
    return bool(
        "?" in value
        and re.search(
            r"(?i)\b(?:what|which|when)\b.{0,80}\b(?:day|date|time|morning|afternoon|evening)\b"
            r"|\b(?:day|date|time)\b.{0,80}\b(?:would you like|should i|do you want)\b",
            value,
        )
    )


def _looks_like_reminder_clarification_reply(text: str) -> bool:
    """Accept a concise date/time answer, not an unrelated dated sentence."""
    value = str(text or "").strip()
    if not value or len(value) > 120 or "?" in value:
        return False
    if re.search(
        r"(?i)\b(?:spent|paid|bought|expense|expenses|agenda|appointment|diary|"
        r"meeting|event|add|log|record|show|find|search|what|when|where|how|why)\b"
        r"|\b(?:rm|myr|sgd)\s*\d",
        value,
    ):
        return False

    temporal = (
        r"(?i)\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|today|tomorrow|tonight|"
        r"morning|afternoon|evening|noon|midnight)\b"
        r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
        r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
        r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
        r"|\b\d{1,2}(?::\d{2})?\b"
    )
    if not re.search(temporal, value):
        return False
    remainder = re.sub(temporal, " ", value)
    remainder = re.sub(
        r"(?i)\b(?:at|on|around|about|please|this|next|the|in|by|for)\b",
        " ",
        remainder,
    )
    remainder = re.sub(r"[\s,./()\-]+", "", remainder)
    return not remainder


def _pending_is_immediate_previous_turn(actor, pending: dict) -> bool:
    """Only the very next unquoted user turn may inherit a reminder draft."""
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT message_id FROM inbound_messages
               WHERE conversation_id=? AND sender_phone=? AND message_id<>?
               ORDER BY received_at_utc DESC,rowid DESC LIMIT 1""",
            (actor.conversation_id, actor.phone, actor.source_message_id),
        ).fetchone()
        return bool(
            row
            and str(row["message_id"]) == str(pending.get("source_message_id") or "")
        )
    finally:
        conn.close()


def _recover_reminder_draft_context(actor, text: str) -> dict | None:
    pending = db.latest_pending_item(actor, "REMINDER_DRAFT", max_age_seconds=1800)
    if not pending:
        return None
    if not _pending_is_immediate_previous_turn(actor, pending):
        return None
    if not _looks_like_reminder_clarification_reply(text):
        return None
    original = str(pending.get("original_text") or "").strip()
    if not original:
        return None
    return {
        "recent_user_instruction": original[:2000],
        "source_message_id": pending.get("source_message_id"),
        "context_kind": "PENDING_ITEM",
        "context_id": pending.get("item_id"),
        "pending_item": {
            "item_id": pending.get("item_id"),
            "kind": pending.get("kind"),
            "media_id": pending.get("media_id"),
            "source_message_id": pending.get("source_message_id"),
        },
    }


def _maybe_create_reminder_draft(actor, reply: str) -> dict | None:
    if not _is_reminder_request(getattr(actor, "trusted_text", "")):
        return None
    if _reminder_created_by_turn(actor):
        return None
    if not _reply_is_reminder_clarification(reply):
        return None
    return db.create_pending_item(
        actor,
        "REMINDER_DRAFT",
        note="Awaiting typed date/time clarification for this reminder request.",
    )


def _created_claimable_reminder_id(actor) -> str | None:
    """Bind a group reminder setup reply to the reminder created by this turn."""
    if actor.conversation_type != "GROUP":
        return None
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT reminder_id FROM reminders
               WHERE source_message_id=? AND conversation_id=?
                 AND claimable=1 AND status='OPEN'
               ORDER BY created_at_utc DESC LIMIT 2""",
            (actor.source_message_id, actor.conversation_id),
        ).fetchall()
        return str(rows[0]["reminder_id"]) if len(rows) == 1 else None
    finally:
        conn.close()


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


def _selection_set_context_parts(context: dict | None) -> tuple[str, str] | None:
    if not context or context.get("context_kind") != "SELECTION_SET":
        return None
    value = str(context.get("context_id") or "")
    if ":" not in value:
        return None
    kind, set_id = value.split(":", 1)
    if not kind or not set_id:
        return None
    return kind, set_id


def _numbered_selection_request(text: str) -> tuple[int, bool] | None:
    """Recognize read-only numbered retrieval; resolve/cancel are excluded."""
    value = str(text or "").strip()
    bare = re.fullmatch(r"(\d+)", value)
    if bare:
        return int(bare.group(1)), False
    match = re.fullmatch(
        r"(?is)\s*(?:play|listen(?:\s+to)?|hear|show|open|view|send|get)\s+"
        r"(?:(?:unresolved|pending)\s+)?"
        r"(?:(?:voice|audio)\s*note\s*)?"
        r"(?:number\s+|no\.?\s*|#\s*)?(\d+)\s*[.!]?\s*",
        value,
    )
    return (int(match.group(1)), True) if match else None


def _deliver_numbered_selection(actor, choice: int,
                                context: dict | None = None) -> dict:
    parts = _selection_set_context_parts(context)
    if parts:
        result = services.resolve_numbered_choice(
            actor, choice, selection_kind=parts[0], selection_id=parts[1]
        )
        set_context = {"kind": parts[0], "id": parts[1]}
    else:
        result = services.resolve_numbered_choice(actor, choice)
        set_context = services.latest_selection_set_context(actor)

    attachments = list(result.get("_attachments") or [])
    reply = (
        "Here it is."
        if attachments
        else str(result.get("content") or result.get("title") or result.get("note") or "Here it is.")
    )
    context_kind = "SELECTION_SET" if set_context else None
    context_id = (
        f"{set_context['kind']}:{set_context['id']}"
        if set_context else None
    )
    db.queue_outbound(
        actor.conversation_id, "TEXT", text=reply,
        source_message_id=actor.source_message_id,
        context_kind=context_kind, context_id=context_id,
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
            context_kind=context_kind, context_id=context_id,
        )
    db.finish_inbound(actor.source_message_id, reply)
    return {"ok": True, "numbered_selection": True}


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
        if payload.get("audio_data"):
            audio_id = next(
                (
                    mid for mid in media_ids
                    if (media.get_media(mid) or {}).get("media_type") == "AUDIO"
                ),
                None,
            )
            pending = db.create_pending_item(
                actor, "VOICE", audio_id,
                note="Original WhatsApp voice note saved for typed clarification.",
            )
            reply = (
                "I’ve saved this voice note for review. I don’t reliably act on "
                "voice messages, so please type what you want me to do when you "
                "have time. I’ll keep this pending until then."
            )
            db.queue_outbound(
                actor.conversation_id, "TEXT", text=reply,
                source_message_id=actor.source_message_id,
                context_kind="PENDING_ITEM",
                context_id=pending["item_id"],
            )
            db.finish_inbound(actor.source_message_id, reply)
            return {"ok": True, "voice_pending": True, "pending_item_id": pending["item_id"]}

        quoted_context = db.resolve_quoted_context(
            actor.conversation_id, payload.get("quoted_message_id"), actor.phone
        )
        if not quoted_context:
            quoted_context = _recover_reminder_draft_context(
                actor, turn["trusted_text"]
            )
        pending_item = db.pending_item_for_reference(actor, quoted_context)
        if pending_item:
            quoted_context = dict(quoted_context or {})
            quoted_context["pending_item"] = {
                "item_id": pending_item["item_id"],
                "kind": pending_item["kind"],
                "media_id": pending_item["media_id"],
                "source_message_id": pending_item["source_message_id"],
            }

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

        quoted_private_offer = (
            pending_item
            if pending_item
            and str(pending_item.get("kind") or "").upper() == "PRIVATE_SEARCH_OFFER"
            else None
        )
        private_offer = quoted_private_offer or db.latest_pending_item(
            actor, "PRIVATE_SEARCH_OFFER", max_age_seconds=600
        )
        answer = _yes_no_answer(turn["trusted_text"])
        offer_is_current = bool(
            private_offer
            and _private_offer_is_current_reference(
                actor, private_offer, quoted_context
            )
        )
        if private_offer and answer == "yes" and offer_is_current:
            return _fulfill_private_search_offer(actor, private_offer)
        if private_offer and answer == "no" and offer_is_current:
            db.cancel_pending_item(
                private_offer["item_id"], actor.user_id, actor.source_message_id
            )

        numbered_request = _numbered_selection_request(turn["trusted_text"])
        if numbered_request:
            choice, has_retrieval_verb = numbered_request
            quoted_set = (
                quoted_context
                if _selection_set_context_parts(quoted_context)
                else None
            )
            # Retrieval verbs are unambiguous read-only selection commands.
            # Bare numbers stay available to diary-conflict resolution unless
            # the user explicitly quoted a numbered-list response.
            if has_retrieval_verb or quoted_set:
                return _deliver_numbered_selection(actor, choice, quoted_set)

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
            private_report_context = _report_context_for_turn(dm_actor)
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
                        context_kind="REPORT" if first_private_attachment and private_report_context else None,
                        context_id=private_report_context if first_private_attachment and private_report_context else None,
                    )
                    first_private_attachment = False
            _resolve_voice_pending_after_success(
                actor, pending_item, turn["trusted_text"]
            )
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
        private_search_offer, reply = _maybe_create_private_search_offer(
            actor, turn["trusted_text"], reply, attachments
        )
        selection_set_context = services.latest_selection_set_context(
            actor, created_after_utc=actor.received_at_utc
        )
        selection_context = (
            None if selection_set_context
            else _selection_context_for_offer(actor, reply, attachments)
        )
        reminder_setup_id = _created_claimable_reminder_id(actor)
        reminder_draft = _maybe_create_reminder_draft(actor, reply)
        report_context = _report_context_for_turn(actor)

        if (
            pending_item
            and str(pending_item.get("kind") or "").upper() == "REMINDER_DRAFT"
            and _reminder_created_by_turn(actor)
        ):
            db.resolve_pending_item(
                pending_item["item_id"], actor.user_id, actor.source_message_id
            )
        if not attachments:
            context_kind = (
                "PENDING_ITEM" if private_search_offer
                else "PENDING_ITEM" if reminder_draft
                else "SELECTION_SET" if selection_set_context
                else "SELECTION" if selection_context
                else "REMINDER_SETUP" if reminder_setup_id
                else "REPORT" if report_context
                else None
            )
            context_id = (
                private_search_offer["item_id"] if private_search_offer
                else reminder_draft["item_id"] if reminder_draft
                else f"{selection_set_context['kind']}:{selection_set_context['id']}"
                if selection_set_context
                else f"{selection_context['kind']}:{selection_context['id']}"
                if selection_context
                else reminder_setup_id if reminder_setup_id
                else report_context
            )
            db.queue_outbound(
                actor.conversation_id, "TEXT", text=reply,
                source_message_id=actor.source_message_id,
                context_kind=context_kind,
                context_id=context_id,
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
                    context_kind="REPORT" if first_attachment and report_context else None,
                    context_id=report_context if first_attachment and report_context else None,
                )
                first_attachment = False
        _resolve_voice_pending_after_success(
            actor, pending_item, turn["trusted_text"]
        )
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
