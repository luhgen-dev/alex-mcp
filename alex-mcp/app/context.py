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
