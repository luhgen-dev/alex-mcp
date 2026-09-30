from __future__ import annotations

import time
import uuid
import json
import os
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from db import connect, utc_now
import runtime_clock
import ha
import phase2_presence
import phase2_monitor


def _owner_phone(conn, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT phone_number FROM user_phone_history WHERE user_id=? AND valid_to_utc IS NULL LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["phone_number"] if row else None


def _presence_state(phone: str) -> str:
    try:
        mapping = phase2_presence.owner_presence_mapping(phone, "DIRECT_DM")
    except Exception:
        mapping = None
    if not mapping:
        return "unknown"
    for key in ("person_entity", "phone_tracker_entity"):
        entity_id = mapping.get(key)
        if not entity_id:
            continue
        try:
            state = str(ha.get_state(entity_id).get("state") or "unknown").lower()
            if state:
                return state
        except Exception:
            continue
    return "unknown"


def _delivery_decision(conn, row, now: datetime) -> dict:
    if row["delivery_class"] == "time_critical":
        return {"decision": "DELIVER", "reason": "TIME_CRITICAL"}
    phone = _owner_phone(conn, row["owner_id"])
    if not phone:
        return {"decision": "DELIVER", "reason": "NO_OWNER_PHONE_POLICY"}
    try:
        pref = dict(phase2_presence.owner_preference(phone, "DIRECT_DM"))
    except Exception:
        pref = {
            "quiet_start": None, "quiet_end": None,
            "presence_aware": False, "follow_up_after_hours": 24,
        }
    if row["presence_aware"]:
        pref["presence_aware"] = True
    presence = _presence_state(phone) if pref.get("presence_aware") else "unknown"
    try:
        local_now = now.astimezone(ZoneInfo(row["timezone_name"]))
    except Exception:
        local_now = now
    return phase2_presence.delivery_decision(
        pref, presence, local_now, row["delivery_class"] or "routine"
    )


def _queue_to(conn, conversation_id: str, reminder_id: str,
              text: str, kind: str) -> None:
    conn.execute(
        """INSERT INTO outbound_messages(
            outbound_id,conversation_id,kind,text_body,context_kind,context_id
           ) VALUES(?,?, 'TEXT', ?,?,?)""",
        (str(uuid.uuid4()), conversation_id, text, kind, reminder_id),
    )


def _queue(conn, row, text: str, kind: str) -> None:
    _queue_to(conn, row["conversation_id"], row["reminder_id"], text, kind)


def _event(conn, reminder_id: str, event_type: str, previous_state: str | None,
           new_state: str | None, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO reminder_events(
            event_id,reminder_id,event_type,previous_state,new_state,note
           ) VALUES(?,?,?,?,?,?)""",
        (str(uuid.uuid4()), reminder_id, event_type, previous_state, new_state, note),
    )


def fire_due():
    now = runtime_clock.now_utc()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status IN ('OPEN','DEFERRED')
                 AND due_at_utc<=?
                 AND (next_delivery_at_utc IS NULL OR next_delivery_at_utc<=?)
               ORDER BY due_at_utc LIMIT 40""",
            (now.isoformat(), now.isoformat()),
        ).fetchall()

        for row in rows:
            decision = _delivery_decision(conn, row, now)
            if decision.get("decision") != "DELIVER":
                next_try = (now + timedelta(minutes=15)).isoformat()
                conn.execute(
                    """UPDATE reminders SET next_delivery_at_utc=?,defer_reason=?
                       WHERE reminder_id=?""",
                    (next_try, decision.get("reason"), row["reminder_id"]),
                )
                continue

            _queue(conn, row, f"⏰ Reminder: {row['task_text']}", "REMINDER_INITIAL")
            previous_state = row["status"]
            recurrence = row["recurrence_rule"]
            if recurrence:
                try:
                    start = datetime.fromisoformat(row["due_at_utc"])
                    rule = rrulestr(recurrence, dtstart=start)
                    next_dt = rule.after(now, inc=False)
                except Exception:
                    next_dt = None
                if next_dt:
                    conn.execute(
                        """UPDATE reminders SET due_at_utc=?,status='OPEN',
                           next_delivery_at_utc=NULL,defer_reason=NULL
                           WHERE reminder_id=?""",
                        (next_dt.astimezone(timezone.utc).isoformat(), row["reminder_id"]),
                    )
                    _event(conn, row["reminder_id"], "DUE", previous_state, "OPEN",
                           "recurring occurrence queued")
                else:
                    conn.execute(
                        """UPDATE reminders SET status='DUE',next_delivery_at_utc=NULL,
                           defer_reason=NULL WHERE reminder_id=?""",
                        (row["reminder_id"],),
                    )
                    _event(conn, row["reminder_id"], "DUE", previous_state, "DUE")
            else:
                next_claimable = None
                if int(row["claimable"] or 0):
                    next_claimable = (
                        now + timedelta(hours=max(1, int(row["follow_up_after_hours"] or 24)))
                    ).isoformat()
                conn.execute(
                    """UPDATE reminders SET status='DUE',next_delivery_at_utc=?,
                       defer_reason=NULL WHERE reminder_id=?""",
                    (next_claimable, row["reminder_id"]),
                )
                _event(conn, row["reminder_id"], "DUE", previous_state, "DUE")

        # Claimable family reminder escalation is deterministic and model-free.
        # 1) If nobody claims the initial group reminder, send one gentle group
        #    follow-up after the configured delay.
        unclaimed_rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status='DUE' AND claimable=1 AND claimed_by_user_id IS NULL
                 AND next_delivery_at_utc IS NOT NULL
                 AND next_delivery_at_utc<=?
                 AND last_follow_up_at_utc IS NULL
               ORDER BY next_delivery_at_utc LIMIT 40""",
            (now.isoformat(),),
        ).fetchall()
        for row in unclaimed_rows:
            _queue(
                conn, row,
                f"↪️ Still unclaimed: {row['task_text']}",
                "REMINDER_FOLLOWUP",
            )
            conn.execute(
                """UPDATE reminders
                   SET last_follow_up_at_utc=?,next_delivery_at_utc=NULL
                   WHERE reminder_id=?""",
                (now.isoformat(), row["reminder_id"]),
            )

        # 2) Once claimed, follow up privately with the claimant first.
        claimant_rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status='DUE' AND claimable=1
                 AND claimed_by_user_id IS NOT NULL
                 AND next_delivery_at_utc IS NOT NULL
                 AND next_delivery_at_utc<=?
                 AND claimant_follow_up_at_utc IS NULL
               ORDER BY next_delivery_at_utc LIMIT 40""",
            (now.isoformat(),),
        ).fetchall()
        for row in claimant_rows:
            claimant_phone = _owner_phone(conn, row["claimed_by_user_id"])
            if not claimant_phone:
                continue
            claimant_conversation = claimant_phone.replace("+", "") + "@s.whatsapp.net"
            decision_row = dict(row)
            decision_row["owner_id"] = row["claimed_by_user_id"]
            decision = _delivery_decision(conn, decision_row, now)
            if decision.get("decision") != "DELIVER":
                conn.execute(
                    """UPDATE reminders SET next_delivery_at_utc=?,defer_reason=?
                       WHERE reminder_id=?""",
                    (
                        (now + timedelta(minutes=15)).isoformat(),
                        decision.get("reason"), row["reminder_id"],
                    ),
                )
                continue
            _queue_to(
                conn, claimant_conversation, row["reminder_id"],
                f"↪️ You claimed this and it is still outstanding: {row['task_text']}",
                "REMINDER_CLAIMANT_FOLLOWUP",
            )
            later = (
                now + timedelta(hours=max(1, int(row["follow_up_after_hours"] or 24)))
            ).isoformat()
            conn.execute(
                """UPDATE reminders
                   SET claimant_follow_up_at_utc=?,next_delivery_at_utc=?,defer_reason=NULL
                   WHERE reminder_id=?""",
                (now.isoformat(), later, row["reminder_id"]),
            )

        # 3) If it is still not completed after the claimant follow-up, resurface
        #    it to Family Shared once. Ownership remains with the claimant.
        resurface_rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status='DUE' AND claimable=1
                 AND claimed_by_user_id IS NOT NULL
                 AND claimant_follow_up_at_utc IS NOT NULL
                 AND family_resurfaced_at_utc IS NULL
                 AND next_delivery_at_utc IS NOT NULL
                 AND next_delivery_at_utc<=?
               ORDER BY next_delivery_at_utc LIMIT 40""",
            (now.isoformat(),),
        ).fetchall()
        for row in resurface_rows:
            _queue(
                conn, row,
                f"↪️ Still outstanding with its claimant: {row['task_text']}",
                "REMINDER_FAMILY_RESURFACE",
            )
            conn.execute(
                """UPDATE reminders
                   SET family_resurfaced_at_utc=?,next_delivery_at_utc=NULL
                   WHERE reminder_id=?""",
                (now.isoformat(), row["reminder_id"]),
            )

        # An acknowledged reminder may receive one quiet/presence-aware follow-up
        # after the configured delay. Completion/cancellation stops it.
        ack_rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status='ACK' AND acknowledged_at_utc IS NOT NULL
                 AND follow_up_after_hours>0
                 AND (last_follow_up_at_utc IS NULL OR last_follow_up_at_utc<acknowledged_at_utc)
               ORDER BY acknowledged_at_utc LIMIT 40"""
        ).fetchall()
        for row in ack_rows:
            try:
                ack = datetime.fromisoformat(row["acknowledged_at_utc"])
                if ack.tzinfo is None:
                    ack = ack.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if now < ack + timedelta(hours=int(row["follow_up_after_hours"])):
                continue
            if row["next_delivery_at_utc"]:
                try:
                    if now < datetime.fromisoformat(row["next_delivery_at_utc"]):
                        continue
                except Exception:
                    pass
            decision = _delivery_decision(conn, row, now)
            if decision.get("decision") != "DELIVER":
                conn.execute(
                    "UPDATE reminders SET next_delivery_at_utc=?,defer_reason=? WHERE reminder_id=?",
                    ((now + timedelta(minutes=15)).isoformat(), decision.get("reason"), row["reminder_id"]),
                )
                continue
            _queue(conn, row, f"↪️ Follow-up: {row['task_text']}", "REMINDER_FOLLOWUP")
            # Mark queued to prevent duplicate queueing. Outbox replaces this with
            # the actual delivered timestamp after transport success.
            conn.execute(
                "UPDATE reminders SET last_follow_up_at_utc=?,next_delivery_at_utc=NULL,defer_reason=NULL WHERE reminder_id=?",
                (now.isoformat(), row["reminder_id"]),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



_last_monitor_hour = None


def _family_group_jid() -> str | None:
    path = os.path.join(os.environ.get("ALEX_DATA_DIR", "/data"), "family_group.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("group_jid")
    except Exception:
        return None


def _conversation_for_space(conn, owner_id: str, space_id: str) -> str | None:
    if space_id == "FAMILY_SHARED":
        return _family_group_jid()
    phone = _owner_phone(conn, owner_id)
    return phone.replace("+", "") + "@s.whatsapp.net" if phone else None


def _candidate_text(candidate: dict) -> str:
    kind = candidate.get("kind")
    if kind == "BILL_FOLLOW_UP":
        return (
            f"📌 Alex check-in: {candidate.get('name')} is {candidate.get('state')}"
            + (f", due {candidate.get('due_date')}." if candidate.get("due_date") else ".")
        )
    if kind == "GOAL_BELOW_PLAN":
        return (
            f"📌 Alex check-in: {candidate.get('name')} is below the {candidate.get('period')} "
            f"plan by {candidate.get('remaining_for_period'):.2f}. I haven't changed the plan."
        )
    if kind == "OT_ALLOCATION_AVAILABLE":
        return (
            f"📌 Alex check-in: {candidate.get('currency')} {candidate.get('unallocated'):.2f} "
            f"from OT is still unallocated for {candidate.get('goal_name')}. "
            "I haven't moved it anywhere."
        )
    if kind == "HOME_STATE_MATCH":
        return (
            f"📌 {candidate.get('friendly_name') or candidate.get('entity_id')} "
            f"is now {candidate.get('state')}."
        )
    return "📌 Alex has an update from a monitor you explicitly enabled."


def _candidate_key(candidate: dict) -> str:
    stable = {
        k: candidate.get(k) for k in sorted(candidate)
        if k not in {"created_at_utc", "updated_at_utc"}
    }
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_delegated_monitors():
    global _last_monitor_hour
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%dT%H")
    run_hourly_monitors = _last_monitor_hour != hour_key
    if run_hourly_monitors:
        _last_monitor_hour = hour_key

    conn = connect()
    try:
        users = conn.execute(
            """SELECT u.user_id,p.phone_number FROM users u
               JOIN user_phone_history p ON p.user_id=u.user_id
               WHERE p.valid_to_utc IS NULL"""
        ).fetchall()
        for user in users:
            phone = user["phone_number"]
            try:
                candidates = []
                # Slow-changing finance/planning monitors stay hourly. Home
                # Assistant one-shot state monitors are checked every scheduler
                # loop (15s) so short transitions are not missed.
                if run_hourly_monitors:
                    candidates.extend(phase2_monitor.bill_candidates(now.date().isoformat(), phone, "DIRECT_DM"))
                    candidates.extend(phase2_monitor.goal_candidates(now.strftime("%Y-%m"), phone, "DIRECT_DM"))
                    candidates.extend(phase2_monitor.ot_allocation_candidates(phone, "DIRECT_DM"))
                candidates.extend(phase2_monitor.home_state_candidates(phone, "DIRECT_DM"))
            except Exception as exc:
                print(f"[Alex MCP monitor] candidate error for {user['user_id']}: {exc}", flush=True)
                continue

            for candidate in candidates:
                key = _candidate_key(candidate)
                seen = conn.execute(
                    "SELECT 1 FROM monitor_notifications WHERE candidate_key=?",
                    (key,),
                ).fetchone()
                if seen:
                    continue
                conversation = _conversation_for_space(
                    conn, user["user_id"], candidate.get("space") or "HUSBAND_PVT"
                )
                if not conversation:
                    continue
                conn.execute(
                    """INSERT INTO outbound_messages(
                        outbound_id,conversation_id,kind,text_body,context_kind,context_id
                       ) VALUES(?,?, 'TEXT', ?, 'MONITOR', ?)""",
                    (str(uuid.uuid4()), conversation, _candidate_text(candidate), key),
                )
                conn.execute(
                    """INSERT INTO monitor_notifications(
                        candidate_key,delegation_id,owner_id,candidate_kind,payload_json
                       ) VALUES(?,?,?,?,?)""",
                    (key, candidate.get("delegation_id") or "", user["user_id"],
                     candidate.get("kind") or "UNKNOWN",
                     json.dumps(candidate, ensure_ascii=False, sort_keys=True)),
                )
                if candidate.get("one_shot") and candidate.get("delegation_id"):
                    conn.execute(
                        """UPDATE alex_phase2_delegations
                           SET status='COMPLETED',updated_at_utc=CURRENT_TIMESTAMP
                           WHERE delegation_id=? AND status='ACTIVE'""",
                        (candidate["delegation_id"],),
                    )
        conn.commit()
    finally:
        conn.close()


def main():
    while True:
        try:
            fire_due()
            run_delegated_monitors()
        except Exception as exc:
            print(f"[Alex MCP scheduler] {exc}", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
