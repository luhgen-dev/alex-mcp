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
import phase2_intent
import phase2_reports
import scope_policy
import diagnostics
from config import DATA_DIR
from text_normalization import normalize_intent_text

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
    intent_text = normalize_intent_text(trusted_text)
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
        "intent_text": intent_text,
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
    value = normalize_intent_text(text).strip().casefold()
    if re.fullmatch(r"(?:yes|yep|yeah|sure|ok|okay|please|please do)[.!]?", value):
        return "yes"
    if re.fullmatch(r"(?:no|nope|nah|not now|cancel)[.!]?", value):
        return "no"
    return None


def _reminder_draft_cancel_command(text: str) -> bool:
    """Recognize ordinary language that abandons an unfinished reminder draft.

    This never deletes a persisted reminder; it only closes the one active
    reminder conversation, so natural phrasing can safely be handled here.
    """
    value = re.sub(
        r"\s+", " ", normalize_intent_text(text).strip().casefold().rstrip(".!")
    )
    if re.fullmatch(r"(?:never\s*mind|nevermind)", value):
        return True
    if re.fullmatch(
        r"(?:cancel|stop|drop)"
        r"(?:\s+(?:it|that|this|the))?"
        r"(?:\s+(?:reminder|request))?",
        value,
    ):
        return True
    if re.search(
        r"\b(?:forget|scrap|skip)\b.{0,70}\b"
        r"(?:it|that|this|one|reminder|request|thing)\b",
        value,
    ):
        return True
    if re.search(
        r"\b(?:i\s+)?(?:don'?t|do\s+not)\s+need\b.{0,70}"
        r"\b(?:it|that|this|one|reminder|request|anymore|now)\b",
        value,
    ):
        return True
    if re.fullmatch(
        r"no\s+need(?:\s+(?:for\s+)?(?:it|that|this|"
        r"the\s+(?:reminder|request)|that\s+(?:reminder|request)|"
        r"this\s+(?:reminder|request)))?",
        value,
    ):
        return True
    return bool(re.fullmatch(
        r"don'?t\s+(?:remind\s+me|set\s+(?:it|that|this)|"
        r"create\s+(?:it|that|this)|do\s+(?:it|that|this))",
        value,
    ))


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
    value = str(text or "")
    if re.search(
        r"(?i)\b(?:remind|reminder|rember|remnder|remidn|remindn|remidr)\b",
        value,
    ):
        return True
    return phase2_intent.reminder_write_signal(value)


def _reply_is_reminder_clarification(reply: str) -> bool:
    """Recognize an unanswered reminder date/time question in natural wording."""
    value = str(reply or "")
    return bool(
        "?" in value
        and re.search(
            r"(?i)"
            r"\b(?:what|which)\b.{0,80}\b(?:day|date|time|morning|afternoon|evening)\b"
            r"|\b(?:day|date|time)\b.{0,80}\b(?:would you like|should i|do you want)\b"
            r"|\bwhen\b.{0,100}\b(?:remind|reminded|reminder|set|schedule)\b"
            r"|\b(?:when should i|when would you like me to)\b.{0,100}\bremind\b"
            r"|\b(?:did you mean|do you mean|could you clarify|could you confirm)\b"
            r".{0,120}\b(?:am|pm|day|date|time|today|tomorrow|tonight)\b",
            value,
        )
    )


def _reply_explicitly_interprets_reminder(reply: str) -> bool:
    """Whether Alex's own clarification clearly interpreted the turn as a reminder.

    The front AI is allowed to interpret natural wording. Creating a pending
    draft is non-terminal; the final reminder write still requires the normal
    trusted tool path and deterministic date/time validation.
    """
    value = str(reply or "")
    if "?" not in value:
        return False
    reminder_language = re.search(
        r"(?i)\b(?:remind|reminder|nudge|notify|notification|alert|ping|"
        r"heads?[ -]?up|shout|wake)\b",
        value,
    )
    slot_question = re.search(
        r"(?i)\b(?:when|what\s+time|which\s+(?:day|date)|"
        r"am\s+or\s+pm|a\.?m\.?|p\.?m\.?|today|tomorrow|tonight|"
        r"should\s+i\s+(?:set|schedule)|do\s+you\s+mean)\b",
        value,
    )
    return bool(reminder_language and slot_question)


def _reply_is_reminder_confirmation_question(reply: str) -> bool:
    """True for a closed 'shall I use these resolved reminder details?' question."""
    value = str(reply or "")
    if "?" not in value:
        return False
    # "When would you like me to remind you?" is an open slot question, not
    # something a bare Yes can answer.
    if re.search(
        r"(?i)\b(?:when|what(?:\s+time)?|which\s+(?:day|date))\b"
        r".{0,70}\b(?:would\s+you\s+like|should\s+i|do\s+you\s+want)\b",
        value,
    ):
        return False
    return bool(
        re.search(
            r"(?i)\b(?:should\s+i|shall\s+i|would\s+you\s+like\s+me\s+to|"
            r"do\s+you\s+want\s+me\s+to)\b.{0,100}"
            r"\b(?:set|schedule|remind|nudge|notify)\b",
            value,
        )
        or re.search(
            r"(?i)\b(?:is\s+that|does\s+that)\b.{0,60}"
            r"\b(?:right|correct|okay|ok|work)\b",
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
        r"later(?:\s+today)?|morning|afternoon|evening|noon|midnight)\b"
        r"|\b\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)\b"
        r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
        r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
        r"|\b\d{1,2}(?:st|nd|rd|th)\b"
        r"|\b\d{1,2}(?:[:.]\d{2})?\b"
    )
    if not re.search(temporal, value):
        return False
    remainder = re.sub(temporal, " ", value)
    remainder = re.sub(
        r"(?i)\b(?:at|on|around|about|please|this|next|coming|the|in|by|for)\b",
        " ",
        remainder,
    )
    remainder = re.sub(r"[\s,./()\-]+", "", remainder)
    return not remainder


def _reminder_draft_is_current_reference(actor, pending: dict,
                                         quoted_context: dict | None = None) -> bool:
    """A draft is current while Alex's newest visible prompt belongs to it."""
    if not _pending_item_recent(pending, 1800):
        return False
    item_id = str(pending.get("item_id") or "")
    if (
        quoted_context
        and quoted_context.get("context_kind") == "PENDING_ITEM"
        and str(quoted_context.get("context_id") or "") == item_id
    ):
        return True
    latest = db.resolve_recent_outbound_context(
        actor.conversation_id, "PENDING_ITEM", max_age_seconds=1800
    )
    return bool(latest and str(latest.get("context_id") or "") == item_id)


def _reminder_draft_context(pending: dict) -> dict:
    accumulated = str(
        pending.get("accumulated_text")
        or pending.get("original_text")
        or ""
    ).strip()
    return {
        "recent_user_instruction": accumulated[:4000],
        "source_message_id": pending.get("source_message_id"),
        "context_kind": "PENDING_ITEM",
        "context_id": pending.get("item_id"),
        "pending_item": {
            "item_id": pending.get("item_id"),
            "kind": pending.get("kind"),
            "media_id": pending.get("media_id"),
            "source_message_id": pending.get("source_message_id"),
            "accumulated_text": accumulated[:4000],
            "routing_json": str(pending.get("routing_json") or ""),
        },
    }


def _semantic_reminder_continuation(
    actor, pending: dict, value: str, latest_question: str
) -> dict | None:
    """Return one grounded positive semantic frame, or fail closed."""
    accumulated = str(
        pending.get("accumulated_text")
        or pending.get("original_text")
        or ""
    ).strip()
    frame = brain.interpret_control_intent(
        actor,
        control_kind="REMINDER_DRAFT",
        current_text=value,
        pending_text=accumulated,
        latest_question=latest_question,
        allowed_intents={
            "ANSWER_PENDING", "CONFIRM_PENDING", "DECLINE_PENDING",
            "CANCEL_PENDING", "NEW_REQUEST", "UNCLEAR",
        },
    )
    intent = str(frame.get("intent") or "")
    confidence = float(frame.get("confidence") or 0.0)
    normalized = str(frame.get("normalized_reply") or "").strip()
    if confidence < brain.SEMANTIC_GATEWAY_MIN_CONFIDENCE:
        return None
    if intent == "ANSWER_PENDING" and normalized:
        return frame
    if intent == "CONFIRM_PENDING":
        return frame
    return None


def _reminder_draft_continuation(
    actor, pending: dict, text: str
) -> tuple[bool, str, str]:
    """Decide whether a natural reply belongs to one active reminder draft.

    The third return value is an optional, bounded semantic time hint. It is
    never an object reference, privacy signal, assignee, or destination.
    """
    value = str(text or "").strip()
    if not value or len(value) > 180:
        return False, "", ""

    latest = db.latest_outbound_for_context(
        actor.conversation_id,
        "PENDING_ITEM",
        str(pending.get("item_id") or ""),
        max_age_seconds=1800,
    )
    latest_question = str((latest or {}).get("text_body") or "").strip()

    if _reminder_draft_cancel_command(value):
        return True, latest_question, ""

    # A fresh reminder request starts/replaces a reminder conversation; it must
    # never be swallowed as a date/time answer to an older draft.
    if _is_reminder_request(value):
        return False, latest_question, ""

    domain_switch = bool(re.search(
        r"(?i)\b(?:spent|paid|bought|expense|receipt|shopping|task|diary|"
        r"calendar|meeting|appointment|show|find|search|list|what|why|how|"
        r"where|goal|stash|cash|report|roster|shift|turn\s+(?:on|off)|"
        r"switch\s+(?:on|off))\b"
        r"|\b(?:rm|myr|sgd)\s*\d",
        value,
    ))

    # Keep the proven concise date/time fast path at zero tokens.
    if _looks_like_reminder_clarification_reply(value) and not domain_switch:
        return True, latest_question, ""

    # Existing natural-temporal continuation remains a safe fallback, but use
    # the narrow interpreter first so informal forms can also produce a
    # normalized time hint for the deterministic reminder validator. If the
    # interpreter is unavailable or uncertain, preserve the old continuation
    # behaviour rather than regressing a previously working phrase.
    natural_temporal = bool(re.search(
        r"(?i)\b(?:today|tomorrow|tonight|later|night|morning|afternoon|"
        r"evening|noon|midnight|after\s+(?:work|dinner|lunch)|"
        r"before\s+(?:work|bed|dinner)|half\s+past|quarter\s+(?:past|to))\b"
        r"|\b\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\b",
        value,
    ))
    if (
        natural_temporal
        and latest_question
        and _reply_is_reminder_clarification(latest_question)
        and "?" not in value
        and not domain_switch
    ):
        semantic_hint = ""
        if not services.reminder_time_text_is_deterministic(value):
            frame = _semantic_reminder_continuation(
                actor, pending, value, latest_question
            )
            semantic_hint = str(
                (frame or {}).get("normalized_reply") or ""
            ).strip()[:240]
        return True, latest_question, semantic_hint

    answer = _yes_no_answer(value)
    if answer and _reply_is_reminder_confirmation_question(latest_question):
        return True, latest_question, ""

    # Preserve the already-proven closed-confirmation continuation behaviour.
    if (
        latest_question
        and _reply_is_reminder_confirmation_question(latest_question)
        and "?" not in value
        and not domain_switch
    ):
        return True, latest_question, ""

    # Phase-1 semantic rescue is narrow: one grounded reminder draft, only
    # after the deterministic gates above, and only while Alex is visibly
    # waiting for a reminder answer. The interpreter has no tools or IDs.
    grounded_prompt = bool(
        latest_question
        and (
            _reply_is_reminder_clarification(latest_question)
            or _reply_is_reminder_confirmation_question(latest_question)
        )
    )
    if grounded_prompt and not domain_switch:
        frame = _semantic_reminder_continuation(
            actor, pending, value, latest_question
        )
        if frame:
            return (
                True,
                latest_question,
                str(frame.get("normalized_reply") or "").strip()[:240],
            )

    return False, latest_question, ""

def _recover_reminder_draft_context(actor, text: str) -> dict | None:
    """Bind natural continuation language to the one active reminder draft."""
    pending = db.single_pending_item(
        actor, "REMINDER_DRAFT", max_age_seconds=1800
    )
    if not pending:
        return None
    continues, latest_question, semantic_hint = _reminder_draft_continuation(
        actor, pending, text
    )
    if not continues:
        return None
    context = _reminder_draft_context(pending)
    if latest_question:
        context["quoted_alex_text"] = latest_question[:1000]
    if semantic_hint:
        context["semantic_reminder_text"] = semantic_hint[:240]
    return context


def _latest_reminder_draft_question(actor, pending: dict) -> str:
    row = db.latest_outbound_for_context(
        actor.conversation_id,
        "PENDING_ITEM",
        str(pending.get("item_id") or ""),
        max_age_seconds=1800,
    )
    text = str((row or {}).get("text_body") or "").strip()
    if text and (
        _reply_is_reminder_clarification(text)
        or _reply_is_reminder_confirmation_question(text)
    ):
        return text
    return "What day, date, or time should I use for that reminder?"


def _maybe_create_reminder_draft(actor, reply: str) -> dict | None:
    if _reminder_created_by_turn(actor):
        return None

    value = str(reply or "").strip()
    user_requested = _is_reminder_request(
        getattr(actor, "intent_text", "")
        or getattr(actor, "trusted_text", "")
    )
    ai_interpreted = _reply_explicitly_interprets_reminder(value)
    if not (user_requested or ai_interpreted):
        return None

    # The AI may interpret novel layman wording, but only an unanswered
    # reminder clarification creates durable pending state. No real reminder is
    # written here; the final write remains below deterministic tool validation.
    if "?" not in value:
        return None

    return db.create_pending_item(
        actor,
        "REMINDER_DRAFT",
        note="Awaiting natural reminder clarification/confirmation.",
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


def _record_deterministic_turn(actor, reply: str) -> None:
    """Keep the model-visible conversation aligned with deterministic replies."""
    db.add_turn(
        actor.user_id, actor.conversation_id, "user",
        str(getattr(actor, "trusted_text", "") or "").strip(),
    )
    db.add_turn(actor.user_id, actor.conversation_id, "assistant", reply)


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
    _record_deterministic_turn(actor, reply)
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
    _record_deterministic_turn(actor, reply)
    db.finish_inbound(actor.source_message_id, reply)
    return {"ok": True, "selection_followup": True}


def _finish_simple_turn(actor, reply: str, **extra) -> dict:
    db.queue_outbound(
        actor.conversation_id, "TEXT", text=reply,
        source_message_id=actor.source_message_id,
    )
    _record_deterministic_turn(actor, reply)
    db.finish_inbound(actor.source_message_id, reply)
    return {"ok": True, **extra}


def _reopen_existing_reminder_command(text: str) -> bool:
    low = normalize_intent_text(text).casefold()
    return bool(
        re.search(
            r"\b(?:re[- ]?open|open\s+(?:it|that|this|the\s+reminder)\s+again)\b"
            r".*\b(?:reminder|remind)\b"
            r"|\b(?:reminder|remind)\b.*\b(?:re[- ]?open|open\s+again)\b",
            low,
        )
    )


def _return_claimed_reminder_to_family_command(
    text: str, quoted_context: dict | None = None
) -> bool:
    low = normalize_intent_text(text).casefold()
    object_bound = bool(
        quoted_context
        and quoted_context.get("context_kind") == "REMINDER_CLAIM_CONFIRMED"
        and quoted_context.get("context_id")
    )
    if object_bound and re.search(
        r"\b(?:release|unclaim)\b"
        r"|\b(?:push|send|put|return|give)\b.{0,50}\b(?:back|group|mcp home|family)\b"
        r"|\b(?:i can'?t|i cannot|i can not)\b.{0,45}\b(?:do|handle|take|claim)\b"
        r"|\bnot\s+me\b",
        low,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:release|unclaim)\b.*\breminder\b"
            r"|\b(?:push|send|put|return)\b.*\breminder\b.*"
            r"\b(?:back|group|mcp home|family)\b"
            r"|\breminder\b.*\b(?:back to|into)\b.*\b(?:group|mcp home|family)\b"
            r"|\b(?:i can'?t|i cannot|i can not|i don'?t think(?: so)? i can)\b"
            r".*\b(?:claim|handle|take|do)\b.*\b(?:it|this|reminder)?\b",
            low,
        )
    )


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
            intent_text=turn["intent_text"],
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
            _record_deterministic_turn(actor, reply)
            db.finish_inbound(actor.source_message_id, reply)
            return {"ok": True, "voice_pending": True, "pending_item_id": pending["item_id"]}

        explicit_group_quote_handoff = bool(
            actor.conversation_type == "GROUP"
            and payload.get("alex_mentioned")
            and payload.get("quoted_message_id")
            and not payload.get("reply_to_alex")
        )
        quoted_context = db.resolve_quoted_context(
            actor.conversation_id,
            payload.get("quoted_message_id"),
            actor.phone,
            allow_group_peer_quote=explicit_group_quote_handoff,
        )

        bridge_quoted_text = ""
        if not quoted_context and explicit_group_quote_handoff:
            quoted_type = str(payload.get("quoted_type") or "").strip().lower()
            bridge_quoted_text = str(payload.get("quoted_text") or "").strip()
            if quoted_type in {"image", "pdf", "audio", "other"}:
                label = {
                    "image": "image",
                    "pdf": "PDF",
                    "audio": "audio message",
                    "other": "file",
                }[quoted_type]
                return _finish_simple_turn(
                    actor,
                    f"I can see you replied to a {label}, but I can’t read the "
                    "quoted file itself. Please resend it with @Alex.",
                    quoted_media_resend=True,
                )
            if quoted_type == "text" and bridge_quoted_text:
                # Ordinary Family Shared chat is intentionally never persisted.
                # The authenticated mentioner is handing this client-supplied
                # quote to Alex now; it is context, not verified authorship.
                quoted_context = {
                    "quoted_user_text": bridge_quoted_text[:2000],
                    "quoted_group_peer": True,
                    "quoted_provenance": "bridge",
                    "quoted_message_id": payload.get("quoted_message_id"),
                    "quoted_participant_phone": payload.get(
                        "quoted_participant_phone"
                    ),
                    "group_mention_authorized": True,
                }
            else:
                return _finish_simple_turn(
                    actor,
                    "I couldn’t read the message you replied to. Please type it, or resend it with @Alex.",
                    quoted_context_unreadable=True,
                )

        if quoted_context and explicit_group_quote_handoff:
            quoted_context = dict(quoted_context)
            quoted_context["group_mention_authorized"] = True
            quoted_text = str(
                quoted_context.get("quoted_user_text") or bridge_quoted_text or ""
            ).strip()
            if quoted_text:
                # The current authenticated @mention deliberately hands this
                # already-visible Family Shared quote to Alex. If the AI
                # interprets it as a reminder, preserve both the quoted text and
                # a small deterministic routing envelope on any draft it opens.
                reminder_basis = "\n".join(
                    part for part in (
                        quoted_text,
                        str(turn["trusted_text"] or "").strip(),
                    ) if part
                )
                routing = services.reminder_draft_routing_envelope(
                    actor,
                    reminder_basis,
                    origin="FAMILY_QUOTE_HANDOFF",
                )
                actor = replace(
                    actor,
                    reminder_context_text=reminder_basis,
                    reminder_routing_json=(
                        json.dumps(
                            routing, ensure_ascii=False, separators=(",", ":")
                        )
                        if routing else ""
                    ),
                )

        reminder_continuation_text = str(turn["trusted_text"] or "").strip()
        if (
            not reminder_continuation_text
            and quoted_context
            and quoted_context.get("group_mention_authorized")
        ):
            reminder_continuation_text = str(
                quoted_context.get("quoted_user_text") or ""
            ).strip()

        # A handed-off quoted time/date may itself be the missing answer to the
        # one active reminder draft. Resolve that before generic AI routing.
        # A direct WhatsApp quote is already authoritative context, so do not
        # spend a semantic call trying to recover some other active draft.
        may_recover_draft = bool(
            reminder_continuation_text
            and (
                not quoted_context
                or quoted_context.get("quoted_provenance") == "bridge"
            )
        )
        draft_context = (
            _recover_reminder_draft_context(actor, reminder_continuation_text)
            if may_recover_draft else None
        )
        recovered_reminder_draft_id = str(
            (draft_context or {}).get("context_id") or ""
        )
        if draft_context and (
            not quoted_context
            or quoted_context.get("quoted_provenance") == "bridge"
        ):
            handoff_metadata = {}
            if quoted_context and quoted_context.get("quoted_provenance") == "bridge":
                handoff_metadata = {
                    "quoted_user_text": quoted_context.get("quoted_user_text"),
                    "quoted_group_peer": quoted_context.get("quoted_group_peer"),
                    "quoted_provenance": "bridge",
                    "quoted_message_id": quoted_context.get("quoted_message_id"),
                    "quoted_participant_phone": quoted_context.get(
                        "quoted_participant_phone"
                    ),
                    "group_mention_authorized": True,
                }
            quoted_context = dict(draft_context)
            quoted_context.update({
                key: value for key, value in handoff_metadata.items()
                if value not in (None, "")
            })
        elif not quoted_context:
            quoted_context = draft_context

        pending_item = db.pending_item_for_reference(actor, quoted_context)
        reminder_draft_continuation = False

        # An explicit swipe-reply to an old private-search offer is authoritative
        # even after that offer expires. Consume the reply here rather than
        # allowing a newer reminder draft or other workflow to steal a bare
        # "Yes"/"No".
        quoted_pending_history = db.pending_item_history_for_reference(
            actor, quoted_context
        )
        quoted_answer = _yes_no_answer(turn["intent_text"])
        if (
            quoted_pending_history
            and str(quoted_pending_history.get("kind") or "").upper()
                == "REMINDER_DRAFT"
            and str(quoted_pending_history.get("status") or "").upper()
                != "PENDING"
            and _reminder_draft_cancel_command(turn["intent_text"])
        ):
            return _finish_simple_turn(
                actor,
                "That reminder request is already no longer active.",
                reminder_draft_cancelled=True,
            )
        if (
            quoted_pending_history
            and str(quoted_pending_history.get("kind") or "").upper()
                == "PRIVATE_SEARCH_OFFER"
            and quoted_answer
            and (
                str(quoted_pending_history.get("status") or "").upper() != "PENDING"
                or not _pending_item_recent(quoted_pending_history, 600)
            )
        ):
            return _finish_simple_turn(
                actor,
                "That private-search offer has expired. Ask me again if you want me to check your private records.",
                expired_private_search_offer=True,
            )

        if (
            pending_item
            and str(pending_item.get("kind") or "").upper() == "REMINDER_DRAFT"
        ):
            if (
                recovered_reminder_draft_id
                and str(pending_item.get("item_id") or "")
                == recovered_reminder_draft_id
            ):
                # Recovery above already made the one continuation decision for
                # this turn. Reuse it so a semantic rescue can never cost two
                # model calls for the same inbound message.
                continues = True
                latest_question = str(
                    (draft_context or {}).get("quoted_alex_text") or ""
                ).strip()
                semantic_hint = str(
                    (draft_context or {}).get("semantic_reminder_text") or ""
                ).strip()[:240]
            else:
                continues, latest_question, semantic_hint = (
                    _reminder_draft_continuation(
                        actor, pending_item, reminder_continuation_text
                    )
                )
            if continues:
                pending_item = db.append_pending_item_text(
                    pending_item["item_id"],
                    actor.user_id,
                    reminder_continuation_text,
                )
                accumulated = str(
                    pending_item.get("accumulated_text")
                    or reminder_continuation_text
                    or ""
                ).strip()
                actor = replace(
                    actor,
                    reminder_context_text=accumulated,
                    reminder_routing_json=str(
                        pending_item.get("routing_json") or ""
                    ),
                    reminder_semantic_text=semantic_hint,
                )
                quoted_context = _reminder_draft_context(pending_item)
                if latest_question:
                    quoted_context["quoted_alex_text"] = latest_question[:1000]
                if semantic_hint:
                    quoted_context["semantic_reminder_text"] = semantic_hint[:240]
                reminder_draft_continuation = True
        if pending_item and not reminder_draft_continuation:
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
            turn["intent_text"]
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

        quoted_reminder_draft = (
            pending_item
            if pending_item
            and str(pending_item.get("kind") or "").upper() == "REMINDER_DRAFT"
            else None
        )
        reminder_draft_for_cancel = quoted_reminder_draft or db.single_pending_item(
            actor, "REMINDER_DRAFT", max_age_seconds=1800
        )
        draft_is_current = bool(
            quoted_reminder_draft or reminder_draft_for_cancel
        )
        if (
            reminder_draft_for_cancel
            and draft_is_current
            and _reminder_draft_cancel_command(turn["intent_text"])
        ):
            db.cancel_pending_item(
                reminder_draft_for_cancel["item_id"],
                actor.user_id,
                actor.source_message_id,
            )
            return _finish_simple_turn(
                actor,
                "Okay, I cancelled that reminder request.",
                reminder_draft_cancelled=True,
            )

        # Explicit reminder lifecycle controls are deterministic because
        # they mutate an already-persisted object and must not degrade into a
        # broad reminder list. Natural task words are resolved conservatively
        # inside the authenticated actor's ACL.
        if _reopen_existing_reminder_command(turn["intent_text"]):
            try:
                reopened = services.update_reminder(
                    actor,
                    None,
                    status="open",
                    reminder_reference=turn["trusted_text"],
                )
            except ValueError as exc:
                code = str(exc)
                if "AMBIGUOUS" in code:
                    return _finish_simple_turn(
                        actor,
                        "Which reminder do you want me to reopen?",
                        reminder_lifecycle_clarification=True,
                    )
                if "NOT_FOUND" in code:
                    return _finish_simple_turn(
                        actor,
                        "I couldn’t find that reminder to reopen.",
                        reminder_lifecycle_not_found=True,
                    )
                raise
            task = str(reopened.get("task") or "that reminder")
            if reopened.get("fresh_family_card"):
                reply = (
                    f"I’ve reopened “{task}” and posted a fresh claimable "
                    "message in MCP Home."
                )
            else:
                reply = f"I’ve reopened “{task}”."
            return _finish_simple_turn(
                actor, reply, reminder_reopened=True,
            )

        if _return_claimed_reminder_to_family_command(
            turn["intent_text"], quoted_context
        ):
            quoted_claim_reminder_id = (
                str(quoted_context.get("context_id") or "")
                if quoted_context
                and quoted_context.get("context_kind") == "REMINDER_CLAIM_CONFIRMED"
                else ""
            )
            try:
                released = services.release_reminder_claim(
                    actor,
                    quoted_claim_reminder_id or None,
                    reminder_reference=(
                        None if quoted_claim_reminder_id
                        else turn["trusted_text"]
                    ),
                )
            except ValueError as exc:
                code = str(exc)
                if "AMBIGUOUS" in code:
                    return _finish_simple_turn(
                        actor,
                        "Which claimed reminder do you want me to put back in MCP Home?",
                        reminder_lifecycle_clarification=True,
                    )
                if "NOT_FOUND" in code:
                    return _finish_simple_turn(
                        actor,
                        "I couldn’t find a claimed reminder of yours to put back in MCP Home.",
                        reminder_lifecycle_not_found=True,
                    )
                raise
            task = str(released.get("task") or "that reminder")
            if released.get("status") == "already_unclaimed":
                reply = f"“{task}” is already available to the family."
            else:
                reply = (
                    f"I’ve put “{task}” back in MCP Home as a fresh claimable reminder."
                )
            return _finish_simple_turn(
                actor, reply, reminder_claim_released=True,
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

        # A yes/no cannot supply a missing time, but it *can* answer a closed
        # confirmation after Alex has already proposed complete reminder
        # details. In that case preserve the draft context and let the AI
        # interpret the natural acknowledgement; otherwise re-ask the slot.
        if answer and not (private_offer and offer_is_current):
            short_answer_draft = (
                pending_item
                if pending_item
                and str(pending_item.get("kind") or "").upper() == "REMINDER_DRAFT"
                else None
            )
            if not short_answer_draft and not payload.get("quoted_message_id"):
                short_answer_draft = db.single_pending_item(
                    actor, "REMINDER_DRAFT", max_age_seconds=1800
                )
            if short_answer_draft:
                latest_question = _latest_reminder_draft_question(
                    actor, short_answer_draft
                )
                if not _reply_is_reminder_confirmation_question(latest_question):
                    return _finish_simple_turn(
                        actor,
                        latest_question,
                        reminder_draft_reprompted=True,
                    )
            elif not payload.get("quoted_message_id"):
                return _finish_simple_turn(
                    actor,
                    "What are you saying yes or no to?",
                    ambiguous_short_answer=True,
                )

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
            _record_deterministic_turn(actor, group_reply)
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
        continuing_reminder_draft = (
            pending_item
            if pending_item
            and str(pending_item.get("kind") or "").upper() == "REMINDER_DRAFT"
            else None
        )
        reminder_created = _reminder_created_by_turn(actor)
        if (
            continuing_reminder_draft
            and not reminder_created
            and reminder_draft_continuation
            and "?" in str(reply or "")
        ):
            # A temporal continuation stays bound when Alex asks another
            # question, regardless of provider wording. Terminal/guard/error
            # replies have no question mark and must never become the draft's
            # new pinned prompt.
            reminder_draft = continuing_reminder_draft
        else:
            reminder_draft = _maybe_create_reminder_draft(actor, reply)
        report_context = _report_context_for_turn(actor)

        if reminder_created:
            draft_to_resolve = continuing_reminder_draft
            if (
                not draft_to_resolve
                and _looks_like_reminder_clarification_reply(
                    turn["trusted_text"]
                )
            ):
                # A pure date/time answer that successfully creates a reminder
                # is completion of the newest active reminder draft, even if a
                # provider quote ID was unavailable on this turn.
                draft_to_resolve = db.latest_pending_item(
                    actor, "REMINDER_DRAFT", max_age_seconds=1800
                )
            if draft_to_resolve:
                db.resolve_pending_item(
                    draft_to_resolve["item_id"],
                    actor.user_id,
                    actor.source_message_id,
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
