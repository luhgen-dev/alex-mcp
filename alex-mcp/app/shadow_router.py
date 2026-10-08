"""Shadow AI router (v0.5.33) — log only, never changes a reply.

Today Alex shows the reasoning model at most six of its ~120 tools per turn,
chosen by deterministic keyword routing. When that routing picks the wrong
domain, the model cannot call the right tool however well it understood the
message. This module measures, on real household traffic, whether a model
choosing from the FULL tool catalogue would pick better:

* After a normal reply has already been produced, a background thread asks
  the owner's ChatGPT plan (no API spend) which tools the turn needed.
* The answer is constrained by a strict JSON schema to real tool names.
* It is only stored in a local table next to what keyword routing exposed and
  what Alex actually called. Nothing is executed, no tool is called, and no
  message is sent.

It runs only when ``chatgpt_plan_mode`` is "shadow_only" or "primary" and the
owner is signed in. Any failure is swallowed and logged as a row.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import chatgpt_plan
from config import get_settings
from db import connect, recent_turns

MAX_TOOLS = 6
PRIOR_TURNS = 4
TURN_TEXT_MAX = 400
SNIPPET_MAX = 160
RETENTION_DAYS = 30

ROUTER_INSTRUCTIONS = """You route messages for Alex, a household assistant that works through tools.

Given the recent conversation and the user's CURRENT message, choose the tools Alex would need to handle the current message completely and correctly. Choose only from the supplied tool list, at most six, most important first.

- The user may write casually, with typos, abbreviations, Malay, Tamil or Tanglish. Read the intent, not the keywords.
- Use the recent conversation to resolve follow-ups such as "that one", "the first one", "bought 1 and 3", or a bare time or amount answering Alex's last question.
- Prefer the tool that performs the requested change or answers the question directly; add a lookup tool only when the change needs one first.
- Return an empty list for pure chit-chat that needs no household data.
- Set needs_clarification to true only when the message is genuinely ambiguous about which household domain it concerns.
- reason: one short sentence."""

_DDL = """
CREATE TABLE IF NOT EXISTS alex_shadow_router_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_utc TEXT NOT NULL,
    source_message_id TEXT,
    conversation_type TEXT,
    message_snippet TEXT,
    keyword_tools TEXT,
    called_tools TEXT,
    live_outcome TEXT,
    shadow_tools TEXT,
    shadow_clarify INTEGER,
    shadow_reason TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    model TEXT,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alex_shadow_router_created
    ON alex_shadow_router_log(created_at_utc);
"""

_CATALOGUE: list[dict] | None = None
_CATALOGUE_LOCK = threading.Lock()


def ensure_schema() -> None:
    conn = connect()
    try:
        conn.executescript(_DDL)
        conn.commit()
    finally:
        conn.close()


def _summary(description: str) -> str:
    text = " ".join(str(description or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    first = match.group(1) if match else text
    return first[:180]


async def _list_tools() -> list[dict]:
    from mcp import Client
    from mcp_server import mcp

    async with Client(mcp) as client:
        result = await client.list_tools()
    return [
        {"name": str(tool.name), "summary": _summary(tool.description or "")}
        for tool in result.tools
    ]


def catalogue() -> list[dict]:
    """Full tool catalogue (name + one-line summary), built once per process.

    Must be called from a thread with no running event loop (ingress calls it
    on its request thread after brain.respond has returned).
    """
    global _CATALOGUE
    with _CATALOGUE_LOCK:
        if _CATALOGUE is None:
            _CATALOGUE = asyncio.run(_list_tools())
        return list(_CATALOGUE)


def enabled(settings=None) -> bool:
    settings = settings or get_settings()
    if chatgpt_plan.mode(settings) not in {"shadow_only", "primary"}:
        return False
    try:
        if not chatgpt_plan.ready_model(settings):
            return False
    except Exception:
        return False
    try:
        import brain
        if brain._provider_cooling("chatgpt"):
            return False  # don't add plan traffic while it's rate-limited/down
    except Exception:
        pass
    return True


def capture_context(actor) -> dict | None:
    """Called BEFORE the live reply, so the router never sees Alex's answer.

    Returns None (and reads nothing) when the shadow router is off.
    """
    try:
        if not enabled():
            return None
        turns = recent_turns(actor.conversation_id, PRIOR_TURNS)
        return {
            "prior_turns": [
                {"role": str(t["role"]), "text": str(t["content"] or "")[:TURN_TEXT_MAX]}
                for t in turns
            ],
        }
    except Exception:
        return None


def _called_tools(names: list[str]) -> list[str]:
    """Tools the live turn used successfully (or that asked a valid question)."""
    out = []
    for raw in names or []:
        name, _, suffix = str(raw).partition(":")
        if suffix == "error" or name == "discover_alex_tools" or not name:
            continue
        if name not in out:
            out.append(name)
    return out


def submit(actor, user_text: str, quoted_context: dict | None, context: dict | None,
           *, has_media: bool = False, trace: dict | None = None) -> bool:
    """Start the background comparison for one finished turn. Never raises."""
    try:
        if context is None or has_media:
            return False
        if str(getattr(actor, "source", "text") or "text") != "text":
            return False
        text = str(user_text or "").strip()
        if not text:
            return False
        if trace is None:
            import brain
            trace = brain.recent_trace(getattr(actor, "source_message_id", None))
        if not trace or trace.get("outcome") not in {"answered", "max_steps"}:
            return False  # local replies/clarifications never reached the model
        tools = catalogue()
        quoted_text = ""
        if quoted_context:
            quoted_text = str(
                quoted_context.get("quoted_user_text")
                or quoted_context.get("quoted_text")
                or ""
            )[:TURN_TEXT_MAX]
        job = {
            "source_message_id": str(getattr(actor, "source_message_id", "") or ""),
            "conversation_type": str(getattr(actor, "conversation_type", "") or ""),
            "text": text,
            "quoted_text": quoted_text,
            "prior_turns": list(context.get("prior_turns") or []),
            "keyword_tools": list(trace.get("exposed_tools") or []),
            "called_tools": _called_tools(trace.get("tools_called") or []),
            "live_outcome": str(trace.get("outcome") or ""),
            "catalogue": tools,
        }
        thread = threading.Thread(target=_run_safely, args=(job,), daemon=True,
                                  name="alex-shadow-router")
        thread.start()
        return True
    except Exception:
        return False


def _schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "tools": {"type": "array", "items": {"type": "string", "enum": names}},
            "needs_clarification": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["tools", "needs_clarification", "reason"],
        "additionalProperties": False,
    }


def build_request(job: dict, model: str) -> dict:
    names = [t["name"] for t in job["catalogue"]]
    # The instructions + catalogue are identical on every call, so they go
    # first (cache-friendly prefix); only the conversation varies.
    stable = ROUTER_INSTRUCTIONS + "\n\nTools:\n" + "\n".join(
        f'- {t["name"]}: {t["summary"]}' for t in job["catalogue"]
    )
    payload = {
        "recent_conversation": job["prior_turns"],
        "quoted_message": job.get("quoted_text") or None,
        "current_message": job["text"],
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": stable},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "tool_route", "schema": _schema(names), "strict": True},
        },
    }


def parse_choice(content: str, allowed: set[str]) -> dict:
    data = json.loads(content or "{}")
    if not isinstance(data, dict):
        raise ValueError("router output is not an object")
    picked = []
    for name in data.get("tools") or []:
        name = str(name)
        if name in allowed and name not in picked:
            picked.append(name)
    return {
        "tools": picked[:MAX_TOOLS],
        "needs_clarification": bool(data.get("needs_clarification")),
        "reason": str(data.get("reason") or "")[:200],
    }


def _run_safely(job: dict, plan_client=None) -> None:
    try:
        _run(job, plan_client=plan_client)
    except Exception:
        pass


def _run(job: dict, plan_client=None) -> dict:
    settings = get_settings()
    model = chatgpt_plan.resolved_model(settings)
    started = time.monotonic()
    row = {
        "source_message_id": job["source_message_id"],
        "conversation_type": job["conversation_type"],
        "message_snippet": job["text"][:SNIPPET_MAX],
        "keyword_tools": job["keyword_tools"],
        "called_tools": job["called_tools"],
        "live_outcome": job["live_outcome"],
        "model": model,
    }
    try:
        client = plan_client or chatgpt_plan.client(settings)
        completion = client.chat.completions.create(**build_request(job, model))
        choice = parse_choice(
            completion.choices[0].message.content,
            {t["name"] for t in job["catalogue"]},
        )
        row.update({
            "status": "ok",
            "shadow_tools": choice["tools"],
            "shadow_clarify": choice["needs_clarification"],
            "shadow_reason": choice["reason"],
        })
    except Exception as exc:
        row.update({
            "status": "error",
            "error_code": str(getattr(exc, "code", "") or exc.__class__.__name__)[:80],
        })
        try:
            import brain
            brain._trip_provider(
                "chatgpt", brain.classify_runtime_error(exc).get("category", "")
            )
        except Exception:
            pass
    row["latency_ms"] = int((time.monotonic() - started) * 1000)
    record(row)
    return row


def record(row: dict) -> None:
    ensure_schema()
    now = datetime.now(timezone.utc)
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO alex_shadow_router_log(
                created_at_utc, source_message_id, conversation_type, message_snippet,
                keyword_tools, called_tools, live_outcome, shadow_tools,
                shadow_clarify, shadow_reason, status, error_code, model, latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now.isoformat(),
                row.get("source_message_id"),
                row.get("conversation_type"),
                row.get("message_snippet"),
                json.dumps(row.get("keyword_tools") or []),
                json.dumps(row.get("called_tools") or []),
                row.get("live_outcome"),
                json.dumps(row.get("shadow_tools") or []),
                1 if row.get("shadow_clarify") else 0,
                row.get("shadow_reason"),
                row.get("status") or "error",
                row.get("error_code"),
                row.get("model"),
                int(row.get("latency_ms") or 0),
            ),
        )
        conn.execute(
            "DELETE FROM alex_shadow_router_log WHERE created_at_utc < ?",
            ((now - timedelta(days=RETENTION_DAYS)).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()


def summary(days: int = 7, recent: int = 10) -> dict:
    """Local comparison report for the panel. Never calls any AI provider."""
    ensure_schema()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM alex_shadow_router_log
               WHERE created_at_utc >= ? ORDER BY id DESC""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    compared = errors = with_used = agreed = keyword_hid = answered_without_tool = 0
    disagreements = []
    latencies = []
    for r in rows:
        if r["status"] != "ok":
            errors += 1
            continue
        compared += 1
        latencies.append(int(r["latency_ms"] or 0))
        keyword = set(json.loads(r["keyword_tools"] or "[]"))
        called = set(json.loads(r["called_tools"] or "[]"))
        shadow = set(json.loads(r["shadow_tools"] or "[]"))
        if called:
            with_used += 1
            if called <= shadow:
                agreed += 1
        hidden = shadow - keyword
        if hidden:
            keyword_hid += 1
        if not called and shadow and r["live_outcome"] == "answered":
            answered_without_tool += 1
        if (hidden or (called and not called <= shadow)) and len(disagreements) < recent:
            disagreements.append({
                "at": r["created_at_utc"],
                "message": r["message_snippet"],
                "keyword_tools": sorted(keyword),
                "alex_used": sorted(called),
                "ai_router_picked": json.loads(r["shadow_tools"] or "[]"),
                "ai_router_reason": r["shadow_reason"],
            })
    latencies.sort()
    return {
        "days": days,
        "compared": compared,
        "errors": errors,
        "turns_where_alex_used_tools": with_used,
        "ai_router_also_picked_what_alex_used": agreed,
        "ai_router_wanted_a_tool_keywords_hid": keyword_hid,
        "alex_answered_without_tools_but_ai_router_picked_some": answered_without_tool,
        "median_latency_ms": latencies[len(latencies) // 2] if latencies else None,
        "recent_disagreements": disagreements,
    }
