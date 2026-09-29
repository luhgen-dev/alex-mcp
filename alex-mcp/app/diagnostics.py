from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

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
        return {
            "window_hours": max(1, min(24 * 30, int(hours))),
            "inbound_failures": inbound,
            "outbound_failures": outbound,
            "tool_failures": tool_rows,
            "interpretation_rule": "Report recorded errors as observed facts. Label any inferred cause as a hypothesis.",
        }
    finally:
        conn.close()
