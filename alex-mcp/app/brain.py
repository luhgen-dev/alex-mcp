from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp import Client
from mcp.types import TextContent
from openai import OpenAI

from config import get_settings
from context import ActorContext, use_actor, with_action_key
from db import add_turn, connect, recent_turns, record_usage
from mcp_server import mcp

SYSTEM_PROMPT = """You are Alex, one household assistant.

Your highest priorities are:
1) understand the user's intent precisely;
2) think precisely;
3) give a precise, concise answer.

The user may type incomplete sentences, spelling mistakes, Tamil, English, Malay, Tanglish, or mix languages. Understand naturally. Ask a clarification only when the ambiguity can materially change data or an action.

Personal facts are never guessed. If the answer depends on household records, receipts, reminders, goals, leave, or saved information, use the appropriate tool. Tool results are the source of truth.

Receipts/images sent for financial logging are already preserved by Alex before you reason. Logging a receipt and explicitly saying "save/remember this" are separate behaviours: automatic receipt retention must never depend on the explicit-memory tool.

For bank-transfer/payment receipts, never invent a spending purpose from a person's name or generic bank text. If purpose/category is not clear, log it as unclear so the user can clarify. Similar recurring receipts can have the same amount/payee; date/reference/media identity distinguish them.

For reminders, convert the user's intended local date/time into an ISO local datetime. Do not silently choose a materially different date. For normal conversational follow-ups, use context naturally.

When you previously asked the user to clarify a pending financial item and their next message answers that question, use list_pending_expenses to recover the exact pending event before confirming it. Never guess an event id.

For money planning, follow the user's allocations and goals. Do not tell the user to raise an allowance or redirect money unless they explicitly ask for analysis or suggestions.

Use local calculator/tool results instead of mental arithmetic when exactness matters. Keep normal WhatsApp replies short and natural; provide detail when requested.
"""


def _tool_to_openai(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


async def _tool_specs() -> list[dict]:
    async with Client(mcp) as client:
        result = await client.list_tools()
        return [_tool_to_openai(t) for t in result.tools]


def _client():
    settings = get_settings()
    if not settings.api_key:
        raise RuntimeError(
            f"No API key configured for {settings.ai_provider}. "
            "Enter it in Alex MCP → Configuration and restart."
        )
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def _runtime_context(actor: ActorContext) -> str:
    now = datetime.now(ZoneInfo(actor.timezone))
    return (
        f"Runtime context: current local datetime is {now.isoformat()}; "
        f"timezone={actor.timezone}; conversation is a private WhatsApp DM. "
        "Authenticated identity and privacy spaces are enforced below MCP and are not model-controlled."
    )


def _content_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for part in value:
            if isinstance(part, dict):
                chunks.append(str(part.get("text", "")))
            else:
                chunks.append(str(getattr(part, "text", "") or ""))
        return "".join(chunks)
    return str(value)


def _unwrap_tool_result(result) -> dict:
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        return data["result"]
    if isinstance(data, dict):
        return data
    text = ""
    for block in result.content:
        if isinstance(block, TextContent):
            text += block.text
    if result.is_error:
        return {"error": text or "MCP tool failed"}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except Exception:
        return {"result": text}


def _strip_internal(data: dict) -> tuple[dict, list[dict]]:
    clean = dict(data)
    attachments = clean.pop("_attachments", [])
    return clean, attachments if isinstance(attachments, list) else []


def _action_key(actor: ActorContext, tool_name: str, args: dict, occurrence: int) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw = f"{actor.source_message_id}|{tool_name}|{canonical}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _audit(actor: ActorContext, tool_name: str, args: dict, result: dict,
           ok: bool, latency_ms: int, action_key: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO tool_audit(
                audit_id,action_key,source_message_id,user_id,tool_name,arguments_json,
                result_json,status,latency_ms
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), action_key, actor.source_message_id, actor.user_id, tool_name,
                json.dumps(args, ensure_ascii=False, sort_keys=True)[:20000],
                json.dumps(result, ensure_ascii=False, sort_keys=True)[:30000],
                "OK" if ok else "ERROR", latency_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _call_mcp(actor: ActorContext, tool_name: str, args: dict, action_key: str) -> tuple[dict, list[dict]]:
    scoped = with_action_key(actor, action_key)
    started = time.monotonic()
    with use_actor(scoped):
        async with Client(mcp) as client:
            result = await client.call_tool(tool_name, args)
    elapsed = int((time.monotonic() - started) * 1000)
    data = _unwrap_tool_result(result)
    clean, attachments = _strip_internal(data)
    _audit(actor, tool_name, args, clean, not bool(result.is_error), elapsed, action_key)
    return clean, attachments


async def respond(actor: ActorContext, user_text: str, media_context: list[str] | None = None,
                  vision_parts: list[dict] | None = None) -> tuple[str, list[dict]]:
    settings = get_settings()
    provider = settings.ai_provider
    model = settings.model
    tools = await _tool_specs()

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": _runtime_context(actor)},
    ]
    for turn in recent_turns(actor.conversation_id, settings.context_turns):
        messages.append({"role": turn["role"], "content": turn["content"]})

    current = (user_text or "").strip()
    if media_context:
        suffix = "\n\n".join(x for x in media_context if x)
        current = (current + "\n\n" + suffix).strip()
    if not current:
        current = "I sent an attachment."
    if vision_parts:
        current_content = [{"type": "text", "text": current}] + list(vision_parts)
        messages.append({"role": "user", "content": current_content})
    else:
        messages.append({"role": "user", "content": current})

    client = _client()
    attachments: list[dict] = []
    input_tokens = output_tokens = 0
    tool_rounds = 0
    occurrence: dict[str, int] = {}
    started = time.monotonic()

    for _ in range(8):
        kwargs = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "reasoning_effort": settings.reasoning_effort,
        }
        if provider == "grok":
            kwargs["extra_headers"] = {"x-grok-conv-id": actor.conversation_id[:200]}
        response = client.chat.completions.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            final = _content_text(msg.content).strip() or "Done."
            elapsed = int((time.monotonic() - started) * 1000)
            record_usage(actor.source_message_id, provider, model, input_tokens, output_tokens, tool_rounds, elapsed)
            add_turn(actor.user_id, actor.conversation_id, "user", user_text or "[attachment]")
            add_turn(actor.user_id, actor.conversation_id, "assistant", final)
            return final, attachments

        tool_rounds += 1
        assistant_dump = msg.model_dump(exclude_none=True)
        messages.append(assistant_dump)

        for call in tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}

            signature = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            occurrence[signature] = occurrence.get(signature, 0) + 1
            action_key = _action_key(actor, name, args, occurrence[signature])

            try:
                result, files = await _call_mcp(actor, name, args, action_key)
                attachments.extend(files)
                payload = result
            except Exception as exc:
                payload = {"error": str(exc)[:1000]}
                _audit(actor, name, args, payload, False, 0, action_key)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            })

    final = "I couldn't complete that safely after several tool steps. Nothing else was changed."
    elapsed = int((time.monotonic() - started) * 1000)
    record_usage(actor.source_message_id, provider, model, input_tokens, output_tokens, tool_rounds, elapsed)
    add_turn(actor.user_id, actor.conversation_id, "user", user_text or "[attachment]")
    add_turn(actor.user_id, actor.conversation_id, "assistant", final)
    return final, attachments
