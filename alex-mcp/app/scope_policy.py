"""Central privacy/scope policy for ALEX household reads and writes.

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
    r"share\s+with\s+(?:the\s+)?family|shared\s+only|family\s+only)\b",
    re.IGNORECASE,
)
_ALL_RE = re.compile(
    r"(?:\ball\b|\beverything\b|\bboth\b).*\b(?:private|personal)\b.*\b(?:shared|family)\b"
    r"|(?:\ball\b|\beverything\b|\bboth\b).*\b(?:shared|family)\b.*\b(?:private|personal)\b"
    r"|\b(?:shared|family)\s+(?:and|plus)\s+(?:private|personal)\b"
    r"|\b(?:private|personal)\s+(?:and|plus)\s+(?:shared|family)\b",
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


def resolve_read_scope(text: str | None) -> str:
    """Resolve the current trusted read intent once.

    Locked policy: ordinary reads are FAMILY_SHARED, explicit private wording
    or any emoji means owner-private, and all-spaces reads require an explicit
    request for both/shared+private. A bare word like "all" never widens scope.
    """
    value = str(text or "")
    if _ALL_RE.search(value):
        return "all"
    if explicit_private(value) or contains_emoji(value):
        return "private"
    if explicit_family(value):
        return "family"
    return "family"


def effective_read_scope(actor, requested_scope: str | None = None) -> str:
    """Resolve an effective backend scope without allowing live model-side widening.

    Actors created directly by service/unit code have read_scope=None and keep
    the explicit legacy backend scope. Real inbound turns are normalized by
    ingress to family/private/all before any model/tool call.
    """
    raw_policy = getattr(actor, "read_scope", None)
    requested = str(requested_scope or "").strip().casefold()
    if raw_policy is None:
        if requested in {"family", "shared", "family_shared"}:
            return "family"
        if requested in {"private", "personal", "my"}:
            return "private"
        if requested not in {"", "all", "visible", "accessible"}:
            raise ValueError("scope must be all, family, or private")
        return "all"
    policy = str(raw_policy).strip().casefold()
    if policy in {"family", "private"}:
        return policy
    if policy != "all":
        raise ValueError("read scope must be family, private, or all")
    if requested in {"family", "shared", "family_shared"}:
        return "family"
    if requested in {"private", "personal", "my"}:
        return "private"
    if requested not in {"", "all", "visible", "accessible"}:
        raise ValueError("scope must be all, family, or private")
    return "all"


def read_spaces(actor, requested_scope: str | None = None) -> list[str]:
    """Return the spaces a read may use; model arguments may never widen policy."""
    policy = effective_read_scope(actor, requested_scope)
    requested = str(requested_scope or "").strip().casefold()
    allowed = list(getattr(actor, "allowed_spaces", ()) or ())

    if getattr(actor, "conversation_type", "") == "GROUP":
        if policy == "private":
            raise PermissionError("private scope must be handed to the owner's DM")
        if "FAMILY_SHARED" not in allowed:
            raise PermissionError("family scope is not accessible in this conversation")
        return ["FAMILY_SHARED"]

    if policy == "private":
        private_space = getattr(actor, "private_space", "")
        if private_space not in allowed:
            raise PermissionError("private scope is not accessible in this conversation")
        return [private_space]

    if policy == "family":
        if "FAMILY_SHARED" not in allowed:
            raise PermissionError("family scope is not accessible in this conversation")
        return ["FAMILY_SHARED"]

    if policy != "all":
        raise ValueError("read scope must be family, private, or all")

    # An explicitly-authorized all-spaces turn may be narrowed by the model,
    # but the model cannot widen family/private turns because those returned above.
    if requested in {"family", "shared", "family_shared"}:
        if "FAMILY_SHARED" not in allowed:
            raise PermissionError("family scope is not accessible in this conversation")
        return ["FAMILY_SHARED"]
    if requested in {"private", "personal", "my"}:
        private_space = getattr(actor, "private_space", "")
        if private_space not in allowed:
            raise PermissionError("private scope is not accessible in this conversation")
        return [private_space]
    if requested not in {"", "all", "visible", "accessible"}:
        raise ValueError("scope must be all, family, or private")
    return allowed


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
