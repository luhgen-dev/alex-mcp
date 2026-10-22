"""Locked additions/regressions from the 22 Oct 2026 live Alex review."""

import base64
import json
from dataclasses import replace

import test_core as core
from test_conversation_reliability import ConversationFixture

import custom_ledgers
import daily_brief
import listening


class LockedAdditionTests(ConversationFixture):
    def test_custom_ledger_money_requires_semantic_kind(self):
        actor = replace(
            self.actor("ledger-create", "Create a private drone tracker"),
            read_scope="private", trusted_text="Create a private drone tracker",
            action_key="ledger-create-action",
        )
        created = custom_ledgers.create_ledger(
            actor, "Drone tracker", "Track drone money", shared=False
        )
        ambiguous = replace(
            self.actor("ledger-entry-a", "Put RM500 in drone tracker"),
            read_scope="private", trusted_text="Put RM500 in drone tracker",
            action_key="ledger-entry-a-action",
        )
        result = custom_ledgers.add_entry(
            ambiguous, created["ledger_id"], "Transfer to drone account",
            amount=500,
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(result["nothing_written"])

        confirmed = replace(
            self.actor("ledger-entry-b", "That is drone savings RM500"),
            read_scope="private", trusted_text="That is drone savings RM500",
            action_key="ledger-entry-b-action",
        )
        saved = custom_ledgers.add_entry(
            confirmed, created["ledger_id"], "Drone savings",
            amount=500, entry_kind="SAVINGS",
        )
        self.assertEqual(saved["entry_kind"], "SAVINGS")
        rows = custom_ledgers.query_entries(confirmed, created["ledger_id"])
        self.assertEqual(rows["entries"][0]["amount"], 500.0)

    def test_task_detail_preserves_original_instruction_and_history(self):
        actor = replace(
            self.actor("task-source", "Add a task called Alex smoke for tomorrow"),
            action_key="task-source-action",
        )
        created = core.phase2.create_task(
            actor, "Alex smoke", due_local="2026-10-22"
        )
        detail = core.phase2.task_detail(actor, created["task_id"])
        self.assertEqual(detail["title"], "Alex smoke")
        self.assertIn("Alex smoke", detail["original_instruction"])
        self.assertEqual(detail["due_date_local"], "2026-10-22")
        self.assertTrue(detail["history"])

    def test_date_only_task_reminder_requires_heads_up_or_task_time(self):
        creator = replace(
            self.actor("task-rem-create", "Add alex smoke task for 22 October"),
            action_key="task-rem-create-action",
        )
        created = core.phase2.create_task(
            creator, "alex smoke", due_local="2026-10-22"
        )
        reminder_actor = replace(
            self.actor("task-rem-ask", "For alex smoke task remind me in 2 minutes"),
            action_key="task-rem-ask-action",
        )
        result = core.mcp_server.create_task_reminder(
            reminder_actor,
            "2026-10-22T03:00:00",
            task_id=created["task_id"],
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("heads-up", result["clarification_question"])

    def test_saved_video_can_round_trip_as_original(self):
        source = self.actor("video-source", "")
        media_id = core.media.save_media(
            source.source_message_id, "VIDEO", "video/mp4",
            base64.b64encode(b"synthetic video").decode(),
        )
        typed = self.actor("video-save", "Save this as video smoke test")
        saved = core.services.save_existing_media_item(
            typed, "video smoke test", "video smoke test",
            media_id, shared=True,
        )
        found = core.services.search_saved_items(typed, "video smoke test")
        self.assertEqual(found["matches"][0]["kind"], "video")
        original = core.services.get_saved_item(typed, saved["item_id"])
        self.assertEqual(original["_attachments"][0]["kind"], "VIDEO")

    def test_listening_mode_captures_between_start_and_stop(self):
        actor = replace(
            self.actor("listen-start", "@Alex"),
            conversation_id="120363000000@g.us",
            conversation_type="GROUP",
            read_scope="family",
        )
        started = listening.start(actor)
        self.assertEqual(started["status"], "listening")
        captured = listening.capture(
            actor, "listen-msg-1", "Maybe Greece in June",
        )
        self.assertEqual(captured["status"], "captured")
        context = listening.context(actor.conversation_id)
        self.assertEqual(context["messages"][0]["text"], "Maybe Greece in June")
        stopped = listening.stop(actor)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(len(stopped["messages"]), 1)

    def test_owner_work_profile_import_persists_permanent_roster_and_leave(self):
        actor = self.actor("profile-import", "Import my work profile")
        payload = {
            "roster": {
                "id": "permanent_roster", "name": "Permanent roster",
                "cycle_start": "2026-02-02", "pattern": "evening,morning",
                "day_start": "08:30", "day_end": "17:30",
                "evening_start": "17:30", "evening_end": "02:00",
                "active": True,
            },
            "overtime": {
                "id": "permanent_ot", "name": "Permanent OT",
                "currency": "MYR",
                "morning_pre_hours": 2, "morning_pre_start": "06:30",
                "morning_weekend_standard_start": "05:30",
                "morning_weekend_standard_end": "17:00",
                "morning_weekend_low_start": "06:30",
                "morning_weekend_low_end": "16:00",
                "evening_post_hours": 3.5,
                "evening_post_start": "02:00", "evening_post_end": "05:30",
                "evening_weekend_days": "saturday",
                "evening_weekend_standard_start": "17:30",
                "evening_weekend_standard_end": "05:30",
                "evening_weekend_low_start": "17:30",
                "evening_weekend_low_end": "23:30",
                "active": True,
            },
            "leave": [{
                "id": "annual_leave", "name": "Annual leave",
                "entitlement_days": 18, "used_days": 4,
                "as_of_date": "2026-02-05", "active": True,
            }],
        }
        result = core.profile_config.import_user_work_profile(
            actor, json.dumps(payload)
        )
        self.assertEqual(result["status"], "imported")
        shift = core.phase2_work.effective_shift(
            "2026-02-05", actor.phone, actor.conversation_type
        )
        self.assertEqual(shift["shift"], "evening")
        leave = core.phase2_work.leave_balance(
            "annual_leave", actor.phone, actor.conversation_type,
            "2026-02-05",
        )
        self.assertEqual(leave["entitlement_days"], 18)
        self.assertEqual(leave["used_days"], 4)
        self.assertEqual(leave["remaining_days"], 14)

    def test_daily_brief_uses_permanent_roster_profile(self):
        core.profile_config.sync_options({
            "roster_profiles": [{
                "id": "permanent_roster", "owner": "husband",
                "visibility": "private", "name": "Permanent roster",
                "cycle_start": "2026-02-02", "pattern": "evening,morning",
                "day_start": "08:30", "day_end": "17:30",
                "evening_start": "17:30", "evening_end": "02:00",
                "active": True,
            }],
            "leave_balances": [{
                "id": "annual_leave", "owner": "husband",
                "visibility": "private", "name": "Annual leave",
                "entitlement_days": 18, "used_days": 4,
                "as_of_date": "2026-02-05", "active": True,
            }],
        })
        actor = self.actor("brief-read", "What's in for me today?")
        result = daily_brief.build(actor, "2026-02-05")
        self.assertIn("Afternoon shift", result["text"])
        self.assertIn("No planned leave recorded today", result["text"])

    def test_commitment_amount_alone_never_identifies_bill(self):
        core.profile_config.sync_options({
            "recurring_payments": [{
                "id": "bike", "owner": "husband", "visibility": "private",
                "name": "Motorbike instalment", "amount_type": "fixed",
                "amount": 621, "currency": "MYR", "due_day": 6,
                "frequency": "monthly", "category": "loan",
                "auto_pay": False, "effective_from": "2026-01-01",
                "match_aliases": "BSNC Leasing", "account_last4": "1234",
                "active": True,
            }]
        })
        actor = replace(
            self.actor("bill-scope", "private bill check"),
            read_scope="private", trusted_text="private bill check",
        )
        core.phase2_finance.ensure_obligation_instances(
            "2026-10", actor.phone, actor.conversation_type, "private"
        )
        amount_only = core.phase2_finance.resolve_obligation_from_evidence(
            "", 621, "2026-10-06", actor.phone, actor.conversation_type,
            requested_scope="private",
        )
        self.assertEqual(amount_only["status"], "NOT_FOUND")
        matched = core.phase2_finance.resolve_obligation_from_evidence(
            "BSNC Leasing", 621, "2026-10-06",
            actor.phone, actor.conversation_type,
            requested_scope="private", account_reference="xx1234",
        )
        self.assertEqual(matched["status"], "MATCH")


if __name__ == "__main__":
    import unittest
    unittest.main()
