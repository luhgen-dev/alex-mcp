"""Central write-scope policy for ALEX household mutations.

Only the current user-authored trusted_text may influence the emoji/private
shortcut. OCR, quoted context, prior turns, reactions and Alex output are never
consulted here.
"""
from __future__ import annotations

import re


_PRIVATE_RE = re.compile(
    r"\b(?:private|privately|my\s+private|just\s+for\s+me|only\s+for\s+me)\b",
    re.IGNORECASE,
)
_FAMILY_RE = re.compile(
    r"\b(?:family\s+shared|shared\s+with\s+(?:the\s+)?family|"
    r"share\s+with\s+(?:the\s+)?family)\b",
    re.IGNORECASE,
)


def _is_emoji_codepoint(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x1F1E6 <= code <= 0x1F1FF
        or 0x1F3FB <= code <= 0x1F3FF
        or code in {0xFE0F, 0x20E3, 0x200D}
    )


def contains_emoji(text: str | None) -> bool:
    """Conservative stdlib-only emoji detector over current trusted text."""
    return any(_is_emoji_codepoint(ch) for ch in str(text or ""))


def strip_control_emoji(text: str | None) -> str:
    """Remove emoji used as ALEX's privacy shortcut from stored semantic text.

    The current trusted command still retains the emoji for scope resolution;
    this helper is only for persisted user-facing title/content fields.
    """
    value = "".join(ch for ch in str(text or "") if not _is_emoji_codepoint(ch))
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def explicit_private(text: str | None) -> bool:
    return bool(_PRIVATE_RE.search(str(text or "")))


def explicit_family(text: str | None) -> bool:
    return bool(_FAMILY_RE.search(str(text or "")))


def resolve_new_write_space(actor, requested_shared: bool | None = None,
                            fallback_space: str | None = None) -> str:
    """Resolve a NEW record only; edit paths must keep the stored space."""
    trusted = str(getattr(actor, "trusted_text", "") or "")
    private_requested = explicit_private(trusted) or contains_emoji(trusted)

    if actor.conversation_type == "GROUP":
        if private_requested:
            raise PermissionError(
                "private writes from the family group must be handed to the owner's DM"
            )
        return "FAMILY_SHARED"

    if trusted:
        if private_requested:
            return actor.private_space
        return "FAMILY_SHARED"

    # Compatibility for direct backend calls/tests with no user-authored turn.
    if requested_shared is True:
        return "FAMILY_SHARED"
    if requested_shared is False:
        return actor.private_space
    if fallback_space:
        return fallback_space
    return "FAMILY_SHARED"


def visibility_for_new_write(actor, requested_shared: bool | None = None) -> str:
    return (
        "family"
        if resolve_new_write_space(actor, requested_shared) == "FAMILY_SHARED"
        else "private"
    )
