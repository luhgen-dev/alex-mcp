"""Failure-path and concurrency regressions from the October reliability audit."""
import asyncio
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import test_core as core
import shadow_router

db, outbox, scheduler, ha_mobile = core.db, core.outbox, core.scheduler, core.ha_mobile
PHONE = "+60111111111"
DM = "60111111111@s.whatsapp.net"
NOW = datetime(2026, 10, 16, 4, tzinfo=timezone.utc)


class AuditReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.AlexCoreTests.setUpClass()

    def setUp(self):
        core.AlexCoreTests.setUp(self)

    def reminder(self, name="audit-reminder", **changes):
        values = dict(reminder_id=name, action_key=name, owner_id="USR_HUSBAND",
                      space_id="FAMILY_SHARED", conversation_id=DM,
                      task_text="Audit fixture", due_at_utc=(NOW-timedelta(minutes=1)).isoformat(),
                      timezone_name="Asia/Kuala_Lumpur", delivery_class="routine")
        values.update(changes)
        with db.connect() as conn:
            conn.execute("INSERT INTO reminders("+",".join(values)+") VALUES("
                         +",".join("?" for _ in values)+")", list(values.values()))
            return dict(conn.execute("SELECT * FROM reminders WHERE reminder_id=?", (name,)).fetchone())

    def carrier(self, kind="REMINDER_CREATED", **changes):
        core.AlexCoreTests.claim(self, "audit-source", PHONE, "fixture")
        reminder = self.reminder()
        oid = db.queue_outbound(DM, "TEXT", text="Reminder set",
                                source_message_id="audit-source", context_kind=kind,
                                context_id=reminder["reminder_id"])
        values = dict(delivery_status="SENT", provider_message_id="WA-AUDIT",
                      job_pinned_at_utc=NOW.isoformat(), job_pin_target="OUTBOUND",
                      job_reacted_at_utc=NOW.isoformat())
        values.update(changes)
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET "+",".join(k+"=?" for k in values)
                         +" WHERE outbound_id=?", list(values.values())+[oid])
        return oid

    def test_completed_reminder_unpins_even_if_reaction_cleanup_fails(self):
        oid = self.carrier()
        sent = []
        def send(payload):
            sent.append(payload)
            return payload["kind"] == "unpin", "reaction unavailable"
        with db.connect() as conn, patch.object(outbox, "_send", side_effect=send):
            outbox._cleanup_claim_setup_markers(conn, outbox._joined_row(conn, oid))
            row = outbox._joined_row(conn, oid)
        self.assertIsNotNone(row["job_unpinned_at_utc"])
        self.assertIsNone(row["job_reaction_cleared_at_utc"])
        self.assertEqual(sent[0]["kind"], "unpin")

    def test_exhausted_hourglass_retries_do_not_disable_future_unpin(self):
        oid = self.carrier(job_reacted_at_utc=None, job_control_attempts=5,
                           job_control_last_kind="reaction", job_control_failed_at_utc=NOW.isoformat())
        with db.connect() as conn, patch.object(outbox, "_send", return_value=(True, "ok")):
            outbox._cleanup_claim_setup_markers(conn, outbox._joined_row(conn, oid))
            self.assertIsNotNone(outbox._joined_row(conn, oid)["job_unpinned_at_utc"])

    def test_document_cleanup_unpins_even_if_reaction_cleanup_fails(self):
        oid = self.carrier()
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET kind='DOCUMENT',job_pin_target='SOURCE' WHERE outbound_id=?", (oid,))
        with db.connect() as conn, patch.object(outbox, "_send", side_effect=lambda p: (p["kind"] == "unpin", "unavailable")):
            outbox._cleanup_resolved_markers(conn, outbox._joined_row(conn, oid))
            self.assertIsNotNone(outbox._joined_row(conn, oid)["job_unpinned_at_utc"])

    def test_delivered_document_cleanup_retries_after_startup_sweep(self):
        oid = self.carrier()
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET kind='DOCUMENT',context_kind=NULL WHERE outbound_id=?", (oid,))
        sent = []
        def send(payload):
            sent.append(payload["kind"])
            return len(sent) > 1, "temporary bridge failure"
        with patch.object(outbox, "_STARTUP_MANAGED_JOB_RECONCILED", True), \
             patch.object(outbox, "_send", side_effect=send):
            outbox.sweep()
            with db.connect() as conn:
                row = outbox._joined_row(conn, oid)
                self.assertEqual(row["job_control_attempts"], 1)
                conn.execute("UPDATE outbound_messages SET job_control_next_attempt_at_utc=NULL WHERE outbound_id=?", (oid,))
            outbox.sweep()
        with db.connect() as conn:
            row = outbox._joined_row(conn, oid)
        self.assertEqual(sent, ["unpin", "unpin", "reaction"])
        self.assertIsNotNone(row["job_unpinned_at_utc"])
        self.assertIsNotNone(row["job_reaction_cleared_at_utc"])

    def test_failed_pin_is_bounded_even_when_reactions_work(self):
        oid = self.carrier(job_pinned_at_utc=None, job_reacted_at_utc=None)
        sent = []
        def send(payload):
            sent.append(payload)
            return payload["kind"] == "reaction", "pin unavailable"
        with db.connect() as conn, patch.object(outbox, "_send", side_effect=send):
            for _ in range(outbox.CONTROL_MAX_ATTEMPTS + 2):
                conn.execute("UPDATE outbound_messages SET job_control_next_attempt_at_utc=NULL WHERE outbound_id=?", (oid,))
                conn.commit()
                outbox._ensure_claim_setup_markers(conn, outbox._joined_row(conn, oid))
            row = outbox._joined_row(conn, oid)
        self.assertEqual(sum(p["kind"] == "pin" for p in sent), outbox.CONTROL_MAX_ATTEMPTS)
        self.assertIsNotNone(row["job_control_failed_at_utc"])

    def settings(self):
        return core.Settings(husband_phone=PHONE, ha_notify_devices=[
            {"id": "h", "owner": "husband", "notify_service": "mobile_app_h", "active": True}])

    def test_ha_notification_deduplicates_each_occurrence_not_entire_reminder(self):
        first = self.reminder(recurrence_rule="FREQ=DAILY")
        second = dict(first, due_at_utc=(NOW+timedelta(days=1)).isoformat())
        with db.connect() as conn, patch.object(ha_mobile, "get_settings", return_value=self.settings()):
            counts = [ha_mobile.queue_due(conn, row, "Reminder") for row in (first, first, second, second)]
        self.assertEqual(counts, [1, 0, 1, 0])

    def test_completed_reminder_is_not_delivered_by_recovering_ha_outbox(self):
        reminder = self.reminder()
        with db.connect() as conn, patch.object(ha_mobile, "get_settings", return_value=self.settings()):
            ha_mobile.queue_due(conn, reminder, "Reminder")
            conn.execute("UPDATE reminders SET status='COMP' WHERE reminder_id=?", (reminder["reminder_id"],))
        with patch.object(ha_mobile.ha, "notify_mobile") as notify:
            ha_mobile.sweep()
        notify.assert_not_called()
        with db.connect() as conn:
            row = conn.execute("SELECT delivery_status,last_error FROM ha_notification_outbox").fetchone()
        self.assertEqual(row["delivery_status"], "FAILED")
        self.assertEqual(row["last_error"], "cancelled_before_delivery")

    def test_presence_lookup_does_not_hold_sqlite_writer_lock(self):
        self.reminder(presence_aware=1)
        locks = []
        def presence(_phone):
            conn = db.connect()
            conn.execute("PRAGMA busy_timeout=0")
            try:
                conn.execute("BEGIN IMMEDIATE")
                locks.append(False)
            except sqlite3.OperationalError:
                locks.append(True)
            finally:
                conn.rollback()
                conn.close()
            return "home"
        with patch.object(core.runtime_clock, "now_utc", return_value=NOW), \
             patch.object(scheduler, "_presence_state", side_effect=presence):
            scheduler.fire_due()
        self.assertEqual(locks, [False])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM reminders").fetchone()[0], "DUE")

    def test_concurrent_redelivery_claims_one_inbound_once(self):
        gate = threading.Barrier(12)
        payload = dict(message_id="audit-racing", sender_phone=PHONE, conversation_id=DM, text="expense")
        def claim(_):
            gate.wait()
            try:
                return db.claim_inbound(payload)
            except Exception as exc:
                return type(exc).__name__
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(claim, range(12)))
        self.assertEqual(results.count("CLAIMED"), 1, results)
        self.assertEqual(results.count("DUPLICATE"), 11, results)

    def test_stale_inbound_redelivery_is_claimed_atomically(self):
        payload = dict(message_id="audit-stale", sender_phone=PHONE, conversation_id=DM, text="expense")
        db.claim_inbound(payload)
        with db.connect() as conn:
            conn.execute("UPDATE inbound_messages SET processing_started_at_utc='2000-01-01T00:00:00+00:00'")
        # Force two readers to see the stale lease together if there is no
        # write transaction protecting it. With serialization the second read
        # happens only after the first worker has refreshed the lease.
        gate = threading.Barrier(2)
        original_connect = db.connect
        class Cursor:
            def __init__(self, raw):
                self.raw = raw
            def fetchone(self):
                row = self.raw.fetchone()
                gate.wait(timeout=2)
                return row
        class Connection:
            def __init__(self):
                self.raw = original_connect()
            def execute(self, sql, args=()):
                cursor = self.raw.execute(sql, args)
                if "SELECT processing_state,cached_response" in sql and not self.raw.in_transaction:
                    return Cursor(cursor)
                return cursor
            def __getattr__(self, key):
                return getattr(self.raw, key)
        with patch.object(db, "connect", side_effect=Connection), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: db.claim_inbound(payload), range(2)))
        self.assertCountEqual(results, ["CLAIMED", "DUPLICATE"])

    def test_scheduler_rechecks_completion_after_presence_lookup(self):
        reminder = self.reminder(presence_aware=1)
        def presence(_phone):
            with db.connect() as conn:
                conn.execute("UPDATE reminders SET status='COMP' WHERE reminder_id=?", (reminder["reminder_id"],))
            return "home"
        with patch.object(core.runtime_clock, "now_utc", return_value=NOW), \
             patch.object(scheduler, "_presence_state", side_effect=presence):
            scheduler.fire_due()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0], 0)


class RouterDeadlineTests(unittest.TestCase):
    def test_stalled_workers_are_bounded_and_later_requests_fall_back(self):
        release = threading.Event()
        finished = [threading.Event(), threading.Event()]
        calls = []
        def slow():
            index = len(calls)
            calls.append(index)
            release.wait(2)
            finished[index].set()
            return "late"
        async def route():
            try:
                await shadow_router._bounded_live_call(slow)
            except asyncio.TimeoutError:
                return "fallback"
        try:
            with patch.object(shadow_router, "_LIVE_WORKER_SLOTS", threading.BoundedSemaphore(2)), \
                 patch.object(shadow_router, "LIVE_TIMEOUT_SECONDS", .02):
                self.assertEqual([asyncio.run(route()) for _ in range(3)], ["fallback"] * 3)
                self.assertEqual(len(calls), 2)
        finally:
            release.set()
            for event in finished:
                event.wait(2)

    def test_worker_returns_result_and_propagates_error_without_losing_capacity(self):
        def fail():
            raise ValueError("invalid response")
        with patch.object(shadow_router, "_LIVE_WORKER_SLOTS", threading.BoundedSemaphore(1)):
            self.assertEqual(asyncio.run(shadow_router._bounded_live_call(lambda: 42)), 42)
            with self.assertRaisesRegex(ValueError, "invalid response"):
                asyncio.run(shadow_router._bounded_live_call(fail))
            self.assertEqual(asyncio.run(shadow_router._bounded_live_call(lambda: 43)), 43)

    def test_timeout_returns_before_blocked_worker_even_when_event_loop_closes(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def slow(**_kwargs):
            started.set()
            release.wait(1.0)
            finished.set()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"tools":[]}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=slow)))
        async def catalogue():
            return [{"name": "list_reminders", "summary": "List"}]
        try:
            with patch.object(shadow_router, "acatalogue", new=catalogue), \
                 patch.object(shadow_router, "LIVE_TIMEOUT_SECONDS", .02), \
                 patch.object(shadow_router.chatgpt_plan, "resolved_model", return_value="test"):
                result = asyncio.run(shadow_router.live_route("list reminders", [], plan_client=client))
                self.assertTrue(started.is_set())
                self.assertIsNone(result)
                self.assertFalse(finished.is_set(), "loop shutdown waited for timed-out model call")
        finally:
            release.set()
            finished.wait(2)


if __name__ == "__main__":
    unittest.main()
