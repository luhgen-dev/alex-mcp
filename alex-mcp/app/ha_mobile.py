from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import websocket

import db
import ha
import runtime_clock
from config import DATA_DIR, get_settings


_ROLE_TO_USER = {"husband": "USR_HUSBAND", "wife": "USR_WIFE"}
_USER_TO_ROLE = {v: k for k, v in _ROLE_TO_USER.items()}
_ALLOWED_ACTIONS = {
    "ACK", "DONE", "SNOOZE10", "CLAIM", "HANDOFF_ACCEPT", "HANDOFF_DECLINE",
}
_SECRET_PATH = Path(DATA_DIR) / "ha_action_secret"


def _secret() -> bytes:
    try:
        value = _SECRET_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value.encode("ascii")
    except OSError:
        pass
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    value = os.urandom(32).hex()
    try:
        fd = os.open(str(_SECRET_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    except FileExistsError:
        value = _SECRET_PATH.read_text(encoding="utf-8").strip()
    return value.encode("ascii")


def _device_configs(user_id: str) -> list[dict]:
    role = _USER_TO_ROLE.get(user_id)
    if not role:
        return []
    out: list[dict] = []
    for item in get_settings().ha_notify_devices:
        if not isinstance(item, dict):
            continue
        if str(item.get("owner") or "").strip().casefold() != role:
            continue
        if item.get("active") is False:
            continue
        service = str(item.get("notify_service") or "").strip()
        if service.startswith("notify."):
            service = service.split(".", 1)[1]
        if not service:
            continue
        out.append({
            "id": str(item.get("id") or service)[:80],
            "notify_service": service[:160],
        })
    return out


def configured_device_summary() -> dict:
    """Observed HA Companion configuration only; no delivery claim."""
    devices = []
    for user_id in ("USR_HUSBAND", "USR_WIFE"):
        for cfg in _device_configs(user_id):
            devices.append({
                "user_id": user_id,
                "id": cfg["id"],
                "notify_service": cfg["notify_service"],
            })
    return {
        "enabled": bool(devices),
        "count": len(devices),
        "devices": devices,
    }


def _sign(action: str, target_kind: str, target_id: str, user_id: str) -> str:
    body = "|".join((action, target_kind, target_id, user_id))
    return hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def action_token(action: str, target_kind: str, target_id: str, user_id: str) -> str:
    action = str(action or "").strip().upper()
    target_kind = str(target_kind or "").strip().lower()
    target_id = str(target_id or "").strip()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("unsupported HA reminder action")
    if target_kind not in {"reminder", "handoff"} or not target_id:
        raise ValueError("invalid HA reminder action target")
    if user_id not in _USER_TO_ROLE:
        raise ValueError("invalid household user")
    sig = _sign(action, target_kind, target_id, user_id)
    return f"ALEX|{action}|{target_kind}|{target_id}|{user_id}|{sig}"


def parse_action_token(value: str) -> dict | None:
    parts = str(value or "").split("|")
    if len(parts) != 6 or parts[0] != "ALEX":
        return None
    _, action, target_kind, target_id, user_id, sig = parts
    if action not in _ALLOWED_ACTIONS:
        return None
    if target_kind not in {"reminder", "handoff"} or user_id not in _USER_TO_ROLE:
        return None
    expected = _sign(action, target_kind, target_id, user_id)
    if not hmac.compare_digest(expected, sig):
        return None
    return {
        "action": action,
        "target_kind": target_kind,
        "target_id": target_id,
        "user_id": user_id,
    }


def _queue(
    conn,
    user_id: str,
    event_key: str,
    message: str,
    *,
    title: str = "Alex Reminder",
    reminder_id: str | None = None,
    handoff_id: str | None = None,
    tag: str | None = None,
    actions: list[tuple[str, str, str, str]] | None = None,
) -> int:
    count = 0
    for cfg in _device_configs(user_id):
        notification_id = str(uuid.uuid4())
        data: dict = {
            "confirmation": True,
            "alex_notification_id": notification_id,
        }
        if tag:
            data["tag"] = tag
        if actions:
            data["actions"] = [
                {
                    "action": action_token(code, kind, target_id, user_id),
                    "title": label,
                }
                for code, label, kind, target_id in actions[:3]
            ]
        service = cfg["notify_service"]
        unique = f"{event_key}:{user_id}:{service}"
        cur = conn.execute(
            """INSERT OR IGNORE INTO ha_notification_outbox(
                   notification_id,event_key,user_id,notify_service,reminder_id,handoff_id,
                   title,message,data_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                notification_id, unique, user_id, service, reminder_id, handoff_id,
                title[:160], str(message or "")[:1000],
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        count += int(cur.rowcount or 0)
    return count


def queue_assigned_ack(conn, user_id: str, reminder_id: str, task: str, due_local: str) -> int:
    return _queue(
        conn, user_id, f"assigned:{reminder_id}",
        f"Reminder assigned to you: {task}. I’ll remind you at {due_local}.",
        reminder_id=reminder_id, tag=f"alex-reminder-{reminder_id}",
    )


def queue_due(conn, row, message: str, phase: str = "due") -> int:
    reminder_id = str(row["reminder_id"])
    task = str(row["task_text"])
    claimed = row["claimed_by_user_id"] if "claimed_by_user_id" in row.keys() else None
    claimable = bool(int(row["claimable"] or 0))
    if claimable and not claimed:
        # Claiming is a pre-due ownership action on the WhatsApp setup message.
        # Once due, the shared reminder stays shared; phone actions acknowledge
        # or complete it instead of creating a late claim.
        total = 0
        for user_id in ("USR_HUSBAND", "USR_WIFE"):
            total += _queue(
                conn, user_id, f"{phase}:{reminder_id}:shared-due",
                message,
                reminder_id=reminder_id, tag=f"alex-reminder-{reminder_id}",
                actions=[
                    ("ACK", "Acknowledge", "reminder", reminder_id),
                    ("DONE", "Done", "reminder", reminder_id),
                ],
            )
        return total
    target = str(claimed or row["owner_id"])
    return _queue(
        conn, target, f"{phase}:{reminder_id}",
        message,
        reminder_id=reminder_id, tag=f"alex-reminder-{reminder_id}",
        actions=[
            ("ACK", "Acknowledge", "reminder", reminder_id),
            ("DONE", "Done", "reminder", reminder_id),
            ("SNOOZE10", "Snooze 10m", "reminder", reminder_id),
        ],
    )


def queue_claim_confirmation(conn, user_id: str, reminder_id: str, task: str,
                             event_key: str | None = None) -> int:
    return _queue(
        conn, user_id, event_key or f"claim:{reminder_id}:{user_id}",
        f"You claimed: {task}. I’ll follow up with you privately.",
        reminder_id=reminder_id, tag=f"alex-reminder-{reminder_id}",
        actions=[
            ("ACK", "Acknowledge", "reminder", reminder_id),
            ("DONE", "Done", "reminder", reminder_id),
            ("SNOOZE10", "Snooze 10m", "reminder", reminder_id),
        ],
    )


def queue_handoff(conn, user_id: str, reminder_id: str, handoff_id: str, task: str) -> int:
    return _queue(
        conn, user_id, f"handoff:{handoff_id}",
        f"Your spouse asked you to take over: {task}",
        reminder_id=reminder_id, handoff_id=handoff_id,
        tag=f"alex-reminder-{reminder_id}",
        actions=[
            ("HANDOFF_ACCEPT", "Accept", "handoff", handoff_id),
            ("HANDOFF_DECLINE", "Decline", "handoff", handoff_id),
        ],
    )


def queue_state(conn, user_id: str, reminder_id: str, task: str, state: str,
                event_key: str | None = None) -> int:
    label = {
        "ACK": "Acknowledged",
        "COMP": "Completed",
        "CANC": "Cancelled",
        "OPEN": "Snoozed",
        "DEFERRED": "Deferred",
    }.get(str(state or "").upper(), str(state or "").title())
    actions: list[tuple[str, str, str, str]] = []
    if str(state or "").upper() in {"ACK", "OPEN", "DEFERRED"}:
        actions = [
            ("DONE", "Done", "reminder", reminder_id),
            ("SNOOZE10", "Snooze 10m", "reminder", reminder_id),
        ]
    return _queue(
        conn, user_id, event_key or f"state:{reminder_id}:{state}:{runtime_clock.utc_iso()}",
        f"{label}: {task}",
        reminder_id=reminder_id, tag=f"alex-reminder-{reminder_id}",
        actions=actions,
    )


def queue_info(user_id: str, event_key: str, message: str,
               reminder_id: str | None = None) -> None:
    conn = db.connect()
    try:
        _queue(
            conn, user_id, event_key, message,
            reminder_id=reminder_id,
            tag=f"alex-reminder-{reminder_id}" if reminder_id else None,
        )
        conn.commit()
    finally:
        conn.close()


def sweep() -> None:
    conn = db.connect()
    try:
        now = runtime_clock.utc_iso()
        rows = conn.execute(
            """SELECT * FROM ha_notification_outbox
               WHERE delivery_status='PENDING'
                 AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc<=?)
               ORDER BY created_at_utc LIMIT 20""",
            (now,),
        ).fetchall()
        for row in rows:
            attempts = int(row["attempt_count"] or 0) + 1
            try:
                data = json.loads(row["data_json"] or "{}")
                ha.notify_mobile(
                    row["notify_service"], row["message"], row["title"], data
                )
                conn.execute(
                    """UPDATE ha_notification_outbox
                       SET delivery_status='SENT',attempt_count=?,delivered_at_utc=?,
                           last_error=NULL,next_attempt_at_utc=NULL
                       WHERE notification_id=?""",
                    (attempts, runtime_clock.utc_iso(), row["notification_id"]),
                )
            except Exception as exc:
                delay = min(300, 2 ** min(attempts, 8))
                next_try = (
                    runtime_clock.now_utc() + timedelta(seconds=delay)
                ).isoformat()
                conn.execute(
                    """UPDATE ha_notification_outbox
                       SET attempt_count=?,last_error=?,next_attempt_at_utc=?
                       WHERE notification_id=?""",
                    (attempts, str(exc)[:1000], next_try, row["notification_id"]),
                )
            conn.commit()
    finally:
        conn.close()


def _active_phone(user_id: str) -> str | None:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT phone_number FROM user_phone_history
               WHERE user_id=? AND valid_to_utc IS NULL LIMIT 1""",
            (user_id,),
        ).fetchone()
        return str(row["phone_number"]) if row else None
    finally:
        conn.close()


def _display_name(user_id: str) -> str:
    settings = get_settings()
    if user_id == "USR_HUSBAND":
        return str(settings.husband_name or "Husband")
    if user_id == "USR_WIFE":
        return str(settings.wife_name or "Wife")
    return "Your spouse"


def _process_received_event(event: dict) -> dict:
    """Record phone receipt separately from Home Assistant service acceptance."""
    payload = event.get("event") or {}
    data = payload.get("data") or {}
    notification_id = str(data.get("alex_notification_id") or "").strip()
    if not notification_id:
        return {"status": "ignored_untracked_receipt"}
    device_id = str(data.get("device_id") or "").strip()[:200] or None
    conn = db.connect()
    try:
        cur = conn.execute(
            """UPDATE ha_notification_outbox
               SET received_at_utc=COALESCE(received_at_utc,?),
                   received_device_id=COALESCE(received_device_id,?)
               WHERE notification_id=?""",
            (runtime_clock.utc_iso(), device_id, notification_id),
        )
        conn.commit()
        return {
            "status": "received" if cur.rowcount else "unknown_notification",
            "notification_id": notification_id,
        }
    finally:
        conn.close()


def _process_action_event(event: dict) -> dict:
    payload = event.get("event") or {}
    data = payload.get("data") or {}
    parsed = parse_action_token(str(data.get("action") or ""))
    if not parsed:
        return {"status": "ignored_unknown_action"}

    context_id = str((payload.get("context") or {}).get("id") or "").strip()
    if not context_id:
        return {"status": "ignored_missing_event_context"}
    message_id = "ha:" + context_id
    user_id = parsed["user_id"]
    phone = _active_phone(user_id)
    if not phone:
        return {"status": "ignored_unconfigured_user"}

    synthetic = {
        "message_id": message_id,
        "provider": "HOME_ASSISTANT",
        "conversation_id": f"ha:{user_id}",
        "conversation_type": "DIRECT_DM",
        "sender_phone": phone,
        "text": parsed["action"],
    }
    if db.claim_inbound(synthetic) == "DUPLICATE":
        return {"status": "duplicate"}

    dm_conversation = phone.replace("+", "") + "@s.whatsapp.net"
    actor = db.resolve_actor(
        phone, dm_conversation, "DIRECT_DM", message_id, []
    )

    try:
        import services

        action = parsed["action"]
        target_id = parsed["target_id"]
        target_kind = parsed["target_kind"]
        if action in {"ACK", "DONE", "SNOOZE10", "CLAIM"} and target_kind != "reminder":
            raise PermissionError("HA action target does not match reminder operation")
        if action in {"HANDOFF_ACCEPT", "HANDOFF_DECLINE"} and target_kind != "handoff":
            raise PermissionError("HA action target does not match handoff operation")

        closed = False
        if action in {"ACK", "DONE", "SNOOZE10"}:
            conn = db.connect()
            try:
                row = conn.execute(
                    "SELECT status FROM reminders WHERE reminder_id=?",
                    (target_id,),
                ).fetchone()
                closed = bool(row and row["status"] in {"COMP", "CANC"})
            finally:
                conn.close()

        if closed:
            result = {"status": "closed", "reminder_id": target_id}
        elif action == "ACK":
            result = services.update_reminder(actor, target_id, status="ack")
        elif action == "DONE":
            result = services.update_reminder(actor, target_id, status="complete")
        elif action == "SNOOZE10":
            result = services.update_reminder(
                actor, target_id, status="snooze", snooze_minutes=10
            )
        elif action == "CLAIM":
            result = services.claim_reminder(actor, target_id, source="HA_ACTION")
            if result.get("status") == "claimed":
                task = str(result.get("task") or "this reminder")
                db.queue_outbound(
                    dm_conversation, "TEXT",
                    text=(
                        f"Got it — you’ve claimed “{task}”. "
                        "I’ll follow up with you privately from here."
                    ),
                    source_message_id=message_id,
                    context_kind="REMINDER_CLAIM_CONFIRMED",
                    context_id=target_id,
                )
                conn = db.connect()
                try:
                    queue_claim_confirmation(
                        conn, user_id, target_id, task,
                        event_key=f"ha-claim:{context_id}",
                    )
                    conn.commit()
                finally:
                    conn.close()
            elif result.get("status") == "already_claimed":
                winner = str(result.get("claimed_by_user_id") or "")
                if winner and winner != user_id:
                    queue_info(
                        user_id, f"ha-claim-collision:{context_id}",
                        f"{_display_name(winner)} has already picked this one up.",
                        target_id,
                    )
        elif action == "HANDOFF_ACCEPT":
            result = services.accept_reminder_handoff(
                actor, target_id, source="HA_ACTION"
            )
        else:
            result = services.decline_reminder_handoff(actor, target_id)
        db.finish_inbound(message_id, json.dumps(result, sort_keys=True))
        return result
    except Exception as exc:
        db.fail_inbound(message_id, str(exc))
        raise


def _listen_once() -> None:
    ws = websocket.create_connection(ha.websocket_url(), timeout=65)
    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError("unexpected Home Assistant websocket greeting")
        ws.send(json.dumps({"type": "auth", "access_token": ha._token()}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise PermissionError("Home Assistant websocket authentication failed")
        ws.send(json.dumps({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "mobile_app_notification_action",
        }))
        ws.send(json.dumps({
            "id": 2,
            "type": "subscribe_events",
            "event_type": "mobile_app_notification_received",
        }))
        while True:
            raw = ws.recv()
            if not raw:
                raise ConnectionError("Home Assistant websocket closed")
            message = json.loads(raw)
            if message.get("type") == "event":
                try:
                    event_type = str((message.get("event") or {}).get("event_type") or "")
                    if event_type == "mobile_app_notification_action":
                        _process_action_event(message)
                    elif event_type == "mobile_app_notification_received":
                        _process_received_event(message)
                except Exception as exc:
                    print(f"[Alex HA mobile] event error: {exc}", flush=True)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _listener_loop() -> None:
    while True:
        try:
            _listen_once()
        except Exception as exc:
            print(f"[Alex HA mobile] websocket reconnect: {exc}", flush=True)
            time.sleep(5)


def run_forever() -> None:
    summary = configured_device_summary()
    if summary["enabled"]:
        threading.Thread(target=_listener_loop, daemon=True).start()
        services = ", ".join(x["notify_service"] for x in summary["devices"])
        print(
            f"[Alex HA mobile] actionable reminders enabled for {summary['count']} device(s): {services}",
            flush=True,
        )
    else:
        print(
            "[Alex HA mobile] actionable reminders disabled — 0 Companion notify devices configured",
            flush=True,
        )
    while True:
        try:
            sweep()
        except Exception as exc:
            print(f"[Alex HA mobile] outbox error: {exc}", flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    run_forever()
