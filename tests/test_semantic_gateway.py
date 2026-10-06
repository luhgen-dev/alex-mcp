import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import test_core as core


db = core.db
brain = core.brain
ingress = core.ingress
services = core.services
runtime_clock = core.runtime_clock
with_action_key = core.with_action_key


class SemanticGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.AlexCoreTests.setUpClass()

    def setUp(self):
        # Reuse the certified core test isolation rather than maintaining a
        # second database-cleanup list that could drift from the real schema.
        core.AlexCoreTests.setUp(self)

    def tearDown(self):
        # Leave no reminder/event rows behind for older test modules that have
        # narrower historical cleanup lists.
        core.AlexCoreTests.setUp(self)

    @staticmethod
    def _payload(message_id: str, text: str) -> dict:
        return {
            "message_id": message_id,
            "provider": "WHATSAPP",
            "conversation_id": "60111111111@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": text,
        }

    def _claim_actor(self, message_id: str, text: str):
        db.claim_inbound(self._payload(message_id, text))
        actor = db.resolve_actor(
            "+60111111111",
            "60111111111@s.whatsapp.net",
            "DIRECT_DM",
            message_id,
            [],
        )
        return replace(actor, trusted_text=text, intent_text=text)

    def _make_draft(
        self,
        *,
        source_id: str = "sg-draft-source",
        original: str = "Remind me to water the plants",
        question: str = "What time should I remind you to water the plants?",
    ):
        actor = self._claim_actor(source_id, original)
        draft = db.create_pending_item(
            actor,
            "REMINDER_DRAFT",
            note="semantic gateway test",
        )
        db.queue_outbound(
            actor.conversation_id,
            "TEXT",
            text=question,
            source_message_id=actor.source_message_id,
            context_kind="PENDING_ITEM",
            context_id=draft["item_id"],
        )
        return actor, draft

    def test_semantic_frame_parser_fails_closed(self):
        malformed = brain._parse_semantic_control_frame(
            "not json",
            {"ANSWER_PENDING", "UNCLEAR"},
        )
        self.assertEqual(malformed["intent"], "UNCLEAR")
        self.assertEqual(malformed["status"], "invalid_json")

        disallowed = brain._parse_semantic_control_frame(
            json.dumps({
                "intent": "DELETE_REMINDER",
                "confidence": 1.0,
                "normalized_reply": "delete it",
            }),
            {"ANSWER_PENDING", "UNCLEAR"},
        )
        self.assertEqual(disallowed["intent"], "UNCLEAR")
        self.assertEqual(disallowed["status"], "disallowed_intent")

        valid = brain._parse_semantic_control_frame(
            json.dumps({
                "intent": "ANSWER_PENDING",
                "confidence": 0.93,
                "normalized_reply": "in 40 minutes",
            }),
            {"ANSWER_PENDING", "UNCLEAR"},
        )
        self.assertEqual(valid["intent"], "ANSWER_PENDING")
        self.assertEqual(valid["normalized_reply"], "in 40 minutes")

    def test_semantic_interpreter_is_bounded_toolless_and_historyless(self):
        actor = self._claim_actor("sg-provider", "roughly forty minits")
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "intent": "ANSWER_PENDING",
                                    "confidence": 0.97,
                                    "normalized_reply": "in 40 minutes",
                                })
                            )
                        )
                    ],
                    usage=None,
                )

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        routes = [{
            "provider": "gemini",
            "model": "gemini-3.1-flash-lite",
            "reasoning_effort": "low",
            "role": "primary_saver",
        }]

        with patch.object(brain, "_provider_routes", return_value=routes), \
             patch.object(brain, "_client_for", return_value=fake_client), \
             patch.object(brain, "_record_usage_buckets"), \
             patch.object(brain, "_audit"):
            frame = brain.interpret_control_intent(
                actor,
                control_kind="REMINDER_DRAFT",
                current_text="x" * 900,
                pending_text="p" * 3000,
                latest_question="q" * 1500,
                allowed_intents={"ANSWER_PENDING", "NEW_REQUEST", "UNCLEAR"},
            )

        self.assertEqual(frame["intent"], "ANSWER_PENDING")
        self.assertNotIn("tools", captured)
        self.assertEqual(len(captured["messages"]), 2)
        user_payload = json.loads(captured["messages"][1]["content"])
        self.assertLessEqual(
            len(user_payload["current_reply"]),
            brain.SEMANTIC_GATEWAY_CURRENT_MAX_CHARS,
        )
        self.assertLessEqual(
            len(user_payload["pending_user_text"]),
            brain.SEMANTIC_GATEWAY_PENDING_MAX_CHARS,
        )
        self.assertLessEqual(
            len(user_payload["latest_alex_prompt"]),
            brain.SEMANTIC_GATEWAY_QUESTION_MAX_CHARS,
        )
        self.assertNotIn("item_id", captured["messages"][1]["content"])

    def test_semantic_interpreter_escalates_once_after_unresolved_saver_frame(self):
        actor = self._claim_actor("sg-provider-fallback", "in abt fourty mins")
        seen_models = []

        class FakeCompletions:
            def create(self, **kwargs):
                seen_models.append(kwargs["model"])
                if kwargs["model"] == "gemini-3.1-flash-lite":
                    content = json.dumps({
                        "intent": "UNCLEAR",
                        "confidence": 0.41,
                        "normalized_reply": "",
                    })
                else:
                    content = json.dumps({
                        "intent": "ANSWER_PENDING",
                        "confidence": 0.98,
                        "normalized_reply": "in 40 minutes",
                    })
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=content)
                    )],
                    usage=None,
                )

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        routes = [
            {
                "provider": "gemini",
                "model": "gemini-3.1-flash-lite",
                "reasoning_effort": "low",
                "role": "primary_saver",
            },
            {
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "reasoning_effort": "low",
                "role": "quality_fallback",
            },
        ]
        with patch.object(brain, "_provider_routes", return_value=routes), \
             patch.object(brain, "_client_for", return_value=fake_client), \
             patch.object(brain, "_record_usage_buckets"), \
             patch.object(brain, "_audit"):
            frame = brain.interpret_control_intent(
                actor,
                control_kind="REMINDER_DRAFT",
                current_text="in abt fourty mins",
                pending_text="Remind me to check the mailbox",
                latest_question="What time should I remind you?",
                allowed_intents={"ANSWER_PENDING", "UNCLEAR"},
            )

        self.assertEqual(
            seen_models,
            ["gemini-3.1-flash-lite", "gemini-3.8-flash"],
        )
        self.assertEqual(frame["intent"], "ANSWER_PENDING")
        self.assertEqual(frame["normalized_reply"], "in 40 minutes")

    def test_brain_models_normalized_time_but_history_keeps_raw_user_words(self):
        _, draft = self._make_draft(
            source_id="sg-model-source",
            original="Remind me to check the mailbox",
            question="What time should I remind you?",
        )
        current = self._claim_actor("sg-model-current", "in abt fourty mins")
        current = replace(
            current,
            reminder_context_text=(
                "Remind me to check the mailbox\nin abt fourty mins"
            ),
            reminder_semantic_text="in 40 minutes",
        )
        quoted = ingress._reminder_draft_context(draft)
        quoted["semantic_reminder_text"] = "in 40 minutes"
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(
                            content="I understood the reminder time.",
                            tool_calls=None,
                        )
                    )],
                    usage=None,
                )

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        routes = [{
            "provider": "gemini",
            "model": "gemini-3.1-flash-lite",
            "reasoning_effort": "low",
            "role": "primary_saver",
        }]
        with patch.object(brain, "_provider_routes", return_value=routes), \
             patch.object(brain, "_client_for", return_value=fake_client), \
             patch.object(brain, "_tool_specs", return_value=[]), \
             patch.object(brain, "_record_usage_buckets"), \
             patch.object(brain, "_trace_turn"):
            asyncio.run(brain.respond(
                current,
                "in abt fourty mins",
                quoted_context=quoted,
                semantic_user_text="in 40 minutes",
            ))

        self.assertEqual(captured["messages"][-1]["content"], "in 40 minutes")
        history = db.recent_turns(current.conversation_id, 2)
        self.assertEqual(history[-2]["role"], "user")
        self.assertEqual(history[-2]["content"], "in abt fourty mins")

    def test_proven_clean_continuation_stays_zero_token(self):
        _, draft = self._make_draft()
        current = self._claim_actor("sg-clean", "Tomorrow")
        with patch.object(
            brain,
            "interpret_control_intent",
            side_effect=AssertionError("semantic call should not be needed"),
        ):
            continues, question, hint = ingress._reminder_draft_continuation(
                current, draft, "Tomorrow"
            )
            natural, _, natural_hint = ingress._reminder_draft_continuation(
                current, draft, "At night, around 10.45"
            )
        self.assertTrue(continues)
        self.assertTrue(natural)
        self.assertIn("time", question.casefold())
        self.assertEqual(hint, "")
        self.assertEqual(natural_hint, "")

    def test_semantic_hint_normalizes_relative_time_for_deterministic_validator(self):
        _, draft = self._make_draft()
        current = self._claim_actor("sg-relative", "in abt fourty mins")
        with patch.object(
            brain,
            "interpret_control_intent",
            return_value={
                "intent": "ANSWER_PENDING",
                "confidence": 0.98,
                "normalized_reply": "in 40 minutes",
                "status": "ok",
            },
        ):
            continues, _, hint = ingress._reminder_draft_continuation(
                current, draft, "in abt fourty mins"
            )

        self.assertTrue(continues)
        self.assertEqual(hint, "in 40 minutes")

        interpreted = replace(
            current,
            reminder_context_text=(
                "Remind me to water the plants\nin abt fourty mins"
            ),
            reminder_semantic_text=hint,
        )
        frozen = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        with patch.object(runtime_clock, "now_utc", return_value=frozen):
            due = services._deterministic_reminder_due_from_text(interpreted)
        self.assertEqual(
            datetime.fromisoformat(due),
            datetime(2026, 10, 6, 0, 40, tzinfo=timezone.utc),
        )

    def test_last_live_failure_wording_is_covered_without_phrase_patches(self):
        _, draft = self._make_draft(
            source_id="sg-live-forms-draft",
            original="Remind me to check the mailbox",
            question="When should I remind you to check the mailbox?",
        )

        clean = self._claim_actor("sg-live-clean", "In 40 minutes")
        with patch.object(
            brain,
            "interpret_control_intent",
            side_effect=AssertionError("standard relative form is deterministic"),
        ):
            continues, _, hint = ingress._reminder_draft_continuation(
                clean, draft, "In 40 minutes"
            )
        self.assertTrue(continues)
        self.assertEqual(hint, "")

        frozen = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        clean_context = replace(
            clean,
            reminder_context_text=(
                "Remind me to check the mailbox\nIn 40 minutes"
            ),
        )
        with patch.object(runtime_clock, "now_utc", return_value=frozen):
            due = services._deterministic_reminder_due_from_text(clean_context)
        self.assertEqual(
            datetime.fromisoformat(due),
            datetime(2026, 10, 6, 0, 40, tzinfo=timezone.utc),
        )

        interpreted_cases = (
            ("in fourty minits", "in 40 minutes"),
            ("in abt fourty mins", "in 40 minutes"),
            ("tmr ard 7ish", "tomorrow around 7"),
        )
        for index, (raw, normalized) in enumerate(interpreted_cases):
            current = self._claim_actor(f"sg-live-odd-{index}", raw)
            with self.subTest(raw=raw), patch.object(
                brain,
                "interpret_control_intent",
                return_value={
                    "intent": "ANSWER_PENDING",
                    "confidence": 0.98,
                    "normalized_reply": normalized,
                    "status": "ok",
                },
            ) as semantic_mock:
                continues, _, hint = ingress._reminder_draft_continuation(
                    current, draft, raw
                )
                self.assertTrue(continues)
                self.assertEqual(hint, normalized)
                self.assertEqual(semantic_mock.call_count, 1)

        ambiguous = replace(
            self._claim_actor("sg-live-ambiguous", "tmr ard 7ish"),
            reminder_context_text=(
                "Remind me to check the mailbox\ntmr ard 7ish"
            ),
            reminder_semantic_text="tomorrow around 7",
        )
        with patch.object(runtime_clock, "now_utc", return_value=frozen):
            with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
                services._deterministic_reminder_due_from_text(ambiguous)

    def test_semantic_hint_cannot_change_recipient_scope_or_destination(self):
        current = self._claim_actor(
            "sg-scope",
            "Remind me to test the lamp",
        )
        interpreted = replace(
            current,
            reminder_context_text="Remind me to test the lamp",
            reminder_semantic_text=(
                "tomorrow at 7 PM, remind Priya in the family group privately"
            ),
        )
        self.assertEqual(
            services._reminder_intent_text(interpreted),
            "Remind me to test the lamp",
        )
        self.assertEqual(
            services._trusted_named_reminder_recipient(interpreted),
            ("me", "USR_HUSBAND"),
        )
        self.assertIn(
            "tomorrow at 7 PM",
            services._reminder_time_intent_text(interpreted),
        )

    def test_domain_switch_never_rebound_by_semantic_interpreter(self):
        _, draft = self._make_draft()
        current = self._claim_actor(
            "sg-domain-switch",
            "I spent RM20 on lunch today",
        )
        with patch.object(
            brain,
            "interpret_control_intent",
            side_effect=AssertionError("domain switch must stay outside reminder"),
        ):
            continues, _, hint = ingress._reminder_draft_continuation(
                current,
                draft,
                "I spent RM20 on lunch today",
            )
        self.assertFalse(continues)
        self.assertEqual(hint, "")

    def test_ingress_semantic_rescue_calls_interpreter_once_and_keeps_unfinished_draft(self):
        first = self._payload(
            "sg-flow-first",
            "Remind me to water the plants",
        )
        with patch.object(
            brain,
            "respond",
            return_value=(
                "What time should I remind you to water the plants?",
                [],
            ),
        ):
            self.assertTrue(ingress.process(first)["ok"])

        conn = db.connect()
        try:
            draft = conn.execute(
                """SELECT * FROM pending_items
                   WHERE source_message_id='sg-flow-first'
                     AND kind='REMINDER_DRAFT'"""
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(draft)

        seen = {}

        async def second_brain(
            actor,
            user_text,
            media_context=None,
            vision_parts=None,
            quoted_context=None,
            semantic_user_text=None,
        ):
            seen["semantic"] = actor.reminder_semantic_text
            seen["semantic_user_text"] = semantic_user_text
            seen["raw_user_text"] = user_text
            seen["context"] = quoted_context
            return (
                "Should I set that reminder for about 40 minutes from now?",
                [],
            )

        second = self._payload(
            "sg-flow-second",
            "roughly fourty minits from now",
        )
        semantic = {
            "intent": "ANSWER_PENDING",
            "confidence": 0.98,
            "normalized_reply": "in 40 minutes",
            "status": "ok",
        }
        with patch.object(
            brain,
            "interpret_control_intent",
            return_value=semantic,
        ) as semantic_mock, patch.object(
            brain,
            "respond",
            new=second_brain,
        ):
            self.assertTrue(ingress.process(second)["ok"])

        self.assertEqual(semantic_mock.call_count, 1)
        self.assertEqual(seen["semantic"], "in 40 minutes")
        self.assertEqual(seen["semantic_user_text"], "in 40 minutes")
        self.assertEqual(seen["raw_user_text"], "roughly fourty minits from now")
        self.assertEqual(
            seen["context"]["pending_item"]["item_id"],
            draft["item_id"],
        )
        self.assertEqual(
            seen["context"]["semantic_reminder_text"],
            "in 40 minutes",
        )

        conn = db.connect()
        try:
            state = conn.execute(
                """SELECT status,accumulated_text FROM pending_items
                   WHERE item_id=?""",
                (draft["item_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(state["status"], "PENDING")
        self.assertEqual(
            state["accumulated_text"].count(
                "roughly fourty minits from now"
            ),
            1,
        )

    def test_semantic_relative_answer_can_create_then_resolve_same_draft(self):
        first = self._payload(
            "sg-create-first",
            "Remind me to check the mailbox",
        )
        with patch.object(
            brain,
            "respond",
            return_value=(
                "When should I remind you to check the mailbox?",
                [],
            ),
        ):
            self.assertTrue(ingress.process(first)["ok"])

        conn = db.connect()
        try:
            draft = conn.execute(
                """SELECT * FROM pending_items
                   WHERE source_message_id='sg-create-first'
                     AND kind='REMINDER_DRAFT'"""
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(draft)

        async def create_on_interpreted_turn(
            actor,
            user_text,
            media_context=None,
            vision_parts=None,
            quoted_context=None,
            semantic_user_text=None,
        ):
            self.assertEqual(
                actor.reminder_semantic_text,
                "in 40 minutes",
            )
            self.assertEqual(semantic_user_text, "in 40 minutes")
            self.assertEqual(user_text, "in abt fourty mins")
            services.create_reminder(
                with_action_key(actor, "sg-create-action"),
                "check the mailbox",
                "2026-10-06T08:40:00+08:00",
            )
            return ("OK. Reminder set.", [])

        second = self._payload(
            "sg-create-second",
            "in abt fourty mins",
        )
        frozen = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        with patch.object(
            brain,
            "interpret_control_intent",
            return_value={
                "intent": "ANSWER_PENDING",
                "confidence": 0.99,
                "normalized_reply": "in 40 minutes",
                "status": "ok",
            },
        ) as semantic_mock, patch.object(
            brain,
            "respond",
            new=create_on_interpreted_turn,
        ), patch.object(
            runtime_clock,
            "now_utc",
            return_value=frozen,
        ):
            self.assertTrue(ingress.process(second)["ok"])

        self.assertEqual(semantic_mock.call_count, 1)
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (draft["item_id"],),
            ).fetchone()["status"]
            reminder = conn.execute(
                """SELECT due_at_utc FROM reminders
                   WHERE source_message_id='sg-create-second'"""
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(state, "RESOLVED")
        self.assertIsNotNone(reminder)
        self.assertEqual(
            datetime.fromisoformat(reminder["due_at_utc"]),
            datetime(2026, 10, 6, 0, 40, tzinfo=timezone.utc),
        )


    def test_live_regression_swipe_reply_typo_is_translated_before_main_brain(self):
        first = self._payload(
            "sg-live-quote-first",
            "Remind me to check the mailbox",
        )
        with patch.object(
            brain,
            "respond",
            return_value=("What time should I remind you?", []),
        ):
            self.assertTrue(ingress.process(first)["ok"])

        conn = db.connect()
        try:
            draft = conn.execute(
                """SELECT * FROM pending_items
                   WHERE source_message_id='sg-live-quote-first'
                     AND kind='REMINDER_DRAFT'"""
            ).fetchone()
            outbound = conn.execute(
                """SELECT * FROM outbound_messages
                   WHERE context_kind='PENDING_ITEM' AND context_id=?
                   ORDER BY rowid DESC LIMIT 1""",
                (draft["item_id"],),
            ).fetchone()
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-sg-live-time-question',
                       delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (outbound["outbound_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        seen = {}

        async def create_from_translated_quote(
            actor,
            user_text,
            media_context=None,
            vision_parts=None,
            quoted_context=None,
            semantic_user_text=None,
        ):
            seen["raw"] = user_text
            seen["semantic"] = semantic_user_text
            seen["trusted"] = actor.trusted_text
            seen["context"] = quoted_context
            services.create_reminder(
                with_action_key(actor, "sg-live-quote-action"),
                "check the mailbox",
                "2026-10-06T03:19:00+08:00",
            )
            return ("I've set a reminder to check the mailbox in 40 minutes.", [])

        second = self._payload(
            "sg-live-quote-second",
            "in abt fourty mins",
        )
        second["quoted_message_id"] = "wa-sg-live-time-question"
        frozen = datetime(2026, 10, 5, 18, 39, tzinfo=timezone.utc)
        with patch.object(
            brain,
            "interpret_control_intent",
            return_value={
                "intent": "ANSWER_PENDING",
                "confidence": 0.99,
                "normalized_reply": "in 40 minutes",
                "status": "ok",
            },
        ) as semantic_mock, patch.object(
            brain,
            "respond",
            new=create_from_translated_quote,
        ), patch.object(
            runtime_clock,
            "now_utc",
            return_value=frozen,
        ):
            result = ingress.process(second)

        self.assertTrue(result["ok"])
        self.assertEqual(semantic_mock.call_count, 1)
        self.assertEqual(seen["raw"], "in abt fourty mins")
        self.assertEqual(seen["trusted"], "in abt fourty mins")
        self.assertEqual(seen["semantic"], "in 40 minutes")
        self.assertEqual(
            seen["context"]["pending_item"]["item_id"],
            draft["item_id"],
        )

        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status,accumulated_text FROM pending_items WHERE item_id=?",
                (draft["item_id"],),
            ).fetchone()
            reminder = conn.execute(
                """SELECT due_at_utc FROM reminders
                   WHERE source_message_id='sg-live-quote-second'"""
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(state["status"], "RESOLVED")
        self.assertIn("in abt fourty mins", state["accumulated_text"])
        self.assertIsNotNone(reminder)
        self.assertEqual(
            datetime.fromisoformat(reminder["due_at_utc"]),
            datetime(2026, 10, 5, 19, 19, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
