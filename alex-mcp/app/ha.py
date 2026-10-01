from __future__ import annotations

import os
import re
import time

import requests

BASE_URL = os.environ.get("ALEX_HA_API_URL", "http://supervisor/core/api").rstrip("/")

SAFE_ACTIONS = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage"},
    "climate": {"turn_on", "turn_off", "set_temperature"},
    "media_player": {"turn_on", "turn_off", "media_play", "media_pause", "volume_set"},
}

SENSITIVE_DOMAINS = {
    "lock", "alarm_control_panel", "cover", "script", "scene", "automation",
    "button", "siren", "vacuum", "camera", "remote",
}


def _token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Home Assistant Supervisor token is unavailable")
    return token


def _request(method: str, path: str, payload: dict | None = None):
    response = requests.request(
        method,
        BASE_URL + path,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if response.status_code == 404:
        raise LookupError("Home Assistant entity or endpoint not found")
    response.raise_for_status()
    if not response.content:
        return None
    return response.json()


def notify_mobile(service_name: str, message: str, title: str | None = None,
                  data: dict | None = None) -> dict:
    """Send one Home Assistant Companion notification through a configured notify service."""
    service = str(service_name or "").strip()
    if service.startswith("notify."):
        service = service.split(".", 1)[1]
    if not re.fullmatch(r"[a-z0-9_]+", service):
        raise ValueError("notify_service must be a Home Assistant notify service name")
    payload = {"message": str(message or "")}
    if title:
        payload["title"] = str(title)
    if data:
        payload["data"] = dict(data)
    result = _request("POST", f"/services/notify/{service}", payload)
    return {"status": "sent_to_home_assistant", "service": service, "result": result}


def websocket_url() -> str:
    override = os.environ.get("ALEX_HA_WS_URL", "").strip()
    if override:
        return override
    base = BASE_URL
    if base.endswith("/api"):
        base = base[:-4]
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base.rstrip("/") + "/websocket"


def list_states() -> list[dict]:
    """Return normalized HA snapshots for deterministic local summarizers."""
    rows = _request("GET", "/states") or []
    out = []
    for row in rows:
        attrs = row.get("attributes") or {}
        out.append({
            "entity_id": row.get("entity_id"),
            "state": row.get("state"),
            "friendly_name": attrs.get("friendly_name"),
            "name": attrs.get("friendly_name"),
            "device_class": attrs.get("device_class"),
        })
    return out


def draft_automation(name: str, trigger_yaml: str, action_yaml: str,
                     condition_yaml: str | None = None) -> dict:
    """Validate a non-deployed automation draft against actual entity IDs."""
    title = (name or "").strip()
    trigger = (trigger_yaml or "").strip()
    action = (action_yaml or "").strip()
    condition = (condition_yaml or "").strip()
    if not title or not trigger or not action:
        raise ValueError("automation name, trigger and action are required")
    if len(trigger) + len(action) + len(condition) > 20000:
        raise ValueError("automation draft is too large")

    combined = "\n".join((trigger, condition, action))
    mentioned = sorted(set(re.findall(
        r"\b(?:light|switch|fan|climate|media_player|binary_sensor|sensor|person|device_tracker|"
        r"lock|cover|alarm_control_panel|automation|script|scene)\.[a-z0-9_]+\b",
        combined, flags=re.IGNORECASE,
    )))
    actual = {str(row.get("entity_id") or "") for row in (_request("GET", "/states") or [])}
    missing = [entity for entity in mentioned if entity not in actual]
    sensitive = [
        entity for entity in mentioned
        if entity.split(".", 1)[0] in SENSITIVE_DOMAINS
    ]
    return {
        "status": "draft_only",
        "name": title[:200],
        "trigger_yaml": trigger,
        "condition_yaml": condition or None,
        "action_yaml": action,
        "entity_ids": mentioned,
        "missing_entity_ids": missing,
        "sensitive_entities": sensitive,
        "validated": not missing,
        "deployed": False,
        "confirmation_required_before_deployment": True,
    }


def find_entities(query: str, domain: str | None = None, limit: int = 20) -> dict:
    def normalize(value: str) -> str:
        text = (value or "").strip().lower().replace("_", " ")
        text = re.sub(r"\bair\s*condition(?:er|ing)?\b|\baircon\b", "ac", text)
        return " ".join(re.findall(r"[a-z0-9]+", text))

    needle = normalize(query)
    if not needle and not domain:
        raise ValueError("entity search query or domain is required")
    wanted = needle.split()
    rows = _request("GET", "/states") or []
    matches = []
    for row in rows:
        entity_id = str(row.get("entity_id", ""))
        entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain and entity_domain != domain:
            continue
        friendly = str((row.get("attributes") or {}).get("friendly_name", ""))
        haystack = normalize(entity_id + " " + friendly)
        if wanted and not all(term in haystack.split() or term in haystack for term in wanted):
            continue
        matches.append({
            "entity_id": entity_id,
            "friendly_name": friendly or entity_id,
            "state": row.get("state"),
        })
        if len(matches) >= max(1, min(50, int(limit))):
            break
    return {"matches": matches}


def get_state(entity_id: str) -> dict:
    entity = (entity_id or "").strip()
    if "." not in entity:
        raise ValueError("exact Home Assistant entity_id is required")
    row = _request("GET", f"/states/{entity}")
    attrs = row.get("attributes") or {}
    return {
        "entity_id": row.get("entity_id"),
        "state": row.get("state"),
        "friendly_name": attrs.get("friendly_name"),
        "attributes": attrs,
        "last_changed": row.get("last_changed"),
    }


def control(entity_id: str, action: str, value: float | None = None) -> dict:
    entity = (entity_id or "").strip()
    if "." not in entity:
        raise ValueError("exact Home Assistant entity_id is required")
    domain = entity.split(".", 1)[0]
    service = (action or "").strip().lower()

    if domain in SENSITIVE_DOMAINS or domain not in SAFE_ACTIONS:
        raise PermissionError(f"Alex MCP does not autonomously control the Home Assistant domain '{domain}'")
    if service not in SAFE_ACTIONS[domain]:
        raise PermissionError(f"Action '{service}' is not allowed for Home Assistant domain '{domain}'")

    payload = {"entity_id": entity}
    if domain == "light" and service == "turn_on" and value is not None:
        payload["brightness_pct"] = max(0, min(100, int(round(value))))
    elif domain == "fan" and service == "set_percentage":
        if value is None:
            raise ValueError("fan set_percentage requires value")
        payload["percentage"] = max(0, min(100, int(round(value))))
    elif domain == "climate" and service == "set_temperature":
        if value is None:
            raise ValueError("climate set_temperature requires value")
        payload["temperature"] = float(value)
    elif domain == "media_player" and service == "volume_set":
        if value is None:
            raise ValueError("media volume_set requires value from 0 to 1")
        payload["volume_level"] = max(0.0, min(1.0, float(value)))
    elif value is not None and service not in {"turn_on"}:
        raise ValueError("value is not used by this Home Assistant action")

    before = get_state(entity)
    _request("POST", f"/services/{domain}/{service}", payload)

    def matches_requested(snapshot: dict) -> bool:
        state = str(snapshot.get("state") or "").lower()
        if state in {"unavailable", "unknown", ""}:
            return False
        attrs = snapshot.get("attributes") or {}
        if service == "turn_on":
            return state == "on"
        if service == "turn_off":
            return state == "off"
        if service == "toggle":
            before_state = str(before.get("state") or "").lower()
            return before_state not in {"", "unknown", "unavailable"} and state != before_state
        if domain == "fan" and service == "set_percentage":
            try:
                return abs(float(attrs.get("percentage")) - float(value)) <= 1.0
            except (TypeError, ValueError):
                return False
        if domain == "climate" and service == "set_temperature":
            try:
                return abs(float(attrs.get("temperature")) - float(value)) <= 0.5
            except (TypeError, ValueError):
                return False
        if domain == "media_player" and service == "media_play":
            return state == "playing"
        if domain == "media_player" and service == "media_pause":
            return state in {"paused", "idle"}
        if domain == "media_player" and service == "volume_set":
            try:
                return abs(float(attrs.get("volume_level")) - float(value)) <= 0.03
            except (TypeError, ValueError):
                return False
        return False

    verified = None
    for _ in range(4):
        time.sleep(0.25)
        verified = get_state(entity)
        if matches_requested(verified):
            return {
                "status": "executed_and_verified",
                "entity_id": entity,
                "action": service,
                "state_after": verified,
            }

    return {
        "status": "requested_unconfirmed",
        "entity_id": entity,
        "action": service,
        "state_before": before,
        "state_after": verified,
        "note": "Home Assistant accepted the service call but the requested state was not verified.",
    }
