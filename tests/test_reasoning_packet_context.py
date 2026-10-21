"""The blind capability reviewer must see its fixture inputs and runtime policy.

These tests validate exported evidence, not language-model accuracy. Expected
capabilities remain private and the scorer remains unchanged.
"""
import asyncio
import copy
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import behavior_cert
import brain
import human_ai_lab


class ReasoningPacketContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Tool schemas are exercised separately by the real corpus audit. These
        # checks isolate evidence assembly so they need no provider or database.
        with patch.object(human_ai_lab, "_tool_snapshot", AsyncMock(return_value=[])):
            cls.packets = asyncio.run(human_ai_lab.build_packets("all"))

    def packets_for(self, contract_id):
        return [p for p in self.packets if p["_contract_id"] == contract_id]

    def test_receipt_packet_carries_the_same_ocr_as_ingress(self):
        import ingress
        import media

        for cid, source, fixture in (
            ("p1.receipt.ingest.image", "image", "management_receipt"),
            ("p1.receipt.ingest.pdf", "pdf", "payment_pdf"),
        ):
            observed = []

            def capture(_payload):
                observed.append(media.extract_text("unused-fixture-path"))
                return {"ok": True}

            with self.subTest(source=source), patch.object(ingress, "process", side_effect=capture):
                behavior_cert._run_ingress_turn(
                    mid="packet-context-test", conv="fixture-dm", prompt="Log this receipt.",
                    source=source, phone=behavior_cert.HUSBAND,
                    conversation_type="DIRECT_DM", media_fixture=fixture,
                )
                self.assertEqual(observed, [behavior_cert.media_fixture_text(fixture)])
                for packet in self.packets_for(cid):
                    public = human_ai_lab._public_packet(packet)
                    self.assertIn(observed[0], "\n".join(public["media_context"]))
                    self.assertNotIn("state_expectations", public)
                    self.assertNotIn("required_any", public)

    def test_bare_diary_numbers_have_visible_conflict_choices(self):
        packets = self.packets_for("p2.diary.conflict.resolve")
        self.assertEqual({p["prompt"] for p in packets}, {"1", "2", "3"})
        for packet in packets:
            history = packet["conversation_history"]
            self.assertEqual([r["role"] for r in history], ["user", "assistant"])
            self.assertIn("overlaps your work shift", history[-1]["content"])
            self.assertIn("1. Add the appointment and create PLANNED leave.", history[-1]["content"])
            self.assertIn("2. Add the appointment and keep the clash.", history[-1]["content"])
            self.assertIn("3. Cancel this request.", history[-1]["content"])
            self.assertNotIn("resolve_", history[-1]["content"])

    def test_pending_voice_number_has_its_displayed_list(self):
        for packet in self.packets_for("p1.media.voice.pending.retrieve"):
            history = packet["conversation_history"]
            self.assertIn("unresolved voice notes", history[0]["content"])
            self.assertIn("1. Original voice note", history[-1]["content"])
            self.assertIn("waiting for typed clarification", history[-1]["content"])

    def test_numbered_picture_followup_sees_the_fixture_list(self):
        packet = next(p for p in self.packets_for("conv.memory.numbered") if p["conversation_step"] == 2)
        self.assertEqual(packet["conversation_history"][-1]["content"],
                         "Saved pictures\n1. Vinyl — original saved picture.")
        self.assertIn("not observed provider execution", packet["evaluation_scope"])

    def test_memory_lookup_policy_is_actual_runtime_policy(self):
        packet = self.packets_for("conv.memory.restart")[-1]
        policy = packet["runtime_policy"]
        self.assertIn(policy["household_records"], brain.SYSTEM_PROMPT)
        self.assertIn("use the appropriate tool", policy["household_records"])
        self.assertEqual(policy["tool_discovery"], brain.DISCOVERY_TOOL["function"]["description"])
        # The fixture's user history is not deleted to force a desired answer.
        self.assertIn("cobalt", packet["conversation_history"][0]["content"])

    def test_visible_policy_and_document_changes_invalidate_snapshot(self):
        original = self.packets_for("p1.receipt.ingest.image")[:1]
        for field in ("runtime_policy", "media_context"):
            changed = copy.deepcopy(original)
            if field == "runtime_policy":
                changed[0][field]["household_records"] += " Changed policy."
            else:
                changed[0][field][0] += " Changed document."
            self.assertNotEqual(human_ai_lab._corpus_fingerprint(original),
                                human_ai_lab._corpus_fingerprint(changed))

    def test_export_omits_private_contract_labels_and_expectations(self):
        for packet in self.packets:
            public = human_ai_lab._public_packet(packet)
            self.assertFalse(any(key.startswith("_") for key in public))
            self.assertNotIn("required_any", public)
            self.assertNotIn("required_all", public)
            self.assertNotIn("expected_terms", public)
            self.assertNotIn("state_expectations", public)

    def test_original_failed_decision_still_fails_the_unchanged_scorer(self):
        packet = copy.deepcopy(self.packets_for("p1.receipt.ingest.image")[0])
        packet["available_tools"] = [{"name": "log_expense", "description": "", "parameters": {}}]
        decisions = [
            {"_meta": {"corpus_fingerprint": human_ai_lab._corpus_fingerprint([packet])}},
            {"packet_id": packet["packet_id"], "decision": "clarify", "tools": [], "reply_language": "en"},
        ]
        report = human_ai_lab.score_packets([packet], decisions, "all")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["summary"]["failed"], 1)

    def test_compact_review_bundle_is_lossless_and_excludes_private_metadata(self):
        rows = copy.deepcopy(self.packets[:2])
        tool = {"name": "example", "description": "Public schema.", "parameters": {}}
        for row in rows:
            row["available_tools"] = [tool]
        bundle = human_ai_lab.public_review_bundle(rows)
        self.assertEqual(len(bundle["tools_by_ref"]), 1)
        self.assertEqual(human_ai_lab.expand_review_bundle(bundle),
                         [human_ai_lab._public_packet(row) for row in rows])
        self.assertTrue(all("_contract_id" not in row for row in bundle["packets"]))
        changed = copy.deepcopy(bundle)
        changed["packets"][0]["prompt"] += " Changed input."
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            human_ai_lab.expand_review_bundle(changed)


if __name__ == "__main__":
    unittest.main()
