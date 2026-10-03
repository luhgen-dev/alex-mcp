from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ActorContext:
    user_id: str
    phone: str
    allowed_spaces: tuple[str, ...]
    private_space: str
    conversation_id: str
    conversation_type: str
    source_message_id: str
    media_ids: tuple[str, ...]
    timezone: str
    action_key: str = ""
    # v0.4.4 normalized-Turn metadata. All fields are additive with safe
    # defaults so existing callers/tests keep working unchanged.
    # source: text | voice | image | document | mixed
    source: str = "text"
    # The user's own words: typed text or the transcript of their voice note.
    # OCR/PDF text is NEVER placed here (it is untrusted document content).
    trusted_text: str = ""
    # When WhatsApp says the message was sent (UTC ISO). Used by the backend
    # to timestamp records instead of letting the model invent a clock time.
    received_at_utc: str = ""
    # Deterministic read boundary derived once from the current trusted command.
    # family is the safe default; private/all require an explicit current-turn signal.
    read_scope: str | None = None
    # True only for a Family Shared turn that must execute in the owner's DM.
    private_handoff: bool = False
    # Durable reminder clarification context. This is composed only from
    # user-authored trusted_text fragments that belong to one REMINDER_DRAFT.
    # Reminder services validate against this accumulated text while all other
    # domains continue to use the current turn's trusted_text.
    reminder_context_text: str = ""


_current_actor: ContextVar[ActorContext | None] = ContextVar("alex_actor", default=None)


def current_actor() -> ActorContext:
    actor = _current_actor.get()
    if actor is None:
        raise RuntimeError("MCP tool called without authenticated Alex actor context")
    return actor


@contextmanager
def use_actor(actor: ActorContext):
    token = _current_actor.set(actor)
    try:
        yield actor
    finally:
        _current_actor.reset(token)


def with_action_key(actor: ActorContext, action_key: str) -> ActorContext:
    return replace(actor, action_key=action_key)
