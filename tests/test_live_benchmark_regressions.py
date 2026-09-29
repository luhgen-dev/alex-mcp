import asyncio
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import behavior_cert  # noqa: E402
import brain  # noqa: E402
import phase2  # noqa: E402
import phase2_intent  # noqa: E402


def _names(specs):
    return {spec["function"]["name"] for spec in specs}


class LiveBenchmarkRegressionTests(unittest.TestCase):
    def tools(self, text):
        return _names(asyncio.run(brain._tool_specs(text)))

    def test_adversarial_mutator_preserves_decimal_and_time_punctuation(self):
        variants = behavior_cert._adversarial_variants(
            "Log RM12.50 parking tomorrow at 09:30."
        )
        self.assertTrue(variants)
        self.assertTrue(all("12.50" in v for v in variants))
        self.assertTrue(all("09:30" in v for v in variants))

    def test_natural_calendar_dates_are_deterministic(self):
        ref = "2026-09-29"
        for phrase in ("1 October 2026", "October 1 2026", "1 October"):
            got = phase2.resolve_date_range(phrase, "Asia/Kuala_Lumpur", ref)
            self.assertEqual(got, {
                "start_date": "2026-10-01",
                "end_date": "2026-10-01",
            }, phrase)
        got = phase2.resolve_date_range("next Thursday", "Asia/Kuala_Lumpur", ref)
        self.assertEqual(got["start_date"], "2026-10-01")

    def test_tamil_is_not_misclassified_as_pure_chat(self):
        phrase = "நாளைக்கு காலை 9 மணிக்கு மின்சார பில் கட்ட நினைவூட்டு"
        names = self.tools(phrase)
        self.assertIn(brain.DISCOVERY_TOOL_NAME, names)
        self.assertGreater(len(names), 0)

    def test_live_smoke_read_routes_are_exposed(self):
        cases = (
            ("Is the hall AC on right now?", {"ha_find_entities", "ha_get_state"}),
            ("Why did Alex fail recently?", {"recent_failures"}),
            ("What are you monitoring for me?", {"monitor_list"}),
            ("What appliances or assets have I saved?", {"asset_list"}),
            ("What reserves or allowances do I have?", {"planning_list_reserves"}),
            ("What's my safe monthly baseline?", {"planning_baseline"}),
            ("What time should I leave home for work tomorrow?", {"work_departure_plan"}),
            ("Calculate RM593.62 minus RM200.", {"calculate"}),
            ("What time is my dentist appointment on 1 October?", {"get_agenda", "get_agenda_range"}),
        )
        for phrase, expected in cases:
            names = self.tools(phrase)
            self.assertTrue(names & expected, (phrase, names, expected))

    def test_plan_read_and_refine_do_not_offer_duplicate_creation(self):
        read = self.tools("Show me the Malacca day-trip draft.")
        self.assertIn("list_plans", read)
        self.assertNotIn("create_plan", read)

        refine = self.tools(
            "For the Malacca plan, keep the date open, make it kid-friendly and aim to be home by 9pm."
        )
        self.assertIn("update_plan", refine)
        self.assertIn("list_plans", refine)
        self.assertNotIn("create_plan", refine)

    def test_goal_create_is_not_activation(self):
        names = self.tools("Create an unlocked Family Holiday Savings goal for RM5,000.")
        self.assertIn("planning_create_goal", names)
        self.assertNotIn("planning_lock_goal", names)

        schemas = behavior_cert._mcp_schemas()
        required = set(schemas["planning_create_goal"]["schema"].get("required") or [])
        self.assertNotIn("baseline_monthly", required)
        self.assertNotIn("status", schemas["planning_create_goal"]["schema"].get("properties", {}))

    def test_compound_finance_reminder_and_cash_income_are_classified(self):
        compound = phase2_intent.classify_write_intent(
            "Log RM6 parking and remind me today at 5pm to renew parking."
        )
        self.assertEqual(compound["status"], "compound")
        self.assertEqual(set(compound["intents"]), {"EXPENSE", "REMINDER"})

        incoming = phase2_intent.classify_write_intent("I got RM400 OT today.")
        self.assertEqual(incoming["intent"], "CASH")
        self.assertFalse(incoming["requires_clarification"])


if __name__ == "__main__":
    unittest.main()
