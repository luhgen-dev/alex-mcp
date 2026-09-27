"""Project Jarvis Phase 2 reminder-delivery policy.

This is a pure decision layer. It does not send messages and is not wired into
Phase-1 reminder_dispatch yet.
"""
from __future__ import annotations

from datetime import datetime, time

import profile_config


def _parse_clock(value):
    if not value:
        return None
    hour, minute = (int(x) for x in value.split(":"))
    return time(hour, minute)


def _in_quiet_hours(now_t, start, end):
    if start is None or end is None:
        return False
    if start == end:
        return False
    if start < end:
        return start <= now_t < end
    return now_t >= start or now_t < end


def delivery_decision(preference_payload, presence_state, now_local,
                      delivery_class="routine"):
    """Return DELIVER or a safe defer reason.

    Time-critical reminders are never held merely because the user is away.
    Routine reminders may wait for presence when the owner explicitly enabled
    presence-aware delivery.
    """
    if not isinstance(now_local, datetime):
        raise ValueError("now_local must be datetime")
    delivery_class = str(delivery_class or "routine").lower()
    if delivery_class not in ("routine", "time_critical"):
        raise ValueError("delivery_class must be routine or time_critical")

    quiet = _in_quiet_hours(
        now_local.time(),
        _parse_clock(preference_payload.get("quiet_start")),
        _parse_clock(preference_payload.get("quiet_end")),
    )
    if quiet and delivery_class != "time_critical":
        return {"decision": "DEFER", "reason": "QUIET_HOURS"}

    presence_aware = bool(preference_payload.get("presence_aware"))
    normalized_presence = str(presence_state or "unknown").lower()
    if (
        presence_aware
        and delivery_class == "routine"
        and normalized_presence not in ("home", "present")
    ):
        return {"decision": "DEFER", "reason": "WAIT_FOR_PRESENCE"}

    return {"decision": "DELIVER", "reason": "DUE"}


def owner_preference(sender_phone, conversation_type="DIRECT_DM"):
    records = profile_config.authorized_records(
        sender_phone, conversation_type,
        kind="reminder_preference", requested_scope="private")
    active = [r for r in records if r["payload"].get("active", True)]
    if not active:
        return {
            "name": "default",
            "bill_days_before": 0,
            "follow_up_after_hours": 24,
            "quiet_start": None,
            "quiet_end": None,
            "presence_aware": False,
            "active": True,
        }
    if len(active) > 1:
        raise ValueError("More than one active reminder preference")
    return active[0]["payload"]


def owner_presence_mapping(sender_phone, conversation_type="DIRECT_DM"):
    records = profile_config.authorized_records(
        sender_phone, conversation_type,
        kind="presence_mapping", requested_scope="private")
    active = [r["payload"] for r in records if r["payload"].get("active", True)]
    if len(active) > 1:
        raise ValueError("More than one active presence mapping")
    return active[0] if active else None
