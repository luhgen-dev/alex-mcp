"""Focused repairs from the first 369/388 blind capability review.

Real routing, MCP and disposable database checks are separated from model
accuracy: scripted provider choices prove plumbing, not natural understanding.
The public review must be rerun independently after these policy/schema changes.
"""

import asyncio
import base64
import json
import unittest
from dataclasses import replace

import test_core as core
from test_conversation_reliability import ConversationFixture

import human_ai_lab


class ReviewDisagreementRepairsTests(ConversationFixture):
    def test_named_memory_deletion_exposes_lookup_and_exact_id_action(self):
        for prompt in (
            "Delete the private cobalt memory.",
            "Remove the saved cobalt note.",
            "Forget the saved cobalt memory.",
        ):
            with self.subTest(prompt=prompt):
                specs = asyncio.run(core.brain._tool_specs(prompt))
                tools = {row["function"]["name"]: row["function"] for row in specs}
                self.assertTrue({"search_saved_items", "remove_saved_item"} <= tools.keys())
                self.assertIn("item_id", tools["remove_saved_item"]["parameters"]["required"])

    def test_named_picture_search_then_get_returns_original_in_one_turn(self):
        seed = self.actor("review-picture-seed", "Save this picture of my vinyl setup.")
        media_id = core.media.save_media(
            seed.source_message_id, "IMAGE", "image/jpeg",
            base64.b64encode(b"synthetic original vinyl image").decode(),
        )
        seed = replace(seed, media_ids=(media_id,), action_key="review-picture-save")
        saved = core.services.save_item(seed, "Vinyl setup", "Saved turntable picture")
        reader = self.actor("review-picture-read", "Find that picture of my vinyl setup.")

        search = core.services.search_saved_items(reader, "vinyl", kind="picture")
        self.assertEqual([row["item_id"] for row in search["matches"]], [saved["item_id"]])
        self.assertTrue(search["matches"][0]["has_original"])
        self.assertNotIn("_attachments", search)

        (reply, attachments), requests = self.scripted([
            {"tool": "search_saved_items", "args": {"query": "vinyl", "kind": "picture"}},
            {"tool": "get_saved_item", "args": {"item_id": saved["item_id"]}},
            {"content": "Here it is."},
        ], actor=reader, text=reader.trusted_text)
        self.assertEqual(reply, "Here it is.")
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["path"], core.media.get_media(media_id)["local_path"])
        lookup_result = next(
            message for message in requests[1]["messages"] if message.get("role") == "tool"
        )
        self.assertIn(saved["item_id"], lookup_result["content"])

    def test_private_memory_lookup_and_removal_never_cross_owner_boundary(self):
        husband = replace(
            self.actor("review-private-h", "Save a private cobalt memory."),
            read_scope="private", action_key="review-private-h-save",
        )
        wife = replace(
            self.actor("review-private-w", "Save a private cobalt memory.", spouse=True),
            read_scope="private", action_key="review-private-w-save",
        )
        foreign = core.services.save_item(husband, "Cobalt", "Husband private fixture")
        owned = core.services.save_item(wife, "Cobalt", "Wife private fixture")
        deleter = replace(
            self.actor("review-private-delete", "Delete the private cobalt memory.", spouse=True),
            read_scope="private",
        )
        found = core.services.search_saved_items(deleter, "cobalt")
        self.assertEqual([row["item_id"] for row in found["matches"]], [owned["item_id"]])
        self.assertNotIn("Husband private fixture", json.dumps(found))
        with self.assertRaises(PermissionError):
            core.services.remove_saved_item(deleter, foreign["item_id"])
        result = core.services.remove_saved_item(deleter, found["matches"][0]["item_id"])
        self.assertEqual(result["status"], "removed")
        self.assertEqual(core.services.search_saved_items(deleter, "cobalt")["count"], 0)
        self.assertEqual(core.services.get_saved_item(husband, foreign["item_id"])["status"], "found")

    def test_empty_cash_pool_default_and_explicit_currency_create_no_funding(self):
        actor = self.actor("review-empty-pool", "Create a stash called Holiday Buffer.")
        default = core.mcp_server.planning_create_cash_pool("Holiday Buffer", actor)
        explicit = core.mcp_server.planning_create_cash_pool("Singapore Buffer", actor, currency="SGD")
        for result, currency in ((default, "MYR"), (explicit, "SGD")):
            with self.subTest(currency=currency):
                self.assertEqual(result["status"], "created")
                self.assertEqual(result["currency"], currency)
                self.assertEqual(result["balance"], 0)
                self.assertFalse(result["opening_balance_applied"])
        conn = core.db.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alex_phase2_cash_pools").fetchone()[0], 2)
            for table in (
                "alex_phase2_cash_events", "alex_phase2_cash_pool_adjustments",
                "alex_phase2_cash_pool_allocations", "financial_events",
            ):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        finally:
            conn.close()

    def test_availability_checks_overlap_and_requires_real_datetime_arguments(self):
        owner = replace(
            self.actor("review-diary-seed", "Add a private appointment on 1 October from 5pm to 8pm."),
            read_scope="private", action_key="review-diary-save",
        )
        core.phase2.add_diary_event(owner, "Private appointment", "2026-10-01T17:00:00", "2026-10-01T20:00:00")
        busy = core.mcp_server.check_my_availability("2026-10-01T18:00:00", owner, "2026-10-01T19:00:00")
        self.assertEqual(busy["availability"], "busy")
        self.assertTrue(busy["has_diary_conflict"])
        self.assertFalse(busy["may_be_posted_to_group"])
        free = core.mcp_server.check_my_availability("2026-10-01T21:00:00", owner, "2026-10-01T22:00:00")
        self.assertEqual(free["availability"], "no_conflict_found")
        with self.assertRaisesRegex(core.ToolError, "Invalid isoformat"):
            core.mcp_server.check_my_availability("Thursday evening", owner)

    def test_public_policy_is_the_same_text_sent_to_the_runtime_provider(self):
        actor = self.actor("review-runtime-policy", "Maybe we need coffee.")
        request = self.capture_request(actor, actor.trusted_text)
        system_messages = "\n".join(
            row["content"] for row in request["messages"] if row["role"] == "system"
        )
        packet = human_ai_lab._packet_base(
            "review-policy-packet", "synthetic-internal-label", "p1", "shopping",
            "text", actor.trusted_text, actor.conversation_type, "husband",
        )
        public = human_ai_lab._public_packet(packet)
        policy = public["runtime_policy"]
        for name in (
            "tentative_shopping", "private_scope", "empty_cash_pool",
            "automation_drafts", "financial_currency",
        ):
            with self.subTest(policy=name):
                self.assertIn(policy[name], system_messages)
        self.assertNotIn("synthetic-internal-label", json.dumps(public))
        self.assertNotIn("required_any", public)
        self.assertNotIn("state_expectations", public)

    def test_automation_request_exposes_draft_without_device_control(self):
        specs = asyncio.run(core.brain._tool_specs("Create an automation to turn on the hall light at sunset."))
        names = {row["function"]["name"] for row in specs}
        self.assertIn("ha_draft_automation", names)
        self.assertNotIn("ha_control", names)


if __name__ == "__main__":
    unittest.main()
