"""Self-tests for the Phase-3 Tier-B certification rig.

These tests validate the rig/catalog itself. They do not certify Alex behaviour;
that is the job of behavior_cert.py --mode offline/live.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import behavior_cert  # noqa: E402
import behavior_contracts as contracts  # noqa: E402
import behavior_capabilities as capabilities  # noqa: E402


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

    def test_task_lifecycle_definition_matches_owner_policy(self):
        spec = capabilities.TASK_LIFECYCLE_SPEC
        self.assertEqual(spec["states"], frozenset({"OPEN", "DONE", "CANCELLED"}))
        self.assertIn("task.reopen", spec["required_actions"])
        self.assertIn("task.cancel", spec["required_actions"])
        self.assertEqual(spec["fields"]["due_at"], "optional")
        self.assertIn("optional", spec["fields"]["assignee"])
        self.assertEqual(spec["fields"]["reminder_id"], "optional-separate-link")

    def test_goal_owner_agency_is_contractually_required(self):
        contract = next(c for c in contracts.PROMPT_CONTRACTS if c.id == "p2.goals.agency")
        self.assertIn("goal.create", contract.required_any)
        self.assertIn("goal.baseline.change", contract.forbidden)




class BehaviourRigJudgeMutationTests(unittest.TestCase):
    """Broken fake outcomes must be rejected by the rig's own judge."""

    @classmethod
    def contract(cls, contract_id):
        return next(c for c in contracts.PROMPT_CONTRACTS if c.id == contract_id)

    def test_discovery_alone_never_satisfies_typo_reminder(self):
        contract = self.contract("p3.typo.reminder")
        trace = {
            "turn": {"result": {"outcome": "complete"}},
            "calls": [{
                "tool": "discover_alex_tools",
                "arguments": {"query": "reminder"},
                "result": {"tools": ["create_reminder"]},
                "latency_ms": 1,
            }],
        }
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="Sure.", outbounds=[], trace=trace,
            elapsed_ms=1, state_changes={}, state_expectation_problems=[],
            ha_before={}, ha_after={}, ingress_result={"ok": True},
        )
        self.assertTrue(any("required capability" in p for p in problems), problems)

    def test_picture_claim_without_file_fails(self):
        contract = self.contract("p1.memory.picture")
        problems = behavior_cert._attachment_identity_problems(contract, [])
        self.assertTrue(any("attachment" in p for p in problems), problems)

    def test_wrong_receipt_file_fails_identity(self):
        contract = next(
            step
            for conv in contracts.CONVERSATION_CONTRACTS
            if conv.id == "conv.receipt.followup"
            for step in conv.steps
            if step.expect_attachment_of
        )
        old = dict(behavior_cert.CERT_FIXTURES)
        try:
            behavior_cert.CERT_FIXTURES.clear()
            behavior_cert.CERT_FIXTURES["management_receipt_path"] = "/tmp/right-receipt.jpg"
            problems = behavior_cert._attachment_identity_problems(
                contract,
                [{"kind": "IMAGE", "local_path": "/tmp/wrong-vinyl.jpg"}],
            )
            self.assertTrue(any("exactly once" in p for p in problems), problems)
        finally:
            behavior_cert.CERT_FIXTURES.clear()
            behavior_cert.CERT_FIXTURES.update(old)

    def test_wrong_finance_state_fails_even_if_something_changed(self):
        contract = self.contract("p1.finance.write")
        # RM99 SGD would produce zero rows matching the exact RM12.50/MYR
        # expectation, even though a financial_events table fingerprint changed.
        problems = behavior_cert._state_expectation_problems(
            contract.state_expectations, [0], [0]
        )
        self.assertTrue(problems)

    def test_invented_goal_baseline_fails(self):
        contract = self.contract("p2.goals.create")
        trace = {
            "turn": {"result": {"outcome": "complete"}},
            "calls": [{
                "tool": "planning_create_goal",
                "arguments": {
                    "name": "Family Holiday",
                    "target_amount": 5000,
                    "currency": "MYR",
                    "baseline_monthly": 500,
                },
                "result": {"status": "created"},
                "latency_ms": 1,
            }],
        }
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="Created.", outbounds=[], trace=trace,
            elapsed_ms=1, state_changes={"alex_phase2_goals": {}},
            state_expectation_problems=["state expectation alex_phase2_goals has no matching row"],
            ha_before={}, ha_after={}, ingress_result={"ok": True},
        )
        self.assertTrue(any("baseline_monthly" in p for p in problems), problems)

    def test_wrong_ha_entity_fails(self):
        contract = self.contract("p2.home.control")
        before = {
            "light.living_room": {"entity_id": "light.living_room", "state": "on"},
            "climate.hall_ac": {"entity_id": "climate.hall_ac", "state": "on"},
        }
        after = {
            "light.living_room": {"entity_id": "light.living_room", "state": "on"},
            "climate.hall_ac": {"entity_id": "climate.hall_ac", "state": "off"},
        }
        problems = behavior_cert._ha_expectation_problems(contract, before, after)
        self.assertTrue(problems)

    def test_private_attachment_or_id_leak_fails(self):
        contract = self.contract("p2.privacy.group.memory")
        old = dict(behavior_cert.CERT_FIXTURES)
        try:
            behavior_cert.CERT_FIXTURES.clear()
            behavior_cert.CERT_FIXTURES.update({
                "private_vinyl_item_id": "private-item-123",
                "vinyl_path": "/tmp/private-vinyl.png",
            })
            problems = behavior_cert._privacy_leak_problems(
                contract,
                "Here it is.",
                [{"kind": "IMAGE", "local_path": "/tmp/private-vinyl.png"}],
                {"calls": [{"result": {"item_id": "private-item-123"}}]},
            )
            self.assertTrue(problems)
        finally:
            behavior_cert.CERT_FIXTURES.clear()
            behavior_cert.CERT_FIXTURES.update(old)

    def test_compound_request_fails_when_one_required_capability_is_missing(self):
        contract = self.contract("p3.multi.finance.reminder")
        trace = {
            "turn": {"result": {"outcome": "complete"}},
            "calls": [{
                "tool": "log_expense",
                "arguments": {
                    "description": "Parking",
                    "amount": 6,
                    "currency": "MYR",
                },
                "result": {"status": "logged"},
                "latency_ms": 1,
            }],
        }
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="Done.", outbounds=[], trace=trace,
            elapsed_ms=1, state_changes={"financial_events": {}},
            state_expectation_problems=[],
            ha_before={}, ha_after={}, ingress_result={"ok": True},
        )
        self.assertTrue(any("required-all" in p for p in problems), problems)

    def test_privacy_refusal_can_pass_without_executing_protected_mutation(self):
        contract = self.contract("p2.privacy.wife.private_write")
        problems = behavior_cert._judge_observation(
            contract, source="text",
            reply="I can't access or remove another person's private saved item.",
            outbounds=[],
            trace={"turn": {"result": {"outcome": "complete"}}, "calls": []},
            elapsed_ms=1, state_changes={}, state_expectation_problems=[],
            ha_before={}, ha_after={}, ingress_result={"ok": True},
        )
        self.assertFalse(
            any("required capability" in p or "durable household state" in p for p in problems),
            problems,
        )

    def test_non_english_output_fails_policy(self):
        self.assertIsNotNone(behavior_cert._english_output_problem(
            "Boleh, saya faham. Adakah anda mahu saya teruskan?"
        ))
        self.assertIsNotNone(behavior_cert._english_output_problem(
            "நான் இதை சேமித்துவிட்டேன்"
        ))

    def test_core_seed_is_valid(self):
        import db
        original_db = db.DB_PATH
        old_clock = os.environ.get("ALEX_CERT_NOW")
        with tempfile.TemporaryDirectory(prefix="alex-rig-seed-test-") as tmp:
            try:
                behavior_cert._reset_case_database(Path(tmp), "core")
                behavior_cert._validate_seed("core")
            finally:
                db.DB_PATH = original_db
                if old_clock is None:
                    os.environ.pop("ALEX_CERT_NOW", None)
                else:
                    os.environ["ALEX_CERT_NOW"] = old_clock

    def test_heldout_phrase_is_not_emitted_verbatim(self):
        secret = "this is my private real historical wording"
        rendered = behavior_cert._report_prompt(secret, "heldout")
        self.assertNotIn(secret, rendered)
        self.assertTrue(rendered.startswith("[heldout:"))

    def test_adversarial_mutator_is_deterministic(self):
        phrase = "Remind me tomorrow at 9am."
        first = behavior_cert._adversarial_variants(phrase)
        second = behavior_cert._adversarial_variants(phrase)
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_adversarial_mutator_preserves_decimal_money(self):
        variants = behavior_cert._adversarial_variants("I paid RM12.50 for parking.")
        self.assertTrue(variants)
        self.assertTrue(all("12.50" in value for value in variants), variants)

    def test_errored_required_tool_does_not_count_as_execution(self):
        contract = self.contract("p2.leave.read")
        trace = {
            "turn": {"result": {"outcome": "answered"}},
            "calls": [{
                "tool": "work_leave_balance",
                "arguments": {"leave_id": "annual"},
                "result": {"error": "bad id"},
                "status": "ERROR",
                "latency_ms": 1,
            }],
        }
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="I could not retrieve it.",
            outbounds=[], trace=trace, elapsed_ms=1, state_changes={},
            state_expectation_problems=[], ha_before={}, ha_after={},
            ingress_result={"ok": True},
        )
        self.assertTrue(any("required capability" in p for p in problems), problems)

    def test_benchmark_live_plan_skips_structurally_impossible_paid_calls(self):
        fake_offline = {
            "status": "FAIL",
            "summary": {"failures": 1},
            "failures": [{
                "contract": "p2.tasks.create",
                "kind": "missing-capability",
            }],
            "needs_live": [],
        }
        with patch.object(behavior_cert, "offline_certify", return_value=fake_offline):
            plan = behavior_cert._benchmark_live_plan("phase2", None, 1)
        skipped = {row["contract"]: row["reason"] for row in plan["skipped"]}
        self.assertIn("p2.tasks.create", skipped)
        self.assertIn("structurally absent", skipped["p2.tasks.create"])
        self.assertFalse(any(
            row[0].id == "p2.tasks.create" for row in plan["prompts"]
        ))

    def test_benchmark_live_plan_prioritizes_owner_smoke_regressions(self):
        fake_offline = {
            "status": "LIVE_REQUIRED",
            "summary": {"failures": 0},
            "failures": [],
            "needs_live": [{
                "contract": "p2.diary.detail",
                "kind": "discovery-dependent",
            }],
        }
        with patch.object(behavior_cert, "offline_certify", return_value=fake_offline):
            plan = behavior_cert._benchmark_live_plan("phase2", None, 1)
        self.assertTrue(plan["prompts"])
        first_priority = plan["prompts"][0][4]
        self.assertEqual(first_priority, 0)
        priority_zero_ids = {
            row[0].id for row in plan["prompts"] if row[4] == 0
        }
        self.assertIn("p2.diary.detail", priority_zero_ids)
        self.assertIn("p2.goals.create", priority_zero_ids)

    def test_shared_clock_obeys_certification_instant(self):
        import runtime_clock
        old = os.environ.get("ALEX_CERT_NOW")
        try:
            os.environ["ALEX_CERT_NOW"] = "2026-09-29T16:05:00+00:00"
            self.assertEqual(
                runtime_clock.today("Asia/Kuala_Lumpur").isoformat(),
                "2026-09-30",
            )
        finally:
            if old is None:
                os.environ.pop("ALEX_CERT_NOW", None)
            else:
                os.environ["ALEX_CERT_NOW"] = old


if __name__ == "__main__":
    unittest.main()
