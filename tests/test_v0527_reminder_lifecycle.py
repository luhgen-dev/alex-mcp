"""v0.5.27 regressions built from the live v0.5.26 WhatsApp failures.

These tests drive the real ingress -> router -> brain loop -> MCP tools ->
services -> outbox path. Only the provider's replies are scripted, and the
scripted model arguments are deliberately the *imperfect* ones a real model
produces (natural-language due times, default recipient/destination), so the
deterministic layer itself has to get the result right.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_core as core


db = core.db
brain = core.brain
ingress = core.ingress
services = core.services
outbox = core.outbox
runtime_clock = core.runtime_clock
with_action_key = core.with_action_key

DM = "60111111111@s.whatsapp.net"
GROUP = "120363527001@g.us"
FIXED = datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)  # 6 Oct 03:00 MYT


class V0527ReminderLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.AlexCoreTests.setUpClass()

    def setUp(self):
        core.AlexCoreTests.setUp(self)
        with open(os.path.join(core.TEST_DIR, "family_group.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % GROUP)

    def tearDown(self):
        core.AlexCoreTests.setUp(self)

    # -- helpers ---------------------------------------------------------
    def run_turn(self, payload, steps, exposed=None):
        return core.AlexCoreTests._v0513_process_with_scripted_provider(
            self, payload, steps, exposed
        )

    @staticmethod
    def dm(mid, text, quoted=None):
        payload = {
            "message_id": mid, "provider": "WHATSAPP", "conversation_id": DM,
            "conversation_type": "DIRECT_DM", "sender_phone": "+60111111111",
            "text": text,
        }
        if quoted:
            payload["quoted_message_id"] = quoted
        return payload

    @staticmethod
    def group(mid, text, quoted=None, mentioned=True, reply_to_alex=False):
        payload = {
            "message_id": mid, "provider": "WHATSAPP", "conversation_id": GROUP,
            "conversation_type": "GROUP", "sender_phone": "+60111111111",
            "text": text, "alex_mentioned": mentioned,
            "reply_to_alex": reply_to_alex,
        }
        if quoted:
            payload["quoted_message_id"] = quoted
        return payload

    @staticmethod
    def mark_sent(source_message_id, provider_id):
        conn = db.connect()
        try:
            row = conn.execute(
                """SELECT outbound_id FROM outbound_messages
                   WHERE source_message_id=? ORDER BY rowid DESC LIMIT 1""",
                (source_message_id,),
            ).fetchone()
            conn.execute(
                """UPDATE outbound_messages
                   SET delivery_status='SENT',provider_message_id=?
                   WHERE outbound_id=?""",
                (provider_id, row["outbound_id"]),
            )
            conn.commit()
            return row["outbound_id"]
        finally:
            conn.close()

    @staticmethod
    def rows(sql, args=()):
        conn = db.connect()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def sweep_controls(self):
        """Run one outbox sweep; return the WhatsApp control payloads sent."""
        sent = []

        def fake_send(payload):
            sent.append(dict(payload))
            if payload.get("kind") == "text":
                return True, '{"message_id":"wa-%d"}' % len(sent)
            return True, "{}"

        outbox._STARTUP_MANAGED_JOB_RECONCILED = True
        with patch.object(outbox, "_send", side_effect=fake_send):
            outbox.sweep()
        return sent

    # -- A/G: natural relative answers ------------------------------------
    def test_relative_parser_accepts_live_shorthand_and_keeps_clock_safety(self):
        cases = {
            "In about 10 mins": 10, "in abt 40 mins": 40, "in 40 minits": 40,
            "In about 40 minutes": 40, "in half an hour": 30,
            "2 hours from now": 120, "10 min": 10, "in an hour": 60,
        }
        for text, minutes in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    services.relative_reminder_delta(text),
                    timedelta(minutes=minutes),
                )
        for text in ("bake for 40 mins at 7pm", "7pm", "tmr ard 7ish",
                     "in 3 months"):
            with self.subTest(text=text):
                self.assertIsNone(services.relative_reminder_delta(text))

    def test_realistic_gateway_paraphrases_pass_deterministic_validation(self):
        for semantic in ("in about 10 minutes", "about 10 minutes from now",
                         "in approximately 10 minutes", "in 10 minutes"):
            with self.subTest(semantic=semantic):
                actor = type("A", (), {
                    "reminder_context_text": "Remind me to check the mailbox",
                    "trusted_text": "in abt ten-ish",
                    "reminder_semantic_text": semantic,
                    "timezone": "Asia/Kuala_Lumpur",
                })()
                with patch.object(runtime_clock, "now_utc", return_value=FIXED):
                    due = services._deterministic_reminder_due_from_text(actor)
                self.assertEqual(
                    datetime.fromisoformat(due), FIXED + timedelta(minutes=10)
                )

    def _clarify_then_answer(self, first, answer_builder, clarification_id):
        with patch.object(runtime_clock, "now_utc", return_value=FIXED):
            self.assertTrue(self.run_turn(
                first, [{"content": "What time should I remind you?"}]
            )["ok"])
        self.mark_sent(first["message_id"], clarification_id)
        with patch.object(runtime_clock, "now_utc", return_value=FIXED), \
             patch.object(brain, "interpret_control_intent",
                          side_effect=AssertionError("must stay zero-token")):
            self.assertTrue(self.run_turn(answer_builder(), [
                {"tool": "create_reminder", "args": {
                    # What a real model typically sends: natural wording and
                    # MCP defaults. The backend must still get it right.
                    "task": "check v0526 mailbox",
                    "due_local": "in about 10 minutes",
                }},
                {"content": "OK, I've set that reminder."},
            ])["ok"])

    def test_live_dm_swipe_in_about_10_mins_creates_without_repeat_question(self):
        self._clarify_then_answer(
            self.dm("v527-dm-1", "Remind me to check v0526 mailbox"),
            lambda: self.dm("v527-dm-2", "In about 10 mins",
                            quoted="wa-v527-dm-q1"),
            "wa-v527-dm-q1",
        )
        reminder = self.rows(
            "SELECT * FROM reminders WHERE task_text LIKE '%mailbox%'"
        )
        self.assertEqual(len(reminder), 1)
        self.assertEqual(
            datetime.fromisoformat(reminder[0]["due_at_utc"]),
            FIXED + timedelta(minutes=10),
        )
        self.assertEqual(reminder[0]["conversation_id"], DM)
        drafts = self.rows(
            "SELECT status FROM pending_items WHERE kind='REMINDER_DRAFT'"
        )
        self.assertEqual([d["status"] for d in drafts], ["RESOLVED"])
        confirmation = self.rows(
            """SELECT context_kind FROM outbound_messages
               WHERE source_message_id='v527-dm-2'"""
        )
        self.assertEqual(confirmation[0]["context_kind"], "REMINDER_CREATED")

    def test_live_group_swipe_in_about_10_mins_creates_family_claimable(self):
        self._clarify_then_answer(
            self.group("v527-g-1", "Remind me to check v0526 mailbox"),
            lambda: self.group("v527-g-2", "In about 10 mins",
                               quoted="wa-v527-g-q1", mentioned=False,
                               reply_to_alex=True),
            "wa-v527-g-q1",
        )
        reminder = self.rows(
            "SELECT * FROM reminders WHERE task_text LIKE '%mailbox%'"
        )
        self.assertEqual(len(reminder), 1)
        self.assertEqual(reminder[0]["conversation_id"], GROUP)
        self.assertEqual(reminder[0]["claimable"], 1)
        self.assertEqual(
            datetime.fromisoformat(reminder[0]["due_at_utc"]),
            FIXED + timedelta(minutes=10),
        )
        assigned = self.rows(
            "SELECT 1 FROM outbound_messages WHERE context_kind='REMINDER_ASSIGNED'"
        )
        self.assertEqual(assigned, [])

    # -- B: group channel default ----------------------------------------
    def test_group_remind_me_with_time_stays_family_claimable(self):
        with patch.object(runtime_clock, "now_utc", return_value=FIXED):
            self.assertTrue(self.run_turn(
                self.group("v527-b-1",
                           "Remind me to check v0526 claim flow in 15 minutes"),
                [
                    {"tool": "create_reminder", "args": {
                        "task": "check v0526 claim flow",
                        "due_local": "in 15 minutes",
                        "recipient": "me", "destination": "dm",
                    }},
                    {"content": "OK. I've set it."},
                ],
            )["ok"])
        reminder = self.rows("SELECT * FROM reminders")
        self.assertEqual(len(reminder), 1)
        self.assertEqual(reminder[0]["conversation_id"], GROUP)
        self.assertEqual(reminder[0]["claimable"], 1)
        self.assertEqual(reminder[0]["claimed_by_user_id"], None)
        self.assertEqual(
            self.rows("""SELECT 1 FROM outbound_messages
                         WHERE context_kind='REMINDER_ASSIGNED'"""),
            [],
        )
        setup = self.rows(
            """SELECT conversation_id FROM outbound_messages
               WHERE context_kind='REMINDER_SETUP'"""
        )
        self.assertEqual([s["conversation_id"] for s in setup], [GROUP])

    def test_group_named_assignee_and_explicit_private_still_route_to_dm(self):
        with patch.object(runtime_clock, "now_utc", return_value=FIXED):
            self.assertTrue(self.run_turn(
                self.group("v527-b-2",
                           "Remind me privately to call the bank in 15 minutes"),
                [
                    {"tool": "create_reminder", "args": {
                        "task": "call the bank",
                        "due_local": "in 15 minutes",
                    }},
                    {"content": "Done."},
                ],
            )["ok"])
        rows = self.rows("SELECT conversation_id,claimable FROM reminders")
        self.assertEqual(rows, [{"conversation_id": DM, "claimable": 0}])

    # -- D/H/I/K: unresolved markers -----------------------------------
    def test_personal_reminder_marked_from_creation_through_due_until_done(self):
        with patch.object(runtime_clock, "now_utc", return_value=FIXED):
            self.assertTrue(self.run_turn(
                self.dm("v527-m-1",
                        "Remind me to test v0526 direct DM pin in 20 minutes"),
                [
                    {"tool": "create_reminder", "args": {
                        "task": "test v0526 direct DM pin",
                        "due_local": "in 20 minutes",
                    }},
                    {"content": "OK. I've set it for 3:20 AM."},
                ],
            )["ok"])
        reminder_id = self.rows("SELECT reminder_id FROM reminders")[0]["reminder_id"]

        sent = self.sweep_controls()
        kinds = [(p["kind"], p.get("emoji")) for p in sent]
        self.assertEqual(kinds[0][0], "text")
        self.assertIn(("reaction", "⏳"), kinds)
        self.assertIn(("pin", None), kinds)

        # Due: the fired reminder takes over the markers.
        conn = db.connect()
        try:
            conn.execute("UPDATE reminders SET status='DUE' WHERE reminder_id=?",
                         (reminder_id,))
            conn.commit()
        finally:
            conn.close()
        db.queue_outbound(DM, "TEXT", text="⏰ Reminder: test v0526 direct DM pin",
                          context_kind="REMINDER_INITIAL", context_id=reminder_id)
        sent = self.sweep_controls()
        fired_markers = [(p["kind"], p.get("emoji")) for p in sent]
        self.assertIn(("reaction", "⏳"), fired_markers)
        self.assertIn(("reaction", ""), fired_markers)  # creation message cleared
        self.assertIn(("unpin", None), fired_markers)
        active = self.rows(
            """SELECT context_kind FROM outbound_messages
               WHERE context_id=? AND job_reacted_at_utc IS NOT NULL
                 AND job_reaction_cleared_at_utc IS NULL""",
            (reminder_id,),
        )
        self.assertEqual([a["context_kind"] for a in active], ["REMINDER_INITIAL"])

        # 👍 = seen only: markers stay.
        actor = with_action_key(
            core.AlexCoreTests.actor(self, "v527-m-1", "+60111111111"), "seen"
        )
        services.update_reminder(actor, reminder_id, "ack")
        self.assertEqual(
            [(p["kind"], p.get("emoji")) for p in self.sweep_controls()], []
        )

        # ✅ = complete: ⏳ and pin both clear.
        services.update_reminder(
            with_action_key(actor, "done"), reminder_id, "complete"
        )
        cleared = [(p["kind"], p.get("emoji")) for p in self.sweep_controls()]
        self.assertEqual(sorted(cleared), [("reaction", ""), ("unpin", None)])

    def test_claimed_reminder_confirmation_in_dm_gets_markers(self):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO reminders(reminder_id,action_key,owner_id,space_id,
                       conversation_id,task_text,due_at_utc,timezone_name,claimable,
                       claimed_by_user_id,claimed_at_utc)
                   VALUES('rem-claimed','ak-claimed','USR_HUSBAND','FAMILY_SHARED',?,
                          'check v0526 claim handover',?,'Asia/Kuala_Lumpur',1,
                          'USR_HUSBAND',?)""",
                (GROUP, (FIXED + timedelta(hours=1)).isoformat(), FIXED.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        db.queue_outbound(
            DM, "TEXT",
            text="Got it — you’ve claimed “check v0526 claim handover”.",
            context_kind="REMINDER_CLAIM_CONFIRMED", context_id="rem-claimed",
        )
        markers = [(p["kind"], p.get("emoji")) for p in self.sweep_controls()]
        self.assertIn(("reaction", "⏳"), markers)
        self.assertIn(("pin", None), markers)

    # -- J/M/N/O: reference safety and listing ---------------------------
    def _seed(self, rid, task, minutes, status="OPEN"):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO reminders(reminder_id,action_key,owner_id,space_id,
                       conversation_id,task_text,due_at_utc,timezone_name,status)
                   VALUES(?,?,'USR_HUSBAND','FAMILY_SHARED',?,?,?,
                          'Asia/Kuala_Lumpur',?)""",
                (rid, "ak-" + rid, DM, task,
                 (FIXED + timedelta(minutes=minutes)).isoformat(), status),
            )
            conn.commit()
        finally:
            conn.close()

    def test_single_shared_token_never_selects_a_reminder_to_mutate(self):
        self._seed("claim-flow", "Check v0526 claim flow", -30, "DUE")
        core.AlexCoreTests.claim(self, "v527-ref-1", "+60111111111",
                                 "Mark v0526 direct DM pin as done")
        actor = with_action_key(
            core.AlexCoreTests.actor(self, "v527-ref-1", "+60111111111"), "ref-1"
        )
        with self.assertRaisesRegex(ValueError, "REMINDER_REFERENCE_NOT_FOUND"):
            services.update_reminder(
                actor, None, "complete",
                reminder_reference="v0526 direct DM pin",
            )
        self.assertEqual(
            self.rows("SELECT status FROM reminders WHERE reminder_id='claim-flow'"),
            [{"status": "DUE"}],
        )
        self._seed("dm-pin", "test v0526 direct DM pin", 20)
        services.update_reminder(
            with_action_key(actor, "ref-2"), None, "complete",
            reminder_reference="v0526 direct DM pin",
        )
        statuses = {
            r["reminder_id"]: r["status"]
            for r in self.rows("SELECT reminder_id,status FROM reminders")
        }
        self.assertEqual(statuses, {"claim-flow": "DUE", "dm-pin": "COMP"})

    def test_pronoun_completion_never_targets_a_closed_reminder(self):
        self._seed("closed-one", "old thing", -90, "COMP")
        self._seed("open-a", "water plants", 10)
        self._seed("open-b", "check gate", 20)
        core.AlexCoreTests.claim(self, "v527-it", "+60111111111", "mark it done")
        actor = with_action_key(
            core.AlexCoreTests.actor(self, "v527-it", "+60111111111"), "it"
        )
        with self.assertRaisesRegex(ValueError, "REMINDER_REFERENCE_AMBIGUOUS"):
            services.update_reminder(actor, None, "complete",
                                     reminder_reference="it")

    def test_mark_named_reminder_done_routes_to_reminders_not_shopping(self):
        self._seed("dm-pin", "test v0526 direct DM pin", 20)
        exposed = []
        with patch.object(runtime_clock, "now_utc", return_value=FIXED):
            self.assertTrue(self.run_turn(
                self.dm("v527-j-1", "Mark v0526 direct DM pin as done"),
                [
                    {"tool": "update_reminder", "args": {
                        "reminder_reference": "v0526 direct DM pin",
                        "status": "complete",
                    }},
                    {"content": "Marked it done."},
                ],
                exposed,
            )["ok"])
        self.assertNotIn("update_shopping_item", exposed[0])
        self.assertEqual(
            self.rows("SELECT status FROM reminders WHERE reminder_id='dm-pin'"),
            [{"status": "COMP"}],
        )

    def test_listing_puts_named_reminder_first_and_reports_truncation(self):
        for i in range(25):
            self._seed(f"old-{i}", f"old test reminder {i}", -600 + i, "DUE")
        self._seed("dm-pin", "test v0526 direct DM pin", 20)
        core.AlexCoreTests.claim(self, "v527-list", "+60111111111",
                                 "Show me the active reminder v0526 direct DM pin")
        actor = core.AlexCoreTests.actor(self, "v527-list", "+60111111111")
        from dataclasses import replace
        actor = replace(
            actor, trusted_text="Show me the active reminder v0526 direct DM pin"
        )
        listing = services.list_reminders(actor, False, 20)
        self.assertEqual(listing["reminders"][0]["reminder_id"], "dm-pin")
        self.assertTrue(listing["reminders"][0]["named_in_request"])
        self.assertEqual(listing["total"], 26)
        self.assertTrue(listing["truncated"])

    def test_last_completed_ordering_uses_completion_time(self):
        self._seed("first-done", "check the mailbox", -60)
        self._seed("second-done", "check v0526 claim flow", -120)
        core.AlexCoreTests.claim(self, "v527-n", "+60111111111", "")
        actor = with_action_key(
            core.AlexCoreTests.actor(self, "v527-n", "+60111111111"), "n-1"
        )
        services.update_reminder(actor, "second-done", "complete")
        services.update_reminder(
            with_action_key(actor, "n-2"), "first-done", "complete"
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE reminder_events SET created_at_utc='2026-10-05 18:00:00'
                   WHERE reminder_id='second-done'"""
            )
            conn.execute(
                """UPDATE reminder_events SET created_at_utc='2026-10-05 18:30:00'
                   WHERE reminder_id='first-done'"""
            )
            conn.commit()
        finally:
            conn.close()
        listing = services.list_reminders(actor, True, 20)
        closed = [r for r in listing["reminders"] if r["status"] == "COMP"]
        self.assertEqual(
            [r["reminder_id"] for r in closed], ["first-done", "second-done"]
        )


if __name__ == "__main__":
    unittest.main()
