from __future__ import annotations

import os
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


def find_entities(query: str, domain: str | None = None, limit: int = 20) -> dict:
    needle = (query or "").strip().lower()
    if not needle:
        raise ValueError("entity search query is required")
    rows = _request("GET", "/states") or []
    matches = []
    for row in rows:
        entity_id = str(row.get("entity_id", ""))
        entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain and entity_domain != domain:
            continue
        friendly = str((row.get("attributes") or {}).get("friendly_name", ""))
        if needle not in entity_id.lower() and needle not in friendly.lower():
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

    _request("POST", f"/services/{domain}/{service}", payload)
    time.sleep(0.25)
    verified = get_state(entity)
    return {
        "status": "executed_and_verified",
        "entity_id": entity,
        "action": service,
        "state_after": verified,
    }
