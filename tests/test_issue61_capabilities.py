"""Capability-backed discovery and stable-domain repair contract checks."""
import unittest
import test_core as core
import capability_menu
import behavior_capabilities as capabilities
import behavior_contracts as contracts


class CapabilityRepairTests(unittest.TestCase):
    def test_menu_omits_disabled_capabilities_and_preserves_numbers(self):
        menu = capability_menu.enabled_sections({"list_shopping_items", "list_discussions"})
        self.assertEqual(set(menu), {5, 9})
        self.assertIn("discussion history", menu[9][1])
        self.assertNotIn("actions", menu[9][1])
        self.assertEqual(capability_menu.enabled_sections(set()), {})

    def test_real_registered_surface_is_covered_in_menu(self):
        menu = capability_menu.enabled_sections()
        self.assertEqual(set(menu), set(range(1, 11)))
        self.assertIn("leave balances", menu[1][1])
        self.assertIn("actions and progress", menu[9][1])

    def test_journal_tools_have_real_public_contracts(self):
        for tool in ("list_discussions", "get_discussion", "update_discussion", "update_discussion_action", "link_discussion_action", "claim_reminder"):
            cap = capabilities.normalize_capability(tool)
            self.assertTrue(any(cap in c.required_any for c in contracts.PROMPT_CONTRACTS), tool)
            self.assertNotIn(tool, capabilities.TOOL_COVERAGE_EXEMPTIONS)

    def test_target_update_contract_forbids_duplicate_create(self):
        contract = next(c for c in contracts.PROMPT_CONTRACTS if c.id == "p2.goals.target.update")
        self.assertIn("goal.create", contract.forbidden)

    def test_claim_contract_uses_real_quote_and_owned_state(self):
        contract = next(c for c in contracts.CONVERSATION_CONTRACTS if c.id == "conv.repair.reminder.quote.claim")
        self.assertTrue(contract.steps[1].quote_previous)
        self.assertIn("reminder.claim.take", contract.steps[1].required_any)
        self.assertTrue(any(("claimed_by_user_id", "$HUSBAND") in e.fields for e in contract.steps[1].state_expectations))
