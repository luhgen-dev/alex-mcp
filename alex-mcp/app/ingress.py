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
        "document_lines": document_lines,
        "source": source,
        "has_document_media": has_image or has_pdf,
        "received_at_utc": _received_at_utc(payload),
    }


_SAVE_NEXT_RE = re.compile(
    r"(?i)\\b(?:save|remember|keep)\\s+(?:the\\s+)?next\\s+"
    r"(?P<kind>picture|photo|image|document|pdf|file)\\b"
    r"(?:\\s+(?:as|called|named)\\s+(?P<title>.+?))?[.!?]*\\s*$"
)


def _save_next_request(text: str) -> dict | None:
    match = _SAVE_NEXT_RE.search(str(text or "").strip())
    if not match:
        return None
    kind = match.group("kind").casefold()
    title = str(match.group("title") or "").strip().strip("'\\\" ")
    if not title:
        title = "Next " + ("picture" if kind in {"picture", "photo", "image"} else "document")
    return {"kind": kind, "title": title[:200]}


def _save_next_context(actor, turn: dict) -> dict | None:
    if not turn.get("has_document_media") or turn.get("trusted_text"):
        return None
    focus = db.get_focus(actor, "save_next", consume=True)
    if not focus:
        return None
    payload = focus.get("payload") if isinstance(focus, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    title = str(payload.get("title") or "Saved item").strip()[:200]
    return {
        "recent_user_instruction": f"Save this attached item as {title}",
        "save_next": {
            "title": title,
            "kind": str(payload.get("kind") or "item")[:40],
            "focus_created_at_utc": focus.get("created_at_utc"),
        },
        "source_message_id": focus.get("source_message_id"),
        "pairing": "explicit_save_next_focus",
    }


def process(payload: dict) -> dict:
    required = ("message_id", "conversation_id", "sender_phone")
    if any(not payload.get(k) for k in required):
        return {"ok": False, "error": "missing required inbound fields"}

    claim = db.claim_inbound(payload)
    if claim == "DUPLICATE":
        return {"ok": True, "duplicate": True}

    try:
        media_ids, media_lines, vision_parts = media.process_payload_media(payload)
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
        )

        # Explicit "save the next picture/document" is a short-lived,
        # single-use actor+chat focus.  It is deterministic and costs no model
        # call until the attachment actually arrives.
        save_next = (
            _save_next_request(turn["trusted_text"])
            if not actor.media_ids else None
        )
        if save_next:
            db.set_focus(
                actor, "save_next", object_type="pending_attachment",
                payload=save_next, ttl_seconds=180,
            )
            kind_label = (
                "picture"
                if save_next["kind"] in {"picture", "photo", "image"}
                else "document"
            )
            reply = (
                f"Sure — send the next {kind_label} within 3 minutes and "
                f"I’ll save it as “{save_next['title']}”."
            )
            db.add_turn(actor.user_id, actor.conversation_id, "user", turn["trusted_text"])
            db.add_turn(actor.user_id, actor.conversation_id, "assistant", reply)
            db.queue_outbound(
                actor.conversation_id, "TEXT", text=reply,
                source_message_id=actor.source_message_id,
            )
            db.finish_inbound(actor.source_message_id, reply)
            return {"ok": True, "save_next_armed": True}

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
            quoted_context = _save_next_context(actor, turn)
        if (
            not quoted_context
            and turn["has_document_media"]
            and not turn["trusted_text"]
        ):
            quoted_context = db.resolve_recent_instruction_context(
                actor.conversation_id, actor.phone, actor.source_message_id
            )
        reply, attachments = asyncio.run(
            brain.respond(
                actor, turn["trusted_text"], turn["document_lines"], vision_parts,
                quoted_context=quoted_context,
            )
        )
        db.queue_outbound(
            actor.conversation_id, "TEXT", text=reply,
            source_message_id=actor.source_message_id,
        )
        sent_paths: set[str] = set()
        for item in attachments:
            path = item.get("path")
            kind = item.get("kind", "DOCUMENT")
            if path and path not in sent_paths:
                sent_paths.add(path)
                db.queue_outbound(
                    actor.conversation_id,
                    "IMAGE" if kind == "IMAGE" else "DOCUMENT",
                    local_path=path,
                    mime_type=item.get("mime_type"),
                    source_message_id=actor.source_message_id,
                )
        db.finish_inbound(actor.source_message_id, reply)
        return {"ok": True}
    except PermissionError as exc:
        db.fail_inbound(payload["message_id"], str(exc))
        return {"ok": False, "unauthorized": True}
    except Exception as exc:
        db.fail_inbound(payload["message_id"], str(exc))
        _record_processing_error(exc, payload)
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
