"""Reminder meaning must survive the complete ingress-to-storage conversation.

Provider decisions and the narrow continuation classifier are scripted. Ingress,
the brain, MCP, time/identity validation, reminder storage, pending completion and
outbound bindings execute normally against a disposable database. These tests
measure execution contracts, not the configured model's language accuracy.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_conversation_reliability import ConversationFixture, brain, core, db


class ReminderHandoffIntegrationTests(ConversationFixture):
    def setUp(self):
        # SQLite's age gates use its wall clock. Freeze Python and receipt time
        # to the same real instant; never put a fixture's receipt hours apart
        # from its processing time, as the preliminary review accidentally did.
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        clock = patch.dict("os.environ", {"ALEX_CERT_NOW": self.now.isoformat()})
        clock.start()
        self.addCleanup(clock.stop)
        super().setUp()
        self.group = "120363544544@g.us"
        group_file = Path(core.TEST_DIR) / "family_group.json"
        original = group_file.read_bytes() if group_file.exists() else None
        group_file.write_text(json.dumps({"group_jid": self.group}))

        def restore_group_file():
            if original is None:
                group_file.unlink(missing_ok=True)
            else:
                group_file.write_bytes(original)

        self.addCleanup(restore_group_file)

    def _payload(self, mid, text, *, spouse=False, group=False,
                 received=None, quote=None):
        phone = "+60222222222" if spouse else "+60111111111"
        received = received or self.now
        payload = {
            "message_id": mid,
            "provider": "WHATSAPP",
            "conversation_id": self.group if group else phone[1:] + "@s.whatsapp.net",
            "conversation_type": "GROUP" if group else "DIRECT_DM",
            "sender_phone": phone,
            "text": text,
            "sent_at_ms": int(received.timestamp() * 1000),
        }
        if group:
            payload["alex_mentioned"] = True
        if quote:
            payload["quoted_message_id"] = quote
        return payload

    def _rows(self, table, where="", parameters=()):
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} " + where, parameters)]
        finally:
            conn.close()

    def _seed_conversation(self, *, suffix="draft",
                           original="Remind me to water the plants",
                           question="What time should I remind you to water the plants?",
                           spouse=False, group=False):
        mid = self._testMethodName + "-" + suffix
        result, requests = self.scripted(
            [{"content": question}],
            payload=self._payload(mid, original, spouse=spouse, group=group),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        drafts = self._rows(
            "pending_items", "WHERE source_message_id=? AND kind='REMINDER_DRAFT'", (mid,))
        self.assertEqual(len(drafts), 1, "Initial ingress did not preserve the missing-time task")
        self.assertEqual(drafts[0]["status"], "PENDING")
        out = self._rows(
            "outbound_messages", "WHERE source_message_id=? AND context_kind='PENDING_ITEM'", (mid,))
        self.assertEqual(len(out), 1)
        provider_id = "wa-" + suffix + "-" + self._testMethodName
        conn = db.connect()
        try:
            # Keep all explicitly seeded rows on the same fixture clock. This
            # changes no action-created rows or results after execution.
            for table, column in (("pending_items", "created_at_utc"),
                                  ("conversation_turns", "created_at_utc"),
                                  ("outbound_messages", "created_at_utc"),
                                  ("inbound_messages", "received_at_utc")):
                conn.execute(f"UPDATE {table} SET {column}=?", (self.now.isoformat(),))
            conn.execute(
                "UPDATE outbound_messages SET provider_message_id=?,delivery_status='SENT' "
                "WHERE outbound_id=?", (provider_id, out[0]["outbound_id"]))
            conn.commit()
        finally:
            conn.close()
        return self._rows("pending_items", "WHERE item_id=?", (drafts[0]["item_id"],))[0], provider_id

    @staticmethod
    def _meaning(text, *, minutes=None, date="tomorrow", clock="19:00",
                 reference="ACTIVE_DRAFT", **extra):
        value = {
            "source_text": text,
            "kind": "RELATIVE" if minutes is not None else "LOCAL_CLOCK",
            "reference": reference,
            "needs_clarification": False,
        }
        if minutes is not None:
            value["relative_minutes"] = minutes
        else:
            value["date_text"] = date
            value["time_text"] = clock
        return {**value, **extra}

    def _local_due(self, *, minutes=None, hour=19, minute=0, received=None):
        if minutes is not None:
            return (received or self.now) + timedelta(minutes=minutes)
        local = self.now.astimezone(ZoneInfo("Asia/Kuala_Lumpur")) + timedelta(days=1)
        return local.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)

    def _create(self, text, meaning, *, suffix="answer", task="water the plants",
                due=None, group=False, spouse=False, quote=None,
                received=None, recipient="me", destination="dm",
                allow_unexposed=False):
        mid = self._testMethodName + "-" + suffix
        due = due or self._local_due(minutes=meaning.get("relative_minutes"))
        payload = self._payload(mid, text, group=group, spouse=spouse,
                                quote=quote, received=received)
        with patch.object(brain, "interpret_control_intent", return_value={
            "intent": "UNCLEAR", "confidence": 0.0,
            "normalized_reply": "", "status": "ok",
        }) as interpreter:
            result, requests = self.scripted([
                {"tool": "create_reminder", "args": {
                    "task": task,
                    "due_local": due.astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(tzinfo=None).isoformat(),
                    "recipient": recipient,
                    "destination": destination,
                    "time_interpretation": meaning,
                }},
                {"content": "I have checked the result."},
            ], payload=payload, allow_unexposed=allow_unexposed)
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 2,
                         "The handoff introduced an extra main-brain model round")
        receipts = [json.loads(message["content"]) for message in requests[-1]["messages"]
                    if message.get("role") == "tool"]
        self.assertEqual(len(receipts), 1)
        return mid, result, requests, receipts[0], interpreter.call_count, payload

    def _assert_created_and_resolved(self, mid, draft, expected_due, *, task="water the plants",
                                     destination=None, owner="USR_HUSBAND", space=None):
        reminders = self._rows("reminders", "WHERE source_message_id=?", (mid,))
        self.assertEqual(len(reminders), 1)
        record = reminders[0]
        self.assertEqual(record["task_text"], task)
        self.assertEqual(record["owner_id"], owner)
        self.assertEqual(record["conversation_id"], destination or "60111111111@s.whatsapp.net")
        self.assertEqual(datetime.fromisoformat(record["due_at_utc"]), expected_due)
        if space:
            self.assertEqual(record["space_id"], space)
        after = self._rows("pending_items", "WHERE item_id=?", (draft["item_id"],))[0]
        self.assertEqual(after["status"], "RESOLVED")
        self.assertEqual(after["resolution_message_id"], mid)
        self.assertTrue(after["accumulated_text"].startswith(draft["accumulated_text"]))
        self.assertEqual(self._rows("pending_items", "WHERE source_message_id=?", (mid,)), [],
                         "Successful continuation also created an orphan draft")
        events = self._rows("reminder_events", "WHERE reminder_id=? AND event_type='CREATED'",
                            (record["reminder_id"],))
        self.assertEqual(len(events), 1)
        replies = self._rows("outbound_messages", "WHERE source_message_id=?", (mid,))
        self.assertTrue(replies)
        self.assertFalse(any(row["context_kind"] == "PENDING_ITEM"
                             and row["context_id"] == draft["item_id"] for row in replies),
                         "A successful reminder still presents the completed draft as pending")
        return record

    def _assert_rejected_and_retained(self, mid, draft):
        self.assertEqual(self._rows("reminders", "WHERE source_message_id=?", (mid,)), [])
        after = self._rows("pending_items", "WHERE item_id=?", (draft["item_id"],))[0]
        self.assertEqual(after["status"], "PENDING")
        self.assertTrue(after["accumulated_text"].startswith(draft["accumulated_text"]))
        self.assertEqual(self._rows("pending_items", "WHERE source_message_id=?", (mid,)), [],
                         "A rejected time fragment replaced the original task with a new draft")
        replies = self._rows("outbound_messages", "WHERE source_message_id=? AND kind='TEXT'", (mid,))
        self.assertEqual(len(replies), 1)
        self.assertIn("?", replies[0]["text_body"])
        self.assertEqual(replies[0]["context_kind"], "PENDING_ITEM")
        self.assertEqual(replies[0]["context_id"], draft["item_id"])

    def _clear_phrase(self, text, *, minutes=None, hour=19, due_override=None):
        draft, _ = self._seed_conversation()
        meaning = self._meaning(text, minutes=minutes, clock=f"{hour:02d}:00")
        mid, _, _, _, calls, _ = self._create(text, meaning, due=due_override)
        self.assertLessEqual(calls, 1, "Ingress interpreted the same continuation repeatedly")
        self._assert_created_and_resolved(mid, draft, self._local_due(minutes=minutes, hour=hour))
        return mid, calls

    def test_word_clock_evening_executes_and_ignores_wrong_model_date_math(self):
        self._clear_phrase("Around seven tomorrow evening would work for me",
                           due_override=datetime(2000, 1, 1, tzinfo=timezone.utc))

    def test_message_anchored_relative_phrase_rescues_unclear_interpreter(self):
        _, calls = self._clear_phrase("Forty minutes from the message I just sent please", minutes=40)
        self.assertEqual(calls, 1)

    def test_after_dinner_word_clock_executes(self):
        self._clear_phrase("Tomorrow after dinner at eight would be best", hour=20)

    def test_reordered_word_clock_executes(self):
        self._clear_phrase("The time should be tomorrow evening at seven")

    def test_half_hour_phrase_rescues_unclear_interpreter(self):
        _, calls = self._clear_phrase("Roughly half an hour from the current time please", minutes=30)
        self.assertEqual(calls, 1)

    def test_apostrophe_word_clock_executes(self):
        self._clear_phrase("At about seven o’clock tomorrow evening")

    def test_referring_to_that_reminder_is_not_mistaken_for_a_new_task(self):
        self._clear_phrase(
            "For that reminder, forty minutes from the message I just sent please",
            minutes=40)

    def test_setting_that_reminders_clock_is_a_continuation(self):
        self._clear_phrase("Please set that reminder for tomorrow at seven in the evening")

    def test_missing_am_pm_is_not_filled_by_overconfident_model(self):
        draft, _ = self._seed_conversation()
        text = "Tomorrow around 7ish would be convenient"
        mid, *_ = self._create(text, self._meaning(text, clock="19:00"))
        self._assert_rejected_and_retained(mid, draft)

    def test_open_ended_after_time_is_not_scheduled_as_exact_clock(self):
        draft, _ = self._seed_conversation()
        text = "Tonight, sometime after eight in the evening"
        mid, *_ = self._create(text, self._meaning(text, date="today", clock="20:00"))
        self._assert_rejected_and_retained(mid, draft)

    def test_open_ended_future_interval_is_not_scheduled_as_exact_clock(self):
        # This future variant isolates ambiguity even when the captured
        # "tonight" wording above has already elapsed on the test runner.
        draft, _ = self._seed_conversation()
        text = "Tomorrow, sometime after eight in the evening"
        mid, *_ = self._create(text, self._meaning(text, clock="20:00"))
        self._assert_rejected_and_retained(mid, draft)

    def test_quoted_original_draft_wins_over_another_pending_draft(self):
        old, old_quote = self._seed_conversation(suffix="old")
        newer, _ = self._seed_conversation(
            suffix="new", original="New reminder: remind me to call the dentist",
            question="What time should I remind you to call the dentist?")
        self.restore_pending_for_defensive_fixture(old["item_id"])
        text = "Forty minutes from the message I just sent please"
        mid, *_ = self._create(text, self._meaning(text, minutes=40), quote=old_quote)
        self._assert_created_and_resolved(mid, old, self._local_due(minutes=40))
        self.assertEqual(self._rows("pending_items", "WHERE item_id=?", (newer["item_id"],)), [newer])

    def test_explicit_new_reminder_does_not_consume_an_existing_draft(self):
        old, _ = self._seed_conversation()
        text = ("New reminder: remind me to call the dentist in forty minutes. "
                "Keep that reminder separate.")
        mid, *_ = self._create(text, self._meaning(text, minutes=40, reference="CURRENT_MESSAGE"),
                               task="call the dentist")
        self.assertEqual(self._rows("pending_items", "WHERE item_id=?", (old["item_id"],)), [old])
        reminders = self._rows("reminders", "WHERE source_message_id=?", (mid,))
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["task_text"], "call the dentist")
        self.assertEqual(datetime.fromisoformat(reminders[0]["due_at_utc"]), self._local_due(minutes=40))

    def test_duplicate_inbound_does_not_repeat_creation_or_pending_completion(self):
        draft, _ = self._seed_conversation()
        text = "Forty minutes from the message I just sent please"
        mid, _, _, _, _, payload = self._create(text, self._meaning(text, minutes=40))
        self._assert_created_and_resolved(mid, draft, self._local_due(minutes=40))
        before = {table: self._rows(table) for table in
                  ("reminders", "pending_items", "reminder_events", "outbound_messages", "tool_audit")}
        with patch.object(brain, "respond", side_effect=AssertionError("Duplicate reached the brain")):
            duplicate = core.ingress.process(payload)
        self.assertTrue(duplicate["ok"])
        self.assertEqual({table: self._rows(table) for table in before}, before)

    def test_group_continuation_preserves_group_destination(self):
        draft, _ = self._seed_conversation(group=True)
        text = "Forty minutes from the message I just sent please"
        mid, *_ = self._create(text, self._meaning(text, minutes=40), group=True,
                               recipient="me", destination="dm")
        reminder = self._assert_created_and_resolved(
            mid, draft, self._local_due(minutes=40), destination=self.group, space="FAMILY_SHARED")
        self.assertEqual(reminder["claimable"], 1)

    def test_named_recipient_survives_group_time_clarification(self):
        draft, _ = self._seed_conversation(group=True,
            original="Remind Priya to water the plants",
            question="What time should I remind Priya to water the plants?")
        text = "Forty minutes from the message I just sent please"
        mid, *_ = self._create(text, self._meaning(text, minutes=40), group=True,
                               recipient="me", destination="dm")
        self._assert_created_and_resolved(
            mid, draft, self._local_due(minutes=40), destination="60222222222@s.whatsapp.net",
            owner="USR_WIFE", space="FAMILY_SHARED")

    def test_private_scope_survives_unbound_natural_time_reply(self):
        draft, _ = self._seed_conversation(original="Privately remind me to water the plants")
        text = "Forty minutes from the message I just sent please"
        mid, *_ = self._create(text, self._meaning(text, minutes=40))
        self._assert_created_and_resolved(mid, draft, self._local_due(minutes=40), space="HUSBAND_PVT")

    def test_fabricated_time_evidence_cannot_authorize_creation(self):
        draft, _ = self._seed_conversation()
        text = "Forty minutes from the message I just sent please"
        mid, *_ = self._create(text, self._meaning("in 120 minutes", minutes=120))
        self._assert_rejected_and_retained(mid, draft)

    def test_closed_quoted_draft_cannot_be_reopened_by_active_reference(self):
        draft, quote = self._seed_conversation()
        db.cancel_pending_item(draft["item_id"], "USR_HUSBAND", "closed-before-answer")
        before = self._rows("pending_items", "WHERE item_id=?", (draft["item_id"],))[0]
        text = "Forty minutes from the message I just sent please"
        mid, *_ = self._create(text, self._meaning(text, minutes=40), quote=quote,
                               allow_unexposed=True)
        self.assertEqual(self._rows("reminders"), [])
        self.assertEqual(self._rows("pending_items", "WHERE item_id=?", (draft["item_id"],)), [before])
        self.assertNotEqual(before["status"], "PENDING")

    def test_draft_closed_after_interpretation_is_revalidated_before_writing(self):
        draft, _ = self._seed_conversation()
        text = "Forty minutes from the message I just sent please"
        real_create = core.services.create_reminder

        def close_before_actual_create(*args, **kwargs):
            db.cancel_pending_item(draft["item_id"], "USR_HUSBAND", "cancelled-concurrently")
            return real_create(*args, **kwargs)

        with patch.object(core.services, "create_reminder", side_effect=close_before_actual_create):
            self._create(text, self._meaning(text, minutes=40))
        self.assertEqual(self._rows("reminders"), [])
        after = self._rows("pending_items", "WHERE item_id=?", (draft["item_id"],))[0]
        self.assertEqual(after["status"], "CANCELLED")
        self.assertEqual(after["resolution_message_id"], "cancelled-concurrently")

    def test_foreign_dm_quote_cannot_authorize_active_draft_creation(self):
        _, quote = self._seed_conversation(spouse=True)
        before = self._rows("pending_items")
        text = "Forty minutes from the message I just sent please"
        self._create(text, self._meaning(text, minutes=40), quote=quote,
                     allow_unexposed=True)
        self.assertEqual(self._rows("pending_items"), before)
        self.assertEqual(self._rows("reminders"), [])

    def test_other_chat_quote_cannot_authorize_active_draft_creation(self):
        _, quote = self._seed_conversation(group=True)
        before = self._rows("pending_items")
        text = "Forty minutes from the message I just sent please"
        self._create(text, self._meaning(text, minutes=40), quote=quote,
                     allow_unexposed=True)
        self.assertEqual(self._rows("pending_items"), before)
        self.assertEqual(self._rows("reminders"), [])

    def test_same_group_foreign_owner_quote_cannot_consume_their_draft(self):
        _, quote = self._seed_conversation(spouse=True, group=True)
        before = self._rows("pending_items")
        text = "Forty minutes from the message I just sent please"
        self._create(text, self._meaning(text, minutes=40), quote=quote,
                     group=True, allow_unexposed=True)
        self.assertEqual(self._rows("pending_items"), before)
        self.assertEqual(self._rows("reminders"), [])

    def test_another_pending_workflow_cannot_be_used_as_a_reminder_draft(self):
        draft, quote = self._seed_conversation()
        # A pre-existing pending row from another workflow is a valid
        # same-owner/same-chat quote, but never an active reminder draft.
        conn = db.connect()
        try:
            conn.execute("UPDATE pending_items SET kind='PRIVATE_SEARCH_OFFER' WHERE item_id=?",
                         (draft["item_id"],))
            conn.commit()
        finally:
            conn.close()
        before = self._rows("pending_items")
        text = "Forty minutes from the message I just sent please"
        self._create(text, self._meaning(text, minutes=40), quote=quote,
                     allow_unexposed=True)
        self.assertEqual(self._rows("pending_items"), before)
        self.assertEqual(self._rows("reminders"), [])

    def test_delayed_current_request_uses_message_time_not_processing_time(self):
        received = self.now - timedelta(minutes=5)
        text = "Remind me to water the plants in forty minutes."
        meaning = self._meaning(text, minutes=40, reference="CURRENT_MESSAGE")
        mid, *_ = self._create(text, meaning, received=received)
        reminders = self._rows("reminders", "WHERE source_message_id=?", (mid,))
        self.assertEqual(len(reminders), 1)
        self.assertEqual(datetime.fromisoformat(reminders[0]["due_at_utc"]), received + timedelta(minutes=40))

    def test_elapsed_relative_request_does_not_restart_its_delay(self):
        received = self.now - timedelta(minutes=5)
        text = "Remind me to water the plants in two minutes."
        meaning = self._meaning(text, minutes=2, reference="CURRENT_MESSAGE")
        mid, *_ = self._create(text, meaning, received=received)
        self.assertEqual(self._rows("reminders"), [])
        drafts = self._rows("pending_items", "WHERE source_message_id=?", (mid,))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "PENDING")
        self.assertEqual(drafts[0]["accumulated_text"], text)

