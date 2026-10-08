"""v0.5.36: structured semantic router and owner-selectable shadow/live modes."""

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_v0534_ai_routing as base

brain = base.brain
shadow_router = base.shadow_router
Settings = base.Settings
ON = base.ON


def run(coro):
    return asyncio.run(coro)


def semantic_choice(**overrides):
    value = {
        "tools": ["create_reminder"],
        "intent": "CREATE_REMINDER",
        "reference": "ACTIVE",
        "slots": {
            "date_text": "tomorrow",
            "time_text": "7 pm",
            "relative_minutes": None,
            "item_numbers": [],
            "target": "GROUP",
            "scope": "FAMILY_SHARED",
            "amount": None,
            "currency": "UNSPECIFIED",
            "name": "bring in the laundry",
            "query": None,
            "action": "remind",
        },
        "confidence": 0.96,
        "needs_clarification": False,
        "reason": "The user is completing a family reminder.",
        "model": "gpt-test",
        "latency_ms": 12,
    }
    value.update(overrides)
    return value


class SemanticRoutingModeTests(base.V0534Base):
    def test_legacy_on_is_live_but_shadow_never_changes_live_tools(self):
        self.sign_in_fixture()
        self.assertEqual(shadow_router.routing_mode(ON), "live")
        self.assertTrue(shadow_router.live_enabled(ON))

        shadow = replace(ON, ai_routing="shadow")
        self.assertEqual(shadow_router.routing_mode(shadow), "shadow")
        self.assertFalse(shadow_router.live_enabled(shadow))

        off = replace(ON, ai_routing="off")
        self.assertEqual(shadow_router.routing_mode(off), "off")
        self.assertFalse(shadow_router.live_enabled(off))

    def test_structured_schema_is_strict_and_tool_names_are_real_enum(self):
        schema = shadow_router._schema(["create_reminder", "list_reminders"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["tools"]["items"]["enum"],
            ["create_reminder", "list_reminders"],
        )
        for key in ("intent", "reference", "slots", "confidence",
                    "needs_clarification", "reason"):
            self.assertIn(key, schema["required"])
        slots = schema["properties"]["slots"]
        self.assertFalse(slots["additionalProperties"])
        self.assertIn("relative_minutes", slots["required"])
        self.assertIn("target", slots["required"])

    def test_parser_keeps_grounded_semantics_and_sanitizes_bad_values(self):
        raw = semantic_choice()
        raw["tools"] = ["create_reminder", "made_up", "create_reminder"]
        raw["intent"] = "create reminder!!"
        raw["reference"] = "quoted"
        raw["slots"]["relative_minutes"] = 40
        raw["slots"]["item_numbers"] = [1, "3", 3, -1, 1000]
        raw["confidence"] = 1.7
        parsed = shadow_router.parse_choice(
            json.dumps(raw), {"create_reminder", "list_reminders"}
        )
        self.assertEqual(parsed["tools"], ["create_reminder"])
        self.assertEqual(parsed["intent"], "CREATE_REMINDER")
        self.assertEqual(parsed["reference"], "QUOTED")
        self.assertEqual(parsed["slots"]["relative_minutes"], 40)
        self.assertEqual(parsed["slots"]["item_numbers"], [1, 3])
        self.assertEqual(parsed["confidence"], 1.0)

    def test_semantic_hint_is_high_confidence_only_and_explicitly_non_authoritative(self):
        high = shadow_router.semantic_hint(semantic_choice())
        self.assertIn("non-authoritative", high)
        self.assertIn("original user-authored", high)
        self.assertIn('"intent":"CREATE_REMINDER"', high)

        self.assertEqual(
            shadow_router.semantic_hint(semantic_choice(confidence=0.4)), ""
        )
        self.assertEqual(
            shadow_router.semantic_hint(semantic_choice(needs_clarification=True)), ""
        )

    def test_live_route_can_help_language_without_bypassing_write_gate(self):
        keyword = self.specs("list_reminders")

        async def fake_live(*_a, **_k):
            return semantic_choice(
                tools=["create_reminder", "list_reminders"],
                intent="CREATE_REMINDER",
            )

        read_trace = {}
        with patch.object(shadow_router, "live_route", new=fake_live):
            read_tools = run(brain._apply_ai_route(
                read_trace, keyword, "what reminders do i have", None, [], None
            ))

        read_names = [x["function"]["name"] for x in read_tools]
        # The semantic model cannot turn a read question into a write, and its
        # conflicting structured hint is withheld as soon as safety filters it.
        self.assertNotIn("create_reminder", read_names)
        self.assertIn("list_reminders", read_names)
        self.assertNotIn("semantic_hint", read_trace["ai_route"])

        write_trace = {}
        with patch.object(shadow_router, "live_route", new=fake_live):
            write_tools = run(brain._apply_ai_route(
                write_trace, keyword,
                "set a reminder tomorrow at 7 pm for laundry", None, [], None
            ))
        write_names = [x["function"]["name"] for x in write_tools]
        self.assertIn("create_reminder", write_names)
        self.assertIn("semantic_hint", write_trace["ai_route"])
        self.assertIn("non-authoritative", write_trace["ai_route"]["semantic_hint"])

    def test_structured_predictions_are_persisted_for_shadow_review(self):
        shadow_router.record({
            "source_message_id": "v0536-shadow",
            "conversation_type": "DIRECT_DM",
            "message_snippet": "tmr ard 7 remind me laundry",
            "keyword_tools": ["list_reminders"],
            "called_tools": ["create_reminder"],
            "live_outcome": "answered",
            "shadow_tools": ["create_reminder"],
            "shadow_clarify": False,
            "shadow_reason": "reminder",
            "status": "ok",
            "model": "gpt-test",
            "latency_ms": 21,
            "ai_routed": 0,
            "semantic_intent": "CREATE_REMINDER",
            "semantic_reference": "NONE",
            "semantic_slots": {"date_text": "tomorrow", "time_text": "7 pm"},
            "semantic_confidence": 0.94,
            "semantic_mode": "shadow",
        })
        report = shadow_router.summary(7)
        self.assertEqual(report["recent_semantics"][0]["intent"], "CREATE_REMINDER")
        self.assertEqual(report["recent_semantics"][0]["mode"], "shadow")
        self.assertEqual(report["recent_semantics"][0]["slots"]["date_text"], "tomorrow")


if __name__ == "__main__":
    unittest.main()
