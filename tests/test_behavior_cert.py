"""Self-tests for the Phase-3 Tier-B certification rig.

These tests validate the rig/catalog itself. They do not certify Alex behaviour;
that is the job of behavior_cert.py --mode offline/live.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import behavior_cert  # noqa: E402
import behavior_contracts as contracts  # noqa: E402


class BehaviourRigCatalogTests(unittest.TestCase):
    def test_catalog_is_complete_and_coherent(self):
        report = behavior_cert.catalog_audit()
        self.assertEqual(report["status"], "PASS", report["failures"])

    def test_all_three_phases_are_covered(self):
        phases = {c.phase for c in contracts.PROMPT_CONTRACTS}
        phases |= {c.phase for c in contracts.CONVERSATION_CONTRACTS}
        phases |= {g.phase for g in contracts.MANUAL_GATES}
        self.assertEqual(phases, {"phase1", "phase2", "phase3"})

    def test_language_contracts_are_not_single_phrase_smoke_tests(self):
        self.assertTrue(contracts.PROMPT_CONTRACTS)
        self.assertTrue(all(len(c.variants) >= 2 for c in contracts.PROMPT_CONTRACTS))
        self.assertGreaterEqual(
            sum(len(c.variants) * len(c.sources) for c in contracts.PROMPT_CONTRACTS),
            80,
        )

    def test_known_non_simulatable_edges_are_explicit(self):
        ids = {g.id for g in contracts.MANUAL_GATES}
        self.assertTrue({
            "manual.voice.transport",
            "manual.media.delivery",
            "manual.group.mention",
            "manual.group.reply",
            "manual.typing.latency",
            "manual.reminder.delivery",
            "manual.ha.physical",
            "manual.session.qr",
        } <= ids)

    def test_task_semantics_are_contractually_required(self):
        ids = {c.id for c in contracts.PROMPT_CONTRACTS}
        self.assertIn("p2.tasks.create", ids)
        self.assertIn("p2.tasks.read", ids)

    def test_goal_owner_agency_is_contractually_required(self):
        contract = next(c for c in contracts.PROMPT_CONTRACTS if c.id == "p2.goals.agency")
        self.assertIn("planning_create_goal", contract.required_any)
        self.assertIn("planning_change_goal_baseline", contract.forbidden)


if __name__ == "__main__":
    unittest.main()
