from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from config import DATA_DIR, get_settings
from db import connect

SELFTEST_FILE = os.path.join(DATA_DIR, "selftest.json")


def _cutoff(hours: int) -> str:
    bounded = max(1, min(24 * 30, int(hours)))
    return (datetime.now(timezone.utc) - timedelta(hours=bounded)).isoformat()


def _selftest() -> dict | None:
    try:
        with open(SELFTEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
            """SELECT COUNT(*) AS calls,COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(AVG(latency_ms),0) AS avg_latency,
                      COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost
               FROM ai_usage WHERE created_at_utc>=?""",
            (cutoff,),
        ).fetchone()
        last_completed = conn.execute(
            """SELECT completed_at_utc FROM inbound_messages
               WHERE processing_state='COMPLETED' ORDER BY completed_at_utc DESC LIMIT 1"""
        ).fetchone()
        selftest = _selftest()
        return {
            "window_hours": max(1, min(24 * 30, int(hours))),
            "database": "ok" if db_ok else "problem",
            "startup_selftest": selftest,
            "provider": {
                "name": settings.ai_provider,
                "model": settings.model,
                "api_key_present": bool(settings.api_key),
            },
            "observed": {
                "failed_inbound": int(failed_inbound),
                "pending_outbound": int(pending_outbound),
                "failed_outbound": int(failed_outbound),
                "tool_errors": int(tool_errors),
                "ai_calls": int(usage["calls"] or 0),
                "input_tokens": int(usage["input_tokens"] or 0),
                "output_tokens": int(usage["output_tokens"] or 0),
                "average_ai_latency_ms": round(float(usage["avg_latency"] or 0), 1),
                "estimated_ai_cost_usd": round(float(usage["estimated_cost"] or 0), 6),
                "last_completed_message_at": last_completed["completed_at_utc"] if last_completed else None,
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
