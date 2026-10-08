"""Semantic AI router for Alex (v0.5.36).

The same schema-bound GPT plan call can run in two owner-selectable modes:

* shadow: interpret real household messages in the background and log the
  proposed tool/intent/slots without changing Alex's reply;
* live: run the same interpreter before the brain, safely merge its tool picks
  ahead of keyword routing, and provide its structured interpretation as a
  non-authoritative language hint.

Alex's deterministic identity, privacy, mutation, scope and tool guards remain
authoritative in both modes. Any router failure falls back to the existing
keyword path.
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

SEMANTIC_REFERENCES = {"NONE", "ACTIVE", "QUOTED", "LATEST_LIST"}
SEMANTIC_TARGETS = {"UNSPECIFIED", "SELF", "SPOUSE", "BOTH", "GROUP", "PRIVATE"}
SEMANTIC_SCOPES = {"UNSPECIFIED", "FAMILY_SHARED", "PRIVATE"}
SEMANTIC_CURRENCIES = {"UNSPECIFIED", "MYR", "SGD"}
SEMANTIC_CONFIDENCE_MIN = 0.70

ROUTER_INSTRUCTIONS = """You are Alex's semantic router. Read the household user's natural message and return a precise, grounded interpretation plus the tools Alex may need.

The user may write casually, with typos, abbreviations, Malay, Tamil or Tanglish. Read meaning, not keywords. Use recent conversation and quoted text only to resolve genuine follow-ups such as "that one", "bought 1 and 3", or a bare time/amount answering Alex's last question.

Rules:
- Choose only from the supplied tool list, at most six, most important first.
- intent: a short UPPER_SNAKE_CASE description such as READ_REMINDERS, CREATE_REMINDER, UPDATE_SHOPPING, READ_FINANCES, CHECK_AVAILABILITY or SHOW_RECEIPT.
- reference: QUOTED only when the quoted message is the target; ACTIVE for an unresolved active interaction; LATEST_LIST for a numbered/list follow-up; otherwise NONE.
- slots contain only information grounded in the current message or clearly resolved active/quoted context. Never invent a date, time, recipient, privacy scope, amount, currency, name or action.
- date_text/time_text may normalize obvious wording (for example "tmr" -> "tomorrow"). relative_minutes may normalize an explicit duration such as "half an hour" -> 30.
- target/scope describe only explicit or context-bound meaning; use UNSPECIFIED when not known.
- Return an empty tools list for pure chit-chat that needs no household data.
- needs_clarification is true only when the user's meaning genuinely cannot be resolved from current + active context.
- confidence is 0..1 for the semantic interpretation, not for whether Alex is allowed to execute it.
- reason: one short sentence.
Alex's deterministic safety layer, not you, decides authorization and execution."""

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
    latency_ms INTEGER,
    ai_routed INTEGER DEFAULT 0,
    semantic_intent TEXT,
    semantic_reference TEXT,
    semantic_slots TEXT,
    semantic_confidence REAL,
    semantic_mode TEXT
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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alex_shadow_router_log)")}
        if "ai_routed" not in cols:  # v0.5.33 tables predate this column
            conn.execute(
                "ALTER TABLE alex_shadow_router_log ADD COLUMN ai_routed INTEGER DEFAULT 0"
            )
        # v0.5.36 structured semantic translator fields. Additive migration only.
        for name, kind in (
            ("semantic_intent", "TEXT"),
            ("semantic_reference", "TEXT"),
            ("semantic_slots", "TEXT"),
            ("semantic_confidence", "REAL"),
            ("semantic_mode", "TEXT"),
        ):
            if name not in cols:
                conn.execute(
                    f"ALTER TABLE alex_shadow_router_log ADD COLUMN {name} {kind}"
                )
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


# --------------------------------------------------------------------------
# v0.5.34 live AI routing. The same schema-bound router call, made BEFORE the
# brain runs, so the brain is shown the tools the model picked. Any failure
# returns None and the caller keeps today's keyword routing untouched.
# --------------------------------------------------------------------------
LIVE_TIMEOUT_SECONDS = 6.0
LIVE_FAIL_LIMIT = 3
LIVE_PAUSE_SECONDS = 120.0
_live_state = {"fails": 0, "pause_until": 0.0}


def routing_mode(settings=None) -> str:
    """Return off/shadow/live; v0.5.35's saved "on" remains a live alias."""
    settings = settings or get_settings()
    raw = str(getattr(settings, "ai_routing", "off") or "off").strip().lower()
    if raw == "on":
        return "live"
    return raw if raw in {"off", "shadow", "live"} else "off"


def live_enabled(settings=None) -> bool:
    settings = settings or get_settings()
    if routing_mode(settings) != "live":
        return False
    if time.monotonic() < _live_state["pause_until"]:
        return False
    return enabled(settings)


def _live_failed(exc: Exception, timed_out: bool) -> None:
    _live_state["fails"] += 1
    if _live_state["fails"] >= LIVE_FAIL_LIMIT:
        _live_state["pause_until"] = time.monotonic() + LIVE_PAUSE_SECONDS
        _live_state["fails"] = 0
    if timed_out:
        return  # slow is not the same as down; the pause above covers repeats
    try:
        import brain
        brain._trip_provider(
            "chatgpt", brain.classify_runtime_error(exc).get("category", "")
        )
    except Exception:
        pass


async def acatalogue() -> list[dict]:
    """Async twin of catalogue() for callers already inside an event loop."""
    global _CATALOGUE
    with _CATALOGUE_LOCK:
        cached = _CATALOGUE
    if cached is None:
        built = await _list_tools()
        with _CATALOGUE_LOCK:
            if _CATALOGUE is None:
                _CATALOGUE = built
            cached = _CATALOGUE
    return list(cached)


async def live_route(text: str, prior_turns: list[dict], quoted_text: str = "",
                     *, plan_client=None) -> dict | None:
    """Ask the plan model which tools this message needs. None on any failure."""
    started = time.monotonic()
    try:
        cat = await acatalogue()
        job = {
            "catalogue": cat,
            "prior_turns": [
                {"role": str(t.get("role")), "text": str(t.get("text") or "")[:TURN_TEXT_MAX]}
                for t in (prior_turns or [])[-PRIOR_TURNS:]
            ],
            "quoted_text": str(quoted_text or "")[:TURN_TEXT_MAX],
            "text": str(text or ""),
        }
        allowed = {t["name"] for t in cat}

        def _call() -> dict:
            settings = get_settings()
            model = chatgpt_plan.resolved_model(settings)
            client = plan_client or chatgpt_plan.client(settings)
            completion = client.chat.completions.create(**build_request(job, model))
            choice = parse_choice(completion.choices[0].message.content, allowed)
            choice["model"] = model
            return choice

        choice = await asyncio.wait_for(
            asyncio.to_thread(_call), timeout=LIVE_TIMEOUT_SECONDS
        )
        _live_state["fails"] = 0
        choice["latency_ms"] = int((time.monotonic() - started) * 1000)
        return choice
    except asyncio.TimeoutError as exc:
        _live_failed(exc, True)
    except Exception as exc:
        _live_failed(exc, False)
    return None


def record_live(actor, user_text: str, trace: dict) -> None:
    """One log row for a turn the AI router actually routed. Never raises."""
    try:
        route = trace.get("ai_route") or {}
        record({
            "source_message_id": str(getattr(actor, "source_message_id", "") or ""),
            "conversation_type": str(getattr(actor, "conversation_type", "") or ""),
            "message_snippet": str(user_text or "")[:SNIPPET_MAX],
            "keyword_tools": list(trace.get("keyword_tools") or []),
            "called_tools": _called_tools(trace.get("tools_called") or []),
            "live_outcome": str(trace.get("outcome") or ""),
            "shadow_tools": list(route.get("tools") or []),
            "shadow_clarify": bool(route.get("needs_clarification")),
            "shadow_reason": route.get("reason"),
            "status": "ok",
            "model": route.get("model"),
            "latency_ms": route.get("latency_ms") or 0,
            "ai_routed": 1,
            "semantic_intent": route.get("intent"),
            "semantic_reference": route.get("reference"),
            "semantic_slots": route.get("slots") or {},
            "semantic_confidence": route.get("confidence"),
            "semantic_mode": "live",
        })
    except Exception:
        pass


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
        if trace and trace.get("ai_route"):
            # v0.5.34: this turn was already routed by the model, so there is
            # nothing left to compare in the background; just log it.
            record_live(actor, text, trace)
            return True
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
    slots = {
        "type": "object",
        "properties": {
            "date_text": {"type": ["string", "null"]},
            "time_text": {"type": ["string", "null"]},
            "relative_minutes": {"type": ["integer", "null"]},
            "item_numbers": {"type": "array", "items": {"type": "integer"}},
            "target": {"type": "string", "enum": sorted(SEMANTIC_TARGETS)},
            "scope": {"type": "string", "enum": sorted(SEMANTIC_SCOPES)},
            "amount": {"type": ["number", "null"]},
            "currency": {"type": "string", "enum": sorted(SEMANTIC_CURRENCIES)},
            "name": {"type": ["string", "null"]},
            "query": {"type": ["string", "null"]},
            "action": {"type": ["string", "null"]},
        },
        "required": [
            "date_text", "time_text", "relative_minutes", "item_numbers",
            "target", "scope", "amount", "currency", "name", "query", "action",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "tools": {"type": "array", "items": {"type": "string", "enum": names}},
            "intent": {"type": "string"},
            "reference": {"type": "string", "enum": sorted(SEMANTIC_REFERENCES)},
            "slots": slots,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "needs_clarification": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": [
            "tools", "intent", "reference", "slots", "confidence",
            "needs_clarification", "reason",
        ],
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


def _clean_semantic_slots(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    def text_slot(name: str, limit: int = 160):
        value = raw.get(name)
        if value is None:
            return None
        value = " ".join(str(value).split()).strip()
        return value[:limit] or None

    relative = raw.get("relative_minutes")
    if isinstance(relative, bool) or not isinstance(relative, int) or not 1 <= relative <= 10080:
        relative = None

    numbers = []
    for value in raw.get("item_numbers") or []:
        if isinstance(value, bool):
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 999 and value not in numbers:
            numbers.append(value)
        if len(numbers) >= 20:
            break

    target = str(raw.get("target") or "UNSPECIFIED").upper()
    scope = str(raw.get("scope") or "UNSPECIFIED").upper()
    currency = str(raw.get("currency") or "UNSPECIFIED").upper()
    if target not in SEMANTIC_TARGETS:
        target = "UNSPECIFIED"
    if scope not in SEMANTIC_SCOPES:
        scope = "UNSPECIFIED"
    if currency not in SEMANTIC_CURRENCIES:
        currency = "UNSPECIFIED"

    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        amount = None
    elif abs(float(amount)) > 1_000_000_000:
        amount = None
    else:
        amount = float(amount)

    return {
        "date_text": text_slot("date_text"),
        "time_text": text_slot("time_text"),
        "relative_minutes": relative,
        "item_numbers": numbers,
        "target": target,
        "scope": scope,
        "amount": amount,
        "currency": currency,
        "name": text_slot("name"),
        "query": text_slot("query", 240),
        "action": text_slot("action"),
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

    intent = re.sub(r"[^A-Z0-9_]+", "_", str(data.get("intent") or "").upper()).strip("_")
    reference = str(data.get("reference") or "NONE").upper()
    if reference not in SEMANTIC_REFERENCES:
        reference = "NONE"
    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "tools": picked[:MAX_TOOLS],
        "intent": intent[:64] or "UNSPECIFIED",
        "reference": reference,
        "slots": _clean_semantic_slots(data.get("slots")),
        "confidence": confidence,
        "needs_clarification": bool(data.get("needs_clarification")),
        "reason": str(data.get("reason") or "")[:200],
    }


def semantic_hint(choice: dict | None) -> str:
    """Model-facing language hint. Never grants permission or changes trusted text."""
    if not choice or choice.get("needs_clarification"):
        return ""
    if float(choice.get("confidence") or 0.0) < SEMANTIC_CONFIDENCE_MIN:
        return ""
    slots = {
        key: value for key, value in (choice.get("slots") or {}).items()
        if value not in (None, "", [], "UNSPECIFIED")
    }
    payload = {
        "intent": choice.get("intent") or "UNSPECIFIED",
        "reference": choice.get("reference") or "NONE",
        "slots": slots,
    }
    return (
        "AI semantic routing hint (non-authoritative): "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\nUse this only to understand the user's wording. The original user-authored "
          "message, trusted quote/context and Alex's deterministic rules remain the sole "
          "authority for privacy, recipient, scope, write permission and execution. "
          "Never invent or execute a slot merely because it appears in this hint; if it "
          "conflicts with the user's text, persisted state or tool evidence, ignore it."
    )


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
            "semantic_intent": choice.get("intent"),
            "semantic_reference": choice.get("reference"),
            "semantic_slots": choice.get("slots") or {},
            "semantic_confidence": choice.get("confidence"),
            "semantic_mode": "shadow",
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
                shadow_clarify, shadow_reason, status, error_code, model, latency_ms,
                ai_routed, semantic_intent, semantic_reference, semantic_slots,
                semantic_confidence, semantic_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                1 if row.get("ai_routed") else 0,
                row.get("semantic_intent"),
                row.get("semantic_reference"),
                json.dumps(row.get("semantic_slots") or {}, ensure_ascii=False),
                float(row.get("semantic_confidence") or 0.0),
                row.get("semantic_mode"),
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
    ai_routed_turns = 0
    disagreements = []
    semantics = []
    latencies = []
    for r in rows:
        if r["status"] != "ok":
            errors += 1
            continue
        compared += 1
        if r["ai_routed"]:
            ai_routed_turns += 1
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
        if r["semantic_intent"] and len(semantics) < recent:
            try:
                semantic_slots = json.loads(r["semantic_slots"] or "{}")
            except Exception:
                semantic_slots = {}
            semantics.append({
                "at": r["created_at_utc"],
                "message": r["message_snippet"],
                "mode": r["semantic_mode"] or ("live" if r["ai_routed"] else "shadow"),
                "intent": r["semantic_intent"],
                "reference": r["semantic_reference"] or "NONE",
                "slots": semantic_slots,
                "confidence": float(r["semantic_confidence"] or 0.0),
                "tools": json.loads(r["shadow_tools"] or "[]"),
            })
    latencies.sort()
    return {
        "days": days,
        "compared": compared,
        "turns_routed_by_ai": ai_routed_turns,
        "errors": errors,
        "turns_where_alex_used_tools": with_used,
        "ai_router_also_picked_what_alex_used": agreed,
        "ai_router_wanted_a_tool_keywords_hid": keyword_hid,
        "alex_answered_without_tools_but_ai_router_picked_some": answered_without_tool,
        "median_latency_ms": latencies[len(latencies) // 2] if latencies else None,
        "routing_mode": routing_mode(),
        "recent_disagreements": disagreements,
        "recent_semantics": semantics,
    }
