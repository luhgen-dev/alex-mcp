from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import DATA_DIR, get_settings
from db import connect
import runtime_clock

SELFTEST_FILE = os.path.join(DATA_DIR, "selftest.json")


def _cutoff(hours: int) -> str:
    bounded = max(1, min(24 * 30, int(hours)))
    return (runtime_clock.now_utc() - timedelta(hours=bounded)).isoformat()


def _selftest() -> dict | None:
    try:
        with open(SELFTEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def usage_summary(hours: int = 24) -> dict:
    """Observed local model usage only; reading this never calls an AI provider."""
    bounded = max(1, min(24 * 30, int(hours)))
    cutoff = _cutoff(bounded)
    conn = connect()
    try:
        row = conn.execute(
            """SELECT COUNT(DISTINCT COALESCE(source_message_id,usage_id)) AS interactions,
                      COALESCE(SUM(CASE WHEN source_message_id IS NULL THEN 1 ELSE 0 END),0) AS probes,
                      COALESCE(SUM(model_calls),0) AS model_calls,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                      COALESCE(SUM(tool_rounds),0) AS tool_rounds,
                      COALESCE(AVG(latency_ms),0) AS avg_latency,
                      COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost
               FROM ai_usage WHERE created_at_utc>=?""",
            (cutoff,),
        ).fetchone()
        providers = conn.execute(
            """SELECT provider,model,
                      COALESCE(SUM(model_calls),0) AS model_calls,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                      COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost
               FROM ai_usage WHERE created_at_utc>=?
               GROUP BY provider,model
               ORDER BY estimated_cost DESC,model_calls DESC""",
            (cutoff,),
        ).fetchall()
        total_in = int(row["input_tokens"] or 0)
        cached = min(total_in, int(row["cached_input_tokens"] or 0))
        by_provider = [{
            "provider": r["provider"], "model": r["model"],
            "model_calls": int(r["model_calls"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "cached_input_tokens": int(r["cached_input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "reasoning_tokens": int(r["reasoning_tokens"] or 0),
            "cost_usd": round(float(r["estimated_cost"] or 0), 6),
        } for r in providers]
        return {
            "window_hours": bounded,
            "interactions": int(row["interactions"] or 0),
            "provider_probes": int(row["probes"] or 0),
            "model_calls": int(row["model_calls"] or 0),
            "input_tokens": total_in,
            "cached_input_tokens": cached,
            "uncached_input_tokens": max(0, total_in - cached),
            "output_tokens": int(row["output_tokens"] or 0),
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            "tool_rounds": int(row["tool_rounds"] or 0),
            "cache_ratio_pct": round((cached / total_in * 100.0) if total_in else 0.0, 1),
            "average_ai_latency_ms": round(float(row["avg_latency"] or 0), 1),
            "estimated_ai_cost_usd": round(float(row["estimated_cost"] or 0), 6),
            "by_provider": by_provider,
            "note": (
                "Local telemetry only; reading this makes no AI request. "
                "xAI rows use provider-reported billed cost when available; other rows use configured token rates."
            ),
        }
    finally:
        conn.close()


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[index])


def delivery_latency_summary(hours: int = 24) -> dict:
    """Separate Alex processing/queue time from durable WhatsApp egress delay.

    This cannot measure when the phone UI actually rendered a message, but it
    can prove whether a delay happened before queueing or while the durable
    outbox retried the bridge.
    """
    bounded = max(1, min(24 * 30, int(hours)))
    cutoff = _cutoff(bounded)
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT o.source_message_id,o.created_at_utc,o.delivered_at_utc,
                      o.attempt_count,o.delivery_status,
                      i.received_at_utc,i.completed_at_utc
               FROM outbound_messages o
               LEFT JOIN inbound_messages i ON i.message_id=o.source_message_id
               WHERE o.created_at_utc>=? AND o.kind='TEXT'
               ORDER BY o.created_at_utc DESC LIMIT 500""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    processing_ms: list[float] = []
    egress_ms: list[float] = []
    end_to_end_ms: list[float] = []
    attempts: list[int] = []
    delivered = 0
    for row in rows:
        received = _parse_utc(row["received_at_utc"])
        queued = _parse_utc(row["created_at_utc"])
        delivered_at = _parse_utc(row["delivered_at_utc"])
        if received and queued:
            processing_ms.append(max(0.0, (queued - received).total_seconds() * 1000.0))
        if queued and delivered_at:
            delivered += 1
            egress_ms.append(max(0.0, (delivered_at - queued).total_seconds() * 1000.0))
        if received and delivered_at:
            end_to_end_ms.append(max(0.0, (delivered_at - received).total_seconds() * 1000.0))
        attempts.append(int(row["attempt_count"] or 0))

    def stats(values: list[float]) -> dict:
        return {
            "count": len(values),
            "p50_ms": round(_percentile(values, 0.50), 1),
            "p95_ms": round(_percentile(values, 0.95), 1),
            "max_ms": round(max(values) if values else 0.0, 1),
        }

    return {
        "window_hours": bounded,
        "queued_text_rows": len(rows),
        "delivered_text_rows": delivered,
        "processing_to_queue": stats(processing_ms),
        "queue_to_bridge_delivery": stats(egress_ms),
        "inbound_to_bridge_delivery": stats(end_to_end_ms),
        "max_delivery_attempts": max(attempts) if attempts else 0,
        "interpretation": (
            "processing_to_queue isolates Alex/AI/tool time; queue_to_bridge_delivery "
            "isolates durable outbox/bridge retry delay. Phone rendering remains external."
        ),
    }


def system_health(actor, hours: int = 24) -> dict:
    cutoff = _cutoff(hours)
    settings = get_settings()
    conn = connect()
    try:
        db_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        failed_inbound = conn.execute(
            "SELECT COUNT(*) FROM inbound_messages WHERE processing_state='FAILED' AND received_at_utc>=?",
            (cutoff,),
        ).fetchone()[0]
        pending_outbound = conn.execute(
            "SELECT COUNT(*) FROM outbound_messages WHERE delivery_status='PENDING'"
        ).fetchone()[0]
        failed_outbound = conn.execute(
            "SELECT COUNT(*) FROM outbound_messages WHERE delivery_status='FAILED' AND created_at_utc>=?",
            (cutoff,),
        ).fetchone()[0]
        tool_errors = conn.execute(
            "SELECT COUNT(*) FROM tool_audit WHERE status='ERROR' AND created_at_utc>=?",
            (cutoff,),
        ).fetchone()[0]
        control_failures = conn.execute(
            """SELECT COUNT(*) FROM outbound_messages
               WHERE job_control_failed_at_utc IS NOT NULL
                 AND created_at_utc>=?""",
            (cutoff,),
        ).fetchone()[0]
        control_last_error = conn.execute(
            """SELECT outbound_id,context_kind,job_control_last_kind,
                      job_control_attempts,job_control_last_error,
                      job_control_failed_at_utc
               FROM outbound_messages
               WHERE job_control_last_error IS NOT NULL
               ORDER BY COALESCE(job_control_failed_at_utc,created_at_utc) DESC,rowid DESC
               LIMIT 1"""
        ).fetchone()
        ha_pending = conn.execute(
            "SELECT COUNT(*) FROM ha_notification_outbox WHERE delivery_status='PENDING'"
        ).fetchone()[0]
        ha_failed = conn.execute(
            """SELECT COUNT(*) FROM ha_notification_outbox
               WHERE delivery_status='FAILED' AND created_at_utc>=?""",
            (cutoff,),
        ).fetchone()[0]
        ha_last_error = conn.execute(
            """SELECT last_error,notify_service,created_at_utc
               FROM ha_notification_outbox
               WHERE last_error IS NOT NULL AND TRIM(last_error)<>''
               ORDER BY created_at_utc DESC,rowid DESC LIMIT 1"""
        ).fetchone()
        usage = conn.execute(
            """SELECT COUNT(DISTINCT COALESCE(source_message_id,usage_id)) AS interactions,
                      COALESCE(SUM(model_calls),0) AS model_calls,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                      COALESCE(AVG(latency_ms),0) AS avg_latency,
                      COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost
               FROM ai_usage WHERE created_at_utc>=?""",
            (cutoff,),
        ).fetchone()
        last_completed = conn.execute(
            """SELECT completed_at_utc FROM inbound_messages
               WHERE processing_state='COMPLETED' ORDER BY completed_at_utc DESC LIMIT 1"""
        ).fetchone()
        voice_rows = conn.execute(
            """SELECT transcript_meta_json,created_at_utc
               FROM media_objects
               WHERE media_type='AUDIO' AND created_at_utc>=?
               ORDER BY created_at_utc DESC LIMIT 100""",
            (cutoff,),
        ).fetchall()
        voice_meta = []
        for voice_row in voice_rows:
            try:
                meta = json.loads(voice_row["transcript_meta_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                meta = {}
            if isinstance(meta, dict):
                voice_meta.append(meta)
        voice_rescues = sum(
            1 for meta in voice_meta
            if meta.get("cloud_rescue")
            or str(meta.get("mode") or "").endswith("_verify")
        )
        voice_low_confidence = 0
        for meta in voice_meta:
            candidates = meta.get("candidates") or []
            chosen = str(meta.get("chosen") or "")
            selected = next(
                (row for row in candidates if str(row.get("label") or "") == chosen),
                None,
            )
            if selected is not None and int(selected.get("score") or 0) < 2:
                voice_low_confidence += 1
        selftest = _selftest()
        return {
            "window_hours": max(1, min(24 * 30, int(hours))),
            "database": "ok" if db_ok else "problem",
            "startup_selftest": selftest,
            "provider": {
                "name": settings.ai_provider,
                "model": settings.model,
                "api_key_present": bool(settings.api_key),
                "gemini_ready": bool(settings.gemini_api_key),
                "grok_ready": bool(settings.xai_api_key),
                "openai_ready": bool(settings.openai_api_key),
                "auto_primary": (
                    settings.gemini_lite_model if settings.ai_provider == "auto"
                    and settings.gemini_api_key else settings.model
                ),
            },
            "observed": {
                "failed_inbound": int(failed_inbound),
                "pending_outbound": int(pending_outbound),
                "failed_outbound": int(failed_outbound),
                "tool_errors": int(tool_errors),
                "whatsapp_controls": {
                    "failed_rows": int(control_failures),
                    "latest_error": (
                        {
                            "outbound_id": control_last_error["outbound_id"],
                            "context_kind": control_last_error["context_kind"],
                            "kind": control_last_error["job_control_last_kind"],
                            "attempts": int(control_last_error["job_control_attempts"] or 0),
                            "error": str(control_last_error["job_control_last_error"] or "")[:500],
                            "failed_at_utc": control_last_error["job_control_failed_at_utc"],
                        }
                        if control_last_error else None
                    ),
                    "note": (
                        "WhatsApp pin/unpin/reaction controls use bounded retry; "
                        "a failed row requires inspection rather than a hot loop."
                    ),
                },
                "ha_companion": {
                    "configured_devices": len(settings.ha_notify_devices or []),
                    "pending_notifications": int(ha_pending),
                    "failed_notifications": int(ha_failed),
                    "latest_error": (
                        {
                            "notify_service": ha_last_error["notify_service"],
                            "error": str(ha_last_error["last_error"])[:500],
                            "created_at_utc": ha_last_error["created_at_utc"],
                        }
                        if ha_last_error else None
                    ),
                    "note": (
                        "Configured-device count is local configuration only; "
                        "delivery is proven by outbox SENT/receipt evidence."
                    ),
                },
                "ai_interactions": int(usage["interactions"] or 0),
                "model_calls": int(usage["model_calls"] or 0),
                "input_tokens": int(usage["input_tokens"] or 0),
                "cached_input_tokens": int(usage["cached_input_tokens"] or 0),
                "output_tokens": int(usage["output_tokens"] or 0),
                "reasoning_tokens": int(usage["reasoning_tokens"] or 0),
                "average_ai_latency_ms": round(float(usage["avg_latency"] or 0), 1),
                "estimated_ai_cost_usd": round(float(usage["estimated_cost"] or 0), 6),
                "last_completed_message_at": last_completed["completed_at_utc"] if last_completed else None,
                "delivery_latency": delivery_latency_summary(hours),
                "voice_transcription": {
                    "audio_notes": len(voice_rows),
                    "verified_or_rescued": voice_rescues,
                    "low_confidence_selected": voice_low_confidence,
                    "latest_mode": (
                        voice_meta[0].get("mode") if voice_meta else None
                    ),
                    "latest_chosen_path": (
                        voice_meta[0].get("chosen") if voice_meta else None
                    ),
                    "note": (
                        "No transcript text is exposed here; only local ASR path/quality metadata."
                    ),
                },
            },
            "note": "These are observed local facts only. A cause must not be claimed unless supported by a recorded error.",
        }
    finally:
        conn.close()


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|cookie|credential)",
    re.IGNORECASE,
)
_SECRET_TEXT_RE = re.compile(
    r"(?i)(?:bearer\s+)[A-Za-z0-9._~+/-]{8,}|"
    r"\b(?:xai-|sk-)[A-Za-z0-9._-]{8,}"
)


def _redact_text(value: object) -> str:
    return _SECRET_TEXT_RE.sub("[redacted]", str(value or ""))[:4000]


def _redact_json(value):
    if isinstance(value, dict):
        return {
            str(k): (
                "[redacted]" if _SECRET_KEY_RE.search(str(k))
                else _redact_json(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(x) for x in value[:100]]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_or_text(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return _redact_text(value)


def _build_fingerprint() -> str:
    """Content fingerprint when the image builder cannot supply a Git commit."""
    digest = hashlib.sha256()
    base = os.path.dirname(__file__)
    names = (
        "brain.py", "ingress.py", "services.py", "diagnostics.py",
        "outbox.py", "db.py", "mcp_server.py", "connect.js",
    )
    found = 0
    for name in names:
        path = os.path.join(base, name)
        try:
            with open(path, "rb") as handle:
                digest.update(name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(handle.read())
                digest.update(b"\0")
            found += 1
        except OSError:
            continue
    return digest.hexdigest()[:24] if found else "unknown"


def begin_user_error_report(actor, quoted_context: dict) -> dict:
    """Persist one quote-bound pending error report without invoking AI."""
    outbound_id = str((quoted_context or {}).get("outbound_id") or "").strip()
    if not outbound_id:
        raise ValueError("Swipe-reply to the Alex message you want to mark as an error")
    conn = connect()
    try:
        existing = conn.execute(
            """SELECT * FROM pending_error_reports
               WHERE user_id=? AND conversation_id=? LIMIT 1""",
            (actor.user_id, actor.conversation_id),
        ).fetchone()
        if existing:
            return {
                "status": "already_pending",
                "error_draft_id": existing["error_draft_id"],
                "target_outbound_id": existing["target_outbound_id"],
            }

        row = conn.execute(
            """SELECT outbound_id,provider_message_id,source_message_id
               FROM outbound_messages
               WHERE outbound_id=? AND conversation_id=? LIMIT 1""",
            (outbound_id, actor.conversation_id),
        ).fetchone()
        if not row:
            raise PermissionError("quoted Alex message is not in this conversation")

        draft_id = "ERRD-" + uuid.uuid4().hex[:16].upper()
        conn.execute(
            """INSERT INTO pending_error_reports(
                   user_id,conversation_id,error_draft_id,target_outbound_id,
                   target_provider_message_id,target_source_message_id,created_at_utc
               ) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                actor.user_id, actor.conversation_id, draft_id,
                row["outbound_id"], row["provider_message_id"],
                row["source_message_id"],
            ),
        )
        conn.commit()
        return {
            "status": "awaiting_explanation",
            "error_draft_id": draft_id,
            "target_outbound_id": outbound_id,
        }
    finally:
        conn.close()


def pending_user_error_report(actor) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT * FROM pending_error_reports
               WHERE user_id=? AND conversation_id=? LIMIT 1""",
            (actor.user_id, actor.conversation_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def pending_user_error_report_for_draft(actor, error_draft_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT * FROM pending_error_reports
               WHERE user_id=? AND conversation_id=? AND error_draft_id=?
               LIMIT 1""",
            (
                actor.user_id, actor.conversation_id,
                str(error_draft_id or "").strip(),
            ),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def cancel_user_error_report(actor, error_draft_id: str) -> dict:
    """Cancel only the explicitly quoted error draft."""
    draft_id = str(error_draft_id or "").strip()
    if not draft_id:
        raise ValueError("error draft reference is required")
    conn = connect()
    try:
        cur = conn.execute(
            """DELETE FROM pending_error_reports
               WHERE user_id=? AND conversation_id=? AND error_draft_id=?""",
            (actor.user_id, actor.conversation_id, draft_id),
        )
        conn.commit()
        return {
            "status": "cancelled" if cur.rowcount else "not_pending",
            "error_draft_id": draft_id,
        }
    finally:
        conn.close()


def _semantic_router_evidence(conn, source_id: str | None) -> dict | None:
    if not source_id:
        return None
    try:
        row = conn.execute(
            """SELECT created_at_utc,keyword_tools,called_tools,live_outcome,
                      shadow_tools,shadow_clarify,shadow_reason,status,error_code,
                      model,latency_ms,ai_routed,semantic_intent,
                      semantic_reference,semantic_slots,semantic_confidence,
                      semantic_mode
               FROM alex_shadow_router_log
               WHERE source_message_id=?
               ORDER BY id DESC LIMIT 1""",
            (source_id,),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None

    def parsed(value, fallback):
        try:
            result = json.loads(value or "")
            return result
        except Exception:
            return fallback

    return _redact_json({
        "created_at_utc": row["created_at_utc"],
        "status": row["status"],
        "error_code": row["error_code"],
        "mode": row["semantic_mode"],
        "model": row["model"],
        "latency_ms": row["latency_ms"],
        "ai_routed": bool(row["ai_routed"]),
        "intent": row["semantic_intent"],
        "reference": row["semantic_reference"],
        "slots": parsed(row["semantic_slots"], {}),
        "confidence": row["semantic_confidence"],
        "needs_clarification": bool(row["shadow_clarify"]),
        "reason": row["shadow_reason"],
        "keyword_tools": parsed(row["keyword_tools"], []),
        "selected_tools": parsed(row["shadow_tools"], []),
        "called_tools": parsed(row["called_tools"], []),
        "live_outcome": row["live_outcome"],
    })


def _timeline_sort_key(item: dict) -> tuple[int, str]:
    parsed = _parse_utc(item.get("at"))
    if parsed is None:
        return (1, str(item.get("at") or ""))
    return (0, parsed.isoformat())


def _build_user_error_bundle(conn, actor, target_outbound_id: str,
                             explanation: str) -> tuple[dict, str]:
    outbound = conn.execute(
        """SELECT outbound_id,source_message_id,conversation_id,kind,text_body,
                  context_kind,context_id,provider_message_id,delivery_status,
                  attempt_count,last_error,created_at_utc,delivered_at_utc,
                  job_reacted_at_utc,job_pinned_at_utc,job_pin_target,
                  job_reaction_cleared_at_utc,job_unpinned_at_utc,
                  job_control_attempts,job_control_last_error,
                  job_control_last_kind,job_control_failed_at_utc
           FROM outbound_messages
           WHERE outbound_id=? AND conversation_id=? LIMIT 1""",
        (target_outbound_id, actor.conversation_id),
    ).fetchone()
    if not outbound:
        raise PermissionError("target Alex message is not accessible")

    source_id = outbound["source_message_id"]
    inbound = conn.execute(
        """SELECT message_id,conversation_type,sender_phone,raw_text,
                  quoted_message_id,processing_state,attempt_count,last_error,
                  received_at_utc,processing_started_at_utc,completed_at_utc
           FROM inbound_messages WHERE message_id=? LIMIT 1""",
        (source_id,),
    ).fetchone() if source_id else None

    tool_rows = conn.execute(
        """SELECT action_key,tool_name,arguments_json,result_json,status,
                  latency_ms,created_at_utc
           FROM tool_audit
           WHERE source_message_id=?
           ORDER BY created_at_utc,rowid""",
        (source_id,),
    ).fetchall() if source_id else []
    usage_rows = conn.execute(
        """SELECT provider,model,input_tokens,cached_input_tokens,output_tokens,
                  reasoning_tokens,model_calls,tool_rounds,latency_ms,
                  estimated_cost_usd,created_at_utc
           FROM ai_usage WHERE source_message_id=?
           ORDER BY created_at_utc,rowid""",
        (source_id,),
    ).fetchall() if source_id else []

    mutation_rows = conn.execute(
        """SELECT DISTINCT a.action_key,a.tool_name,c.state,c.started_at_utc,
                           c.completed_at_utc,c.result_json
           FROM tool_audit a
           JOIN tool_execution_claims c ON c.action_key=a.action_key
           WHERE a.source_message_id=? AND a.tool_name<>'_turn_trace'
           ORDER BY c.started_at_utc""",
        (source_id,),
    ).fetchall() if source_id else []

    semantic = _semantic_router_evidence(conn, source_id)
    tool_errors = [
        r for r in tool_rows
        if r["tool_name"] != "_turn_trace" and r["status"] == "ERROR"
    ]
    inbound_failed = bool(inbound and inbound["processing_state"] == "FAILED")
    control_failed = bool(outbound["job_control_failed_at_utc"])
    detected = (
        inbound_failed or bool(tool_errors)
        or outbound["delivery_status"] == "FAILED" or control_failed
    )
    detection_state = "both" if detected else "reported-by-user"

    tools = []
    trace = None
    for row in tool_rows:
        result = _json_or_text(row["result_json"])
        args = _json_or_text(row["arguments_json"])
        entry = {
            "action_key": row["action_key"],
            "tool": row["tool_name"],
            "status": row["status"],
            "arguments": _redact_json(args),
            "result": _redact_json(result),
            "latency_ms": row["latency_ms"],
            "created_at_utc": row["created_at_utc"],
        }
        if row["tool_name"] == "_turn_trace":
            trace = {
                "input": entry["arguments"],
                "result": entry["result"],
            }
        else:
            tools.append(entry)

    mutations = [{
        "action_key": row["action_key"],
        "tool": row["tool_name"],
        "state": row["state"],
        "started_at_utc": row["started_at_utc"],
        "completed_at_utc": row["completed_at_utc"],
        "result": _redact_json(_json_or_text(row["result_json"])),
    } for row in mutation_rows]

    trace_input = (trace or {}).get("input") or {}
    trace_result = (trace or {}).get("result") or {}
    semantic_status = (
        "FAILED" if semantic and semantic.get("status") == "error"
        else "OK" if semantic
        else "NOT_USED"
    )
    tool_status = (
        "FAILED" if tool_errors else "OK" if tools else "NOT_USED"
    )
    mutation_states = {str(row.get("state") or "") for row in mutations}
    state_status = (
        "FAILED" if "UNCERTAIN" in mutation_states
        else "OK" if mutations
        else "NOT_USED"
    )
    outbox_status = (
        "FAILED" if outbound["delivery_status"] == "FAILED"
        else "OK" if outbound["delivery_status"] == "SENT"
        else "PENDING"
    )
    control_used = bool(
        outbound["job_reacted_at_utc"] or outbound["job_pinned_at_utc"]
        or outbound["job_control_attempts"]
    )
    control_status = (
        "FAILED" if control_failed
        else "OK" if control_used
        else "NOT_USED"
    )

    first_machine_failure_stage = None
    if semantic_status == "FAILED":
        first_machine_failure_stage = "SEMANTIC_ROUTER"
    elif tool_status == "FAILED":
        first_machine_failure_stage = "TOOL"
    elif state_status == "FAILED":
        first_machine_failure_stage = "STATE"
    elif inbound_failed:
        first_machine_failure_stage = "INGRESS_OR_BRAIN"
    elif outbox_status == "FAILED":
        first_machine_failure_stage = "OUTBOX"
    elif control_status == "FAILED":
        first_machine_failure_stage = "WHATSAPP_CONTROL"

    stages = {
        "INPUT": {
            "status": "OK" if inbound else "UNKNOWN",
            "quoted_message": bool(inbound and inbound["quoted_message_id"]),
        },
        "CONTEXT": {
            "status": "OK" if trace else "UNKNOWN",
            "history_turns": trace_input.get("history_turns"),
            "quoted_context": trace_input.get("quoted_context"),
        },
        "SEMANTIC_ROUTER": {
            "status": semantic_status,
            "intent": semantic.get("intent") if semantic else None,
            "reference": semantic.get("reference") if semantic else None,
            "confidence": semantic.get("confidence") if semantic else None,
        },
        "BRAIN": {
            "status": "FAILED" if inbound_failed and not tool_errors else (
                "OK" if trace else "UNKNOWN"
            ),
            "outcome": trace_result.get("outcome"),
            "routes": trace_result.get("routes") or [],
        },
        "TOOL": {
            "status": tool_status,
            "called": [row["tool"] for row in tools],
        },
        "STATE": {
            "status": state_status,
            "mutations": [
                {"tool": row["tool"], "state": row["state"]}
                for row in mutations
            ],
        },
        "OUTBOX": {
            "status": outbox_status,
            "attempts": int(outbound["attempt_count"] or 0),
        },
        "WHATSAPP_CONTROL": {
            "status": control_status,
            "kind": outbound["job_control_last_kind"],
            "attempts": int(outbound["job_control_attempts"] or 0),
        },
    }

    timeline = []
    if inbound:
        for event, field in (
            ("inbound_received", "received_at_utc"),
            ("processing_started", "processing_started_at_utc"),
            ("inbound_completed", "completed_at_utc"),
        ):
            if inbound[field]:
                timeline.append({"event": event, "at": inbound[field]})
    if semantic and semantic.get("created_at_utc"):
        timeline.append({
            "event": "semantic_router",
            "at": semantic["created_at_utc"],
            "latency_ms": semantic.get("latency_ms"),
        })
    for row in tools:
        timeline.append({
            "event": "tool:" + str(row["tool"]),
            "at": row["created_at_utc"],
            "status": row["status"],
            "latency_ms": row["latency_ms"],
        })
    for row in usage_rows:
        timeline.append({
            "event": "provider:" + str(row["provider"]),
            "at": row["created_at_utc"],
            "model": row["model"],
            "latency_ms": row["latency_ms"],
            "model_calls": row["model_calls"],
        })
    timeline.append({
        "event": "outbound_queued",
        "at": outbound["created_at_utc"],
    })
    if outbound["delivered_at_utc"]:
        timeline.append({
            "event": "whatsapp_bridge_delivered",
            "at": outbound["delivered_at_utc"],
        })
    timeline.sort(key=_timeline_sort_key)

    bundle = {
        "bundle_format": "alex-diagnostic-v2",
        "reported_at_utc": runtime_clock.now_utc().isoformat(),
        "build": {
            "app_version": os.environ.get("ALEX_APP_VERSION", "0.5.37"),
            "git_commit": os.environ.get("ALEX_BUILD_COMMIT", "unknown"),
            "source_fingerprint": _build_fingerprint(),
        },
        # Compatibility fields retained for older tooling.
        "app_version": os.environ.get("ALEX_APP_VERSION", "0.5.37"),
        "build_commit": os.environ.get("ALEX_BUILD_COMMIT", "unknown"),
        "conversation": {
            "type": inbound["conversation_type"] if inbound else actor.conversation_type,
            "id_hash": hashlib.sha256(
                actor.conversation_id.encode("utf-8")
            ).hexdigest()[:16],
        },
        "original_user_message": _redact_text(inbound["raw_text"] if inbound else ""),
        "alex_message": _redact_text(outbound["text_body"]),
        "intent_trace": _redact_json(trace),
        "semantic_router": semantic,
        "stages": stages,
        "first_machine_failure_stage": first_machine_failure_stage,
        "timeline": timeline,
        "tools": tools,
        "mutations": mutations,
        "provider_usage": [_redact_json(dict(r)) for r in usage_rows],
        "retries": {
            "inbound_attempts": int(inbound["attempt_count"] or 0) if inbound else 0,
            "outbound_attempts": int(outbound["attempt_count"] or 0),
        },
        "ids": {
            "source_message_id": source_id,
            "quoted_message_id": inbound["quoted_message_id"] if inbound else None,
            "outbound_id": outbound["outbound_id"],
            "provider_message_id": outbound["provider_message_id"],
            "context_kind": outbound["context_kind"],
            "context_id": outbound["context_id"],
        },
        "outbound_control": {
            "reaction_at_utc": outbound["job_reacted_at_utc"],
            "pinned_at_utc": outbound["job_pinned_at_utc"],
            "pin_target": outbound["job_pin_target"],
            "reaction_cleared_at_utc": outbound["job_reaction_cleared_at_utc"],
            "unpinned_at_utc": outbound["job_unpinned_at_utc"],
            "last_kind": outbound["job_control_last_kind"],
            "attempts": int(outbound["job_control_attempts"] or 0),
            "last_error": _redact_text(outbound["job_control_last_error"]),
            "failed_at_utc": outbound["job_control_failed_at_utc"],
        },
        "machine_errors": {
            "inbound_error": _redact_text(inbound["last_error"]) if inbound else None,
            "outbound_error": _redact_text(outbound["last_error"]),
            "tool_error_count": len(tool_errors),
        },
        "user_explanation": _redact_text(explanation),
        "detection_state": detection_state,
        "limits": {
            "phone_ui_observable": False,
            "note": (
                "Alex records bridge/control evidence but cannot prove how the "
                "WhatsApp client rendered pixels on the phone."
            ),
        },
    }
    return bundle, detection_state


def complete_user_error_report(actor, explanation: str,
                               quoted_context: dict | None = None,
                               *, error_draft_id: str | None = None) -> dict:
    """Create one durable diagnostic bundle; never retries or undoes the action."""
    explanation = str(explanation or "").strip()
    if not explanation:
        raise ValueError("error explanation is required")
    conn = connect()
    pending_to_clear = None
    try:
        if error_draft_id:
            pending = conn.execute(
                """SELECT * FROM pending_error_reports
                   WHERE user_id=? AND conversation_id=? AND error_draft_id=?
                   LIMIT 1""",
                (
                    actor.user_id, actor.conversation_id,
                    str(error_draft_id).strip(),
                ),
            ).fetchone()
            if not pending:
                raise ValueError("that error report is no longer pending")
            target_outbound_id = pending["target_outbound_id"]
            pending_to_clear = str(pending["error_draft_id"])
        elif quoted_context and quoted_context.get("outbound_id"):
            # Inline "Mark this as error because ..." is already bound to the
            # exact wrong Alex message and therefore needs no pending draft.
            target_outbound_id = str(quoted_context["outbound_id"])
        else:
            # Backward-compatible internal fallback. Ingress v0.5.37 never
            # uses this for an unquoted user message.
            pending = conn.execute(
                """SELECT * FROM pending_error_reports
                   WHERE user_id=? AND conversation_id=? LIMIT 1""",
                (actor.user_id, actor.conversation_id),
            ).fetchone()
            if not pending:
                raise ValueError("no pending error report; swipe-reply to an Alex message first")
            target_outbound_id = pending["target_outbound_id"]
            pending_to_clear = str(pending["error_draft_id"] or "")

        bundle, detection_state = _build_user_error_bundle(
            conn, actor, target_outbound_id, explanation
        )
        error_id = "ALEX-" + uuid.uuid4().hex[:8].upper()
        outbound = conn.execute(
            """SELECT provider_message_id,source_message_id
               FROM outbound_messages WHERE outbound_id=?""",
            (target_outbound_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO user_reported_errors(
                   error_id,reporter_user_id,conversation_id,target_outbound_id,
                   target_provider_message_id,target_source_message_id,
                   user_explanation,detection_state,bundle_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                error_id, actor.user_id, actor.conversation_id,
                target_outbound_id,
                outbound["provider_message_id"] if outbound else None,
                outbound["source_message_id"] if outbound else None,
                explanation[:4000], detection_state,
                json.dumps(bundle, ensure_ascii=False, sort_keys=True),
            ),
        )
        if pending_to_clear:
            conn.execute(
                """DELETE FROM pending_error_reports
                   WHERE user_id=? AND conversation_id=? AND error_draft_id=?""",
                (actor.user_id, actor.conversation_id, pending_to_clear),
            )
        conn.commit()
        return {
            "status": "recorded",
            "error_id": error_id,
            "detection_state": detection_state,
            "note": "Diagnostic only; no retry or undo was performed.",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_reported_error(actor, error_id: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT error_id,user_explanation,detection_state,bundle_json,created_at_utc
               FROM user_reported_errors
               WHERE error_id=? AND reporter_user_id=? LIMIT 1""",
            (str(error_id or "").strip().upper(), actor.user_id),
        ).fetchone()
        if not row:
            raise PermissionError("diagnostic error bundle not found")
        return {
            "error_id": row["error_id"],
            "user_explanation": row["user_explanation"],
            "detection_state": row["detection_state"],
            "created_at_utc": row["created_at_utc"],
            "bundle": _redact_json(json.loads(row["bundle_json"])),
        }
    finally:
        conn.close()


def list_user_reported_errors(actor, limit: int = 10) -> dict:
    bounded = max(1, min(50, int(limit)))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT error_id,user_explanation,detection_state,created_at_utc
               FROM user_reported_errors
               WHERE reporter_user_id=?
               ORDER BY created_at_utc DESC LIMIT ?""",
            (actor.user_id, bounded),
        ).fetchall()
        return {"errors": [dict(r) for r in rows]}
    finally:
        conn.close()


def export_user_reported_error(actor, error_id: str) -> dict:
    """Export one owner-scoped incident bundle as a redacted JSON attachment."""
    if str(getattr(actor, "conversation_type", "") or "") != "DIRECT_DM":
        raise PermissionError("Diagnostic bundles can only be exported in your private DM")
    report = get_user_reported_error(actor, error_id)
    safe_id = str(report["error_id"]).upper()
    directory = os.path.join(DATA_DIR, "diagnostics")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, safe_id + ".json")
    tmp = path + ".tmp"
    payload = {
        "format": "alex-diagnostic-export-v2",
        **report,
    }
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {
        "status": "exported",
        "error_id": safe_id,
        "_attachments": [{
            "kind": "DOCUMENT",
            "path": path,
            "mime_type": "application/json",
        }],
    }


def recent_failures(actor, hours: int = 24, limit: int = 20) -> dict:
    cutoff = _cutoff(hours)
    bounded = max(1, min(100, int(limit)))
    conn = connect()
    try:
        inbound = [dict(r) for r in conn.execute(
            """SELECT message_id,conversation_type,last_error,received_at_utc,attempt_count
               FROM inbound_messages WHERE processing_state='FAILED' AND received_at_utc>=?
               ORDER BY received_at_utc DESC LIMIT ?""",
            (cutoff, bounded),
        ).fetchall()]
        outbound = [dict(r) for r in conn.execute(
            """SELECT outbound_id,last_error,attempt_count,created_at_utc
               FROM outbound_messages WHERE delivery_status='FAILED' AND created_at_utc>=?
               ORDER BY created_at_utc DESC LIMIT ?""",
            (cutoff, bounded),
        ).fetchall()]
        # tool_audit deliberately stores structured result_json rather than a free-form
        # exception column; fetch a second sanitized view with the recorded result.
        tool_rows = [dict(r) for r in conn.execute(
            """SELECT tool_name,result_json,created_at_utc
               FROM tool_audit WHERE status='ERROR' AND created_at_utc>=?
               ORDER BY created_at_utc DESC LIMIT ?""",
            (cutoff, bounded),
        ).fetchall()]
        tz = ZoneInfo(getattr(actor, "timezone", None) or get_settings().timezone)
        def local_label(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(tz).strftime("%-d %B %Y, %-I:%M:%S %p")
            except Exception:
                return None

        display_failures = []
        for row in inbound:
            display_failures.append({
                "kind": "Inbound message",
                "name": "Message processing",
                "time_local": local_label(row.get("received_at_utc")),
                "detail": row.get("last_error"),
            })
        for row in outbound:
            display_failures.append({
                "kind": "Outbound delivery",
                "name": "WhatsApp delivery",
                "time_local": local_label(row.get("created_at_utc")),
                "detail": row.get("last_error"),
            })
        for row in tool_rows:
            display_failures.append({
                "kind": "Tool",
                "name": row.get("tool_name"),
                "time_local": local_label(row.get("created_at_utc")),
                "detail": row.get("result_json"),
            })

        return {
            "window_hours": max(1, min(24 * 30, int(hours))),
            "display_failures": display_failures,
            "inbound_failures": inbound,
            "outbound_failures": outbound,
            "tool_failures": tool_rows,
            "common_cause_established": False,
            "presentation_rule": (
                "For normal user-facing replies use display_failures.time_local. "
                "Do not print raw *_utc fields unless the user explicitly asks for debugging data."
            ),
            "interpretation_rule": (
                "Report recorded errors as observed facts. Do not claim broader instability "
                "or one shared root cause unless this payload explicitly establishes it. "
                "Any possible cause must be labelled as a hypothesis."
            ),
        }
    finally:
        conn.close()
