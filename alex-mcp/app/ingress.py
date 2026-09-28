from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brain
import db
import media
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


def process(payload: dict) -> dict:
    required = ("message_id", "conversation_id", "sender_phone")
    if any(not payload.get(k) for k in required):
        return {"ok": False, "error": "missing required inbound fields"}

    claim = db.claim_inbound(payload)
    if claim == "DUPLICATE":
        return {"ok": True, "duplicate": True}

    try:
        media_ids, media_context, vision_parts = media.process_payload_media(payload)
        actor = db.resolve_actor(
            payload["sender_phone"],
            payload["conversation_id"],
            payload.get("conversation_type", "DIRECT_DM"),
            payload["message_id"],
            media_ids,
        )
        reply, attachments = asyncio.run(
            brain.respond(actor, payload.get("text", "") or "", media_context, vision_parts)
        )
        db.queue_outbound(
            actor.conversation_id, "TEXT", text=reply,
            source_message_id=actor.source_message_id,
        )
        for item in attachments:
            path = item.get("path")
            kind = item.get("kind", "DOCUMENT")
            if path:
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
