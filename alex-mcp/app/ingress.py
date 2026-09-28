from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brain
import db
import media
import diagnostics
from config import DATA_DIR

PORT = 5001
RUNTIME_STATUS = os.path.join(DATA_DIR, "runtime_status.json")


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
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _write_runtime_status(last_processing_error=safe)
    return safe


def _received_at_utc(payload: dict) -> str:
    """Prefer WhatsApp's own send timestamp; fall back to arrival time.

    Guards against clock skew or bogus values: anything in the future or more
    than 7 days old falls back to now.
    """
    now = datetime.now(timezone.utc)
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
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/provider-probe":
            result = brain.provider_probe()
            _write_runtime_status(provider_probe=result)
            self._json(200 if result.get("status") == "ok" else 503, result)
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
