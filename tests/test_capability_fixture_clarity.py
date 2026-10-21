"""Missing-detail fixtures must not reward invented writes or erase coverage.

These checks exercise the existing judges and public evidence assembly. They
do not claim provider accuracy or certify Tamil reminder execution.
"""
import asyncio
import copy
import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import test_core  # Configure disposable household paths before application imports.
import behavior_cert
import behavior_contracts
import human_ai_lab
import phase2
import phase2_home


# These original prompts permit a safe lookup before asking for missing details.
# Keep them outside the blind first-decision corpus: its existing clarify-only
# expectation cannot represent both a direct question and a preliminary read.
# The existing final-observation judge can check both without being relaxed.
ORIGINAL_BILL_PROMPTS = (
    "Record the TNB bill as paid.",
    "I paid the electricity bill; record the payment.",
)
ORIGINAL_AVAILABILITY_PROMPTS = (
    "Check my availability Thursday evening.",
    "Check whether my spouse is available Thursday evening.",
)
ORIGINAL_UNBOUND_HANDOFF_PROMPTS = (
    "Push this reminder to Priya.",
    "Ask Priya to take this.",
    "Hand this reminder to my wife.",
)
ORIGINAL_DEPARTURE_PROMPTS = (
    "What time should I leave home for work tomorrow?",
    "Plan my departure for tomorrow's shift.",
)


class CapabilityFixtureClarityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(human_ai_lab, "_tool_snapshot", AsyncMock(return_value=[])):
            cls.packets = asyncio.run(human_ai_lab.build_packets("all"))

    @staticmethod
    def contract(contract_id):
        return next(c for c in behavior_contracts.PROMPT_CONTRACTS if c.id == contract_id)

    def missing_bill_contract(self):
        return replace(
            self.contract("p2.bills.record"),
            id="regression.bills.missing_amount", variants=ORIGINAL_BILL_PROMPTS,
            required_any=behavior_contracts._fs("bills_list"),
            expect_clarification=True,
            unchanged_tables=("alex_phase2_obligation_instances",),
        )

    def assert_lookup_or_clarification(self, contract, read_tool, question):
        for prompt in contract.variants:
            for calls in ([], [{"tool": read_tool, "status": "OK"}]):
                with self.subTest(prompt=prompt, calls=calls):
                    problems = behavior_cert._judge_observation(
                        contract, source="text", reply=question, outbounds=[],
                        trace={"calls": calls}, elapsed_ms=1, state_changes={},
                        state_expectation_problems=[], ha_before={}, ha_after={},
                        ingress_result={"ok": True},
                    )
                    self.assertEqual(problems, [])

    def score(self, contract_id, kind, selected_tools=()):
        packet = copy.deepcopy(next(
            p for p in self.packets if p["_contract_id"] == contract_id
        ))
        packet["available_tools"] = [
            {"name": name, "description": "Synthetic judge input.", "parameters": {}}
            for name in selected_tools
        ]
        decisions = [
            {"_meta": {"corpus_fingerprint": human_ai_lab._corpus_fingerprint([packet])}},
            {
                "packet_id": packet["packet_id"], "decision": kind,
                "tools": list(selected_tools), "reply_language": "en",
            },
        ]
        return human_ai_lab.score_packets([packet], decisions, "all")

    def test_underspecified_originals_remain_clarification_cases(self):
        originals = {
            "p2.home.automation.missing_details": (
                "Prepare a Home Assistant automation for the hall light; don't deploy it.",
            ),
            "p3.typo.reminder.ambiguous": (
                "alx plz remidn me tmrw 9 pay elctrcity",
                "alex set remnder tmr 9 electricity",
            ),
            "p3.language.tamil.reminder.ambiguous": (
                "நாளை 9 மணிக்கு மின்சார கட்டணம் செலுத்த நினைவூட்டு",
            ),
        }
        for contract_id, phrases in originals.items():
            with self.subTest(contract_id=contract_id):
                contract = self.contract(contract_id)
                self.assertTrue(set(phrases) <= set(contract.variants))
                self.assertTrue(contract.expect_clarification)
                self.assertEqual(self.score(contract_id, "clarify")["status"], "PASS")

    def test_missing_details_cannot_be_scored_as_completed_actions(self):
        cases = (
            ("p2.home.automation.missing_details", "ha_draft_automation"),
            ("p3.typo.reminder.ambiguous", "create_reminder"),
            ("p3.language.tamil.reminder.ambiguous", "create_reminder"),
        )
        for contract_id, tool in cases:
            with self.subTest(contract_id=contract_id):
                report = self.score(contract_id, "tools", [tool])
                self.assertEqual(report["status"], "FAIL")
                self.assertIn("expected a clarification decision", report["results"][0]["problems"])

    def test_complete_positive_requests_still_require_the_real_capability(self):
        cases = (
            ("p2.bills.record", "bills_record_payment"),
            ("p2.home.automation", "ha_draft_automation"),
            ("p3.typo.reminder", "create_reminder"),
            ("p3.language.tamil.reminder", "create_reminder"),
        )
        for contract_id, tool in cases:
            with self.subTest(contract_id=contract_id):
                self.assertEqual(self.score(contract_id, "clarify")["status"], "FAIL")
                self.assertEqual(self.score(contract_id, "tools", [tool])["status"], "PASS")
                if tool == "create_reminder":
                    expectation = self.contract(contract_id).state_expectations[0]
                    self.assertEqual((expectation.table, expectation.count, expectation.delta),
                                     ("reminders", 1, 1))

    def test_ambiguous_reminder_cannot_write_even_if_reply_is_a_question(self):
        for contract_id in ("p3.typo.reminder.ambiguous", "p3.language.tamil.reminder.ambiguous"):
            with self.subTest(contract_id=contract_id):
                contract = self.contract(contract_id)
                state_problems = behavior_cert._state_expectation_problems(
                    contract.state_expectations, [0], [1]
                )
                problems = behavior_cert._judge_observation(
                    contract, source="text", reply="Did you mean 9am or 9pm?",
                    outbounds=[], trace={"calls": [{"tool": "create_reminder", "status": "OK"}]},
                    elapsed_ms=1, state_changes={"reminders": {}},
                    state_expectation_problems=state_problems,
                    ha_before={}, ha_after={}, ingress_result={"ok": True},
                )
                self.assertTrue(state_problems)
                self.assertIn("table reminders changed but contract requires it unchanged", problems)
                self.assertIn("clarification turn performed a persistent mutation", problems)

    def test_missing_bill_amount_cannot_change_the_payment_ledger(self):
        contract = self.missing_bill_contract()
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="How much did you pay?", outbounds=[],
            trace={"calls": [{"tool": "bills_record_payment", "status": "OK"}]},
            elapsed_ms=1, state_changes={"alex_phase2_obligation_instances": {}},
            state_expectation_problems=[], ha_before={}, ha_after={},
            ingress_result={"ok": True},
        )
        self.assertIn("table alex_phase2_obligation_instances changed but contract requires it unchanged", problems)
        self.assertIn("clarification turn performed a persistent mutation", problems)

    def test_original_bill_requests_allow_read_then_ask_without_payment(self):
        self.assert_lookup_or_clarification(
            self.missing_bill_contract(), "bills_list", "How much did you pay?",
        )

    def test_original_unbound_handoff_allows_lookup_but_cannot_transfer(self):
        contract = replace(
            self.contract("p1.reminder.claim.handoff"),
            id="regression.reminder.handoff.unbound",
            variants=ORIGINAL_UNBOUND_HANDOFF_PROMPTS,
            required_any=behavior_contracts._fs("list_reminders"),
            expect_clarification=True,
            unchanged_tables=("reminders", "reminder_handoffs"),
        )
        self.assert_lookup_or_clarification(
            contract, "list_reminders", "Which claimed family reminder do you mean?",
        )
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="Which reminder do you mean?", outbounds=[],
            trace={"calls": [{"tool": "handoff_reminder_claim", "status": "OK"}]},
            elapsed_ms=1, state_changes={"reminder_handoffs": {}},
            state_expectation_problems=[], ha_before={}, ha_after={},
            ingress_result={"ok": True},
        )
        self.assertIn("table reminder_handoffs changed but contract requires it unchanged", problems)
        self.assertIn("clarification turn performed a persistent mutation", problems)

    def test_original_broad_availability_does_not_supply_invented_hours(self):
        contract = replace(
            self.contract("p2.availability.self"),
            id="regression.availability.broad",
            variants=ORIGINAL_AVAILABILITY_PROMPTS,
            required_any=behavior_contracts._fs("get_agenda_range"),
            expect_clarification=True,
        )
        self.assert_lookup_or_clarification(
            contract, "get_agenda_range", "What start and end time should I check?",
        )
        actor = SimpleNamespace(
            conversation_type="DIRECT_DM", user_id="USR_HUSBAND",
            timezone="Asia/Kuala_Lumpur",
        )
        for check in (phase2.check_my_availability, phase2.check_spouse_availability):
            with self.subTest(check=check.__name__), patch.object(phase2, "connect") as connect:
                with self.assertRaises(ValueError):
                    check(actor, "Thursday evening")
                connect.assert_not_called()

    def test_original_time_only_reschedule_allows_lookup_or_date_question(self):
        contract = replace(
            self.contract("p1.reminder.update"),
            id="regression.reminder.reschedule.missing_date",
            variants=("Move my dentist reminder to 3pm.",),
            expect_clarification=True, unchanged_tables=("reminders",),
        )
        self.assert_lookup_or_clarification(
            contract, "list_reminders", "Which day should I move it to at 3 PM?",
        )
        problems = behavior_cert._judge_observation(
            contract, source="text", reply="Which day do you mean?", outbounds=[],
            trace={"calls": [{"tool": "update_reminder", "status": "OK"}]},
            elapsed_ms=1, state_changes={"reminders": {}},
            state_expectation_problems=[], ha_before={}, ha_after={},
            ingress_result={"ok": True},
        )
        self.assertIn("table reminders changed but contract requires it unchanged", problems)
        self.assertIn("clarification turn performed a persistent mutation", problems)

    def test_original_departure_requests_allow_read_then_travel_question(self):
        contract = replace(
            self.contract("p2.work.departure"),
            id="regression.work.departure.missing_travel",
            variants=ORIGINAL_DEPARTURE_PROMPTS, expect_clarification=True,
        )
        for read_tool in ("work_schedule", "work_departure_plan"):
            self.assert_lookup_or_clarification(
                contract, read_tool, "How many minutes does your commute take?",
            )

    def test_incomplete_automation_cannot_operate_the_mentioned_light(self):
        contract = self.contract("p2.home.automation.missing_details")
        before = {
            "light.hall": {"state": "off"},
            "light.living_room": {"state": "on"},
            "climate.hall_ac": {"state": "on"},
        }
        after = copy.deepcopy(before)
        after["light.hall"]["state"] = "on"
        problems = behavior_cert._ha_expectation_problems(contract, before, after)
        self.assertTrue(problems)
        self.assertTrue(any("light.hall" in problem for problem in problems))

    def test_handoff_has_a_visible_claimed_family_reminder_antecedent(self):
        for packet in self.packets:
            if packet["_contract_id"] != "p1.reminder.claim.handoff":
                continue
            public = human_ai_lab._public_packet(packet)
            history = public["conversation_history"]
            self.assertEqual([turn["role"] for turn in history], ["user", "assistant"])
            self.assertIn("Take out bins", history[-1]["content"])
            self.assertIn("Family Shared", history[-1]["content"])
            self.assertIn("open and currently claimed by you", history[-1]["content"])
            self.assertNotIn("handoff_reminder_claim", history[-1]["content"])
            self.assertIn("not observed provider execution", public["evaluation_scope"])
            self.assertNotIn("_contract_id", public)

    def test_departure_without_travel_does_not_answer_the_shift_question(self):
        with (
            patch.object(phase2_home, "_active_roster") as roster,
            patch.object(phase2_home.phase2_work, "effective_shift") as shift,
            patch.object(phase2_home.phase2_schedule, "leave_plans_for_date") as leave,
        ):
            result = phase2_home.shift_departure_plan("2026-10-22", "synthetic-phone")
        self.assertEqual(result, {
            "date": "2026-10-22", "status": "NEEDS_TRAVEL_ESTIMATE", "action_taken": False,
        })
        roster.assert_not_called()
        shift.assert_not_called()
        leave.assert_not_called()


if __name__ == "__main__":
    unittest.main()
