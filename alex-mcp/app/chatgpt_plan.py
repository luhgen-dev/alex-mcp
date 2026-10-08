"""ChatGPT plan provider for Alex (v0.5.33).

Lets Alex use the owner's own ChatGPT subscription allowance through OpenAI's
official "Sign in with ChatGPT" plan usage for self-hosted / locally hosted
apps, instead of a paid API key.

Design rules
------------
* Off by default. Nothing here runs unless ``chatgpt_plan_mode`` is not "off"
  AND the owner has signed in from Alex's own panel.
* The rest of Alex is untouched: ``client(settings)`` returns an object with the
  same ``chat.completions.create(**kwargs)`` surface the other providers use,
  translating to/from the Responses API that plan usage requires
  (``store=false``, ``stream=true``, instructions/developer messages instead of
  system items, function tools).
* Credentials live only in ``<data>/chatgpt_plan/`` with 0600 permissions, never
  in options, logs, the database or the repository. Refresh tokens rotate, so
  every refresh is serialised across threads AND processes and the replacement
  is written atomically before it is used.
* Failures surface as exceptions carrying ``status_code`` so Alex's existing
  provider classifier, fallback chain and circuit breaker handle them.

Reference: https://developers.openai.com/siwc/token-sharing-open-source
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:  # Linux (the HA add-on) has fcntl; keep imports safe elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX development machines
    fcntl = None


AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
API_BASE = "https://api.openai.com/v1"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Alex"

PENDING_TTL_SECONDS = 15 * 60
REFRESH_MARGIN_SECONDS = 5 * 60
MODELS_MAX_AGE_SECONDS = 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 20
INFERENCE_TIMEOUT_SECONDS = 30.0

# Refresh errors after which the stored refresh token can never work again.
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
}
# Confirmed revocation (owner disconnected Alex in ChatGPT settings).
REVOKED_ERRORS = {"subscription_sharing_invalid_user"}
USAGE_LIMIT_ERRORS = {"subscription_sharing_usage_limit_exceeded"}
TEMPORARY_ERRORS = {
    "subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
}


class ChatGPTPlanError(Exception):
    """Provider-shaped error: Alex's classifier reads ``status_code``."""

    def __init__(self, message: str, *, status_code: int = 503, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class NotSignedIn(ChatGPTPlanError):
    def __init__(self, message: str = "ChatGPT plan is not signed in.", *, code: str = "not_signed_in"):
        super().__init__(message, status_code=401, code=code)


class SignInError(Exception):
    """Owner-facing sign-in problem (shown in the panel, never a provider error)."""


# ---------------------------------------------------------------------------
# Private storage
# ---------------------------------------------------------------------------

def _base_dir() -> str:
    # Same data directory as Alex's database (fixed at process start).
    import config
    return os.path.join(config.DATA_DIR, "chatgpt_plan")


def _path(name: str) -> str:
    return os.path.join(_base_dir(), name)


def _ensure_dir() -> None:
    os.makedirs(_base_dir(), mode=0o700, exist_ok=True)
    try:
        os.chmod(_base_dir(), 0o700)
    except OSError:
        pass


def _read_json(name: str) -> dict | None:
    try:
        with open(_path(name), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_private_json(name: str, data: dict) -> None:
    """Atomic 0600 write: a crash never leaves a half-written token file."""
    _ensure_dir()
    final = _path(name)
    tmp = f"{final}.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _delete(name: str) -> None:
    try:
        os.unlink(_path(name))
    except OSError:
        pass


_THREAD_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


@contextmanager
def _session_lock():
    """Serialise refreshes across threads and processes (rotating tokens).

    Re-entrant within one thread: a nested use must not take a second flock on
    a new descriptor, which would block on the process's own lock.
    """
    with _THREAD_LOCK:
        depth = getattr(_LOCK_STATE, "depth", 0)
        if depth:
            _LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _LOCK_STATE.depth = depth
            return
        _ensure_dir()
        fd = os.open(_path("refresh.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        _LOCK_STATE.depth = 1
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            _LOCK_STATE.depth = 0
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def host_id() -> str:
    """Stable opaque per-host identifier (never an email or user id)."""
    _ensure_dir()
    path = _path("host_id")
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        if value.startswith("urn:uuid:"):
            return value
    except OSError:
        pass
    value = "urn:uuid:" + str(uuid.uuid4())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing.startswith("urn:uuid:"):
            return existing
        os.unlink(path)
        return host_id()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def load_credentials() -> dict | None:
    return _read_json("credentials.json")


def load_registration() -> dict | None:
    return _read_json("registration.json")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwt_claims(token: str) -> dict:
    """Decode JWT claims WITHOUT signature verification.

    Only used for tokens received directly from OpenAI's token endpoint over
    TLS (OIDC Core 3.1.3.7 permits TLS server validation in place of the
    signature check for that case) and for reading our own access-token expiry.
    """
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _absolute_time(value, now: float) -> float | None:
    """Accept an epoch timestamp or a relative number of seconds."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number if number > 1_000_000_000 else now + number


def _oauth_error_code(response) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")
    return str(error or data.get("code") or "")


def _scopes(value) -> set[str]:
    if isinstance(value, (list, tuple)):
        return {str(x) for x in value}
    return set(str(value or "").split())


def _credential_record(token_data: dict, *, client_id: str, now: float,
                       previous: dict | None = None, email: str | None = None) -> dict:
    previous = previous or {}
    expires_at = _absolute_time(token_data.get("expires_in"), now)
    if expires_at is None:
        expires_at = float(_jwt_claims(token_data.get("access_token", "")).get("exp") or now + 3600)
    record = {
        "status": "active",
        "client_id": client_id,
        "access_token": token_data["access_token"],
        # Rotation: always prefer the replacement; keep the old one only if
        # the server did not issue a new one.
        "refresh_token": token_data.get("refresh_token") or previous.get("refresh_token"),
        "id_token": token_data.get("id_token") or previous.get("id_token"),
        "scope": token_data.get("scope") or previous.get("scope") or "",
        "expires_at": expires_at,
        "earliest_refresh_at": _absolute_time(token_data.get("earliest_refresh_at"), now),
        "obtained_at": previous.get("obtained_at") or now,
        "refreshed_at": now,
        "email": email if email is not None else previous.get("email"),
        "ext_agent_host_id": host_id(),
    }
    return record


def _mark_needs_sign_in(reason: str, *, forget_registration: bool = False) -> None:
    creds = load_credentials() or {}
    _write_private_json("credentials.json", {
        "status": "needs_sign_in",
        "reason": str(reason or "")[:80],
        "client_id": creds.get("client_id"),
        "email": creds.get("email"),
        "marked_at": time.time(),
    })
    if forget_registration:
        _delete("registration.json")


# ---------------------------------------------------------------------------
# Sign-in (owner pastes the browser's redirect URL into Alex's panel)
# ---------------------------------------------------------------------------

def start_sign_in(*, now: float | None = None) -> dict:
    """Create a fresh PKCE attempt and return the URL the owner opens."""
    current = time.time() if now is None else now
    registration = load_registration() or {}
    client_id = str(registration.get("client_id") or "") or DYNAMIC_CLIENT_ID
    verifier = _b64url(secrets.token_bytes(48))
    state = _b64url(secrets.token_bytes(24))
    nonce = _b64url(secrets.token_bytes(24))
    params = {
        "client_id": client_id,
        "ext_agent_host_id": host_id(),
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
    }
    if client_id == DYNAMIC_CLIENT_ID:
        params["agent_name_hint"] = AGENT_NAME
    _write_private_json("pending.json", {
        "state": state,
        "nonce": nonce,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "created_at": current,
    })
    return {
        "authorize_url": AUTHORIZE_URL + "?" + urlencode(params),
        "expires_in": PENDING_TTL_SECONDS,
        "first_registration": client_id == DYNAMIC_CLIENT_ID,
    }


def _parse_callback(pasted: str) -> dict:
    text = str(pasted or "").strip()
    if not text:
        raise SignInError("Paste the full address from your browser's address bar.")
    parsed = urlparse(text)
    query = parsed.query if parsed.query else text.lstrip("?")
    values = parse_qs(query, keep_blank_values=False)
    return {k: v[0] for k, v in values.items() if v}


def finish_sign_in(pasted: str, *, now: float | None = None, session=None) -> dict:
    """Validate the pasted redirect, exchange the code and store the session."""
    current = time.time() if now is None else now
    http = session or requests
    params = _parse_callback(pasted)
    pending = _read_json("pending.json")
    if not pending or current - float(pending.get("created_at") or 0) > PENDING_TTL_SECONDS:
        _delete("pending.json")
        raise SignInError("That sign-in has expired. Tap “Start sign-in” and try again.")
    if params.get("state") != pending.get("state"):
        raise SignInError(
            "That address doesn't belong to the current sign-in. "
            "Use the address from the latest “Start sign-in” attempt."
        )
    if params.get("error"):
        _delete("pending.json")
        if params["error"] == "access_denied":
            raise SignInError("Sign-in was cancelled in the browser. Nothing was saved.")
        raise SignInError(f"OpenAI declined the sign-in ({params['error'][:60]}). Nothing was saved.")
    code = params.get("code")
    if not code:
        raise SignInError("That address has no sign-in code. Copy the whole address after signing in.")
    client_id = params.get("client_id") or pending.get("client_id") or ""
    if not client_id or client_id == DYNAMIC_CLIENT_ID:
        raise SignInError("OpenAI didn't return Alex's app registration. Start the sign-in again.")

    try:
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": pending["code_verifier"],
                "redirect_uri": pending.get("redirect_uri") or REDIRECT_URI,
                "resource": RESOURCE,
            },
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SignInError(f"Couldn't reach OpenAI to finish sign-in ({exc.__class__.__name__}). Try again.")
    if response.status_code != 200:
        error = _oauth_error_code(response)
        if error == "invalid_grant":
            _delete("pending.json")
            raise SignInError("That sign-in code was already used or has expired. Start the sign-in again.")
        raise SignInError(f"OpenAI rejected the sign-in ({error or response.status_code}). Start again.")
    try:
        token_data = response.json()
    except Exception:
        token_data = {}
    if not isinstance(token_data, dict) or not token_data.get("access_token") \
            or not token_data.get("refresh_token"):
        raise SignInError("OpenAI didn't return a usable session. Start the sign-in again.")
    granted = _scopes(token_data.get("scope")) | _scopes(
        _jwt_claims(token_data.get("access_token", "")).get("scope")
    )
    if REQUIRED_SCOPE not in granted:
        _delete("pending.json")
        raise SignInError(
            "ChatGPT plan usage wasn't granted. Start again and allow Alex to use your plan."
        )
    id_claims = _jwt_claims(token_data.get("id_token", ""))
    audience = id_claims.get("aud")
    audiences = set(audience) if isinstance(audience, list) else {audience}
    if (
        not id_claims
        or id_claims.get("iss") != ISSUER
        or client_id not in audiences
        or float(id_claims.get("exp") or 0) <= current
        or id_claims.get("nonce") != pending.get("nonce")
    ):
        _delete("pending.json")
        raise SignInError("The sign-in response couldn't be verified. Nothing was saved; start again.")

    with _session_lock():
        _write_private_json("registration.json", {
            "client_id": client_id,
            "ext_agent_host_id": host_id(),
            "registered_at": (load_registration() or {}).get("registered_at") or current,
        })
        _write_private_json("credentials.json", _credential_record(
            token_data, client_id=client_id, now=current,
            email=str(id_claims.get("email") or "") or None,
        ))
    _delete("pending.json")
    try:
        refresh_models(session=session)
    except Exception:
        pass  # The model list is a convenience; status shows if it's missing.
    return status()


def sign_out() -> dict:
    """Forget the local session (the owner can also disconnect in ChatGPT)."""
    with _session_lock():
        _delete("credentials.json")
        _delete("pending.json")
        _delete("models.json")
    return status()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _needs_refresh(creds: dict, now: float) -> bool:
    expires_at = float(creds.get("expires_at") or 0)
    if now < expires_at - REFRESH_MARGIN_SECONDS:
        return False
    earliest = creds.get("earliest_refresh_at")
    if earliest and now < float(earliest) and now < expires_at:
        return False  # still valid and the server asked us to wait
    return True


def _refresh_locked(creds: dict, now: float, session=None) -> dict:
    http = session or requests
    client_id = str(creds.get("client_id") or "")
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": creds["refresh_token"],
                "resource": RESOURCE,
            },
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        # Temporary: never erase credentials for a network problem.
        raise ChatGPTPlanError(f"ChatGPT token refresh unreachable: {exc.__class__.__name__}",
                               status_code=503, code="refresh_unreachable")
    if response.status_code == 200:
        try:
            token_data = response.json()
        except Exception:
            token_data = None
        if not isinstance(token_data, dict) or not token_data.get("access_token"):
            raise ChatGPTPlanError("ChatGPT token refresh returned no access token.",
                                   status_code=503, code="refresh_malformed")
        record = _credential_record(token_data, client_id=client_id, now=now, previous=creds)
        _write_private_json("credentials.json", record)
        return record
    error = _oauth_error_code(response)
    if error in TERMINAL_REFRESH_ERRORS:
        _mark_needs_sign_in(error)
        raise NotSignedIn("ChatGPT sign-in expired; sign in again from the Alex panel.", code=error)
    if error == "invalid_client":
        _mark_needs_sign_in(error, forget_registration=True)
        raise NotSignedIn("Alex's ChatGPT registration is no longer valid; sign in again.", code=error)
    raise ChatGPTPlanError(f"ChatGPT token refresh failed ({error or response.status_code}).",
                           status_code=503 if response.status_code >= 500 else 401,
                           code=error or f"http_{response.status_code}")


def access_token(*, now: float | None = None, force: bool = False, session=None) -> str:
    current = time.time() if now is None else now
    creds = load_credentials()
    if not creds or creds.get("status") != "active" or not creds.get("refresh_token"):
        raise NotSignedIn()
    if not force and not _needs_refresh(creds, current):
        return str(creds["access_token"])
    with _session_lock():
        # Another thread/process may have refreshed while we waited.
        creds = load_credentials()
        if not creds or creds.get("status") != "active" or not creds.get("refresh_token"):
            raise NotSignedIn()
        if not force and not _needs_refresh(creds, current):
            return str(creds["access_token"])
        if force:
            fresh_window = float(creds.get("refreshed_at") or 0)
            if current - fresh_window < 30:
                return str(creds["access_token"])  # someone just refreshed
        return str(_refresh_locked(creds, current, session=session)["access_token"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def _parse_models(data) -> list[dict]:
    items = []
    if isinstance(data, dict):
        items = data.get("models") or data.get("data") or []
    models = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        visibility = item.get("visibility")
        if visibility is not None and visibility != "list":
            continue
        slug = str(item.get("slug") or item.get("id") or "").strip()
        if slug:
            models.append({"slug": slug, "display_name": str(item.get("display_name") or slug)})
    return models


def refresh_models(*, session=None) -> list[dict]:
    http = session or requests
    token = access_token(session=session)
    response = http.get(
        API_BASE + "/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise ChatGPTPlanError(f"Model list failed ({response.status_code}).",
                               status_code=response.status_code)
    models = _parse_models(response.json())
    _write_private_json("models.json", {"fetched_at": time.time(), "models": models})
    return models


def cached_models() -> list[dict]:
    data = _read_json("models.json") or {}
    models = data.get("models")
    return models if isinstance(models, list) else []


def resolved_model(settings) -> str:
    configured = str(getattr(settings, "chatgpt_plan_model", "") or "").strip()
    if configured:
        return configured
    models = cached_models()
    return str(models[0].get("slug") or "") if models else ""


def mode(settings) -> str:
    value = str(getattr(settings, "chatgpt_plan_mode", "off") or "off").strip().lower()
    return value if value in {"off", "shadow_only", "primary"} else "off"


def ready_model(settings) -> str:
    """Model to use when the plan is enabled and signed in, else ''.

    Cheap: local file reads only, no network. Safe to call per message.
    """
    if mode(settings) == "off":
        return ""
    creds = load_credentials()
    if not creds or creds.get("status") != "active" or not creds.get("refresh_token"):
        return ""
    return resolved_model(settings)


def status(settings=None) -> dict:
    creds = load_credentials() or {}
    signed_in = creds.get("status") == "active" and bool(creds.get("refresh_token"))
    pending = _read_json("pending.json")
    result = {
        "signed_in": signed_in,
        "needs_sign_in": creds.get("status") == "needs_sign_in",
        "needs_sign_in_reason": creds.get("reason") if creds.get("status") == "needs_sign_in" else None,
        "account_email": creds.get("email") if signed_in else None,
        "registered": bool((load_registration() or {}).get("client_id")),
        "access_expires_at": creds.get("expires_at") if signed_in else None,
        "sign_in_pending": bool(
            pending and time.time() - float(pending.get("created_at") or 0) <= PENDING_TTL_SECONDS
        ),
        "models": [m.get("slug") for m in cached_models()],
    }
    if settings is not None:
        result["mode"] = mode(settings)
        result["model_in_use"] = resolved_model(settings) or None
    return result


# ---------------------------------------------------------------------------
# Chat Completions <-> Responses translation (pure, unit-tested)
# ---------------------------------------------------------------------------

def _plain_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"text", "input_text", "output_text"}:
                    parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(x for x in parts if x)
    return str(content)


def _user_content(content):
    if not isinstance(content, list):
        return _plain_text(content)
    converted = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"text", "input_text"}:
            converted.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                item = {"type": "input_image", "image_url": str(url)}
                detail = image.get("detail") if isinstance(image, dict) else None
                if detail:
                    item["detail"] = detail
                converted.append(item)
        elif kind == "input_image":
            converted.append(dict(part))
    return converted or ""


def chat_to_responses(kwargs: dict) -> dict:
    """Translate an Alex chat.completions request into a plan-usage Responses body."""
    instructions: list[str] = []
    items: list[dict] = []
    leading = True
    for message in kwargs.get("messages") or []:
        role = message.get("role")
        if role == "system":
            text = _plain_text(message.get("content"))
            if leading:
                instructions.append(text)
            elif text:
                # Plan usage rejects system items; later system guidance becomes
                # a developer message in its original position.
                items.append({"role": "developer", "content": text})
            continue
        leading = False
        if role == "developer":
            items.append({"role": "developer", "content": _plain_text(message.get("content"))})
        elif role == "user":
            items.append({"role": "user", "content": _user_content(message.get("content"))})
        elif role == "assistant":
            text = _plain_text(message.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or "{}"),
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": _plain_text(message.get("content")),
            })

    body: dict = {
        "model": kwargs["model"],
        "input": items,
        "store": False,
        "stream": True,
    }
    if instructions:
        body["instructions"] = "\n\n".join(x for x in instructions if x)
    tools = kwargs.get("tools") or []
    if tools:
        converted = []
        for tool in tools:
            function = (tool or {}).get("function") or {}
            if not function.get("name"):
                continue
            converted.append({
                "type": "function",
                "name": function["name"],
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                "strict": False,
            })
        if converted:
            body["tools"] = converted
            choice = kwargs.get("tool_choice")
            if choice in {"auto", "none", "required"}:
                body["tool_choice"] = choice
    effort = kwargs.get("reasoning_effort")
    if effort:
        body["reasoning"] = {"effort": effort}
    response_format = kwargs.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        schema = response_format.get("json_schema") or {}
        body["text"] = {"format": {
            "type": "json_schema",
            "name": schema.get("name") or "result",
            "schema": schema.get("schema") or {},
            "strict": bool(schema.get("strict", True)),
        }}
    return body


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def collect_stream(events):
    """Return the final Response from a plan-usage stream, or raise."""
    completed = None
    try:
        for event in events:
            kind = _get(event, "type")
            if kind == "response.completed":
                completed = _get(event, "response")
                break
            if kind == "response.failed":
                error = _get(_get(event, "response"), "error") or {}
                code = str(_get(error, "code") or "response_failed")
                message = str(_get(error, "message") or code)
                raise _error_for_code(code, message)
            if kind == "response.incomplete":
                details = _get(_get(event, "response"), "incomplete_details") or {}
                reason = str(_get(details, "reason") or "incomplete")
                raise ChatGPTPlanError(f"ChatGPT response incomplete: {reason}",
                                       status_code=502, code="incomplete")
            if kind == "error":
                code = str(_get(event, "code") or "stream_error")
                raise _error_for_code(code, str(_get(event, "message") or code))
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    if completed is None:
        raise ChatGPTPlanError("ChatGPT stream ended without completion.",
                               status_code=503, code="stream_incomplete")
    return completed


def _error_for_code(code: str, message: str) -> ChatGPTPlanError:
    if code in USAGE_LIMIT_ERRORS:
        return ChatGPTPlanError("ChatGPT plan usage limit reached.", status_code=429, code=code)
    if code in TEMPORARY_ERRORS:
        return ChatGPTPlanError("ChatGPT plan temporarily unavailable.", status_code=503, code=code)
    if code in REVOKED_ERRORS:
        return ChatGPTPlanError("ChatGPT access was revoked.", status_code=401, code=code)
    return ChatGPTPlanError(f"ChatGPT request failed: {message[:200]}", status_code=502, code=code)


def responses_to_chat(response, model: str):
    """Shape a Responses result like the ChatCompletion Alex already consumes."""
    from openai.types.chat import ChatCompletion

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for item in _get(response, "output") or []:
        kind = _get(item, "type")
        if kind == "message":
            for part in _get(item, "content") or []:
                part_kind = _get(part, "type")
                if part_kind == "output_text":
                    text_parts.append(str(_get(part, "text") or ""))
                elif part_kind == "refusal":
                    text_parts.append(str(_get(part, "refusal") or ""))
        elif kind == "function_call":
            tool_calls.append({
                "id": str(_get(item, "call_id") or _get(item, "id") or ""),
                "type": "function",
                "function": {
                    "name": str(_get(item, "name") or ""),
                    "arguments": str(_get(item, "arguments") or "{}"),
                },
            })
    usage = _get(response, "usage")
    input_tokens = int(_get(usage, "input_tokens", 0) or 0) if usage else 0
    output_tokens = int(_get(usage, "output_tokens", 0) or 0) if usage else 0
    cached = int(_get(_get(usage, "input_tokens_details"), "cached_tokens", 0) or 0) if usage else 0
    reasoning = int(_get(_get(usage, "output_tokens_details"), "reasoning_tokens", 0) or 0) if usage else 0
    created = _get(response, "created_at")
    return ChatCompletion.model_validate({
        "id": str(_get(response, "id") or "chatgpt-plan"),
        "object": "chat.completion",
        "created": int(created or time.time()),
        "model": str(_get(response, "model") or model),
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "message": {
                "role": "assistant",
                "content": "".join(text_parts) if text_parts else None,
                "tool_calls": tool_calls or None,
            },
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    })


# ---------------------------------------------------------------------------
# Client shim
# ---------------------------------------------------------------------------

_NO_REASONING_MODELS: set[str] = set()


def _error_code(exc) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return str(error.get("code") or "")
    return ""


class _Completions:
    def __init__(self, settings, openai_factory=None):
        self._settings = settings
        self._factory = openai_factory

    def _openai(self, token: str):
        if self._factory is not None:
            return self._factory(token)
        from openai import OpenAI
        return OpenAI(api_key=token, base_url=API_BASE, max_retries=0,
                      timeout=INFERENCE_TIMEOUT_SECONDS)

    def _send(self, body: dict, token: str):
        return collect_stream(self._openai(token).responses.create(**body))

    def create(self, **kwargs):
        body = chat_to_responses(kwargs)
        model = str(body["model"])
        if model in _NO_REASONING_MODELS:
            body.pop("reasoning", None)
        token = access_token()
        refreshed = False
        reasoning_dropped = False
        while True:
            try:
                return responses_to_chat(self._send(body, token), model)
            except ChatGPTPlanError as exc:
                if exc.code in REVOKED_ERRORS:
                    _mark_needs_sign_in(exc.code)
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                code = _error_code(exc)
                if code in REVOKED_ERRORS:
                    _mark_needs_sign_in(code)
                    raise NotSignedIn("ChatGPT access was revoked; sign in again.", code=code)
                if status == 401 and not refreshed:
                    refreshed = True
                    token = access_token(force=True)
                    continue
                if (status == 400 and "reasoning" in body and not reasoning_dropped
                        and "reasoning" in str(exc).lower()):
                    # This model doesn't take a reasoning effort; remember it.
                    reasoning_dropped = True
                    _NO_REASONING_MODELS.add(model)
                    body.pop("reasoning", None)
                    continue
                if code in USAGE_LIMIT_ERRORS or code in TEMPORARY_ERRORS:
                    raise _error_for_code(code, str(exc)) from exc
                raise


class PlanClient:
    """Drop-in for the subset of ``openai.OpenAI`` that Alex's brain uses."""

    def __init__(self, settings, openai_factory=None):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(settings, openai_factory)


def client(settings, openai_factory=None) -> PlanClient:
    creds = load_credentials()
    if not creds or creds.get("status") != "active" or not creds.get("refresh_token"):
        # Signed out: fail like a provider so Alex's fallbacks take over.
        raise NotSignedIn()
    return PlanClient(settings, openai_factory)


def test_connection(settings) -> dict:
    """Owner-triggered check from the panel; works in any mode once signed in."""
    started = time.monotonic()
    model = resolved_model(settings)
    if not model:
        try:
            refresh_models()
            model = resolved_model(settings)
        except Exception:
            model = ""
    if not model:
        return {"status": "error", "message": "No ChatGPT model available yet. Sign in first."}
    try:
        completion = PlanClient(settings).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Reply exactly OK."},
                {"role": "user", "content": "Connection check."},
            ],
            reasoning_effort="low",
        )
        content = completion.choices[0].message.content or ""
        return {
            "status": "ok", "model": model,
            "message": content.strip()[:40] or "OK",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "error", "model": model,
            "code": getattr(exc, "code", "") or exc.__class__.__name__,
            "status_code": getattr(exc, "status_code", None),
            "message": str(exc)[:200],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
