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
        current = self._claim_actor("sg-relative", "in abt 40 mins")
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
                current, draft, "in abt 40 mins"
            )

        self.assertTrue(continues)
        self.assertEqual(hint, "in 40 minutes")

        interpreted = replace(
            current,
            reminder_context_text=(
                "Remind me to water the plants\nin abt 40 mins"
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
        ):
            seen["semantic"] = actor.reminder_semantic_text
            seen["context"] = quoted_context
            return (
                "Should I set that reminder for about 40 minutes from now?",
                [],
            )

        second = self._payload(
            "sg-flow-second",
            "roughly forty minits from now",
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
                "roughly forty minits from now"
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
        ):
            self.assertEqual(
                actor.reminder_semantic_text,
                "in 40 minutes",
            )
            services.create_reminder(
                with_action_key(actor, "sg-create-action"),
                "check the mailbox",
                "2026-10-06T08:40:00+08:00",
            )
            return ("OK. Reminder set.", [])

        second = self._payload(
            "sg-create-second",
            "in abt 40 mins",
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


if __name__ == "__main__":
    unittest.main()
