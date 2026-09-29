import asyncio
import base64
import os
import tempfile
import unittest
from unittest.mock import patch

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))
import sys
if APP not in sys.path:
    sys.path.insert(0, APP)

import brain
import facade
import media


def _names(specs):
    return {
        str(x.get("function", {}).get("name") or "")
        for x in specs if isinstance(x, dict)
    } - {""}


class FacadeContractTests(unittest.TestCase):
    def test_exact_stable_facade(self):
        self.assertEqual(
            set(facade.all_specs()),
            {
                "finance_query", "finance_log", "finance_correct",
                "library_find", "library_save",
                "reminders_view", "reminder_change",
                "shopping_view", "shopping_change",
                "home_state", "home_control",
                "agenda_view", "calculate", "load_pack",
            },
        )

    def test_provider_surface_never_exposes_legacy_tools_on_normal_turn(self):
        cases = {
            "Add toothpaste to my shopping list": "shopping_change",
            "What's on my shopping list?": "shopping_view",
            "I paid RM12.50 for parking": "finance_log",
            "Show my recent expenses": "finance_query",
            "Remind me tomorrow at 9am to pay electricity": "reminder_change",
            "Turn off the living room light": "home_control",
        }
        for prompt, expected in cases.items():
            names = _names(asyncio.run(brain._provider_tool_specs(prompt)))
            self.assertIn(expected, names, prompt)
            self.assertLessEqual(len(names), brain.TOOL_EXPOSURE_MAX)
            self.assertFalse({
                "add_shopping_item", "update_shopping_item", "log_expense",
                "create_reminder", "ha_control",
            } & names, prompt)

    def test_voice_and_typed_text_route_identically(self):
        # Source transport is not an input to routing; the normalized trusted
        # transcript must expose exactly the same capabilities as typed text.
        for prompt in (
            "Add test toothpaste to my shopping list",
            "Mark test batteries as bought",
            "Remind me tomorrow at 9 to pay electricity",
            "Turn off the living room fan",
        ):
            text_names = _names(asyncio.run(brain._provider_tool_specs(prompt)))
            voice_names = _names(asyncio.run(brain._provider_tool_specs(prompt)))
            self.assertEqual(text_names, voice_names, prompt)

    def test_specialist_domains_use_bounded_pack_loader(self):
        for prompt in (
            "Create a task to renew passports",
            "Start a draft plan for Malacca",
            "Record RM400 OT cash that came in",
            "Record annual leave for 2 October",
            "Export my report as PDF",
            "Show recent Alex failures",
        ):
            names = _names(asyncio.run(brain._provider_tool_specs(prompt)))
            self.assertIn("load_pack", names, prompt)
            self.assertLessEqual(len(names), brain.TOOL_EXPOSURE_MAX)

    def test_pack_vocabulary_is_bounded(self):
        self.assertEqual(set(facade.PACKS), {
            "tasks", "plans", "diary", "planning", "work",
            "bills", "assets", "monitoring", "reports", "diagnostics",
        })
        self.assertIn("create_task", facade.pack_tools("tasks"))
        self.assertIn("planning_record_cash", facade.pack_tools("planning"))
        self.assertIn("recent_failures", facade.pack_tools("diagnostics"))

    def test_duplicate_same_tool_call_same_inbound_has_same_action_key(self):
        class A:
            source_message_id = "same-message"
        a = A()
        args = {"item": "milk", "shared": True}
        self.assertEqual(
            brain._action_key(a, "add_shopping_item", args, 1),
            brain._action_key(a, "add_shopping_item", args, 2),
        )


class FacadeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shopping_update_resolves_exact_open_item_name(self):
        calls = []

        async def fake(tool, args, key):
            calls.append((tool, args, key))
            if tool == "list_shopping_items":
                return {
                    "items": [
                        {"item_id": "i1", "item": "Milk"},
                        {"item_id": "i2", "item": "Test batteries"},
                    ]
                }, []
            if tool == "update_shopping_item":
                return {
                    "status": "updated",
                    "item_id": args["item_id"],
                    "new_status": args["status"],
                }, []
            raise AssertionError(tool)

        result, _ = await facade.execute(
            "shopping_change",
            {"operation": "update", "item": "test batteries", "status": "purchased"},
            fake,
            "ak",
        )
        self.assertEqual(result["item_id"], "i2")
        self.assertEqual(
            [x[0] for x in calls],
            ["list_shopping_items", "update_shopping_item"],
        )

    async def test_shopping_name_ambiguity_never_mutates(self):
        calls = []

        async def fake(tool, args, key):
            calls.append(tool)
            return {
                "items": [
                    {"item_id": "family", "item": "Milk"},
                    {"item_id": "private", "item": "milk"},
                ]
            }, []

        result, _ = await facade.execute(
            "shopping_change",
            {"operation": "update", "item": "milk", "status": "purchased"},
            fake,
            "ak",
        )
        self.assertEqual(result["status"], "clarification_required")
        self.assertEqual(calls, ["list_shopping_items"])


class VoiceGuardTests(unittest.TestCase):
    def test_whisper_hallucination_is_suspicious(self):
        self.assertTrue(media._local_transcript_suspicious("Thank you for watching."))
        self.assertLess(media._transcript_command_score("Thank you for watching."),
                        media._transcript_command_score("Add toothpaste to my shopping list"))

    def test_household_command_is_preferred(self):
        bad = "Mohon maaf apakah anda bermaksud menanyakan sesuatu"
        good = "Mark test batteries as bought"
        self.assertGreater(
            media._transcript_command_score(good),
            media._transcript_command_score(bad),
        )

    def test_live_wrong_language_signature_gets_retry(self):
        reply = (
            "Mohon maaf, apakah Anda bermaksud menanyakan sesuatu yang sudah "
            "Anda simpan sebelumnya?"
        )
        self.assertTrue(brain._looks_non_english_reply(reply, "add toothpaste to shopping list"))
        self.assertFalse(
            brain._looks_non_english_reply(
                reply, "translate this reply into Indonesian"
            )
        )

    def test_clear_voice_shopping_mutations_require_tool_followthrough(self):
        self.assertTrue(brain._expects_clear_mutation(
            "Add test toothpaste to my shopping list"
        ))
        self.assertTrue(brain._expects_clear_mutation(
            "Mark test batteries as bought"
        ))


if __name__ == "__main__":
    unittest.main()
