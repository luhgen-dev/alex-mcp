"""A rejected unbound time answer must not replace its grounded reminder task.

The provider and continuation interpreter are scripted; ingress, brain, MCP,
time validation, pending storage and outbound bindings run normally offline.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_conversation_reliability import ConversationFixture, brain, core, db


class PendingReminderRetentionTests(ConversationFixture):
    def setUp(self):
        # Ingress's existing SQL age gates use SQLite's wall clock. Keep the
        # Python clock and payload receipt aligned with it for this fixture.
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        clock = patch.dict("os.environ", {"ALEX_CERT_NOW": self.now.isoformat()})
        clock.start()
        self.addCleanup(clock.stop)
        super().setUp()

    def records(self):
        conn = db.connect()
        try:
            return {
                "pending": [dict(row) for row in conn.execute("SELECT * FROM pending_items")],
                "reminders": [dict(row) for row in conn.execute("SELECT * FROM reminders")],
                "outbound": [dict(row) for row in conn.execute("SELECT * FROM outbound_messages")],
            }
        finally:
            conn.close()

    def run_creation(self, message_id, text, task="water the plants", minutes=40):
        due = (self.now + timedelta(minutes=minutes)).astimezone(
            ZoneInfo("Asia/Kuala_Lumpur")
        ).replace(tzinfo=None).isoformat()
        with patch.object(brain, "interpret_control_intent", return_value={
            "intent": "UNCLEAR", "confidence": 0.0, "normalized_reply": "",
        }) as interpreter:
            result, requests = self.scripted([
                {"tool": "create_reminder", "args": {
                    "task": task, "due_local": due, "recipient": "me",
                }},
                {"content": "I have checked the result."},
            ], payload=self.payload(message_id, text,
                                    sent_at_ms=int(self.now.timestamp() * 1000)))
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 2)
        return requests, interpreter.call_count

    def assert_time_fragment_retained(self, text, minutes):
        _, draft, _ = self.draft()
        mid = "retention-fragment"
        requests, interpreter_calls = self.run_creation(mid, text, minutes=minutes)
        self.assertEqual(interpreter_calls, 1)
        trace = brain.recent_trace(mid)
        self.assertIn("create_reminder:clarification", trace["tools_called"])
        self.assertEqual(trace["conversation_state"]["active"]["id"], draft["item_id"])
        receipts = [message["content"] for message in requests[-1]["messages"]
                    if message.get("role") == "tool"]
        self.assertEqual(len(receipts), 1)
        self.assertIn("needs_clarification", receipts[0])
        records = self.records()
        self.assertEqual(records["reminders"], [], "A rejected time must not create a reminder")
        self.assertEqual(records["pending"], [draft],
                         "Retaining a draft must not append, replace, cancel or resolve it")
        reply = [row for row in records["outbound"] if row["source_message_id"] == mid]
        self.assertEqual(len(reply), 1)
        self.assertEqual(reply[0]["context_kind"], "PENDING_ITEM")
        self.assertEqual(reply[0]["context_id"], draft["item_id"])
        self.assertIn("?", reply[0]["text_body"])

    def test_relative_message_time_fragment_preserves_original_pending_task(self):
        self.assert_time_fragment_retained(
            "Forty minutes from the message I just sent please", 40)

    def test_half_hour_fragment_preserves_original_pending_task(self):
        self.assert_time_fragment_retained(
            "Roughly half an hour from the current time please", 30)

    def test_explicit_new_reminder_missing_time_can_create_its_own_draft(self):
        _, old, _ = self.draft()
        text = "New reminder: remind me to call the dentist."
        mid = "retention-new-incomplete"
        self.run_creation(mid, text, task="call the dentist")
        records = self.records()
        self.assertEqual(records["reminders"], [])
        self.assertEqual(len(records["pending"]), 2)
        old_after = next(row for row in records["pending"] if row["item_id"] == old["item_id"])
        self.assertEqual(old_after["status"], "CANCELLED")
        self.assertEqual(old_after["resolution_message_id"], mid)
        new = next(row for row in records["pending"] if row["status"] == "PENDING")
        self.assertNotEqual(new["item_id"], old["item_id"])
        self.assertEqual(new["source_message_id"], mid)
        self.assertEqual(new["accumulated_text"], text)
        reply = next(row for row in records["outbound"] if row["source_message_id"] == mid)
        self.assertEqual(reply["context_id"], new["item_id"])

    def test_successful_new_reminder_does_not_consume_original_pending_task(self):
        _, old, _ = self.draft()
        mid = "retention-new-complete"
        self.run_creation(mid, "New reminder: remind me to call the dentist in forty minutes.",
                          task="call the dentist")
        records = self.records()
        self.assertEqual(records["pending"], [old])
        self.assertEqual(len(records["reminders"]), 1)
        self.assertEqual(records["reminders"][0]["source_message_id"], mid)
        self.assertEqual(records["reminders"][0]["task_text"], "call the dentist")
        self.assertIn("create_reminder", brain.recent_trace(mid)["tools_called"])
        self.assertNotIn("create_reminder:clarification", brain.recent_trace(mid)["tools_called"])

    def test_retention_revalidates_exact_pending_owner_chat_and_status(self):
        _, closed, _ = self.draft("retention-old")
        _, latest, _ = self.draft("retention-latest")  # supersedes the older row
        _, foreign, _ = self.draft("retention-foreign", spouse=True)
        _, other_chat, _ = self.draft("retention-other-chat",
                                     conversation="retention-other@s.whatsapp.net")
        actor = self.actor("retention-recheck", "Forty minutes from the message I just sent please")
        before = self.records()["pending"]
        for name, item_id in (
            ("closed", closed["item_id"]), ("foreign", foreign["item_id"]),
            ("other_chat", other_chat["item_id"]), ("missing", "missing-draft-id"),
        ):
            with self.subTest(reference=name):
                trace = {"tools_called": ["create_reminder:clarification"],
                         "conversation_state": {"active": {"kind": "REMINDER_DRAFT", "id": item_id}}}
                with patch.object(brain, "recent_trace", return_value=trace) as lookup:
                    retained = core.ingress._retained_reminder_draft_after_clarification(
                        actor, "What time should I remind you?")
                lookup.assert_called_once_with(actor.source_message_id)
                self.assertIsNone(retained, "Invalid exact binding must not fall back to the latest draft")
        valid_trace = {"tools_called": ["create_reminder:clarification"],
                       "conversation_state": {"active": {"kind": "REMINDER_DRAFT", "id": latest["item_id"]}}}
        with patch.object(brain, "recent_trace", return_value=valid_trace):
            self.assertEqual(core.ingress._retained_reminder_draft_after_clarification(
                actor, "What time should I remind you?"), latest)
        self.assertEqual(self.records()["pending"], before)

    def test_retention_requires_time_clarification_and_rejected_create(self):
        _, draft, _ = self.draft()
        actor = self.actor("retention-evidence", "Forty minutes from the message I just sent please")
        for calls, reply in (
            ([], "What time should I remind you?"),
            (["create_reminder"], "What time should I remind you?"),
            (["create_reminder:error"], "What time should I remind you?"),
            (["create_reminder:clarification"], "Which reminder do you mean?"),
            (["create_reminder:clarification"], "Something went wrong."),
        ):
            with self.subTest(calls=calls, reply=reply):
                trace = {"tools_called": calls,
                         "conversation_state": {"active": {"kind": "REMINDER_DRAFT", "id": draft["item_id"]}}}
                with patch.object(brain, "recent_trace", return_value=trace):
                    self.assertIsNone(core.ingress._retained_reminder_draft_after_clarification(actor, reply))
        self.assertEqual(self.records()["pending"], [draft])
