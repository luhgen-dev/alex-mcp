"""Regressions from the 22 Oct 2026 live Alex smoke test.

These checks exercise deterministic routing/backends against a disposable DB.
They do not claim live-provider language accuracy or WhatsApp rendering.
"""

import asyncio
import base64
import json
from dataclasses import replace

import test_core as core
from test_conversation_reliability import ConversationFixture

brain = core.brain
db = core.db
services = core.services


class LiveSmokeRepairTests(ConversationFixture):
    def tool_names(self, text):
        return {
            row["function"]["name"]
            for row in asyncio.run(brain._tool_specs(text))
        }

    def test_natural_goal_phrases_expose_real_mutators(self):
        cases = {
            "Set a shared goal, name it Alex smoke goal, target RM100.": "planning_create_goal",
            "Change Alex smoke goal to RM150 instead.": "planning_update_goal_target",
            "Make Alex smoke goal active.": "planning_lock_goal",
            "Cancel Alex smoke goal.": "planning_cancel_goal",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, self.tool_names(text))

    def test_monthly_commitments_route_to_bills_not_expense_ledger(self):
        names = self.tool_names("What are my monthly commitments for October?")
        self.assertIn("bills_list", names)

    def test_stash_spend_never_exposes_ordinary_expense_write(self):
        names = self.tool_names("I spent RM10 from v051 test stash on parking.")
        self.assertIn("planning_record_cash_pool_spend", names)
        self.assertNotIn("log_expense", names)

    def test_unspecified_expense_currency_defaults_to_myr(self):
        actor = replace(
            self.actor("live-smoke-myr", "Spent 1.23 on coffee today"),
            action_key="live-smoke-myr-action",
        )
        result = services.log_expense(actor, "coffee", 1.23, "food", None)
        self.assertEqual(result["currency"], "MYR")
        self.assertIn(result["status"], {"logged", "needs_confirmation"})

    def test_plain_finance_read_cannot_return_private_rows(self):
        shared = replace(
            self.actor("live-shared-expense", "Spent RM10 on shared lunch"),
            action_key="live-shared-action",
        )
        services.log_expense(shared, "shared lunch", 10, "food", "MYR")

        private = replace(
            self.actor("live-private-expense", "Spent RM20 privately on coffee"),
            read_scope="private", trusted_text="Spent RM20 privately on coffee",
            action_key="live-private-action",
        )
        services.log_expense(private, "private coffee", 20, "food", "MYR")

        reader = self.actor("live-family-read", "Show me my expenses for October")
        result = services.query_finances(reader)
        self.assertEqual(result["spending_totals"], {"MYR": 10.0})
        self.assertTrue(result["records"])
        self.assertTrue(all(row["scope"] == "family" for row in result["records"]))
        self.assertNotIn("private coffee", json.dumps(result))

    def test_exact_transaction_receipt_never_falls_back_to_other_media(self):
        actor = self.actor("live-receipt-owner", "receipt fixture")
        first_media = core.media.save_media(
            actor.source_message_id, "IMAGE", "image/jpeg",
            base64.b64encode(b"correct receipt bytes").decode(),
        )
        first_actor = replace(
            actor, media_ids=(first_media,), action_key="live-receipt-first",
        )
        first = services.log_expense(
            first_actor, "car payment", 1657, "vehicle_loan", "MYR"
        )

        other_actor = self.actor("live-other-receipt", "other receipt fixture")
        other_media = core.media.save_media(
            other_actor.source_message_id, "IMAGE", "image/jpeg",
            base64.b64encode(b"wrong receipt bytes").decode(),
        )
        other_actor = replace(
            other_actor, media_ids=(other_media,), action_key="live-receipt-other",
        )
        services.log_expense(
            other_actor, "blender", 279, "home", "MYR"
        )

        exact = services.get_receipt_for_event(actor, first["event_id"])
        self.assertEqual(exact["status"], "found")
        self.assertEqual(len(exact["_attachments"]), 1)
        self.assertEqual(
            exact["_attachments"][0]["path"],
            core.media.get_media(first_media)["local_path"],
        )
        self.assertNotEqual(
            exact["_attachments"][0]["path"],
            core.media.get_media(other_media)["local_path"],
        )

    def test_numbered_finance_selection_is_frozen_and_detailed(self):
        actor = replace(
            self.actor("live-finance-list", "Spent RM12 on lunch"),
            action_key="live-finance-list-action",
        )
        event = services.log_expense(actor, "lunch", 12, "food", "MYR")
        reader = self.actor("live-finance-list-read", "Show my expenses")
        ledger = services.query_finances(reader)
        selection_id = services.store_finance_selection(
            reader, [row["event_id"] for row in ledger["records"]]
        )
        detail = services.resolve_numbered_choice(
            reader, 1, selection_kind="FINANCE", selection_id=selection_id
        )
        self.assertEqual(detail["event_id"], event["event_id"])
        self.assertEqual(detail["amount"], 12.0)
        self.assertIn("logged_at_local", detail)
        self.assertFalse(detail["receipt_available"])

    def test_undated_and_private_plans_do_not_pollute_family_day_agenda(self):
        family = replace(
            self.actor("live-plan-family", "Create a shared plan"),
            action_key="live-plan-family-action",
        )
        core.phase2.create_plan(family, "Undated family draft", shared=True)

        private = replace(
            self.actor("live-plan-private", "Create a private plan"),
            read_scope="private", action_key="live-plan-private-action",
        )
        core.phase2.create_plan(private, "Private dated draft",
                                "2026-10-22T18:00:00", shared=False)

        dated = replace(
            self.actor("live-plan-dated", "Create dated shared plan"),
            action_key="live-plan-dated-action",
        )
        core.phase2.create_plan(dated, "Dated family draft",
                                "2026-10-22T19:00:00", shared=True)

        reader = self.actor("live-agenda-read", "What's my agenda tomorrow?")
        result = core.phase2.get_agenda(reader, "2026-10-22", "2026-10-22")
        self.assertEqual([row["title"] for row in result["plans"]],
                         ["Dated family draft"])

    def test_diary_can_be_resolved_by_name_and_date_for_cancel(self):
        creator = replace(
            self.actor("live-diary-create", "Create shared appointment"),
            action_key="live-diary-create-action",
        )
        created = core.phase2.add_diary_event(
            creator, "alex diary test", "2026-10-22T20:00:00",
            "2026-10-22T21:00:00", shared=True,
        )
        actor = self.actor(
            "live-diary-cancel", 'cancel "alex diary test" on 22 October'
        )
        resolved = core.phase2.resolve_diary_reference(
            actor, diary_name="alex diary test", on_date="2026-10-22"
        )
        self.assertEqual(resolved, created["diary_id"])
        result = core.phase2.update_diary_event(actor, resolved, status="CANCELLED")
        self.assertEqual(result["status"], "updated")

    def test_due_claimed_reminder_quote_is_valid_release_identity(self):
        self.assertEqual(
            core.ingress._quoted_reminder_id_for_claim_release({
                "context_kind": "REMINDER_INITIAL",
                "context_id": "reminder-123",
            }),
            "reminder-123",
        )
        self.assertTrue(core.ingress._return_claimed_reminder_to_family_command(
            "I can’t do this",
            {"context_kind": "REMINDER_INITIAL", "context_id": "reminder-123"},
        ))

    def test_goal_cancel_preserves_contributions(self):
        actor = self.actor("live-goal-create", "Create shared goal")
        created = core.mcp_server.planning_create_goal(
            "Alex smoke goal", 100, actor, shared=True
        )
        core.mcp_server.planning_record_goal_contribution(
            20, actor, goal_id=created["goal_id"], contribution_date="2026-10-22"
        )
        cancelled = core.mcp_server.planning_cancel_goal(
            actor, goal_id=created["goal_id"]
        )
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertTrue(cancelled["history_preserved"])
        conn = db.connect()
        try:
            amount = conn.execute(
                """SELECT COALESCE(SUM(amount_minor),0)
                   FROM alex_phase2_goal_contributions WHERE goal_id=?""",
                (created["goal_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(amount, 2000)

    def test_clear_shopping_quantity_and_purchase_work_by_name(self):
        creator = replace(
            self.actor("live-shopping-add", "Add detergent"),
            action_key="live-shopping-add-action",
        )
        services.add_shopping_item(creator, "detergent")
        actor = self.actor(
            "live-shopping-update", "I actually need to buy 2 detergents"
        )
        changed = core.mcp_server.update_shopping_item(
            actor, item_name="detergent", quantity="2"
        )
        self.assertEqual(changed["quantity"], "2")
        bought = core.mcp_server.update_shopping_item(
            actor, item_name="detergent", status="purchased"
        )
        self.assertEqual(bought["state"], "PURCHASED")
        active = services.list_shopping_items(actor)
        self.assertEqual(active["items"], [])

    def test_voice_note_can_be_saved_as_original_media_after_typed_instruction(self):
        voice_actor = self.actor("live-voice-source", "")
        media_id = core.media.save_media(
            voice_actor.source_message_id, "AUDIO", "audio/ogg",
            base64.b64encode(b"synthetic voice note").decode(),
        )
        pending = db.create_pending_item(
            voice_actor, "VOICE", media_id, note="voice fixture"
        )
        typed = self.actor(
            "live-voice-save", "Save this as voicenote smoke test"
        )
        saved = services.save_existing_media_item(
            typed, "voicenote smoke test", "voicenote smoke test",
            pending["media_id"], shared=True,
        )
        db.resolve_pending_item(
            pending["item_id"], typed.user_id, typed.source_message_id
        )
        found = services.search_saved_items(typed, "voicenote smoke test")
        self.assertEqual(found["matches"][0]["kind"], "voice note")
        original = services.get_saved_item(typed, saved["item_id"])
        self.assertEqual(original["_attachments"][0]["mime_type"], "audio/ogg")

    def test_asset_warranty_enriches_same_physical_asset_not_duplicate(self):
        first = core.phase2_library.create_asset(
            "v05 test appliance", "+60111111111",
            category="Air fryer", brand="Philips", serial_number="V05-12345",
        )
        second = core.phase2_library.create_asset(
            "v05 test appliance", "+60111111111",
            warranty_end="2027-09-27",
        )
        self.assertEqual(second["status"], "enriched_existing")
        self.assertEqual(second["asset_id"], first["asset_id"])
        rows = core.phase2_library.list_assets(
            "+60111111111", requested_scope="family"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["warranty_end"], "2027-09-27")

    def test_leave_profile_accepts_entitled_and_used_format(self):
        rows = core.profile_config.normalize_options({
            "leave_balances": [{
                "id": "annual_leave", "owner": "husband",
                "visibility": "private", "name": "Annual leave",
                "entitlement_days": 18, "used_days": 4,
                "as_of_date": "2025-02-13", "active": True,
            }]
        })
        payload = rows[0]["payload"]
        self.assertEqual(payload["entitlement_days"], 18)
        self.assertEqual(payload["used_days"], 4)
        self.assertEqual(payload["remaining_days"], 14)

    def test_send_that_export_reuses_exact_active_finance_payload(self):
        actor = self.actor("live-export-read", "Show my food expenses")
        first_creator = replace(
            self.actor("live-export-first", "Spent RM15 on food"),
            action_key="live-export-first-action",
        )
        services.log_expense(first_creator, "food one", 15, "food", "MYR")
        shown = core.mcp_server.query_finances(
            actor, category="food", start_date="2026-10-01",
            end_date="2026-10-31",
        )
        self.assertEqual(shown["count"], 1)

        later = replace(
            self.actor("live-export-later", "Spent RM20 on food"),
            action_key="live-export-later-action",
        )
        services.log_expense(later, "food two", 20, "food", "MYR")

        exported = core.mcp_server.report_export(
            "json", actor, report_type="finance"
        )
        self.assertEqual(exported["record_count"], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
