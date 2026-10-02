import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from dataclasses import replace

TEST_DIR = tempfile.mkdtemp(prefix="alex-mcp-tests-")
OPTIONS = os.path.join(TEST_DIR, "options.json")
os.environ["ALEX_DATA_DIR"] = TEST_DIR
os.environ["ALEX_OPTIONS_PATH"] = OPTIONS

with open(OPTIONS, "w", encoding="utf-8") as f:
    f.write("""{
      "ai_provider": "grok",
      "xai_api_key": "",
      "husband_phone": "+60111111111",
      "wife_phone": "+60222222222",
      "timezone": "Asia/Kuala_Lumpur",
      "ocr_enabled": false,
      "context_turns": 8
    }""")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import db
import media
import services
import ha
import ha_mobile
import phase2
import phase2_intent
import scope_policy
import diagnostics
import ingress
import profile_config
import phase2_finance
import phase2_work
import phase2_library
import phase2_delegation
import phase2_monitor
import phase2_presence
import phase2_reports
import phase2_home
import brain
import mcp_server
import runtime_clock
import scheduler
import outbox
from context import use_actor, with_action_key
from config import Settings
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp_server import mcp


class AlexCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.initialize()
        profile_config.ensure_schema()
        phase2_finance.ensure_schema()
        phase2_work.ensure_schema()
        phase2_library.ensure_schema()
        phase2_delegation.ensure_schema()

    def setUp(self):
        conn = db.connect()
        try:
            for table in (
                "alex_phase2_asset_documents", "alex_phase2_assets",
                "alex_phase2_cash_allocations", "alex_phase2_cash_pool_allocations",
                "alex_phase2_cash_pool_adjustments",
                "alex_phase2_goal_contributions", "alex_phase2_goal_period_targets",
                "alex_phase2_goal_baseline_versions", "alex_phase2_obligation_instances",
                "alex_phase2_evidence_facts", "alex_phase2_cash_events",
                "alex_phase2_cash_pools", "alex_phase2_plan_reserves", "alex_phase2_goals",
                "alex_phase2_work_events", "alex_phase2_delegations",
                "alex_profile_config_versions",
                "tool_audit", "tool_execution_claims", "ai_usage", "diagnostic_runs", "monitor_notifications",
                "ha_notification_outbox", "outbound_messages", "conversation_turns", "selection_sets", "pending_selection_sets", "active_report_contexts",
                "reminder_handoffs", "reminder_claim_events", "reminder_events",
                "task_reminder_links", "task_events", "tasks",
                "diary_reminder_links", "plan_diary_links", "schedule_conflicts", "diary_events", "plans",
                "leave_records", "work_roster", "cashflow_baselines",
                "event_media_links", "financial_event_corrections", "financial_events",
                "saved_items", "shopping_items", "reminders", "savings_goals", "money_buckets",
                "leave_state", "pending_items", "media_objects", "inbound_messages",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()

    def claim(self, mid, phone, text=""):
        db.claim_inbound({
            "message_id": mid,
            "provider": "WHATSAPP",
            "conversation_id": phone.replace("+", "") + "@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": phone,
            "text": text,
        })

    def actor(self, mid, phone, media_ids=None):
        return db.resolve_actor(
            phone, phone.replace("+", "") + "@s.whatsapp.net",
            "DIRECT_DM", mid, media_ids or [],
        )

    def sync_phase2_fixture(self):
        return profile_config.sync_options({
            "income_profiles": [{
                "id": "salary", "owner": "husband", "visibility": "private",
                "name": "Base salary", "amount": 10000, "currency": "MYR",
                "income_class": "fixed", "frequency": "monthly",
                "payday_day": 25, "active": True,
            }],
            "roster_profiles": [{
                "id": "main_roster", "owner": "husband", "visibility": "private",
                "name": "Alternating roster", "cycle_start": "2026-09-21",
                "pattern": "evening,morning", "day_start": "07:45",
                "day_end": "16:15", "evening_start": "16:30",
                "evening_end": "01:00", "active": True,
            }],
            "overtime_profiles": [{
                "id": "standard_ot", "owner": "husband", "visibility": "private",
                "name": "Standard overtime", "currency": "MYR",
                "morning_pre_hours": 2, "morning_pre_start": "05:45",
                "morning_weekend_standard_start": "04:45",
                "morning_weekend_standard_end": "16:45",
                "morning_weekend_low_start": "05:45",
                "morning_weekend_low_end": "15:45",
                "evening_post_hours": 2.75, "evening_post_start": "01:00",
                "evening_post_end": "04:15", "evening_weekend_days": "saturday",
                "evening_weekend_standard_start": "16:15",
                "evening_weekend_standard_end": "04:15",
                "evening_weekend_low_start": "16:15",
                "evening_weekend_low_end": "23:59",
                "sunday_override_allowed": True, "payout_days": "7,12",
                "rate_formula": "", "active": True,
            }],
            "leave_balances": [
                {"id": "annual_leave", "owner": "husband", "visibility": "private",
                 "name": "Annual leave", "entitlement_days": 25, "remaining_days": 2,
                 "as_of_date": "2026-09-26", "active": True},
                {"id": "medical_leave", "owner": "husband", "visibility": "private",
                 "name": "Medical leave", "entitlement_days": 26, "remaining_days": 8,
                 "as_of_date": "2026-09-26", "active": True},
            ],
            "recurring_payments": [{
                "id": "family_bill", "owner": "family", "visibility": "family",
                "name": "Synthetic family bill", "amount_type": "fixed",
                "amount": 1000, "currency": "MYR", "due_day": 5,
                "frequency": "monthly", "active": True,
            }],
            "account_aliases": [{
                "id": "europe", "owner": "husband", "visibility": "private",
                "label": "Europe Savings", "purpose": "Europe", "active": True,
            }],
            "reminder_preferences": [{
                "id": "default", "owner": "husband", "visibility": "private",
                "name": "Default reminder policy", "bill_days_before": 3,
                "follow_up_after_hours": 24, "quiet_start": "22:00",
                "quiet_end": "06:00", "presence_aware": True, "active": True,
            }],
            "presence_mappings": [{
                "id": "home", "owner": "husband", "visibility": "private",
                "name": "Home presence", "person_entity": "person.husband",
                "phone_tracker_entity": "", "home_zone": "home", "active": True,
            }],
        })

    def test_private_and_shared_space_isolation(self):
        self.claim("p1", "+60111111111", "coffee")
        husband = with_action_key(self.actor("p1", "+60111111111"), "a-private")
        result = services.log_expense(husband, "Coffee", 8.50, "food", currency="MYR")
        self.assertEqual(result["space"], "HUSBAND_PVT")

        self.claim("q-wife", "+60222222222", "how much")
        wife = self.actor("q-wife", "+60222222222")
        self.assertEqual(services.query_finances(wife)["count"], 0)

        self.claim("s1", "+60111111111", "TNB")
        husband_shared = with_action_key(self.actor("s1", "+60111111111"), "a-shared")
        shared = services.log_expense(husband_shared, "TNB electricity bill", 120.00, None, currency="MYR")
        self.assertEqual(shared["space"], "FAMILY_SHARED")
        self.assertEqual(services.query_finances(wife, search="TNB")["count"], 1)

    def test_idempotent_action_key(self):
        self.claim("i1", "+60111111111", "lunch 10")
        actor = with_action_key(self.actor("i1", "+60111111111"), "same-action")
        first = services.log_expense(actor, "Lunch", 10, "food", currency="MYR")
        second = services.log_expense(actor, "Lunch", 10, "food", currency="MYR")
        self.assertEqual(first["event_id"], second["event_id"])
        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM financial_events WHERE action_key='same-action'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_append_only_correction(self):
        self.claim("c1", "+60111111111", "lunch")
        original_actor = with_action_key(self.actor("c1", "+60111111111"), "create-c")
        original = services.log_expense(original_actor, "Lunch", 12, "food", currency="MYR")

        self.claim("c2", "+60111111111", "actually 15")
        correction_actor = with_action_key(self.actor("c2", "+60111111111"), "correct-c")
        corrected = services.correct_expense(
            correction_actor, original["event_id"], amount=15, reason="user correction"
        )
        self.assertNotEqual(original["event_id"], corrected["event_id"])

        conn = db.connect()
        try:
            old = conn.execute("SELECT status FROM financial_events WHERE event_id=?",
                               (original["event_id"],)).fetchone()[0]
            new = conn.execute("SELECT status,amount_minor FROM financial_events WHERE event_id=?",
                               (corrected["event_id"],)).fetchone()
            self.assertEqual(old, "SUPERSEDED")
            self.assertEqual(new["status"], "ACTIVE")
            self.assertEqual(new["amount_minor"], 1500)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM financial_event_corrections WHERE parent_event_id=?",
                (original["event_id"],)
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_recurring_receipts_remain_distinct_and_retrievable(self):
        raw = base64.b64encode(b"same-looking-recurring-bank-receipt").decode("ascii")
        mids = []
        for idx, ref in ((1, "REF001"), (2, "REF002")):
            mid = f"r{idx}"
            self.claim(mid, "+60111111111", "")
            media_id = media.save_media(mid, "IMAGE", "image/jpeg", raw)
            mids.append(media_id)
            actor = with_action_key(self.actor(mid, "+60111111111", [media_id]), f"receipt-{idx}")
            result = services.log_expense(
                actor, "TNB electricity bill", 100, "utilities", currency="MYR", reference=ref
            )
            self.assertTrue(result["receipt_saved"])

        self.assertNotEqual(mids[0], mids[1])
        self.claim("rq", "+60111111111", "receipt")
        actor = self.actor("rq", "+60111111111")
        found = services.find_receipts(actor, amount=100, limit=10)
        self.assertEqual(len(found["matches"]), 2)
        self.assertEqual({x["media_id"] for x in found["matches"]}, set(mids))
        receipt = services.get_receipt(actor, mids[0])
        self.assertEqual(receipt["status"], "found")
        self.assertTrue(os.path.isfile(receipt["_attachments"][0]["path"]))

    def test_explicit_memory_is_separate(self):
        self.claim("m1", "+60111111111", "save this")
        actor = with_action_key(self.actor("m1", "+60111111111"), "save-memory")
        result = services.save_item(actor, "Parking spot", "Level 4, Bay 18", "car")
        self.assertEqual(result["status"], "saved")
        found = services.search_saved_items(actor, "Bay 18")
        self.assertEqual(len(found["matches"]), 1)

    def test_pending_clarification_and_money_buckets(self):
        self.claim("pend1", "+60111111111", "paid someone 50")
        actor = with_action_key(self.actor("pend1", "+60111111111"), "pending-action")
        pending = services.log_expense(actor, "Transfer to person", 50, None, currency="MYR", reference="REF9")
        self.assertEqual(pending["status"], "needs_confirmation")

        listed = services.list_pending_expenses(actor)
        self.assertEqual(len(listed["pending"]), 1)
        self.assertEqual(listed["pending"][0]["event_id"], pending["event_id"])

        confirmed = services.confirm_expense(actor, pending["event_id"], True, category="groceries")
        self.assertEqual(confirmed["status"], "confirmed")

        saved = services.set_money_bucket(actor, "UK trip", 2500, notes="User-set allocation")
        self.assertEqual(saved["amount"], 2500)
        buckets = services.list_money_buckets(actor)
        self.assertEqual(len(buckets["buckets"]), 1)
        self.assertEqual(buckets["buckets"][0]["name"], "UK trip")

    def test_auto_stt_prefers_local_whisper(self):
        self.claim("voice1", "+60111111111", "")
        raw = base64.b64encode(b"dummy-voice-bytes").decode("ascii")
        media_id = media.save_media("voice1", "AUDIO", "audio/ogg", raw)
        with patch.object(media, "_local_whisper", return_value="வணக்கம் alex") as local, \
             patch.object(media, "_gemini_stt") as gemini, \
             patch.object(media, "_openai_stt") as openai_stt, \
             patch.object(media, "_xai_stt") as xai:
            text = media.transcribe_audio(media_id)
        self.assertEqual(text, "வணக்கம் alex")
        local.assert_called_once()
        gemini.assert_not_called()
        openai_stt.assert_not_called()
        xai.assert_not_called()
        self.assertEqual(media.get_media(media_id)["transcript_text"], "வணக்கம் alex")

    def test_auto_stt_fails_closed_on_uncertain_mutating_command_without_cloud_opt_in(self):
        self.claim("voice-uncertain", "+60111111111", "")
        raw = base64.b64encode(b"dummy-voice-bytes").decode("ascii")
        media_id = media.save_media("voice-uncertain", "AUDIO", "audio/ogg", raw)
        settings = Settings(
            stt_provider="auto",
            cloud_stt_rescue_enabled=False,
            whisper_model="base",
            gemini_api_key="configured-chat-key",
        )

        def local_side_effect(_path, _model, language="auto"):
            return {
                "auto": "add milk to my shopping list",
                "en": "what time is it today",
                "ta": "நாளைக்கு வானிலை எப்படி",
            }[language]

        with patch.object(media, "get_settings", return_value=settings), \
             patch.object(media, "_local_whisper", side_effect=local_side_effect) as local, \
             patch.object(media, "_gemini_stt") as gemini, \
             patch.object(media, "_openai_stt") as openai_stt, \
             patch.object(media, "_xai_stt") as xai:
            with self.assertRaises(media.VoiceTranscriptionUncertain):
                media.transcribe_audio(media_id)

        self.assertEqual(local.call_count, 3)
        gemini.assert_not_called()
        openai_stt.assert_not_called()
        xai.assert_not_called()
        stored = media.get_media(media_id)
        self.assertIn('"confidence": "uncertain"', stored["transcript_meta_json"])

    def test_auto_stt_cloud_rescue_requires_explicit_opt_in(self):
        self.claim("voice-cloud-rescue", "+60111111111", "")
        raw = base64.b64encode(b"dummy-voice-bytes").decode("ascii")
        media_id = media.save_media("voice-cloud-rescue", "AUDIO", "audio/ogg", raw)
        settings = Settings(
            stt_provider="auto",
            cloud_stt_rescue_enabled=True,
            whisper_model="base",
            gemini_api_key="configured-key",
        )

        def local_side_effect(_path, _model, language="auto"):
            return {
                "auto": "add milk to my shopping list",
                "en": "what time is it today",
                "ta": "நாளைக்கு வானிலை எப்படி",
            }[language]

        with patch.object(media, "get_settings", return_value=settings), \
             patch.object(media, "_local_whisper", side_effect=local_side_effect), \
             patch.object(media, "_gemini_stt", return_value="add milk to my shopping list") as gemini, \
             patch.object(media, "_openai_stt") as openai_stt, \
             patch.object(media, "_xai_stt") as xai:
            text_value = media.transcribe_audio(media_id)

        self.assertEqual(text_value, "add milk to my shopping list")
        gemini.assert_called_once()
        openai_stt.assert_not_called()
        xai.assert_not_called()
        stored = media.get_media(media_id)
        self.assertIn('"cloud_rescue": true', stored["transcript_meta_json"])

    def test_nonfinancial_image_is_available_to_model_vision(self):
        self.claim("img1", "+60111111111", "what is this?")
        payload = {
            "message_id": "img1",
            "text": "what is this?",
            "image_data": base64.b64encode(b"not-a-real-jpeg-but-persistable").decode("ascii"),
            "image_mime_type": "image/jpeg",
        }
        media_ids, context_lines, vision_parts = media.process_payload_media(payload)
        self.assertEqual(len(media_ids), 1)
        self.assertEqual(context_lines, [])
        self.assertEqual(len(vision_parts), 1)
        self.assertTrue(vision_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_aggregates_ignore_record_display_limit(self):
        self.claim("sum1", "+60111111111", "many expenses")
        base = self.actor("sum1", "+60111111111")
        for i in range(25):
            actor = with_action_key(base, f"sum-{i}")
            services.log_expense(actor, f"Lunch {i}", 10, "food", currency="MYR")
        result = services.query_finances(base, category="food", limit=5)
        self.assertEqual(result["count"], 25)
        self.assertEqual(result["returned_records"], 5)
        self.assertEqual(result["spending_totals"]["MYR"], 250.0)

    def test_generic_p2p_category_guess_is_rejected(self):
        self.claim("p2p1", "+60111111111", "sent RM50")
        actor = with_action_key(self.actor("p2p1", "+60111111111"), "p2p-action")
        result = services.log_expense(
            actor, "Transfer to PRIYA", 50, "groceries", currency="MYR", reference="BANKREF"
        )
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertIsNone(result["category"])
        self.assertEqual(result["pending_reason"], "purpose_or_category_unclear")

    def test_unlinked_original_receipt_is_still_retrievable_by_sender(self):
        self.claim("orphan1", "+60111111111", "")
        raw = base64.b64encode(b"orphan-receipt-bytes").decode("ascii")
        media_id = media.save_media("orphan1", "IMAGE", "image/jpeg", raw)
        actor = self.actor("orphan1", "+60111111111")
        found = services.find_receipts(actor, limit=10)
        self.assertTrue(any(x["media_id"] == media_id and not x["linked"] for x in found["matches"]))
        receipt = services.get_receipt(actor, media_id)
        self.assertEqual(receipt["status"], "found_unlinked")
        self.assertTrue(os.path.isfile(receipt["_attachments"][0]["path"]))

        self.claim("orphan-wife", "+60222222222", "find")
        wife = self.actor("orphan-wife", "+60222222222")
        with self.assertRaises(PermissionError):
            services.get_receipt(wife, media_id)

    def test_explicit_saved_media_can_be_retrieved(self):
        self.claim("memimg1", "+60111111111", "save this")
        raw = base64.b64encode(b"saved-photo-bytes").decode("ascii")
        media_id = media.save_media("memimg1", "IMAGE", "image/jpeg", raw)
        actor = with_action_key(self.actor("memimg1", "+60111111111", [media_id]), "memimg-action")
        saved = services.save_item(actor, "Parking", "Level 4 Bay 18", "car")
        item = services.get_saved_item(actor, saved["item_id"])
        self.assertEqual(item["status"], "found")
        self.assertEqual(item["content"], "Level 4 Bay 18")
        self.assertEqual(item["_attachments"][0]["path"], media.get_media(media_id)["local_path"])

    def test_shared_goal_and_bucket_are_single_household_records(self):
        self.claim("sg1", "+60111111111", "trip goal")
        h = with_action_key(self.actor("sg1", "+60111111111"), "goal-h")
        goal = services.set_goal(h, "Family trip", target_amount=5000, current_amount=1000, shared=True)
        services.set_money_bucket(h, "Trip spending", 800, shared=True)

        self.claim("sg2", "+60222222222", "update trip")
        w = with_action_key(self.actor("sg2", "+60222222222"), "goal-w")
        updated = services.set_goal(w, "Family trip", current_amount=1500, shared=True)
        services.set_money_bucket(w, "Trip spending", 900, shared=True)
        self.assertEqual(goal["goal_id"], updated["goal_id"])
        self.assertEqual(services.list_goals(w)["goals"][0]["current_amount"], 1500)
        buckets = services.list_money_buckets(w)["buckets"]
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["amount"], 900)

    def test_reminder_scheduler_is_durable(self):
        self.claim("rem1", "+60111111111", "remind me")
        actor = with_action_key(self.actor("rem1", "+60111111111"), "reminder-action")
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        created = services.create_reminder(actor, "Pay electricity", due)
        self.assertEqual(created["status"], "created")

        import scheduler
        scheduler.fire_due()

        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM reminders WHERE reminder_id=?", (created["reminder_id"],)
            ).fetchone()
            self.assertEqual(row["status"], "DUE")
            out = conn.execute(
                "SELECT text_body FROM outbound_messages WHERE delivery_status='PENDING'"
            ).fetchall()
            self.assertTrue(any("Pay electricity" in r["text_body"] for r in out))
        finally:
            conn.close()

    def test_mcp_actor_is_hidden_and_context_injected(self):
        self.claim("mc1", "+60111111111", "log lunch")
        actor = with_action_key(self.actor("mc1", "+60111111111"), "mcp-action")

        async def exercise():
            with use_actor(actor):
                async with Client(mcp) as client:
                    listed = await client.list_tools()
                    log_tool = next(t for t in listed.tools if t.name == "log_expense")
                    self.assertNotIn("actor", log_tool.input_schema.get("properties", {}))
                    calc_tool = next(t for t in listed.tools if t.name == "calculate")
                    self.assertNotIn("actor", calc_tool.input_schema.get("properties", {}))
                    result = await client.call_tool("log_expense", {
                        "description": "Lunch", "amount": 9.5, "category": "food", "currency": "MYR"
                    })
                    self.assertFalse(result.is_error)

        asyncio.run(exercise())
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT owner_id,space_id,amount_minor FROM financial_events WHERE action_key='mcp-action'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["owner_id"], "USR_HUSBAND")
            self.assertEqual(row["space_id"], "HUSBAND_PVT")
            self.assertEqual(row["amount_minor"], 950)
        finally:
            conn.close()


    def test_mcp_natural_reference_facades_execute_without_opaque_ids(self):
        goal = phase2_finance.create_goal(
            "Family Holiday Savings", 5000, 200,
            "+60111111111", visibility="private"
        )
        cash = phase2_finance.record_cash_event(
            "OT", 400, "2026-09-29", "+60111111111",
            visibility="private"
        )
        asset = phase2_library.create_asset(
            "Water Dispenser", "+60111111111", visibility="private"
        )
        self.claim("mcp-natural", "+60111111111", "natural references")
        media_id = media.save_media(
            "mcp-natural", "PDF", "application/pdf",
            base64.b64encode(b"%PDF-1.4 natural facade manual").decode("ascii"),
        )
        actor = self.actor(
            "mcp-natural", "+60111111111", [media_id]
        )

        async def exercise():
            with use_actor(actor):
                async with Client(mcp) as client:
                    progress = await client.call_tool(
                        "planning_goal_progress",
                        {"goal_name": "holiday savings"},
                    )
                    self.assertFalse(progress.is_error)

                    deviation = await client.call_tool(
                        "planning_goal_deviation",
                        {"goal_name": "Family Holiday Savings",
                         "period": "2026-09"},
                    )
                    self.assertFalse(deviation.is_error)

                    allocation = await client.call_tool(
                        "planning_allocate_cash_to_goal",
                        {
                            "amount": 100,
                            "cash_event_type": "OT",
                            "cash_event_date": "2026-09-29",
                            "goal_name": "Family Holiday Savings",
                        },
                    )
                    self.assertFalse(allocation.is_error)

                    linked = await client.call_tool(
                        "asset_link_document",
                        {
                            "document_type": "MANUAL",
                            "asset_name": "Water Dispenser",
                        },
                    )
                    self.assertFalse(linked.is_error)

        asyncio.run(exercise())
        self.assertEqual(
            phase2_finance.goal_progress(
                goal["goal_id"], "+60111111111"
            )["funded"],
            100,
        )
        stored = phase2_library.list_assets(
            "+60111111111", include_documents=True
        )
        self.assertEqual(stored[0]["asset_id"], asset["asset_id"])
        self.assertEqual(stored[0]["documents"][0]["evidence_ref"], media_id)
        self.assertEqual(
            phase2_finance.cash_event_status(
                cash["cash_event_id"], "+60111111111"
            )["unallocated"],
            300,
        )

    def test_audio_media_does_not_force_private_expense_shared(self):
        self.claim("audio-route", "+60111111111", "")
        raw = base64.b64encode(b"dummy-audio").decode("ascii")
        media_id = media.save_media("audio-route", "AUDIO", "audio/ogg", raw)
        actor = with_action_key(self.actor("audio-route", "+60111111111", [media_id]), "audio-route-action")
        result = services.log_expense(actor, "Coffee", 5, "food", currency="MYR")
        self.assertEqual(result["space"], "HUSBAND_PVT")

    def test_shared_shopping_list_and_duplicate_guard(self):
        self.claim("shop1", "+60111111111", "add detergent")
        h = with_action_key(self.actor("shop1", "+60111111111"), "shop-action")
        first = services.add_shopping_item(h, "Detergent")
        self.assertEqual(first["status"], "added")

        self.claim("shop2", "+60222222222", "shopping list")
        w = with_action_key(self.actor("shop2", "+60222222222"), "shop-action-wife")
        listed = services.list_shopping_items(w)
        self.assertEqual(len(listed["items"]), 1)
        duplicate = services.add_shopping_item(w, "detergent")
        self.assertEqual(duplicate["status"], "already_listed")

    def test_reminder_can_target_spouse_without_code_changes(self):
        self.claim("rem-spouse", "+60111111111", "remind my wife")
        actor = with_action_key(self.actor("rem-spouse", "+60111111111"), "rem-spouse-action")
        due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        created = services.create_reminder(actor, "Renew road tax", due, recipient="wife")
        self.assertEqual(created["recipient_user_id"], "USR_WIFE")
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT owner_id,space_id,conversation_id FROM reminders WHERE reminder_id=?",
                (created["reminder_id"],),
            ).fetchone()
            self.assertEqual(row["owner_id"], "USR_WIFE")
            self.assertEqual(row["space_id"], "FAMILY_SHARED")
            self.assertEqual(row["conversation_id"], "60222222222@s.whatsapp.net")
        finally:
            conn.close()

    def test_failed_inbound_message_can_be_reclaimed(self):
        payload = {
            "message_id": "retry1", "provider": "WHATSAPP",
            "conversation_id": "60111111111@s.whatsapp.net",
            "conversation_type": "DIRECT_DM", "sender_phone": "+60111111111",
            "text": "test",
        }
        self.assertEqual(db.claim_inbound(payload), "CLAIMED")
        db.fail_inbound("retry1", "temporary failure")
        # Immediate Node transport retry is suppressed.
        self.assertEqual(db.claim_inbound(payload), "DUPLICATE")
        conn = db.connect()
        try:
            stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            conn.execute(
                "UPDATE inbound_messages SET processing_started_at_utc=? WHERE message_id='retry1'",
                (stale,),
            )
            conn.commit()
        finally:
            conn.close()
        # A genuinely stale provider redelivery can recover the same message id.
        self.assertEqual(db.claim_inbound(payload), "CLAIMED")
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT attempt_count,processing_state FROM inbound_messages WHERE message_id='retry1'"
            ).fetchone()
            self.assertEqual(row["attempt_count"], 2)
            self.assertEqual(row["processing_state"], "PROCESSING")
        finally:
            conn.close()

    def test_sensitive_home_assistant_domains_are_rejected_before_network(self):
        with self.assertRaises(PermissionError):
            ha.control("lock.front_door", "unlock")


    def test_currency_is_never_silently_guessed(self):
        self.claim("currency-amb", "+60111111111", "spent 10 for lunch")
        actor = with_action_key(self.actor("currency-amb", "+60111111111"), "currency-amb-action")
        result = services.log_expense(actor, "Lunch", 10, "food", currency=None)
        self.assertEqual(result["status"], "clarification_required")
        conn = db.connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM financial_events WHERE source_message_id='currency-amb'"
            ).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            conn.close()


    def group_actor(self, mid, phone):
        db.claim_inbound({
            "message_id": mid,
            "provider": "WHATSAPP",
            "conversation_id": "family@g.us",
            "conversation_type": "GROUP",
            "sender_phone": phone,
            "text": "group test",
        })
        return db.resolve_actor(phone, "family@g.us", "GROUP", mid, [])

    def test_group_channel_is_structurally_shared_only(self):
        self.claim("privx", "+60111111111", "private coffee")
        husband = with_action_key(self.actor("privx", "+60111111111"), "privx-action")
        services.log_expense(husband, "Coffee", 10, "food", currency="MYR")

        group = self.group_actor("groupx", "+60111111111")
        self.assertEqual(group.allowed_spaces, ("FAMILY_SHARED",))
        self.assertEqual(services.query_finances(group)["count"], 0)

        shared_group = with_action_key(group, "group-save")
        saved = services.save_item(shared_group, "Family note", "buy batteries")
        self.assertEqual(saved["space"], "FAMILY_SHARED")

    def test_diary_conflict_gate_choice_one_creates_planned_leave(self):
        self.claim("rost1", "+60111111111", "work")
        actor = with_action_key(self.actor("rost1", "+60111111111"), "roster-a")
        phase2.set_work_roster(
            actor, "2026-10-02", "Day shift",
            "2026-10-02T08:00:00+08:00", "2026-10-02T17:00:00+08:00"
        )

        self.claim("diary1", "+60111111111", "appointment")
        actor = with_action_key(self.actor("diary1", "+60111111111"), "diary-a")
        pending = phase2.add_diary_event(
            actor, "Appointment", "2026-10-02T10:00:00+08:00",
            "2026-10-02T11:00:00+08:00", reminder_minutes_before=30,
        )
        self.assertEqual(pending["status"], "needs_choice")
        self.assertEqual(set(pending["choices"]), {"1", "2", "3"})

        resolved = phase2.resolve_diary_conflict(actor, pending["conflict_id"], 1)
        self.assertEqual(resolved["choice"], 1)
        self.assertEqual(resolved["leave"]["state"], "PLANNED")
        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM diary_events WHERE status='ACTIVE'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM leave_records WHERE status='PLANNED'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_linked_reminder_moves_and_cancels_with_diary(self):
        self.claim("dlink", "+60111111111", "dentist")
        actor = with_action_key(self.actor("dlink", "+60111111111"), "diary-link")
        created = phase2.add_diary_event(
            actor, "Dentist", "2026-10-05T10:00:00+08:00",
            reminder_minutes_before=60,
        )
        rid = created["linked_reminder_id"]
        self.assertTrue(rid)

        ask_move = phase2.update_diary_event(
            actor, created["diary_id"], start_local="2026-10-05T12:00:00+08:00"
        )
        self.assertEqual(ask_move["status"], "needs_reminder_choice")
        self.assertEqual(set(ask_move["choices"]), {"keep", "shift"})

        moved = phase2.update_diary_event(
            actor, created["diary_id"], start_local="2026-10-05T12:00:00+08:00",
            linked_reminders="shift",
        )
        self.assertEqual(moved["linked_reminders_updated"], 1)
        conn = db.connect()
        try:
            due = conn.execute("SELECT due_at_utc,status FROM reminders WHERE reminder_id=?", (rid,)).fetchone()
            self.assertEqual(due["status"], "OPEN")
            self.assertIn("03:00:00", due["due_at_utc"])
        finally:
            conn.close()

        ask_cancel = phase2.update_diary_event(actor, created["diary_id"], status="CANCELLED")
        self.assertEqual(ask_cancel["status"], "needs_reminder_choice")
        self.assertEqual(set(ask_cancel["choices"]), {"keep", "cancel"})
        phase2.update_diary_event(
            actor, created["diary_id"], status="CANCELLED", linked_reminders="cancel"
        )
        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT status FROM reminders WHERE reminder_id=?", (rid,)
            ).fetchone()[0], "CANC")
        finally:
            conn.close()

    def test_private_plan_shares_copy_not_source_or_private_notes(self):
        self.claim("plan1", "+60111111111", "plan")
        actor = with_action_key(self.actor("plan1", "+60111111111"), "plan-a")
        plan = phase2.create_plan(actor, "Weekend trip", notes="private budget note")
        self.assertEqual(plan["space"], "HUSBAND_PVT")
        shared_actor = with_action_key(actor, "plan-share")
        copied = phase2.share_plan(shared_actor, plan["plan_id"])
        conn = db.connect()
        try:
            src = conn.execute(
                "SELECT space_id,notes FROM plans WHERE plan_id=?", (plan["plan_id"],)
            ).fetchone()
            dst = conn.execute(
                "SELECT space_id,notes FROM plans WHERE plan_id=?", (copied["plan_id"],)
            ).fetchone()
            self.assertEqual(src["space_id"], "HUSBAND_PVT")
            self.assertEqual(src["notes"], "private budget note")
            self.assertEqual(dst["space_id"], "FAMILY_SHARED")
            self.assertIsNone(dst["notes"])
            self.assertFalse(copied["private_notes_copied"])
        finally:
            conn.close()

    def test_task_lifecycle_is_first_class_and_does_not_mutate_links(self):
        self.claim("task-life", "+60111111111", "passport task")
        base = self.actor("task-life", "+60111111111")
        plan = phase2.create_plan(
            with_action_key(base, "task-plan"),
            "Malacca trip",
            notes="private trip notes",
        )
        reminder = services.create_reminder(
            with_action_key(base, "task-reminder"),
            "Check passports",
            "2026-10-01T09:00:00+08:00",
        )
        created = phase2.create_task(
            with_action_key(base, "task-create"),
            "Check passport expiry dates",
            notes="Check every passport",
            due_local="2026-09-30",
            plan_id=plan["plan_id"],
            reminder_id=reminder["reminder_id"],
        )
        self.assertEqual(created["task_status"], "OPEN")
        self.assertIsNone(created["due_at_utc"])
        self.assertEqual(created["due_date_local"], "2026-09-30")

        listed = phase2.list_tasks(base, "open", plan["plan_id"])
        self.assertEqual([t["task_id"] for t in listed["tasks"]], [created["task_id"]])
        self.assertEqual(listed["tasks"][0]["reminder_id"], reminder["reminder_id"])

        phase2.update_task(
            with_action_key(base, "task-update"),
            created["task_id"],
            title="Check all passport expiry dates",
            notes="Check every passport and copy",
        )
        phase2.complete_task(with_action_key(base, "task-complete"), created["task_id"])
        self.assertEqual(
            phase2.list_tasks(base, "done", plan["plan_id"])["tasks"][0]["task_id"],
            created["task_id"],
        )
        phase2.reopen_task(with_action_key(base, "task-reopen"), created["task_id"])
        phase2.cancel_task(with_action_key(base, "task-cancel"), created["task_id"])

        conn = db.connect()
        try:
            task = conn.execute(
                "SELECT status,title,plan_id FROM tasks WHERE task_id=?",
                (created["task_id"],),
            ).fetchone()
            self.assertEqual(task["status"], "CANCELLED")
            self.assertEqual(task["title"], "Check all passport expiry dates")
            self.assertEqual(task["plan_id"], plan["plan_id"])
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM plans WHERE plan_id=?", (plan["plan_id"],)
                ).fetchone()[0],
                "DRAFT",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM reminders WHERE reminder_id=?",
                    (reminder["reminder_id"],),
                ).fetchone()[0],
                "OPEN",
            )
            events = conn.execute(
                "SELECT event_type FROM task_events WHERE task_id=? ORDER BY created_at_utc,rowid",
                (created["task_id"],),
            ).fetchall()
            self.assertEqual(
                [r["event_type"] for r in events],
                ["CREATED", "UPDATED", "COMPLETED", "REOPENED", "CANCELLED"],
            )
        finally:
            conn.close()

    def test_spouse_availability_never_reads_private_schedule(self):
        self.claim("wife-roster", "+60222222222", "work")
        wife = with_action_key(self.actor("wife-roster", "+60222222222"), "wife-roster-a")
        phase2.set_work_roster(wife, "2026-10-10", "Secret named shift")

        self.claim("hus-check", "+60111111111", "is she free")
        husband = self.actor("hus-check", "+60111111111")
        result = phase2.check_spouse_availability(husband, "2026-10-10T12:00:00+08:00")
        self.assertEqual(result["availability"], "private_check_required")
        self.assertFalse(result["private_schedule_read"])
        self.assertEqual(result["privacy"], "details_hidden")
        self.assertNotIn("secret", str(result).lower())
        self.assertNotIn("shift", str(result).lower())

    def test_agenda_combines_life_work_leave_and_reminders(self):
        self.claim("agenda-base", "+60111111111", "setup")
        base = self.actor("agenda-base", "+60111111111")
        phase2.set_work_roster(with_action_key(base, "ag-r"), "2026-10-20", "Day")
        phase2.set_leave_record(with_action_key(base, "ag-l"), "2026-10-21", "PLANNED")
        phase2.create_plan(with_action_key(base, "ag-p"), "Family outing", "2026-10-22T09:00:00+08:00")
        phase2.add_diary_event(with_action_key(base, "ag-d"), "Doctor", "2026-10-23T10:00:00+08:00")
        services.create_reminder(with_action_key(base, "ag-rem"), "Pay bill", "2026-10-24T09:00:00+08:00")
        agenda = phase2.get_agenda(base, "2026-10-20", "2026-10-25")
        self.assertEqual(len(agenda["roster"]), 1)
        self.assertEqual(len(agenda["leave"]), 1)
        self.assertEqual(len(agenda["plans"]), 1)
        self.assertEqual(len(agenda["diary"]), 1)
        self.assertEqual(len(agenda["reminders"]), 1)

    def test_cashflow_baseline_excludes_variable_income(self):
        self.claim("cash1", "+60111111111", "baseline")
        actor = self.actor("cash1", "+60111111111")
        snap = phase2.set_cashflow_baseline(
            actor, "MYR", guaranteed_income=5000, fixed_commitments=2500,
            locked_allocations=1000, reserves=500,
        )
        self.assertEqual(snap["baseline_unallocated"], 1000)
        self.assertIn("Variable/OT/extra cash", snap["rule"])

    def test_diagnostics_are_sanitized_and_observation_based(self):
        self.claim("diag1", "+60111111111", "health")
        actor = self.actor("diag1", "+60111111111")
        health = diagnostics.system_health(actor, 24)
        self.assertIn(health["database"], {"ok", "problem"})
        self.assertIn("observed", health)
        self.assertNotIn("xai_api_key", str(health))


    def test_natural_planning_references_never_require_model_uuid_invention(self):
        self.sync_phase2_fixture()
        goal = phase2_finance.create_goal(
            "Family Holiday Savings", 5000, 200,
            "+60111111111", visibility="private"
        )
        self.assertEqual(
            phase2_finance.resolve_goal_reference(
                None, "holiday savings", "+60111111111"
            ),
            goal["goal_id"],
        )

        cash = phase2_finance.record_cash_event(
            "OT", 400, "2026-09-29", "+60111111111",
            visibility="private"
        )
        resolved_cash = phase2_finance.resolve_cash_event_reference(
            None, "+60111111111", event_type="OT",
            event_date="2026-09-29", require_unallocated=True,
        )
        self.assertEqual(resolved_cash, cash["cash_event_id"])

        allocation = phase2_finance.allocate_cash_to_goal(
            resolved_cash,
            phase2_finance.resolve_goal_reference(
                None, "Family Holiday Savings", "+60111111111"
            ),
            100, "+60111111111", contribution_date="2026-09-29",
        )
        self.assertEqual(allocation["remaining_unallocated"], 300)

        # The model need not calculate or invent the period's actual
        # contribution before asking whether the goal is below plan.
        deviation = phase2_finance.evaluate_goal_deviation(
            goal["goal_id"], None, "2026-09", "+60111111111"
        )
        self.assertEqual(deviation["actual"], 100)
        self.assertEqual(deviation["status"], "BELOW_PLAN")

        pool = phase2_finance.create_cash_pool(
            "Holiday Buffer", "+60111111111", visibility="private"
        )
        self.assertEqual(
            phase2_finance.resolve_cash_pool_reference(
                None, "holiday buffer", "+60111111111"
            ),
            pool["pool_id"],
        )
        phase2_finance.allocate_cash_to_pool(
            cash["cash_event_id"], pool["pool_id"], 50, "+60111111111"
        )
        self.assertEqual(
            phase2_finance.cash_pool_balance(
                phase2_finance.resolve_cash_pool_reference(
                    None, "Holiday Buffer", "+60111111111"
                ),
                "+60111111111",
            )["balance"],
            50,
        )

        reserve = phase2_finance.add_plan_reserve(
            "School Reserve", 300, "+60111111111", visibility="private"
        )
        self.assertEqual(
            phase2_finance.resolve_reserve_reference(
                None, "school reserve", "+60111111111"
            ),
            reserve["reserve_id"],
        )

    def test_natural_reference_resolution_refuses_ambiguous_planning_objects(self):
        phase2_finance.create_goal(
            "Holiday Europe", 5000, 0, "+60111111111", visibility="private"
        )
        phase2_finance.create_goal(
            "Holiday Japan", 5000, 0, "+60111111111", visibility="private"
        )
        with self.assertRaises(ValueError):
            phase2_finance.resolve_goal_reference(
                None, "Holiday", "+60111111111"
            )

    def test_advanced_goal_cash_and_obligation_lifecycle(self):
        self.sync_phase2_fixture()
        goal = phase2_finance.create_goal(
            "Europe", 6000, 200, "+60111111111", visibility="private"
        )
        cash = phase2_finance.record_cash_event(
            "OT", 1000, "2026-09-26", "+60111111111", visibility="private"
        )
        before = phase2_finance.cash_event_status(cash["cash_event_id"], "+60111111111")
        self.assertEqual(before["unallocated"], 1000)
        allocation = phase2_finance.allocate_cash_to_goal(
            cash["cash_event_id"], goal["goal_id"], 350, "+60111111111",
            contribution_date="2026-09-26",
        )
        self.assertEqual(allocation["remaining_unallocated"], 650)
        self.assertFalse(allocation["baseline_changed"])
        progress = phase2_finance.goal_progress(goal["goal_id"], "+60111111111")
        self.assertEqual(progress["baseline_monthly"], 200)

        deviation = phase2_finance.evaluate_goal_deviation(
            goal["goal_id"], 100, "2026-09", "+60111111111"
        )
        self.assertEqual(deviation["status"], "BELOW_PLAN")
        self.assertFalse(deviation["baseline_changed"])

        phase2_finance.ensure_obligation_instances("2026-09", "+60111111111")
        phase2_finance.refresh_obligation_states("2026-09-26", "+60111111111")
        obligations = phase2_finance.list_obligations(
            "+60111111111", period="2026-09"
        )
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["state"], "UNCONFIRMED_DUE")
        partial = phase2_finance.record_obligation_payment(
            obligations[0]["instance_id"], 500, "+60111111111"
        )
        self.assertEqual(partial["state"], "PARTIAL")
        explicit = phase2_finance.confirm_obligation_unpaid(
            obligations[0]["instance_id"], "+60111111111",
            note="Owner explicitly confirmed"
        )
        self.assertEqual(explicit["state"], "CONFIRMED_UNPAID")

    def test_advanced_work_ot_and_leave_rules_are_preserved(self):
        self.sync_phase2_fixture()
        self.assertEqual(
            phase2_work.effective_shift("2026-09-26", "+60111111111")["shift"],
            "evening",
        )
        self.assertEqual(
            phase2_work.effective_shift("2026-09-28", "+60111111111")["shift"],
            "morning",
        )
        phase2_work.record_work_event(
            "ANNUAL_LEAVE", "2026-10-02", "+60111111111", units_days=1
        )
        saturday = phase2_work.effective_ot_for_date("2026-10-03", "+60111111111")
        self.assertFalse(saturday["ot"])
        self.assertEqual(
            saturday["eligibility"]["reason"], "FRIDAY_ABSENCE_BLOCKS_WEEKEND_OT"
        )
        phase2_work.record_work_event(
            "OT_WORKED", "2026-10-03", "+60111111111",
            start_time="06:00", end_time="10:00", hours=4,
            note="Observed actual work"
        )
        actual = phase2_work.effective_ot_for_date("2026-10-03", "+60111111111")
        self.assertTrue(actual["ot"])
        self.assertEqual(actual["kind"], "OT_WORKED")
        self.assertEqual(actual["hours"], 4)
        self.assertFalse(actual["eligibility"]["eligible"])
        self.assertEqual(
            phase2_work.next_ot_payout_dates("2026-09-26", "+60111111111", count=4),
            ["2026-10-07", "2026-10-12", "2026-11-07", "2026-11-12"],
        )

    def test_asset_name_and_current_attachment_are_groundable(self):
        asset = phase2_library.create_asset(
            "Water Dispenser", "+60111111111", visibility="private"
        )
        self.assertEqual(
            phase2_library.resolve_asset_reference(
                None, "water dispenser", "+60111111111"
            ),
            asset["asset_id"],
        )

        self.claim("asset-doc", "+60111111111", "attach this manual")
        media_id = media.save_media(
            "asset-doc", "PDF", "application/pdf",
            base64.b64encode(b"%PDF-1.4 synthetic manual").decode("ascii"),
        )
        actor = self.actor("asset-doc", "+60111111111", [media_id])
        # This mirrors the MCP wrapper's deterministic current-attachment bind.
        linked = phase2_library.link_document(
            phase2_library.resolve_asset_reference(
                None, "Water Dispenser", "+60111111111"
            ),
            "MANUAL", actor.media_ids[0], "+60111111111",
            source_message_id=actor.source_message_id,
        )
        self.assertEqual(linked["asset_id"], asset["asset_id"])
        stored = phase2_library.list_assets(
            "+60111111111", include_documents=True
        )
        self.assertEqual(stored[0]["documents"][0]["evidence_ref"], media_id)

    def test_asset_warranty_privacy_and_exact_document_reference(self):
        self.sync_phase2_fixture()
        asset = phase2_library.create_asset(
            "Private device", "+60111111111", visibility="private",
            warranty_end="2026-10-10"
        )
        phase2_library.link_document(
            asset["asset_id"], "MANUAL", "media:manual1", "+60111111111"
        )
        owner = phase2_library.list_assets("+60111111111", include_documents=True)
        wife = phase2_library.list_assets("+60222222222")
        group = phase2_library.list_assets("+60111111111", "GROUP")
        self.assertEqual(owner[0]["documents"][0]["evidence_ref"], "media:manual1")
        self.assertEqual(wife, [])
        self.assertEqual(group, [])

        phase2_library.create_asset(
            "Family appliance", "+60111111111", visibility="family",
            warranty_end="2026-10-10"
        )
        owner_due = phase2_library.warranties_expiring(
            30, "2026-09-26", "+60111111111"
        )
        wife_due = phase2_library.warranties_expiring(
            30, "2026-09-26", "+60222222222"
        )
        group_due = phase2_library.warranties_expiring(
            30, "2026-09-26", "+60111111111", "GROUP"
        )
        self.assertEqual(
            {x["name"] for x in owner_due},
            {"Family appliance", "Private device"},
        )
        self.assertEqual([x["name"] for x in wife_due], ["Family appliance"])
        self.assertEqual([x["name"] for x in group_due], ["Family appliance"])

    def test_monitoring_is_quiet_until_explicitly_delegated(self):
        self.sync_phase2_fixture()
        goal = phase2_finance.create_goal(
            "Europe", 6000, 200, "+60111111111", visibility="private"
        )
        cash = phase2_finance.record_cash_event(
            "OT", 400, "2026-09-26", "+60111111111", visibility="private"
        )
        self.assertEqual(phase2_monitor.bill_candidates("2026-09-26", "+60111111111"), [])
        self.assertEqual(phase2_monitor.goal_candidates("2026-09", "+60111111111"), [])
        self.assertEqual(phase2_monitor.ot_allocation_candidates("+60111111111"), [])

        with self.assertRaises(PermissionError):
            phase2_delegation.create_delegation(
                "OT_GOAL_TRACK", "Europe", "+60111111111", "no-explicit",
                explicit_user_instruction=False,
            )
        phase2_delegation.create_delegation(
            "OT_GOAL_TRACK", "Europe", "+60111111111", "explicit",
            explicit_user_instruction=True,
        )
        candidates = phase2_monitor.ot_allocation_candidates("+60111111111")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["cash_event_id"], cash["cash_event_id"])
        self.assertFalse(candidates[0]["automatic_allocation"])
        self.assertEqual(candidates[0]["goal_id"], goal["goal_id"])

    def test_quiet_hours_and_time_critical_policy(self):
        self.sync_phase2_fixture()
        pref = phase2_presence.owner_preference("+60111111111")
        at_night = datetime(2026, 9, 26, 23, 0, tzinfo=timezone.utc)
        routine = phase2_presence.delivery_decision(pref, "away", at_night, "routine")
        urgent = phase2_presence.delivery_decision(pref, "away", at_night, "time_critical")
        self.assertEqual(routine["decision"], "DEFER")
        self.assertEqual(routine["reason"], "QUIET_HOURS")
        self.assertEqual(urgent["decision"], "DELIVER")

    def test_numbered_retrieval_resolves_exact_latest_item(self):
        raw1 = base64.b64encode(b"receipt-one").decode("ascii")
        raw2 = base64.b64encode(b"receipt-two").decode("ascii")
        mids = []
        for idx, raw in enumerate((raw1, raw2), 1):
            mid = f"nr{idx}"
            self.claim(mid, "+60111111111", "")
            media_id = media.save_media(mid, "IMAGE", "image/jpeg", raw)
            mids.append(media_id)
            actor = with_action_key(self.actor(mid, "+60111111111", [media_id]), f"nr-action-{idx}")
            services.log_expense(
                actor, f"Recurring payment {idx}", 100, "utilities",
                currency="MYR", reference=f"REF{idx:03d}"
            )
        self.claim("nrq", "+60111111111", "show receipts")
        actor = self.actor("nrq", "+60111111111")
        found = services.find_receipts(actor, amount=100)
        self.assertEqual([x["choice"] for x in found["matches"]], [1, 2])
        picked = services.resolve_numbered_choice(actor, 2)
        self.assertEqual(picked["status"], "found")
        self.assertEqual(picked["media_id"], found["matches"][1]["media_id"])

    def test_basic_are_you_working_is_pure_chat_not_work_tool_query(self):
        self.assertTrue(brain._pure_chat("Hi Alex, are you working?"))
        self.assertTrue(brain._pure_chat("Alex are u working"))
        async def run():
            specs = await brain._tool_specs("Hi Alex, are you working?")
            self.assertEqual(specs, [])
        asyncio.run(run())

    def test_runtime_error_classifier_scrubs_provider_secrets(self):
        class FakeProviderError(Exception):
            status_code = 401
            __module__ = "openai"
        info = brain.classify_runtime_error(
            FakeProviderError("Authorization: Bearer xai-secretsecretsecret")
        )
        self.assertEqual(info["scope"], "ai_provider")
        self.assertEqual(info["category"], "provider_authentication_failed")
        self.assertNotIn("xai-secret", info["message"])

    def test_selective_tool_exposure_is_small_and_relevant(self):
        cases = [
            ("how much did I spend this weekend?", "query_finances"),
            ("turn off the living room light", "ha_control"),
            ("remind me tomorrow at 9 to pay TNB", "create_reminder"),
            ("show me the receipt reference ABC123", "find_receipts"),
            ("I got RM500 extra cash, allocate RM300 to Europe goal", "planning_allocate_cash_to_goal"),
            ("my electricity bill is paid, match this payment", "bills_match_payment"),
            ("what shift am I working next week?", "work_schedule"),
            ("move my dentist appointment to Friday 3pm", "update_diary_event"),
            ("save this photo as my keys photo", "save_item"),
            ("monitor my Europe goal", "monitor_delegate"),
        ]
        for utterance, required in cases:
            selected = brain._select_tool_names(utterance, ["attachment"] if "this photo" in utterance else None)
            self.assertIn(required, selected, (utterance, selected))
            self.assertLessEqual(len(selected), brain.TOOL_EXPOSURE_MAX, (utterance, selected))

        finance = brain._select_tool_names("how much did I spend this weekend?")
        self.assertNotIn("ha_control", finance)

        home = brain._select_tool_names("turn off the living room light")
        self.assertNotIn("planning_create_goal", home)

        casual = brain._select_tool_names("hello alex, how are you?")
        self.assertEqual(casual, set())

        # Bare numeric replies must keep both persisted-choice resolvers available.
        numbered = brain._select_tool_names("2")
        self.assertIn("resolve_latest_diary_conflict", numbered)
        self.assertIn("resolve_numbered_choice", numbered)
        self.assertLessEqual(len(numbered), brain.TOOL_EXPOSURE_MAX)

        # Tamil input still receives a bounded, useful tool surface.
        tamil = brain._select_tool_names("நாளைக்கு 9 மணிக்கு பில் கட்ட நினைவூட்டு")
        self.assertLessEqual(len(tamil), brain.TOOL_EXPOSURE_MAX)
        self.assertTrue(tamil)



    def test_explicit_saved_receipt_intent_does_not_expose_finance_write(self):
        selected = brain._select_tool_names(
            "Save this as my BSNC Leasing payment receipt",
            ["OCR: BSNC Leasing MYR 621.00 Reference 63144508"],
        )
        self.assertIn("save_item", selected)
        self.assertNotIn("log_expense", selected)
        self.assertNotIn("bills_record_payment", selected)

        compound = brain._select_tool_names(
            "Save this and log the expense RM621",
            ["OCR: BSNC Leasing MYR 621.00"],
        )
        self.assertIn("save_item", compound)
        self.assertIn("log_expense", compound)

    def test_latest_named_expense_is_deterministic(self):
        old_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        new_time = datetime.now(timezone.utc).isoformat()
        for mid, key, amount, when in (
            ("latest-old", "latest-old-action", 4.0, old_time),
            ("latest-new", "latest-new-action", 8.5, new_time),
        ):
            self.claim(mid, "+60111111111", "coffee")
            actor = with_action_key(self.actor(mid, "+60111111111"), key)
            services.log_expense(
                actor, "coffee", amount, "food", currency="MYR",
                event_date_local=when,
            )
        self.claim("latest-query", "+60111111111", "coffee just now")
        result = services.query_finances(
            self.actor("latest-query", "+60111111111"), search="coffee"
        )
        self.assertEqual(result["latest_record"]["amount"], 8.5)
        self.assertEqual(result["spending_totals"]["MYR"], 12.5)

    def test_one_period_goal_target_does_not_rewrite_baseline(self):
        self.sync_phase2_fixture()
        goal = phase2_finance.create_goal(
            "Europe", 6000, 200, "+60111111111", visibility="private"
        )
        phase2_finance.set_goal_period_target(
            goal["goal_id"], "2026-09", 100, "+60111111111",
            reason="Owner says RM100 is enough this month",
        )
        september = phase2_finance.evaluate_goal_deviation(
            goal["goal_id"], 100, "2026-09", "+60111111111"
        )
        progress = phase2_finance.goal_progress(goal["goal_id"], "+60111111111")
        self.assertEqual(september["status"], "ON_PLAN")
        self.assertEqual(progress["baseline_monthly"], 200)

    def test_reserve_and_stash_are_explicit_owner_actions(self):
        self.sync_phase2_fixture()
        reserve = phase2_finance.add_plan_reserve(
            "Shopping allowance", 300, "+60111111111", visibility="private"
        )
        listed = phase2_finance.list_plan_reserves("+60111111111")
        self.assertTrue(any(r["reserve_id"] == reserve["reserve_id"] for r in listed))

        cash = phase2_finance.record_cash_event(
            "BONUS", 500, "2026-09-27", "+60111111111", visibility="private"
        )
        pool = phase2_finance.create_cash_pool(
            "Stash", "+60111111111", visibility="private"
        )
        before = phase2_finance.cash_event_status(cash["cash_event_id"], "+60111111111")
        self.assertEqual(before["allocation_state"], "UNALLOCATED")
        phase2_finance.allocate_cash_to_pool(
            cash["cash_event_id"], pool["pool_id"], 200, "+60111111111"
        )
        balance = phase2_finance.cash_pool_balance(pool["pool_id"], "+60111111111")
        self.assertEqual(balance["balance"], 200)
        after = phase2_finance.cash_event_status(cash["cash_event_id"], "+60111111111")
        self.assertEqual(after["unallocated"], 300)

    def test_report_exports_are_privacy_scoped_and_local(self):
        self.sync_phase2_fixture()
        snapshot = phase2_reports.build_snapshot(
            "+60111111111", "DIRECT_DM", "all", "2026-09",
            include_raw_income=False,
        )
        self.assertIsNone(snapshot["baseline_plan"]["fixed_income_monthly"])
        pdf = phase2_reports.minimal_pdf(snapshot)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        csv = phase2_reports.finance_csv(snapshot)
        self.assertIsInstance(csv, str)
        self.assertTrue(csv.strip())

    def test_home_assistant_low_risk_control_is_verified(self):
        states = [
            {"entity_id": "light.living_room", "state": "on",
             "attributes": {"friendly_name": "Living Room Light"}}
        ]
        calls = []

        def fake_request(method, path, payload=None):
            calls.append((method, path, payload))
            if path == "/states":
                return states
            if path == "/states/light.living_room":
                return {
                    "entity_id": "light.living_room", "state": "off",
                    "attributes": {"friendly_name": "Living Room Light"},
                    "last_changed": "2026-09-27T00:00:00+00:00",
                }
            if path == "/services/light/turn_off":
                return []
            raise AssertionError((method, path, payload))

        with patch.object(ha, "_request", side_effect=fake_request):
            found = ha.find_entities("living room", "light")
            self.assertEqual(found["matches"][0]["entity_id"], "light.living_room")
            controlled = ha.control("light.living_room", "turn_off")
        self.assertEqual(controlled["status"], "executed_and_verified")
        self.assertEqual(controlled["state_after"]["state"], "off")
        self.assertTrue(any(path == "/services/light/turn_off" for _, path, _ in calls))

    def test_cost_estimator_and_budget_settings_are_conservative(self):
        self.assertEqual(brain._estimate_cost("grok", "grok-4.7", 1_000_000, 1_000_000), 8.0)
        self.assertEqual(brain._estimate_cost("gemini", "gemini-3.8-flash", 1_000_000, 1_000_000), 4.5)
        self.assertEqual(brain._estimate_cost("openai", "gpt-5.6-luna", 1_000_000, 1_000_000), 1.4)
        self.assertIsNone(brain._estimate_cost("grok", "custom-unknown-model", 1000, 1000))

    def test_typo_heavy_request_has_discovery_safety_valve(self):
        async def exercise():
            specs = await brain._tool_specs("alx plz remidn me tmrw 9 pay elctrcity")
            names = [x["function"]["name"] for x in specs]
            self.assertIn(brain.DISCOVERY_TOOL_NAME, names)
            self.assertLessEqual(len(names), brain.TOOL_EXPOSURE_MAX)

        asyncio.run(exercise())



    def test_taken_leave_materializes_history_but_planned_leave_does_not(self):
        self.sync_phase2_fixture()
        self.claim("leave-plan", "+60111111111", "plan leave")
        base = self.actor("leave-plan", "+60111111111")
        planned = phase2.set_leave_record(
            with_action_key(base, "leave-plan-action"),
            "2026-10-02", status="PLANNED", end_date="2026-10-03",
            leave_type="ANNUAL_LEAVE",
        )
        self.assertEqual(planned["materialized_days"], 0)
        self.assertTrue(phase2_work.effective_ot_for_date("2026-10-03", "+60111111111")["ot"])

        taken = phase2.set_leave_record(
            with_action_key(base, "leave-taken-action"),
            "2026-10-02", status="TAKEN", end_date="2026-10-03",
            leave_type="ANNUAL_LEAVE",
        )
        self.assertEqual(taken["materialized_days"], 2)
        blocked = phase2_work.effective_ot_for_date("2026-10-03", "+60111111111")
        self.assertFalse(blocked["ot"])

        repeat = phase2.set_leave_record(
            with_action_key(base, "leave-taken-repeat"),
            "2026-10-02", status="TAKEN", end_date="2026-10-03",
            leave_type="ANNUAL_LEAVE",
        )
        self.assertEqual(repeat["materialized_days"], 0)

    def test_family_agenda_never_contains_private_roster_or_leave(self):
        self.claim("group-roster-setup", "+60111111111", "work")
        dm = self.actor("group-roster-setup", "+60111111111")
        phase2.set_work_roster(
            with_action_key(dm, "group-roster-action"), "2026-11-10", "Private Shift"
        )
        phase2.set_leave_record(
            with_action_key(dm, "group-leave-action"), "2026-11-11", "PLANNED"
        )
        group = self.group_actor("group-agenda", "+60111111111")
        agenda = phase2.get_agenda(group, "2026-11-09", "2026-11-12")
        self.assertEqual(agenda["roster"], [])
        self.assertEqual(agenda["leave"], [])
        self.assertNotIn("Private Shift", str(agenda))



    def test_date_only_diary_is_heads_up_not_midnight_conflict(self):
        self.sync_phase2_fixture()
        self.claim("date-only-base", "+60111111111", "wedding")
        actor = self.actor("date-only-base", "+60111111111")
        timed = phase2.add_diary_event(
            with_action_key(actor, "date-only-timed"),
            "Wedding", "2026-10-03T19:00:00+08:00",
            "2026-10-03T22:00:00+08:00",
        )
        self.assertEqual(timed["status"], "created")

        date_only = phase2.add_diary_event(
            with_action_key(actor, "date-only-new"),
            "Family errand", "2026-10-03", time_known=False,
        )
        self.assertEqual(date_only["status"], "created")
        self.assertFalse(date_only["time_known"])
        self.assertTrue(any(x["kind"] == "DIARY_SAME_DAY" for x in date_only["heads_up"]))
        self.assertTrue(any(x["kind"] == "WORK_SAME_DAY" for x in date_only["heads_up"]))

    def test_date_only_plan_confirmation_does_not_invent_midnight(self):
        self.claim("date-plan", "+60111111111", "holiday plan")
        actor = self.actor("date-plan", "+60111111111")
        plan = phase2.create_plan(
            with_action_key(actor, "date-plan-create"),
            "Day trip", "2026-12-05", time_known=False,
        )
        self.assertFalse(plan["time_known"])
        result = phase2.confirm_plan(
            with_action_key(actor, "date-plan-confirm"),
            plan["plan_id"], add_to_diary=True,
        )
        self.assertEqual(result["status"], "created")
        self.assertFalse(result["time_known"])
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT time_known FROM diary_events WHERE diary_id=?",
                (result["diary_id"],),
            ).fetchone()
            self.assertEqual(row["time_known"], 0)
        finally:
            conn.close()



    def test_mutating_mcp_action_is_cached_at_most_once(self):
        self.claim("atmost1", "+60111111111", "log lunch")
        actor = self.actor("atmost1", "+60111111111")
        key = brain._action_key(
            actor, "log_expense",
            {"description": "Lunch", "amount": 9.5, "category": "food", "currency": "MYR"},
            1,
        )

        async def exercise():
            args = {"description": "Lunch", "amount": 9.5, "category": "food", "currency": "MYR"}
            first, _ = await brain._call_mcp(actor, "log_expense", args, key)
            second, _ = await brain._call_mcp(actor, "log_expense", args, key)
            return first, second

        first, second = asyncio.run(exercise())
        self.assertEqual(first["event_id"], second["event_id"])
        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM financial_events WHERE source_message_id='atmost1'"
                ).fetchone()[0],
                1,
            )
            claim = conn.execute(
                "SELECT state FROM tool_execution_claims WHERE action_key=?", (key,)
            ).fetchone()
            self.assertEqual(claim["state"], "COMPLETED")
        finally:
            conn.close()

    def test_identical_model_mutation_retries_share_one_action_key(self):
        self.claim("same-call", "+60111111111", "log lunch")
        actor = self.actor("same-call", "+60111111111")
        args = {
            "description": "Lunch", "amount": 9.5,
            "category": "food", "currency": "MYR",
        }
        self.assertEqual(
            brain._action_key(actor, "log_expense", args, 1),
            brain._action_key(actor, "log_expense", args, 2),
        )

    def test_uncertain_mutation_is_not_replayed(self):
        self.claim("uncertain1", "+60111111111", "turn off light")
        actor = self.actor("uncertain1", "+60111111111")
        key = "uncertain-action-key"
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO tool_execution_claims(action_key,tool_name,state)
                   VALUES(?,?,'UNCERTAIN')""",
                (key, "ha_control"),
            )
            conn.commit()
        finally:
            conn.close()

        async def exercise():
            with patch.object(ha, "control", side_effect=AssertionError("must not replay")):
                return await brain._call_mcp(
                    actor, "ha_control",
                    {"entity_id": "light.living_room", "action": "turn_off"},
                    key,
                )

        result, _ = asyncio.run(exercise())
        self.assertEqual(result["status"], "previous_attempt_uncertain")


    def test_live_financial_correction_phrases_bypass_generic_date_money_clarification(self):
        phrases = [
            "Actually, the second RM8 parking transaction today was RM10. Correct it.",
            "Change today's second parking expense from RM8 to RM10.",
        ]
        for phrase in phrases:
            result = brain.phase2_intent.classify_write_intent(phrase)
            self.assertEqual(result.get("intent"), "EXPENSE", (phrase, result))
            self.assertFalse(result.get("requires_clarification"), (phrase, result))
            selected = brain._select_tool_names(phrase)
            self.assertIn("correct_expense", selected)
            self.assertIn("query_finances", selected)
            self.assertLessEqual(len(selected), brain.TOOL_EXPOSURE_MAX)

    def test_finance_scope_family_private_and_voice_are_deterministic(self):
        self.claim("scope-private", "+60111111111", "coffee")
        private_actor = with_action_key(self.actor("scope-private", "+60111111111"), "scope-private-a")
        services.log_expense(private_actor, "Coffee", 8.5, "food", currency="MYR")

        self.claim("scope-family", "+60111111111", "parking")
        family_actor = with_action_key(self.actor("scope-family", "+60111111111"), "scope-family-a")
        services.log_expense(family_actor, "Parking", 10, "transport", currency="MYR")

        self.claim("scope-voice", "+60111111111", "")
        raw = base64.b64encode(b"dummy-voice").decode("ascii")
        voice_media = media.save_media("scope-voice", "AUDIO", "audio/ogg", raw)
        voice_actor = with_action_key(
            self.actor("scope-voice", "+60111111111", [voice_media]), "scope-voice-a"
        )
        services.log_expense(voice_actor, "Voice coffee", 6, "food", currency="MYR")

        reader = self.actor("scope-family", "+60111111111")
        family = services.query_finances(reader, scope="family")
        private = services.query_finances(reader, scope="private")
        voice = services.query_finances(reader, source="voice")
        self.assertEqual(family["count"], 1)
        self.assertEqual(family["spending_totals"]["MYR"], 10)
        self.assertEqual(private["count"], 2)
        self.assertEqual(voice["count"], 1)
        self.assertEqual(voice["records"][0]["description"], "Voice coffee")

        group = self.group_actor("scope-group", "+60111111111")
        with self.assertRaises(PermissionError):
            services.query_finances(group, scope="private")

    def test_dm_receipt_media_does_not_override_current_command_scope(self):
        self.claim("scope-receipt-default", "+60111111111", "log this receipt")
        raw = base64.b64encode(b"generic-receipt").decode("ascii")
        receipt_media = media.save_media(
            "scope-receipt-default", "IMAGE", "image/jpeg", raw
        )
        actor = with_action_key(
            replace(
                self.actor(
                    "scope-receipt-default", "+60111111111", [receipt_media]
                ),
                trusted_text="log this receipt",
            ),
            "scope-receipt-default-a",
        )
        result = services.log_expense(
            actor, "Personal purchase", 15, "personal", currency="MYR"
        )
        # Current locked rule: new writes are Family Shared unless THIS user
        # command explicitly says private or contains any emoji. The receipt
        # bytes/OCR never influence that decision.
        self.assertEqual(result["space"], "FAMILY_SHARED")

    def test_new_finance_write_scope_uses_command_not_category(self):
        self.claim("scope-pharmacy-default", "+60111111111", "pharmacy medicine")
        actor = with_action_key(
            replace(
                self.actor("scope-pharmacy-default", "+60111111111"),
                trusted_text="pharmacy medicine",
            ),
            "scope-pharmacy-default-a",
        )
        default_shared = services.log_expense(
            actor, "Pharmacy medicine", 20, "pharmacy", currency="MYR"
        )
        self.assertEqual(default_shared["space"], "FAMILY_SHARED")

        self.claim(
            "scope-pharmacy-private", "+60111111111",
            "log this privately pharmacy medicine",
        )
        private_actor = with_action_key(
            replace(
                self.actor("scope-pharmacy-private", "+60111111111"),
                trusted_text="log this privately pharmacy medicine",
            ),
            "scope-pharmacy-private-a",
        )
        explicit_private = services.log_expense(
            private_actor, "Pharmacy medicine", 21, "pharmacy", currency="MYR"
        )
        self.assertEqual(explicit_private["space"], "HUSBAND_PVT")

        self.claim(
            "scope-pharmacy-emoji", "+60111111111",
            "pharmacy medicine 🙂",
        )
        emoji_actor = with_action_key(
            replace(
                self.actor("scope-pharmacy-emoji", "+60111111111"),
                trusted_text="pharmacy medicine 🙂",
            ),
            "scope-pharmacy-emoji-a",
        )
        emoji_private = services.log_expense(
            emoji_actor, "Pharmacy medicine", 22, "pharmacy", currency="MYR"
        )
        self.assertEqual(emoji_private["space"], "HUSBAND_PVT")

    def test_shopping_private_and_family_lists_do_not_collapse_each_other(self):
        self.claim("shop-scope", "+60111111111", "shopping")
        actor = self.actor("shop-scope", "+60111111111")
        family = services.add_shopping_item(
            with_action_key(actor, "shop-family"), "Milk", shared=True
        )
        private = services.add_shopping_item(
            with_action_key(actor, "shop-private"), "Milk", shared=False
        )
        self.assertNotEqual(family["item_id"], private["item_id"])
        family_list = services.list_shopping_items(actor, scope="family")
        private_list = services.list_shopping_items(actor, scope="private")
        self.assertEqual([x["item_id"] for x in family_list["items"]], [family["item_id"]])
        self.assertEqual([x["item_id"] for x in private_list["items"]], [private["item_id"]])

    def test_swipe_reply_context_resolves_exact_financial_event(self):
        self.claim("quote-src", "+60111111111", "parking RM8")
        actor = with_action_key(self.actor("quote-src", "+60111111111"), "quote-expense")
        event = services.log_expense(actor, "Parking", 8, "transport", currency="MYR")
        oid = db.queue_outbound(
            actor.conversation_id, "TEXT", text="Logged RM8 parking.",
            source_message_id="quote-src",
        )
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE outbound_messages SET provider_message_id=?,delivery_status='SENT' WHERE outbound_id=?",
                ("WA-QUOTE-1", oid),
            )
            conn.commit()
        finally:
            conn.close()

        context = db.resolve_quoted_context(
            actor.conversation_id, "WA-QUOTE-1", actor.phone
        )
        self.assertEqual(context["financial_event"]["event_id"], event["event_id"])
        self.assertEqual(context["financial_event"]["amount"], 8)
        self.assertIsNone(db.resolve_quoted_context(
            "another@s.whatsapp.net", "WA-QUOTE-1", actor.phone
        ))

        specs = asyncio.run(brain._tool_specs("RM9", quoted_context=context))
        names = {x["function"]["name"] for x in specs}
        self.assertIn("correct_expense", names)
        self.assertIn("confirm_expense", names)

    def test_reply_to_own_instruction_and_short_attachment_pairing_are_scoped(self):
        self.claim("instruction-1", "+60111111111", "Save this PDF for me")
        db.finish_inbound("instruction-1", "Send it here.")
        conversation = "60111111111@s.whatsapp.net"
        quoted = db.resolve_quoted_context(
            conversation, "instruction-1", "+60111111111"
        )
        self.assertEqual(quoted["quoted_user_text"], "Save this PDF for me")

        self.claim("attachment-2", "+60111111111", "")
        recent = db.resolve_recent_instruction_context(
            conversation, "+60111111111", "attachment-2", max_age_seconds=120
        )
        self.assertEqual(recent["recent_user_instruction"], "Save this PDF for me")
        self.assertIsNone(db.resolve_recent_instruction_context(
            conversation, "+60222222222", "attachment-2", max_age_seconds=120
        ))

    def test_numbered_show_ten_retrieves_tenth_item_from_latest_list(self):
        for idx in range(1, 11):
            mid = f"saved-ten-{idx}"
            self.claim(mid, "+60111111111", "save note")
            actor = with_action_key(self.actor(mid, "+60111111111"), f"save-ten-{idx}")
            services.save_item(actor, f"Smoke note {idx}", "numbered regression")
        self.claim("saved-list", "+60111111111", "find smoke notes")
        reader = self.actor("saved-list", "+60111111111")
        found = services.search_saved_items(reader, "Smoke note", limit=10)
        self.assertEqual(len(found["matches"]), 10)
        picked = services.resolve_numbered_choice(reader, 10)
        self.assertEqual(picked["item_id"], found["matches"][9]["item_id"])
        selected = brain._select_tool_names("Show 10")
        self.assertIn("resolve_numbered_choice", selected)

    def test_cost_telemetry_accounts_for_cached_tokens_and_model_calls(self):
        self.assertEqual(
            brain._estimate_cost(
                "grok", "grok-4.7", 1_000_000, 1_000_000, cached_input_tokens=500_000
            ),
            7.25,
        )
        db.record_usage(
            None, "grok", "grok-4.7", 1000, 100, 1, 900, 0.002,
            cached_input_tokens=600, reasoning_tokens=20, model_calls=2,
        )
        usage = diagnostics.usage_summary(24)
        self.assertEqual(usage["model_calls"], 2)
        self.assertEqual(usage["cached_input_tokens"], 600)
        self.assertEqual(usage["uncached_input_tokens"], 400)
        self.assertEqual(usage["reasoning_tokens"], 20)
        self.assertEqual(usage["cache_ratio_pct"], 60.0)

    def test_auto_saver_prefers_gemini_lite_then_quality_then_grok(self):
        settings = Settings(
            ai_provider="auto",
            gemini_api_key="gemini-test",
            xai_api_key="xai-test",
        )
        routes = brain._provider_routes(
            settings, user_text="How much did I spend yesterday?", tools=[]
        )
        self.assertEqual(
            [(r["provider"], r["model"], r["role"]) for r in routes[:3]],
            [
                ("gemini", "gemini-3.1-flash-lite", "primary_saver"),
                ("gemini", "gemini-3.8-flash", "quality_fallback"),
                ("grok", "grok-4.7", "resilience_fallback"),
            ],
        )

    def test_auto_saver_uses_full_gemini_first_for_visual_or_complex_turn(self):
        settings = Settings(
            ai_provider="auto",
            gemini_api_key="gemini-test",
            xai_api_key="xai-test",
        )
        visual = brain._provider_routes(
            settings, user_text="What is in this image?", tools=[],
            vision_parts=[{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}],
        )
        self.assertEqual(visual[0]["model"], "gemini-3.8-flash")
        self.assertFalse(any(r["model"] == "gemini-3.1-flash-lite" for r in visual))

        complex_routes = brain._provider_routes(
            settings,
            user_text="Analyse my goals and recommend the best way to allocate this extra cash",
            tools=[{"type": "function", "function": {"name": "planning_brief"}}],
        )
        self.assertEqual(complex_routes[0]["model"], "gemini-3.8-flash")

    def test_manual_provider_mode_remains_single_provider(self):
        settings = Settings(ai_provider="grok", xai_api_key="xai-test")
        routes = brain._provider_routes(settings, user_text="hello", tools=[])
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["provider"], "grok")
        self.assertEqual(routes[0]["role"], "manual")

    def test_tiny_chat_health_checks_are_zero_model_candidate(self):
        self.assertIn("working", brain._local_chat_reply("Hi Alex, are you working?"))
        self.assertIsNotNone(brain._local_chat_reply("thanks"))
        self.assertIsNone(brain._local_chat_reply("How much did I spend today?"))

    def test_gemini_lite_cost_telemetry_uses_current_low_cost_rate(self):
        self.assertEqual(
            brain._estimate_cost(
                "gemini", "gemini-3.1-flash-lite",
                1_000_000, 1_000_000, cached_input_tokens=500_000,
            ),
            1.6375,
        )

    def test_xai_reported_cost_ticks_override_estimate_when_available(self):
        class Usage:
            cost_in_usd_ticks = 25_000_000
        self.assertEqual(
            brain._provider_reported_cost_usd("grok", Usage()),
            0.0025,
        )
        self.assertIsNone(brain._provider_reported_cost_usd("gemini", Usage()))

    def test_hybrid_usage_rows_count_one_source_message_as_one_interaction(self):
        self.claim("hybrid-one", "+60111111111", "hybrid")
        db.record_usage(
            "hybrid-one", "gemini", "gemini-3.1-flash-lite",
            100, 10, 1, 100, 0.0001, model_calls=1,
        )
        db.record_usage(
            "hybrid-one", "grok", "grok-4.7",
            50, 5, 0, 50, 0.0002, model_calls=1,
        )
        usage = diagnostics.usage_summary(24)
        self.assertEqual(usage["interactions"], 1)
        self.assertEqual(usage["model_calls"], 2)
        providers = {(x["provider"], x["model"]) for x in usage["by_provider"]}
        self.assertIn(("gemini", "gemini-3.1-flash-lite"), providers)
        self.assertIn(("grok", "grok-4.7"), providers)

    def test_low_cost_context_policy_avoids_history_for_self_contained_requests(self):
        self.assertEqual(brain.get_settings().reasoning_effort, "low")
        self.assertEqual(
            brain._history_turn_limit("How much did I spend yesterday?", 8), 0
        )
        self.assertGreater(
            brain._history_turn_limit("Actually change it to RM10", 8), 0
        )
        date_context = brain._runtime_context(
            self.actor_for_context(), "How much did I spend yesterday?"
        )
        self.assertIn("current local date is", date_context)
        self.assertNotIn("current local datetime is", date_context)
        clock_context = brain._runtime_context(
            self.actor_for_context(), "Remind me in 5 minutes"
        )
        self.assertIn("current local datetime is", clock_context)
        self.assertLessEqual(brain.MAX_MODEL_CALLS, 4)

    def test_private_group_read_handoffs_to_owner_dm_without_group_leak(self):
        group_id = "120363777777@g.us"

        async def fake_respond(actor, user_text, *args, **kwargs):
            self.assertEqual(actor.conversation_type, "DIRECT_DM")
            self.assertIn("HUSBAND_PVT", actor.allowed_spaces)
            self.assertNotEqual(actor.conversation_id, group_id)
            return ("Your private salary figure is RM10,000.", [])

        payload = {
            "message_id": "private-group-handoff",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "show me my private salary",
        }
        with patch.object(brain, "respond", new=fake_respond):
            result = ingress.process(payload)
        self.assertTrue(result["private_handoff"])

        conn = db.connect()
        try:
            rows = conn.execute(
                """SELECT conversation_id,text_body FROM outbound_messages
                   WHERE source_message_id=? ORDER BY created_at_utc,rowid""",
                ("private-group-handoff",),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        by_conversation = {row["conversation_id"]: row["text_body"] for row in rows}
        self.assertEqual(by_conversation[group_id], "I sent that to you privately.")
        self.assertNotIn("RM10,000", by_conversation[group_id])
        self.assertIn(
            "RM10,000", by_conversation["60111111111@s.whatsapp.net"]
        )

    def test_group_receipt_lookup_never_surfaces_private_unlinked_dm_media(self):
        self.claim("private-receipt-media", "+60111111111", "")
        media_id = media.save_media(
            "private-receipt-media", "IMAGE", "image/jpeg",
            base64.b64encode(b"private-receipt").decode(),
        )
        group_id = "120363000000@g.us"
        db.claim_inbound({
            "message_id": "group-receipt-query",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "@Alex show my receipts",
        })
        group_actor = db.resolve_actor(
            "+60111111111", group_id, "GROUP", "group-receipt-query", []
        )

        found = services.find_receipts(group_actor)
        self.assertFalse(any(x["media_id"] == media_id for x in found["matches"]))
        with self.assertRaises(PermissionError):
            services.get_receipt(group_actor, media_id)

        dm_actor = self.actor("private-receipt-media", "+60111111111", [media_id])
        dm_found = services.find_receipts(dm_actor)
        self.assertTrue(any(x["media_id"] == media_id for x in dm_found["matches"]))
        self.assertEqual(services.get_receipt(dm_actor, media_id)["media_id"], media_id)

    def test_report_exports_use_immutable_unique_paths(self):
        self.claim("export-one", "+60111111111", "send September as JSON")
        first = mcp_server.report_export(
            "json", self.actor("export-one", "+60111111111"), period="2026-09"
        )
        self.claim("export-two", "+60111111111", "send September as JSON again")
        second = mcp_server.report_export(
            "json", self.actor("export-two", "+60111111111"), period="2026-09"
        )
        first_path = first["_attachments"][0]["path"]
        second_path = second["_attachments"][0]["path"]
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(os.path.exists(first_path))
        self.assertTrue(os.path.exists(second_path))

    def test_post_smoke_golden_routes_preserve_passes_and_repair_failures(self):
        cases = {
            "Add milk to the shopping list": {"add_shopping_item"},
            "Mark milk bought on the shopping list": {"update_shopping_item"},
            "What's on my agenda tomorrow?": {"get_agenda_range"},
            "naalku agenda la ena iruku?": {"get_agenda_range"},
            "What's my plan tomorrow?": {"get_agenda_range"},
            "Rename the task buy detergent to buy fabric softener": {"update_task"},
            "Forget the wifi password": {"remove_saved_item"},
            "When is check v05 DM reminder due now?": {"list_reminders"},
            "Show me my recent reminder history": {"reminder_history"},
            "Turn off the hall AC": {"ha_control"},
            "Change hall aircon temperature to 25": {"ha_control"},
            "Turn on the TV": {"ha_control"},
            "Show recent failures": {"recent_failures"},
            "Change my iPhone goal target to RM3500": {"planning_update_goal_target"},
            "Change the toaster warranty expiry to 1 December 2027": {"asset_update"},
            "Monitor Hall AC and tell me when it turns on": {"ha_find_entities", "monitor_home_state"},
            "Send me the home status card": {"ha_home_report"},
        }
        for phrase, expected in cases.items():
            names = {x["function"]["name"] for x in asyncio.run(brain._tool_specs(phrase))}
            self.assertTrue(expected <= names, (phrase, names))
            self.assertLessEqual(len(names), brain.TOOL_EXPOSURE_MAX, phrase)
            # Discovery may fill a spare slot, but must never displace a
            # deterministic route or push provider exposure above six.
            if len(names) < brain.TOOL_EXPOSURE_MAX:
                self.assertIn(brain.DISCOVERY_TOOL_NAME, names, phrase)

        simple_ha = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Is the hall AC on?")
        )}
        self.assertNotIn("ha_home_report", simple_ha)

    def test_shopping_rename_does_not_mark_item_purchased(self):
        self.claim("shop-add-post", "+60111111111", "add toothbrush")
        actor = with_action_key(
            self.actor("shop-add-post", "+60111111111"), "shop-add-post-action"
        )
        added = services.add_shopping_item(actor, "toothbrush", shared=True)
        renamed = services.update_shopping_item(
            self.actor("shop-add-post", "+60111111111"),
            added["item_id"], item="toothpaste"
        )
        self.assertEqual(renamed["item"], "toothpaste")
        self.assertEqual(renamed["state"], "OPEN")
        rows = services.list_shopping_items(
            self.actor("shop-add-post", "+60111111111"), scope="family"
        )["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_name"], "toothpaste")
        self.assertEqual(rows[0]["status"], "OPEN")

    def test_reminder_snooze_is_from_now_reopens_and_aggregate_history_works(self):
        self.claim("snooze-post", "+60111111111", "remind me")
        actor = with_action_key(
            self.actor("snooze-post", "+60111111111"), "snooze-post-action"
        )
        reminder = services.create_reminder(
            actor, "check v05 DM reminder", "2026-09-30T13:40:52+08:00"
        )
        frozen = datetime(2026, 9, 30, 9, 19, 42, tzinfo=timezone.utc)
        with patch.object(runtime_clock, "now_utc", return_value=frozen):
            updated = services.update_reminder(
                self.actor("snooze-post", "+60111111111"),
                reminder["reminder_id"], snooze_minutes=5
            )
        self.assertEqual(updated["state"], "OPEN")
        due = datetime.fromisoformat(updated["due_at_utc"].replace("Z", "+00:00"))
        self.assertEqual(due, frozen + timedelta(minutes=5))
        listed = services.list_reminders(
            self.actor("snooze-post", "+60111111111")
        )["reminders"][0]
        self.assertIn("due_local", listed)
        history = services.reminder_history(
            self.actor("snooze-post", "+60111111111"), reminder["reminder_id"]
        )["history"]
        self.assertEqual(history[-1]["event_type"], "RESCHEDULED")
        self.assertEqual(history[-1]["new_state"], "OPEN")
        aggregate = services.reminder_history(
            self.actor("snooze-post", "+60111111111")
        )
        self.assertTrue(aggregate["aggregate"])
        self.assertTrue(any(
            x["reminder_id"] == reminder["reminder_id"]
            for x in aggregate["history"]
        ))

    def test_reminder_destination_is_intent_not_command_origin(self):
        group_id = "120363999999@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)

        db.claim_inbound({
            "message_id": "group-remind-me",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "@Alex remind me tomorrow",
        })
        group_actor = with_action_key(
            db.resolve_actor(
                "+60111111111", group_id, "GROUP", "group-remind-me", []
            ),
            "group-remind-me-action",
        )
        mine = services.create_reminder(
            group_actor, "personal thing", "2026-10-01T09:00:00+08:00",
            recipient="me", destination="dm",
        )
        self.assertEqual(mine["conversation_id"], "60111111111@s.whatsapp.net")

        self.claim("group-dest", "+60111111111", "put reminder in group at 6pm")
        dm_actor = with_action_key(
            self.actor("group-dest", "+60111111111"), "group-dest-action"
        )
        shared = services.create_reminder(
            dm_actor, "family parcel", "2026-10-01T10:00:00+08:00",
            destination="group",
        )
        self.assertEqual(shared["conversation_id"], group_id)
        self.assertEqual(shared["space"], "FAMILY_SHARED")

    def test_reaction_entrypoint_is_quiet_and_claims_shared_reminder(self):
        group_id = "120363777777@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)

        self.claim("reaction-create", "+60111111111", "group reminder")
        creator = with_action_key(
            self.actor("reaction-create", "+60111111111"),
            "reaction-create-action",
        )
        reminder = services.create_reminder(
            creator, "pick up parcel", "2026-10-01T18:00:00+08:00",
            destination="group",
        )
        self.assertTrue(reminder["claimable"])

        confirmation_id = db.queue_outbound(
            group_id, "TEXT", text="Reminder created.",
            source_message_id="reaction-create",
        )
        reminder_outbound_id = db.queue_outbound(
            group_id, "TEXT", text="Reminder: pick up parcel",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages SET provider_message_id='wa-confirm',
                   delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (confirmation_id,),
            )
            conn.execute(
                """UPDATE outbound_messages SET provider_message_id='wa-fired',
                   delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (reminder_outbound_id,),
            )
            conn.commit()
        finally:
            conn.close()

        ignored = ingress.process({
            "message_id": "reaction-entry-ignore",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60222222222",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-confirm",
            "reaction_text": "👍",
        })
        self.assertTrue(ignored["ok"])
        self.assertEqual(ignored["reaction"]["status"], "not_a_reminder_message")

        claimed = ingress.process({
            "message_id": "reaction-entry-claim",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60222222222",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-fired",
            "reaction_text": "✅",
        })
        self.assertTrue(claimed["ok"])
        self.assertEqual(claimed["reaction"]["status"], "claimed")
        self.assertEqual(claimed["reaction"]["claimed_by_user_id"], "USR_WIFE")

        collision = ingress.process({
            "message_id": "reaction-entry-second",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-fired",
            "reaction_text": "❤️",
        })
        self.assertEqual(collision["reaction"]["status"], "already_claimed")
        self.assertEqual(collision["reaction"]["claimed_by_user_id"], "USR_WIFE")
        self.assertIn("already picked this one up", collision["reaction"]["collision_reply"])
        conn = db.connect()
        try:
            collision_post = conn.execute(
                """SELECT text_body FROM outbound_messages
                   WHERE source_message_id='reaction-entry-second'"""
            ).fetchone()
            owner = conn.execute(
                "SELECT claimed_by_user_id FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()["claimed_by_user_id"]
        finally:
            conn.close()
        self.assertIsNotNone(collision_post)
        self.assertEqual(owner, "USR_WIFE")

        removed = ingress.process({
            "message_id": "reaction-entry-remove",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60222222222",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-fired",
            "reaction_text": "",
        })
        self.assertTrue(removed["ok"])
        self.assertEqual(removed["reaction"]["status"], "ignored_reaction_removal")

        conn = db.connect()
        try:
            reaction_posts = conn.execute(
                """SELECT source_message_id,conversation_id,text_body,context_kind
                   FROM outbound_messages
                   WHERE source_message_id IN (
                     'reaction-entry-ignore','reaction-entry-claim','reaction-entry-remove'
                   )
                   ORDER BY rowid"""
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(reaction_posts), 1)
        self.assertEqual(reaction_posts[0]["source_message_id"], "reaction-entry-claim")
        self.assertEqual(reaction_posts[0]["conversation_id"], "60222222222@s.whatsapp.net")
        self.assertEqual(reaction_posts[0]["context_kind"], "REMINDER_CLAIM_CONFIRMED")
        self.assertIn("claimed", reaction_posts[0]["text_body"].casefold())

    def test_v054_reaction_can_bind_stable_alex_provider_message_id(self):
        group_id = "120363744444@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)
        self.claim("stable-reaction-create", "+60111111111", "group reminder")
        creator = with_action_key(
            self.actor("stable-reaction-create", "+60111111111"),
            "stable-reaction-action",
        )
        reminder = services.create_reminder(
            creator, "stable reaction", "2026-10-01T18:00:00+08:00",
            destination="group",
        )
        outbound_id = db.queue_outbound(
            group_id, "TEXT", text="Reminder: stable reaction",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id=NULL,delivery_status='SENT',
                       delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (outbound_id,),
            )
            conn.commit()
        finally:
            conn.close()
        stable_id = "ALEX" + hashlib.sha256(outbound_id.encode("utf-8")).hexdigest().upper()[:28]
        db.claim_inbound({
            "message_id": "stable-reaction-wife",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60222222222",
            "text": "",
        })
        wife = db.resolve_actor(
            "+60222222222", group_id, "GROUP", "stable-reaction-wife", []
        )
        result = services.claim_reminder_from_reaction(wife, stable_id, "👍")
        self.assertEqual(result["status"], "claimed")
        self.assertEqual(result["claimed_by_user_id"], "USR_WIFE")

    def test_reminder_claim_handoff_transfers_only_after_recipient_reaction(self):
        group_id = "120363888888@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)

        self.claim("handoff-create", "+60111111111", "put reminder in group at 6pm")
        creator = with_action_key(
            replace(
                self.actor("handoff-create", "+60111111111"),
                trusted_text="put reminder in group at 6pm",
            ),
            "handoff-create-action",
        )
        reminder = services.create_reminder(
            creator, "pick up parcel", "2026-10-01T18:00:00+08:00",
            destination="group",
        )

        reminder_outbound_id = db.queue_outbound(
            group_id, "TEXT", text="Reminder: pick up parcel",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages SET provider_message_id='wa-handoff-fired',
                   delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (reminder_outbound_id,),
            )
            conn.commit()
        finally:
            conn.close()

        claimed = ingress.process({
            "message_id": "handoff-claim-husband",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-handoff-fired",
            "reaction_text": "👍",
        })
        self.assertEqual(claimed["reaction"]["claimed_by_user_id"], "USR_HUSBAND")

        self.claim("handoff-request", "+60111111111", "Push this to Priya")
        handoff_actor = replace(
            self.actor("handoff-request", "+60111111111"),
            trusted_text="Push this to Priya",
        )
        requested = services.request_reminder_handoff(
            handoff_actor, reminder["reminder_id"], "Priya"
        )
        self.assertEqual(requested["status"], "handoff_requested")
        self.assertTrue(requested["claimant_unchanged"])

        conn = db.connect()
        try:
            before = conn.execute(
                "SELECT claimed_by_user_id FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()["claimed_by_user_id"]
            outbound = conn.execute(
                """SELECT outbound_id FROM outbound_messages
                   WHERE context_kind='REMINDER_HANDOFF' AND context_id=?""",
                (requested["handoff_id"],),
            ).fetchone()
            conn.execute(
                """UPDATE outbound_messages SET provider_message_id='wa-handoff-dm',
                   delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (outbound["outbound_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(before, "USR_HUSBAND")

        accepted = ingress.process({
            "message_id": "handoff-accept-wife",
            "provider": "WHATSAPP",
            "conversation_id": "60222222222@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60222222222",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-handoff-dm",
            "reaction_text": "✅",
        })
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["reaction"]["status"], "handoff_accepted")
        self.assertEqual(accepted["reaction"]["claimed_by_user_id"], "USR_WIFE")

        conn = db.connect()
        try:
            after = conn.execute(
                "SELECT claimed_by_user_id FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()["claimed_by_user_id"]
            status = conn.execute(
                "SELECT status FROM reminder_handoffs WHERE handoff_id=?",
                (requested["handoff_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(after, "USR_WIFE")
        self.assertEqual(status, "ACCEPTED")
        conn = db.connect()
        try:
            notice = conn.execute(
                """SELECT text_body FROM outbound_messages
                   WHERE conversation_id='60111111111@s.whatsapp.net'
                     AND context_kind='REMINDER_HANDOFF_ACCEPTED'
                     AND context_id=?""",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(notice)
        self.assertIn("Priya has taken over", notice["text_body"])
        self.assertIn("pick up parcel", notice["text_body"])

    def test_reminder_handoff_does_not_overwrite_pending_or_unreachable_recipient(self):
        # With no active recipient phone, ownership must remain untouched.
        self.claim("handoff-no-phone-create", "+60111111111", "shared reminder")
        creator = with_action_key(
            self.actor("handoff-no-phone-create", "+60111111111"),
            "handoff-no-phone-action",
        )
        reminder = services.create_reminder(
            creator, "parcel", "2026-10-01T18:00:00+08:00", destination="group"
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE reminders SET claimed_by_user_id='USR_HUSBAND',
                   claimed_at_utc=CURRENT_TIMESTAMP WHERE reminder_id=?""",
                (reminder["reminder_id"],),
            )
            wife_phone = conn.execute(
                """SELECT history_id FROM user_phone_history
                   WHERE user_id='USR_WIFE' AND valid_to_utc IS NULL"""
            ).fetchone()
            if wife_phone:
                conn.execute(
                    "UPDATE user_phone_history SET valid_to_utc=CURRENT_TIMESTAMP WHERE history_id=?",
                    (wife_phone["history_id"],),
                )
            conn.commit()
        finally:
            conn.close()

        self.claim("handoff-no-phone", "+60111111111", "Ask Priya to take this")
        actor = replace(
            self.actor("handoff-no-phone", "+60111111111"),
            trusted_text="Ask Priya to take this",
        )
        result = services.request_reminder_handoff(
            actor, reminder["reminder_id"], "Priya"
        )
        self.assertEqual(result["status"], "recipient_not_configured")
        self.assertTrue(result["claimant_unchanged"])
        conn = db.connect()
        try:
            claimant = conn.execute(
                "SELECT claimed_by_user_id FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()["claimed_by_user_id"]
        finally:
            conn.close()
        self.assertEqual(claimant, "USR_HUSBAND")
        # Restore test fixture phone for following tests.
        db.initialize()

    def test_monitor_home_state_executes_real_tool_body(self):
        self.claim("monitor-entry", "+60111111111", "tell me when the Hall AC turns on")
        actor = self.actor("monitor-entry", "+60111111111")
        with patch.object(
            ha, "get_state",
            return_value={"state": "off", "attributes": {"friendly_name": "Hall AC"}},
        ), patch.object(
            phase2_delegation, "create_delegation",
            return_value={"delegation_id": "monitor-1", "status": "ACTIVE"},
        ) as create:
            result = mcp_server.monitor_home_state(
                "climate.hall_ac", "on", actor
            )
        self.assertEqual(result["monitor_kind"], "HA_STATE")
        self.assertEqual(result["current_state"], "off")
        create.assert_called_once()

    def test_home_state_monitor_is_checked_each_scheduler_loop(self):
        scheduler._last_monitor_hour = None
        with patch.object(
            phase2_monitor, "bill_candidates", return_value=[]
        ) as bills, patch.object(
            phase2_monitor, "goal_candidates", return_value=[]
        ), patch.object(
            phase2_monitor, "ot_allocation_candidates", return_value=[]
        ), patch.object(
            phase2_monitor, "home_state_candidates", return_value=[]
        ) as home:
            scheduler.run_delegated_monitors()
            first_home_calls = home.call_count
            first_bill_calls = bills.call_count
            scheduler.run_delegated_monitors()
        self.assertGreater(first_home_calls, 0)
        self.assertEqual(home.call_count, first_home_calls * 2)
        self.assertEqual(bills.call_count, first_bill_calls)

    def test_claimable_group_reminder_first_reaction_wins_and_release_is_explicit(self):
        group_id = "120363999999@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)

        db.claim_inbound({
            "message_id": "claimable-create",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "@Alex remind the family about parcel pickup",
        })
        creator = with_action_key(
            db.resolve_actor(
                "+60111111111", group_id, "GROUP", "claimable-create", []
            ),
            "claimable-create-action",
        )
        reminder = services.create_reminder(
            creator, "pick up parcel", "2026-10-01T18:00:00+08:00",
            destination="group", claimable=True,
        )
        outbound_id = db.queue_outbound(
            group_id, "TEXT", text="⏰ Reminder: pick up parcel",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-reminder-1',delivery_status='SENT',
                       delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (outbound_id,),
            )
            conn.commit()
        finally:
            conn.close()

        db.claim_inbound({
            "message_id": "wife-reaction",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60222222222",
            "text": "",
        })
        wife = db.resolve_actor(
            "+60222222222", group_id, "GROUP", "wife-reaction", []
        )
        claimed = services.claim_reminder_from_reaction(
            wife, "wa-reminder-1", "👍"
        )
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["claimed_by_user_id"], "USR_WIFE")

        # A different emoji or second household member must not steal the claim.
        second = services.claim_reminder_from_reaction(
            creator, "wa-reminder-1", "❤️"
        )
        self.assertEqual(second["status"], "already_claimed")
        self.assertEqual(second["claimed_by_user_id"], "USR_WIFE")

        # Removing the reaction is intentionally a no-op.
        removed = services.claim_reminder_from_reaction(
            wife, "wa-reminder-1", ""
        )
        self.assertEqual(removed["status"], "ignored_reaction_removal")
        history = services.reminder_history(
            wife, reminder["reminder_id"]
        )["claim_history"]
        self.assertEqual([x["event_type"] for x in history], ["CLAIMED"])

        released = services.release_reminder_claim(
            wife, reminder["reminder_id"]
        )
        self.assertEqual(released["status"], "released")
        history = services.reminder_history(
            wife, reminder["reminder_id"]
        )["claim_history"]
        self.assertEqual(
            [x["event_type"] for x in history], ["CLAIMED", "RELEASED"]
        )

    def test_claimed_reminder_follows_claimant_then_resurfaces_family(self):
        group_id = "120363888888@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)

        self.claim("claim-schedule-create", "+60111111111", "group reminder")
        creator = with_action_key(
            self.actor("claim-schedule-create", "+60111111111"),
            "claim-schedule-create-action",
        )
        reminder = services.create_reminder(
            creator, "collect parcel", "2026-09-30T18:00:00+08:00",
            destination="group", claimable=True, follow_up_after_hours=1,
        )

        # Claim before the due time from a bound setup message.
        setup_id = db.queue_outbound(
            group_id, "TEXT", text="Reminder created: collect parcel",
            context_kind="REMINDER_SETUP", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-claim-setup',delivery_status='SENT',
                       delivered_at_utc='2026-09-30T09:50:00+00:00'
                   WHERE outbound_id=?""",
                (setup_id,),
            )
            conn.execute(
                """UPDATE reminders SET due_at_utc='2026-09-30T10:00:00+00:00'
                   WHERE reminder_id=?""",
                (reminder["reminder_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        db.claim_inbound({
            "message_id": "wife-claim-schedule",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60222222222",
            "text": "",
        })
        wife = db.resolve_actor(
            "+60222222222", group_id, "GROUP", "wife-claim-schedule", []
        )
        with patch.object(
            runtime_clock, "now_utc",
            return_value=datetime(2026, 9, 30, 9, 55, tzinfo=timezone.utc),
        ):
            claimed = services.claim_reminder_from_reaction(
                wife, "wa-claim-setup", "✅"
            )
        self.assertEqual(claimed["status"], "claimed")

        # At due time the initial reminder must go to the claimant DM, not group.
        with patch.object(
            runtime_clock, "now_utc",
            return_value=datetime(2026, 9, 30, 10, 1, tzinfo=timezone.utc),
        ):
            scheduler.fire_due()
        conn = db.connect()
        try:
            initial = conn.execute(
                """SELECT conversation_id,context_kind FROM outbound_messages
                   WHERE context_id=? AND context_kind='REMINDER_INITIAL'
                   ORDER BY rowid DESC LIMIT 1""",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(initial)
        self.assertEqual(initial["conversation_id"], "60222222222@s.whatsapp.net")

        with patch.object(
            runtime_clock, "now_utc",
            return_value=datetime(2026, 9, 30, 11, 6, tzinfo=timezone.utc),
        ):
            scheduler.fire_due()

        conn = db.connect()
        try:
            private_follow = conn.execute(
                """SELECT conversation_id,context_kind,text_body
                   FROM outbound_messages
                   WHERE context_id=? AND context_kind='REMINDER_CLAIMANT_FOLLOWUP'
                   ORDER BY rowid DESC LIMIT 1""",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(private_follow)
        self.assertEqual(
            private_follow["conversation_id"], "60222222222@s.whatsapp.net"
        )

        with patch.object(
            runtime_clock, "now_utc",
            return_value=datetime(2026, 9, 30, 12, 7, tzinfo=timezone.utc),
        ):
            scheduler.fire_due()

        conn = db.connect()
        try:
            escalated = conn.execute(
                """SELECT conversation_id,context_kind,text_body
                   FROM outbound_messages
                   WHERE context_id=? AND context_kind='REMINDER_INITIATOR_ESCALATION'
                   ORDER BY rowid DESC LIMIT 1""",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(escalated)
        self.assertEqual(escalated["conversation_id"], "60111111111@s.whatsapp.net")
        self.assertIn("Still unresolved", escalated["text_body"])
        self.assertIn("Wife", escalated["text_body"])

    def test_home_state_monitor_is_explicit_one_shot_candidate(self):
        self.claim("home-monitor-create", "+60111111111", "monitor hall ac")
        delegation = phase2_delegation.create_delegation(
            "CUSTOM", "Hall AC", "+60111111111", "home-monitor-create",
            "DIRECT_DM", "private",
            {
                "kind": "HA_STATE",
                "entity_id": "climate.hall_ac",
                "target_state": "on",
                "one_shot": True,
            },
            True,
        )
        with patch.object(
            ha, "get_state",
            return_value={
                "entity_id": "climate.hall_ac",
                "state": "off",
                "attributes": {"friendly_name": "Hall AC"},
            },
        ):
            self.assertEqual(
                phase2_monitor.home_state_candidates("+60111111111"), []
            )
        with patch.object(
            ha, "get_state",
            return_value={
                "entity_id": "climate.hall_ac",
                "state": "on",
                "attributes": {"friendly_name": "Hall AC"},
            },
        ):
            candidates = phase2_monitor.home_state_candidates("+60111111111")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["delegation_id"], delegation["delegation_id"])
        self.assertEqual(candidates[0]["target_state"], "on")
        self.assertTrue(candidates[0]["one_shot"])

    def test_declared_stash_balance_and_spend_do_not_require_cash_event(self):
        pool = phase2_finance.create_cash_pool(
            "Stash", "+60111111111", visibility="private"
        )
        declared = phase2_finance.declare_cash_pool_balance(
            pool["pool_id"], 300, "2026-09-30", "+60111111111",
            source_message_id="stash-declare-1",
        )
        self.assertEqual(declared["balance"], 300.0)
        balance = phase2_finance.cash_pool_balance(
            pool["pool_id"], "+60111111111"
        )
        self.assertEqual(balance["balance"], 300.0)
        self.assertEqual(balance["cash_event_allocations"], 0.0)

        spent = phase2_finance.record_cash_pool_spend(
            pool["pool_id"], 40, "2026-09-30", "+60111111111",
            category="food", funding_source="stash",
            source_message_id="stash-spend-1",
        )
        self.assertEqual(spent["spent"], 40.0)
        self.assertEqual(spent["balance"], 260.0)
        after = phase2_finance.cash_pool_balance(
            pool["pool_id"], "+60111111111"
        )
        self.assertEqual(after["balance"], 260.0)

        conn = db.connect()
        try:
            rows = conn.execute(
                """SELECT adjustment_kind,amount_minor,category,funding_source
                   FROM alex_phase2_cash_pool_adjustments
                   WHERE pool_id=? ORDER BY created_at_utc,rowid""",
                (pool["pool_id"],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["adjustment_kind"], "OPENING_BALANCE")
        self.assertEqual(rows[0]["amount_minor"], 30000)
        self.assertEqual(rows[1]["adjustment_kind"], "SPEND")
        self.assertEqual(rows[1]["amount_minor"], -4000)
        self.assertEqual(rows[1]["category"], "food")

    def test_leave_new_write_scope_follows_current_trusted_command(self):
        cases = (
            ("leave-scope-shared", "I'm on annual leave 6 October 2026, save that", "FAMILY_SHARED"),
            ("leave-scope-private", "Save my annual leave 7 October 2026 privately", "HUSBAND_PVT"),
            ("leave-scope-emoji", "Save my annual leave 8 October 2026 🙂", "HUSBAND_PVT"),
        )
        for mid, trusted_text, expected_space in cases:
            self.claim(mid, "+60111111111", trusted_text)
            actor = with_action_key(
                replace(
                    self.actor(mid, "+60111111111"),
                    trusted_text=trusted_text,
                ),
                mid + "-action",
            )
            day = {
                "leave-scope-shared": "2026-10-06",
                "leave-scope-private": "2026-10-07",
                "leave-scope-emoji": "2026-10-08",
            }[mid]
            result = phase2.set_leave_record(
                actor, day, "PLANNED", "FULL", leave_type="ANNUAL_LEAVE"
            )
            self.assertEqual(result["space"], expected_space)

        group = self.group_actor("leave-scope-group", "+60111111111")
        visible = phase2.list_leave_records(
            group, "2026-10-06", "2026-10-08"
        )["leave"]
        self.assertEqual([row["leave_date"] for row in visible], ["2026-10-06"])

    def test_leave_full_to_half_edits_same_record(self):
        self.claim("leave-full", "+60111111111", "record annual leave")
        first_actor = with_action_key(
            self.actor("leave-full", "+60111111111"), "leave-full-action"
        )
        first = phase2.set_leave_record(
            first_actor, "2026-10-03", "PLANNED", "FULL"
        )
        self.claim("leave-half", "+60111111111", "change leave to half day")
        second_actor = with_action_key(
            self.actor("leave-half", "+60111111111"), "leave-half-action"
        )
        second = phase2.set_leave_record(
            second_actor, "2026-10-03", "PLANNED", "HALF"
        )
        self.assertEqual(first["leave_id"], second["leave_id"])
        self.assertEqual(second["portion"], "HALF")
        records = phase2.list_leave_records(
            self.actor("leave-half", "+60111111111"),
            "2026-10-03", "2026-10-03"
        )["leave"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["portion"], "HALF")

    def test_confirmed_plan_cancel_cascades_to_linked_diary(self):
        self.claim("plan-create-post", "+60111111111", "create plan")
        create_actor = with_action_key(
            self.actor("plan-create-post", "+60111111111"), "plan-create-post-action"
        )
        plan = phase2.create_plan(
            create_actor, "v05 post-smoke plan",
            "2026-10-20T18:00:00+08:00",
            "2026-10-20T19:00:00+08:00",
        )
        self.claim("plan-confirm-post", "+60111111111", "confirm plan")
        confirm_actor = with_action_key(
            self.actor("plan-confirm-post", "+60111111111"), "plan-confirm-post-action"
        )
        confirmed = phase2.confirm_plan(confirm_actor, plan["plan_id"])
        self.assertTrue(confirmed.get("diary_id"))
        self.claim("plan-cancel-post", "+60111111111", "cancel plan")
        cancel_actor = with_action_key(
            self.actor("plan-cancel-post", "+60111111111"), "plan-cancel-post-action"
        )
        cancelled = phase2.update_plan(
            cancel_actor, plan["plan_id"], status="CANCELLED"
        )
        self.assertTrue(cancelled["linked_diary_updated"])
        conn = db.connect()
        try:
            diary = conn.execute(
                "SELECT status FROM diary_events WHERE diary_id=?",
                (confirmed["diary_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(diary["status"], "CANCELLED")

    def test_goal_target_edit_preserves_baseline_and_deadline(self):
        self.claim("goal-create-post", "+60111111111", "create iphone goal")
        created = phase2_finance.create_goal(
            "iPhone", 3000, 0, "+60111111111",
            target_date=None, status="DRAFT"
        )
        changed = phase2_finance.update_goal_target(
            created["goal_id"], 3500, "+60111111111"
        )
        self.assertEqual(changed["target"], 3500.0)
        self.assertEqual(changed["baseline_monthly"], 0.0)
        self.assertIsNone(changed["target_date"])
        self.assertFalse(changed["baseline_changed"])
        self.assertFalse(changed["deadline_changed"])

    def test_asset_warranty_update_preserves_asset_identity(self):
        asset = phase2_library.create_asset(
            "v05 toaster", "+60111111111", warranty_end="2026-10-01"
        )
        changed = phase2_library.update_asset(
            asset["asset_id"], "+60111111111", warranty_end="2027-12-01"
        )
        self.assertEqual(changed["asset_id"], asset["asset_id"])
        self.assertEqual(changed["warranty_end"], "2027-12-01")
        listed = phase2_library.list_assets("+60111111111")
        row = next(x for x in listed if x["asset_id"] == asset["asset_id"])
        self.assertEqual(row["warranty_end"], "2027-12-01")

    def test_receipt_search_uses_caption_and_token_matching(self):
        self.claim(
            "caption-receipt-post", "+60111111111",
            "September house payment receipt"
        )
        media_id = media.save_media(
            "caption-receipt-post", "IMAGE", "image/jpeg",
            base64.b64encode(b"receipt").decode(),
        )
        actor = self.actor("caption-receipt-post", "+60111111111", [media_id])
        found = services.find_receipts(
            actor, query="latest house payment receipt"
        )["matches"]
        self.assertTrue(any(x["media_id"] == media_id for x in found))
        match = next(x for x in found if x["media_id"] == media_id)
        self.assertEqual(match["label"], "September house payment receipt")

    def test_presenter_scaffolding_is_rejected_and_false_success_is_guarded(self):
        leaked = (
            "Self-correction/Constraint Check: The user wants me to rewrite my "
            "previous answer. Previous Answer Analysis: ..."
        )
        self.assertIsNone(brain._validated_rewrite("மன்னிக்கவும்", leaked))
        clean = "Your dentist appointment is at 4:00 PM tomorrow."
        self.assertEqual(brain._validated_rewrite("தமிழ்", clean), clean)

        guarded = brain._guard_mutation_success(
            "Done, I've updated it.",
            "Change the toaster warranty expiry to tomorrow",
            [{"tool": "asset_update", "committed": False, "reason": "not_found"}],
        )
        self.assertIn("won't claim", guarded)
        self.assertNotIn("I've updated", guarded)

    def test_v054_group_handoff_only_for_explicit_private_or_sensitive_profile(self):
        self.assertFalse(ingress._private_group_handoff_requested("show me the latest expenses"))
        self.assertFalse(ingress._private_group_handoff_requested("show me the receipt for our blender"))
        self.assertTrue(ingress._private_group_handoff_requested("how much did I spend privately this month?"))
        # Global v0.5.7 rule: plain wording stays Family Shared even for
        # historically-sensitive domains. Explicit private wording/emoji is
        # what authorizes owner-DM handoff.
        self.assertFalse(ingress._private_group_handoff_requested("show me my salary"))
        self.assertTrue(ingress._private_group_handoff_requested("show me my private salary"))
        self.assertTrue(ingress._private_group_handoff_requested("save this privately: test note"))

    def test_v054_shared_saved_receipt_is_retrievable_from_family_group(self):
        self.claim("shared-receipt-v054", "+60111111111", "Alex, save this as the receipt for our Philips blender.")
        media_id = media.save_media(
            "shared-receipt-v054", "IMAGE", "image/jpeg",
            base64.b64encode(b"mock receipt").decode(),
        )
        media._update_text(media_id, "ocr_text", "SENHENG TAX INVOICE PHILIPS BLENDER")
        dm = with_action_key(
            replace(
                self.actor("shared-receipt-v054", "+60111111111", [media_id]),
                trusted_text="Alex, save this as the receipt for our Philips blender.",
            ),
            "shared-receipt-v054-action",
        )
        saved = services.save_item(
            dm, "receipt for our Philips blender",
            "Senheng receipt for our Philips blender"
        )
        self.assertEqual(saved["space"], "FAMILY_SHARED")

        group = self.group_actor("shared-receipt-v054-group", "+60111111111")
        found = services.find_receipts(group, query="our Philips blender")["matches"]
        match = next(x for x in found if x["media_id"] == media_id)
        self.assertEqual(match["scope"], "family")
        retrieved = services.get_receipt(group, media_id)
        self.assertEqual(retrieved["status"], "found_saved_receipt")
        self.assertEqual(retrieved["scope"], "family")

    def test_v054_private_receipt_search_excludes_generic_saved_media(self):
        self.claim("generic-media-v054", "+60111111111", "Save this as v054 image memory test privately")
        media_id = media.save_media(
            "generic-media-v054", "IMAGE", "image/jpeg",
            base64.b64encode(b"generic image").decode(),
        )
        media._update_text(media_id, "ocr_text", "ordinary picture no document")
        actor = with_action_key(
            replace(
                self.actor("generic-media-v054", "+60111111111", [media_id]),
                trusted_text="Save this as v054 image memory test privately",
            ),
            "generic-media-v054-action",
        )
        services.save_item(actor, "v054 image memory test", "ordinary saved picture")
        found = services.find_receipts(actor, scope="private")["matches"]
        self.assertFalse(any(x["media_id"] == media_id for x in found))

    def test_v054_deleted_memory_does_not_fall_back_to_partial_word_match(self):
        self.claim("memory-a-v054", "+60111111111", "Remember v054 memory test phrase is blue lantern")
        a = with_action_key(
            replace(self.actor("memory-a-v054", "+60111111111"),
                    trusted_text="Remember v054 memory test phrase is blue lantern"),
            "memory-a-v054-action",
        )
        saved = services.save_item(a, "v054 memory test phrase", "blue lantern")
        self.claim("memory-b-v054", "+60111111111", "Save v054 normal scope test")
        b = with_action_key(
            replace(self.actor("memory-b-v054", "+60111111111"),
                    trusted_text="Save v054 normal scope test"),
            "memory-b-v054-action",
        )
        services.save_item(b, "v054 normal scope test", "v054 normal scope test")
        services.remove_saved_item(a, saved["item_id"])
        result = services.search_saved_items(a, "v054 memory test phrase")
        self.assertEqual(result["count"], 0)

    def test_v054_emoji_privacy_control_is_not_persisted_in_saved_note(self):
        text = "Save this note: v054 emoji scope test 😂"
        self.claim("emoji-content-v054", "+60111111111", text)
        actor = with_action_key(
            replace(self.actor("emoji-content-v054", "+60111111111"), trusted_text=text),
            "emoji-content-v054-action",
        )
        result = services.save_item(actor, "v054 emoji scope test 😂", "v054 emoji scope test 😂")
        self.assertEqual(result["space"], "HUSBAND_PVT")
        found = services.search_saved_items(actor, "v054 emoji scope test")["matches"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "v054 emoji scope test")
        self.assertEqual(found[0]["content"], "v054 emoji scope test")

    def test_v054_report_export_period_preserves_active_finance_context(self):
        self.claim("finance-v054", "+60111111111", "show September 2026 finance report")
        actor = self.actor("finance-v054", "+60111111111")
        services.log_expense(
            with_action_key(actor, "finance-v054-expense"),
            "v054 test expense", 12.34, "food", currency="MYR",
            event_date_local="2026-09-30T10:00:00+08:00",
        )
        report = mcp_server.finance_report(actor, "2026-09")
        self.assertEqual(report["report_type"], "monthly_finance")
        exported = mcp_server.report_export("csv", actor, period="2026-09")
        self.assertEqual(exported["source_report_kind"], "monthly_finance")
        self.assertGreaterEqual(exported["record_count"], 1)
        csv_text = open(exported["_attachments"][0]["path"], encoding="utf-8").read()
        self.assertIn("v054 test expense", csv_text)

    def test_v054_stash_create_opening_balance_list_and_spend(self):
        created = phase2_finance.create_cash_pool(
            "v054 test stash", "+60111111111", visibility="private",
            opening_balance=100, event_date="2026-10-01",
            source_message_id="stash-create-v054",
        )
        self.assertEqual(created["balance"], 100.0)
        pools = phase2_finance.list_cash_pools(
            "+60111111111", requested_scope="private"
        )
        row = next(x for x in pools if x["name"] == "v054 test stash")
        self.assertEqual(row["balance"], 100.0)
        resolved = phase2_finance.resolve_cash_pool_reference(
            None, "v054 test stash", "+60111111111"
        )
        spent = phase2_finance.record_cash_pool_spend(
            resolved, 20, "2026-10-01", "+60111111111",
            category="food", source_message_id="stash-spend-v054",
        )
        self.assertEqual(spent["balance"], 80.0)

    def test_v054_smoke_phrases_route_to_required_tools(self):
        cases = {
            "I'm on annual leave on 6 October 2026. Save that.": {"set_leave_record"},
            "When is v053 reaction test due?": {"list_reminders", "reminder_history"},
            "What stash or cash pools do I currently have?": {"planning_list_cash_pools"},
            "Create a new stash called v054 test stash and put RM100 in it.": {"planning_create_cash_pool"},
            "Generate my September 2026 finance report as PDF.": {"finance_report", "report_export"},
        }
        for phrase, required in cases.items():
            names = {x["function"]["name"] for x in asyncio.run(brain._tool_specs(phrase))}
            self.assertTrue(required <= names, (phrase, names))

    def test_v054_home_card_is_landscape_premium_png(self):
        payload = phase2_home.render_home_report_png({
            "status": "ATTENTION",
            "attention_count": 2,
            "total_entities": 42,
            "people_home": ["Husband"],
            "lights_on": ["Hall Pendant"],
            "open_entries": ["Kitchen Door"],
            "unlocked": [],
            "climate_active": ["Hall AC"],
            "media_playing": [],
            "unavailable": ["TV"],
        })
        self.assertTrue(payload.startswith(bytes.fromhex("89504e470d0a1a0a")))
        width = int.from_bytes(payload[16:20], "big")
        height = int.from_bytes(payload[20:24], "big")
        self.assertGreater(width, height)
        self.assertGreaterEqual(width, 960)

    def test_v057_cash_pool_follows_global_scope_policy_and_preserves_space(self):
        self.claim("pool-family-create", "+60111111111", "Create a stash with RM100")
        family_actor = replace(
            self.actor("pool-family-create", "+60111111111"),
            trusted_text="Create a stash with RM100",
            read_scope="family",
        )
        family_pool = mcp_server.planning_create_cash_pool(
            "v057 family stash", family_actor, opening_balance=100
        )
        self.assertEqual(family_pool["space"], "FAMILY_SHARED")

        self.claim(
            "pool-private-create", "+60111111111",
            "Create a private stash with RM50 😊"
        )
        private_actor = replace(
            self.actor("pool-private-create", "+60111111111"),
            trusted_text="Create a private stash with RM50 😊",
            read_scope="private",
        )
        private_pool = mcp_server.planning_create_cash_pool(
            "v057 private stash", private_actor, opening_balance=50
        )
        self.assertEqual(private_pool["space"], "HUSBAND_PVT")

        family_names = {
            row["name"]
            for row in mcp_server.planning_list_cash_pools(family_actor)["pools"]
        }
        private_names = {
            row["name"]
            for row in mcp_server.planning_list_cash_pools(private_actor)["pools"]
        }
        self.assertIn("v057 family stash", family_names)
        self.assertNotIn("v057 private stash", family_names)
        self.assertIn("v057 private stash", private_names)
        self.assertNotIn("v057 family stash", private_names)

        # Schema maintenance must preserve the stored visibility; it must never
        # silently rewrite Family pools into owner-private pools.
        phase2_finance.ensure_schema()
        conn = db.connect()
        try:
            spaces = {
                row["pool_id"]: row["space_id"]
                for row in conn.execute(
                    """SELECT pool_id,space_id FROM alex_phase2_cash_pools
                       WHERE pool_id IN (?,?)""",
                    (family_pool["pool_id"], private_pool["pool_id"]),
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(spaces[family_pool["pool_id"]], "FAMILY_SHARED")
        self.assertEqual(spaces[private_pool["pool_id"]], "HUSBAND_PVT")

    def test_v054_postaudit_caption_only_shared_receipt_gets_in_group(self):
        caption = "July TNB receipt - share with the family"
        self.claim("caption-only-shared-receipt", "+60111111111", caption)
        media_id = media.save_media(
            "caption-only-shared-receipt", "IMAGE", "image/jpeg",
            base64.b64encode(b"caption only receipt").decode(),
        )
        dm = with_action_key(
            replace(
                self.actor("caption-only-shared-receipt", "+60111111111", [media_id]),
                trusted_text=caption,
            ),
            "caption-only-shared-receipt-action",
        )
        saved = services.save_item(dm, "TNB July", "electricity bill July")
        self.assertEqual(saved["space"], "FAMILY_SHARED")
        group = self.group_actor("caption-only-shared-receipt-group", "+60111111111")
        found = services.find_receipts(group, query="tnb")["matches"]
        self.assertTrue(any(row["media_id"] == media_id for row in found))
        retrieved = services.get_receipt(group, media_id)
        self.assertEqual(retrieved["status"], "found_saved_receipt")
        self.assertEqual(retrieved["scope"], "family")

    def test_v054_postaudit_period_change_stays_finance_report(self):
        self.claim("period-change-report", "+60111111111", "show September finance report")
        actor = self.actor("period-change-report", "+60111111111")
        services.log_expense(
            with_action_key(actor, "period-change-sep"), "September meal", 10,
            "food", currency="MYR", event_date_local="2026-09-10T10:00:00+08:00",
        )
        services.log_expense(
            with_action_key(actor, "period-change-aug"), "August meal", 20,
            "food", currency="MYR", event_date_local="2026-08-10T10:00:00+08:00",
        )
        mcp_server.finance_report(actor, "2026-09")
        exported = mcp_server.report_export("csv", actor, period="2026-08")
        self.assertEqual(exported["source_report_kind"], "monthly_finance")
        csv_text = open(exported["_attachments"][0]["path"], encoding="utf-8").read()
        self.assertIn("August meal", csv_text)
        self.assertNotIn("September meal", csv_text)

    def test_v054_postaudit_same_turn_probe_does_not_replace_monthly_report(self):
        self.claim("same-turn-report", "+60111111111", "show September finance report")
        actor = self.actor("same-turn-report", "+60111111111")
        services.log_expense(
            with_action_key(actor, "same-turn-food"), "Lunch", 12,
            "food", currency="MYR", event_date_local="2026-09-10T10:00:00+08:00",
        )
        services.log_expense(
            with_action_key(actor, "same-turn-fuel"), "Shell petrol", 30,
            "fuel", currency="MYR", event_date_local="2026-09-11T10:00:00+08:00",
        )
        mcp_server.finance_report(actor, "2026-09")
        mcp_server.query_finances(
            actor, start_date="2026-09-01", end_date="2026-09-30", category="food"
        )
        active = phase2_reports.load_active_report(actor.user_id, actor.conversation_id)
        self.assertEqual(active["kind"], "monthly_finance")
        exported = mcp_server.report_export("csv", actor)
        csv_text = open(exported["_attachments"][0]["path"], encoding="utf-8").read()
        self.assertIn("Lunch", csv_text)
        self.assertIn("Shell petrol", csv_text)

    def test_v054_postaudit_common_obligation_due_questions_keep_bills(self):
        for phrase in (
            "When is my credit card due?",
            "When is the rent due?",
            "When is my car loan due?",
        ):
            names = {x["function"]["name"] for x in asyncio.run(brain._tool_specs(phrase))}
            self.assertIn("bills_list", names, (phrase, names))

    def test_v054_postaudit_delivery_guard_preserves_compound_content(self):
        value = (
            "Logged RM20 petrol (Transport). Your September PDF is on its way, "
            "and 2 items still need a category."
        )
        guarded = brain._guard_delivery_claim(value, [{"path": "/tmp/report.pdf"}])
        self.assertIn("Logged RM20 petrol", guarded)
        self.assertIn("2 items still need a category", guarded)
        self.assertNotIn("on its way", guarded.casefold())
        self.assertIn("attached", guarded.casefold())

    def test_v054_postaudit_managed_job_reconcile_does_not_precede_normal_send(self):
        self.claim("stuck-doc-source", "+60111111111", "send document")
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO outbound_messages(
                       outbound_id,source_message_id,conversation_id,kind,text_body,
                       local_path,mime_type,delivery_status,attempt_count,last_error
                   ) VALUES(
                       'stuck-document','stuck-doc-source','60111111111@s.whatsapp.net',
                       'DOCUMENT','doc','/tmp/missing.pdf','application/pdf','FAILED',3,'missing'
                   )"""
            )
            conn.execute(
                """INSERT INTO outbound_messages(
                       outbound_id,conversation_id,kind,text_body,delivery_status
                   ) VALUES(
                       'normal-text', '60111111111@s.whatsapp.net','TEXT','hello','PENDING'
                   )"""
            )
            conn.commit()
        finally:
            conn.close()
        calls = []
        def fake_send(payload):
            calls.append(payload.get("kind"))
            return (payload.get("kind") == "text", "{}")
        outbox._STARTUP_MANAGED_JOB_RECONCILED = False
        with patch.object(outbox, "_send", side_effect=fake_send):
            outbox.sweep()
            first_sweep = list(calls)
            outbox.sweep()
        self.assertTrue(first_sweep)
        self.assertEqual(first_sweep[0], "text")
        self.assertEqual(calls.count("reaction"), first_sweep.count("reaction"))
        self.assertEqual(calls.count("pin"), first_sweep.count("pin"))

    def test_v054_postaudit_category_aliases_roll_up_food_and_drink(self):
        self.claim("category-rollup", "+60111111111", "finance")
        actor = self.actor("category-rollup", "+60111111111")
        first = services.log_expense(
            with_action_key(actor, "category-food"), "Lunch", 21.29,
            "food", currency="MYR", event_date_local="2026-09-10T10:00:00+08:00",
        )
        second = services.log_expense(
            with_action_key(actor, "category-food-drink"), "Coffee", 12.34,
            "food", currency="MYR", event_date_local="2026-09-11T10:00:00+08:00",
        )
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE financial_events SET category='food_drink' WHERE event_id=?",
                (second["event_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        result = services.query_finances(
            actor, start_date="2026-09-01", end_date="2026-09-30",
            include_all_records=True,
        )
        food = [row for row in result["category_totals"] if row["category"] == "food"]
        self.assertEqual(len(food), 1)
        self.assertAlmostEqual(food[0]["amount"], 33.63, places=2)
        self.assertEqual(phase2_reports._human_category("food"), "Food & Drink")

    def test_v054_postaudit_direct_nonhandoff_reaction_is_ignored(self):
        self.claim("direct-reaction-create", "+60111111111", "remind me")
        creator = with_action_key(
            self.actor("direct-reaction-create", "+60111111111"),
            "direct-reaction-create-action",
        )
        reminder = services.create_reminder(
            creator, "personal task", "2026-10-02T10:00:00+08:00"
        )
        outbound_id = db.queue_outbound(
            "60111111111@s.whatsapp.net", "TEXT", text="Reminder: personal task",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages SET delivery_status='SENT',
                   provider_message_id='wa-direct-reminder',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (outbound_id,),
            )
            conn.commit()
        finally:
            conn.close()
        actor = self.actor("direct-reaction-actor", "+60111111111")
        result = services.claim_reminder_from_reaction(actor, "wa-direct-reminder", "👍")
        self.assertEqual(result["status"], "ignored_direct_reminder_reaction")

    def test_v055_final_model_call_is_answer_only_and_does_not_execute_a_fourth_tool(self):
        class FakeFunction:
            def __init__(self, name, arguments="{}"):
                self.name = name
                self.arguments = arguments

        class FakeCall:
            def __init__(self, call_id, name):
                self.id = call_id
                self.function = FakeFunction(name)

        class FakeMessage:
            def __init__(self, content="", calls=None):
                self.content = content
                self.tool_calls = calls or []

            def model_dump(self, exclude_none=True):
                payload = {"role": "assistant", "content": self.content}
                if self.tool_calls:
                    payload["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in self.tool_calls
                    ]
                return payload

        class FakeResponse:
            def __init__(self, message):
                self.choices = [type("Choice", (), {"message": message})()]
                self.usage = None

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                n = len(self.calls)
                if n <= 3:
                    return FakeResponse(
                        FakeMessage(calls=[FakeCall(f"call-{n}", "list_reminders")])
                    )
                # Regression target: the fourth and final model call must be
                # answer-only, not another executable tool round.
                self.assert_final(kwargs)
                return FakeResponse(FakeMessage(content="Final answer from tool results."))

            @staticmethod
            def assert_final(kwargs):
                if "tools" in kwargs or "tool_choice" in kwargs:
                    raise AssertionError(
                        "final answer call must not expose tools or tool_choice"
                    )

        fake_completions = FakeCompletions()
        fake_client = type(
            "FakeClient",
            (),
            {"chat": type("FakeChat", (), {"completions": fake_completions})()},
        )()
        executed = []

        async def fake_call_mcp(actor, name, args, action_key):
            executed.append(name)
            return ({"status": "ok", "round": len(executed)}, [])

        self.claim("v055-loop-budget", "+60111111111", "What reminders do I have?")
        actor = self.actor("v055-loop-budget", "+60111111111")
        route = {
            "provider": "grok",
            "model": "stub",
            "reasoning_effort": "low",
            "role": "manual",
        }
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=fake_client), \
             patch.object(brain, "_call_mcp", new=fake_call_mcp):
            reply, attachments = asyncio.run(
                brain.respond(actor, "What reminders do I have?")
            )
        self.assertEqual(reply, "Final answer from tool results.")
        self.assertEqual(attachments, [])
        self.assertEqual(len(executed), 3)
        self.assertEqual(
            [call.get("tool_choice") for call in fake_completions.calls],
            ["auto", "auto", "auto", None],
        )

    def test_v055_read_tools_are_not_misclassified_as_mutations(self):
        self.assertFalse(brain._is_mutating_tool("planning_list_cash_pools"))
        self.assertFalse(brain._is_mutating_tool("finance_report"))

    def test_v055_attachment_completion_covers_live_wording_and_report_exports(self):
        guarded = brain._guard_delivery_claim(
            "I've generated the September report. It is attached through shortly.",
            [{"path": "/tmp/report.csv"}],
        )
        self.assertNotIn("shortly", guarded.casefold())
        self.assertIn("attached", guarded.casefold())
        self.assertTrue(brain._attachment_request_finished({
            "compound": False,
            "tools_called": ["finance_report", "report_export"],
        }))

    def test_v057_named_private_pool_requires_current_private_scope(self):
        self.claim(
            "v057-pocket-private-create", "+60111111111",
            "Create pocket cash privately 😊"
        )
        creator = replace(
            self.actor("v057-pocket-private-create", "+60111111111"),
            trusted_text="Create pocket cash privately 😊",
            read_scope="private",
        )
        created = mcp_server.planning_create_cash_pool(
            "v057 pocket cash", creator, opening_balance=100
        )
        self.assertEqual(created["space"], "HUSBAND_PVT")

        family_actor = replace(
            creator,
            trusted_text="How much do I have in v057 pocket cash?",
            read_scope="family",
        )
        private_actor = replace(
            creator,
            trusted_text="How much do I have in v057 pocket cash? 😊",
            read_scope="private",
        )
        self.assertFalse(
            brain._owned_cash_pool_name_mentioned(
                family_actor, "How much do I have in v057 pocket cash?"
            )
        )
        self.assertTrue(
            brain._owned_cash_pool_name_mentioned(
                private_actor, "How much do I have in v057 pocket cash? 😊"
            )
        )

        with use_actor(family_actor):
            with self.assertRaisesRegex(ValueError, "current scope|authorized"):
                phase2_finance.resolve_cash_pool_reference(
                    None, "v057 pocket cash", creator.phone, "DIRECT_DM"
                )
        with use_actor(private_actor):
            resolved = phase2_finance.resolve_cash_pool_reference(
                None, "v057 pocket cash", creator.phone, "DIRECT_DM"
            )
            balance = phase2_finance.cash_pool_balance(
                resolved, creator.phone, "DIRECT_DM"
            )
        self.assertEqual(balance["balance"], 100.0)

        self.assertFalse(
            ingress._private_group_handoff_requested(
                "How much do I have in v057 pocket cash?"
            )
        )
        self.assertTrue(
            ingress._private_group_handoff_requested(
                "How much do I have in v057 pocket cash? 😊"
            )
        )

    def test_v057_group_plain_private_note_miss_does_not_probe_or_handoff(self):
        self.claim("v057-private-note", "+60111111111", "save this privately 😊")
        private_actor = with_action_key(
            replace(
                self.actor("v057-private-note", "+60111111111"),
                trusted_text="save this privately 😊",
                read_scope="private",
            ),
            "v057-private-note-action",
        )
        saved = services.save_item(
            private_actor, "v057 secret drawer note", "keys in blue drawer"
        )
        self.assertEqual(saved["space"], "HUSBAND_PVT")
        group = self.group_actor("v057-private-note-group", "+60111111111")
        self.assertEqual(
            services.search_saved_items(group, "v057 secret drawer note")["count"], 0
        )
        self.assertFalse(
            ingress._private_group_handoff_requested(
                "show me the v057 secret drawer note"
            )
        )
        self.assertTrue(
            ingress._private_group_handoff_requested(
                "show me the v057 secret drawer note 😊"
            )
        )

    def test_v055_named_assignee_from_group_forces_dm_and_queues_immediate_ack(self):
        group_id = "120363955555@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)
        db.claim_inbound({
            "message_id": "v055-assigned-reminder",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "remind Luhgen to take the parcel in 2 minutes",
        })
        group_actor = db.resolve_actor(
            "+60111111111", group_id, "GROUP", "v055-assigned-reminder", []
        )
        group_actor = with_action_key(
            replace(
                group_actor,
                trusted_text="remind Luhgen to take the parcel in 2 minutes",
            ),
            "v055-assigned-reminder-action",
        )
        reminder = services.create_reminder(
            group_actor,
            "Luhgen to take the parcel",
            "2026-10-01T15:00:00+08:00",
            recipient="me",
            destination="group",
        )
        self.assertEqual(reminder["recipient_user_id"], "USR_HUSBAND")
        self.assertEqual(reminder["destination"], "dm")
        self.assertEqual(reminder["conversation_id"], "60111111111@s.whatsapp.net")
        self.assertFalse(reminder["claimable"])
        self.assertEqual(reminder["task"], "take the parcel")
        conn = db.connect()
        try:
            ack = conn.execute(
                """SELECT conversation_id,text_body,context_kind
                   FROM outbound_messages
                   WHERE context_kind='REMINDER_ASSIGNED' AND context_id=?""",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(ack)
        self.assertEqual(ack["conversation_id"], "60111111111@s.whatsapp.net")
        self.assertIn("assigned to you", ack["text_body"].casefold())
        self.assertIn("1st October 2026, 3.00PM", ack["text_body"])

    def test_v055_natural_emoji_memory_shortcut_is_narrow_and_private_capable(self):
        memory = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Car keys are in the blue drawer 😂")
        )}
        self.assertIn("save_item", memory)
        self.assertTrue(brain._trusted_mutation_requested(
            "Car keys are in the blue drawer 😂"
        ))

        casual = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Thanks 😂")
        )}
        self.assertNotIn("save_item", casual)

        shopping = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Add milk 🥛")
        )}
        self.assertIn("add_shopping_item", shopping)
        self.assertNotIn("save_item", shopping)

    def test_v055_plain_leave_read_and_cancel_are_reachable(self):
        upcoming = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Do I have any leave coming up?")
        )}
        self.assertIn("list_leave_records", upcoming)
        delete = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Delete my leave on 15 October 2026 for v054 test")
        )}
        self.assertIn("set_leave_record", delete)
        self.assertIn("list_leave_records", delete)

    def test_v055_whole_home_status_gets_card_but_single_device_stays_text(self):
        whole = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Show me my home status")
        )}
        self.assertIn("ha_home_report", whole)
        single = {x["function"]["name"] for x in asyncio.run(
            brain._tool_specs("Is the hall AC on?")
        )}
        self.assertNotIn("ha_home_report", single)

    def test_v055_empty_bills_result_tells_model_to_stop_probing(self):
        self.claim("v055-bills-empty", "+60111111111", "When is my car loan due?")
        result = mcp_server.bills_list(self.actor("v055-bills-empty", "+60111111111"))
        self.assertEqual(result["obligations"], [])
        self.assertIn("no recurring obligation", result["empty_means"].casefold())


    def test_v055_ha_action_tokens_are_signed_and_tamper_evident(self):
        token = ha_mobile.action_token(
            "DONE", "reminder", "reminder-123", "USR_HUSBAND"
        )
        parsed = ha_mobile.parse_action_token(token)
        self.assertEqual(parsed["action"], "DONE")
        self.assertEqual(parsed["target_id"], "reminder-123")
        self.assertEqual(parsed["user_id"], "USR_HUSBAND")
        self.assertIsNone(
            ha_mobile.parse_action_token(token.replace("DONE", "ACK", 1))
        )

    def test_v055_ha_claimable_due_notification_offers_claim_to_both(self):
        conn = db.connect()
        settings = Settings(
            husband_phone="+60111111111",
            wife_phone="+60222222222",
            husband_name="Luhgen",
            wife_name="Priya",
            ha_notify_devices=[
                {"id": "h", "owner": "husband", "notify_service": "mobile_app_h", "active": True},
                {"id": "w", "owner": "wife", "notify_service": "mobile_app_w", "active": True},
            ],
        )
        try:
            conn.execute(
                """INSERT INTO reminders(
                       reminder_id,action_key,owner_id,space_id,conversation_id,
                       task_text,due_at_utc,timezone_name,claimable
                   ) VALUES(?,?,?,?,?,?,?,?,1)""",
                (
                    "ha-claimable-1", "ha-claimable-action", "USR_HUSBAND",
                    "FAMILY_SHARED", "120363900000@g.us", "collect parcel",
                    "2026-10-02T07:00:00+00:00", "Asia/Kuala_Lumpur",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM reminders WHERE reminder_id='ha-claimable-1'"
            ).fetchone()
            with patch.object(ha_mobile, "get_settings", return_value=settings):
                queued = ha_mobile.queue_due(
                    conn, row, "⏰ Reminder: collect parcel", "test-due"
                )
            conn.commit()
            self.assertEqual(queued, 2)
            rows = conn.execute(
                """SELECT user_id,data_json FROM ha_notification_outbox
                   WHERE reminder_id='ha-claimable-1'
                   ORDER BY user_id"""
            ).fetchall()
            self.assertEqual([x["user_id"] for x in rows], ["USR_HUSBAND", "USR_WIFE"])
            for item in rows:
                data = json.loads(item["data_json"])
                self.assertTrue(data["confirmation"])
                self.assertTrue(data["alex_notification_id"])
                self.assertEqual(len(data["actions"]), 2)
                parsed = [
                    ha_mobile.parse_action_token(action["action"])["action"]
                    for action in data["actions"]
                ]
                self.assertEqual(parsed, ["ACK", "DONE"])
        finally:
            conn.close()

    def test_v055_ha_phone_receipt_is_distinct_from_human_acknowledgement(self):
        conn = db.connect()
        settings = Settings(
            husband_phone="+60111111111",
            wife_phone="+60222222222",
            ha_notify_devices=[
                {"id": "h", "owner": "husband", "notify_service": "mobile_app_h", "active": True},
            ],
        )
        try:
            with patch.object(ha_mobile, "get_settings", return_value=settings):
                ha_mobile._queue(
                    conn, "USR_HUSBAND", "receipt-distinct-test",
                    "Test reminder notification",
                )
            conn.commit()
            row = conn.execute(
                """SELECT notification_id,delivery_status,received_at_utc
                   FROM ha_notification_outbox
                   WHERE event_key='receipt-distinct-test:USR_HUSBAND:mobile_app_h'"""
            ).fetchone()
            self.assertEqual(row["delivery_status"], "PENDING")
            self.assertIsNone(row["received_at_utc"])
            notification_id = row["notification_id"]
        finally:
            conn.close()

        result = ha_mobile._process_received_event({
            "event": {
                "event_type": "mobile_app_notification_received",
                "data": {
                    "alex_notification_id": notification_id,
                    "device_id": "phone-device-id",
                },
            }
        })
        self.assertEqual(result["status"], "received")
        conn = db.connect()
        try:
            row = conn.execute(
                """SELECT delivery_status,received_at_utc,received_device_id
                   FROM ha_notification_outbox WHERE notification_id=?""",
                (notification_id,),
            ).fetchone()
        finally:
            conn.close()
        # Phone receipt does not acknowledge/complete anything; it records only
        # the delivery evidence from the Companion app.
        self.assertEqual(row["delivery_status"], "PENDING")
        self.assertTrue(row["received_at_utc"])
        self.assertEqual(row["received_device_id"], "phone-device-id")

    def test_v055_direct_ha_claim_uses_same_atomic_first_winner_rule(self):
        group_id = "120363966666@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)
        db.claim_inbound({
            "message_id": "ha-claim-create",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "put a reminder in this group tomorrow at 3pm: collect parcel",
        })
        group_actor = db.resolve_actor(
            "+60111111111", group_id, "GROUP", "ha-claim-create", []
        )
        group_actor = with_action_key(
            replace(
                group_actor,
                trusted_text="put a reminder in this group tomorrow at 3pm: collect parcel",
            ),
            "ha-claim-create-action",
        )
        reminder = services.create_reminder(
            group_actor, "collect parcel", "2026-10-02T15:00:00+08:00",
            destination="group",
        )
        self.claim("ha-claim-h", "+60111111111", "claim")
        self.claim("ha-claim-w", "+60222222222", "claim")
        first = services.claim_reminder(
            self.actor("ha-claim-h", "+60111111111"),
            reminder["reminder_id"], source="HA_ACTION",
        )
        second = services.claim_reminder(
            self.actor("ha-claim-w", "+60222222222"),
            reminder["reminder_id"], source="HA_ACTION",
        )
        self.assertEqual(first["status"], "claimed")
        self.assertEqual(first["claimed_by_user_id"], "USR_HUSBAND")
        self.assertEqual(second["status"], "already_claimed")
        self.assertEqual(second["claimed_by_user_id"], "USR_HUSBAND")

    def test_v055_ha_action_event_updates_reminder_without_new_inbound_port(self):
        self.claim("ha-action-create", "+60111111111", "remind me on 2 October 2026 at 3pm")
        actor = with_action_key(
            replace(
                self.actor("ha-action-create", "+60111111111"),
                trusted_text="remind me on 2 October 2026 at 3pm",
            ),
            "ha-action-create-key",
        )
        reminder = services.create_reminder(
            actor, "take parcel", "2026-10-02T15:00:00+08:00"
        )
        token = ha_mobile.action_token(
            "ACK", "reminder", reminder["reminder_id"], "USR_HUSBAND"
        )
        result = ha_mobile._process_action_event({
            "event": {
                "data": {"action": token},
                "context": {"id": "ctx-ha-action-ack"},
            }
        })
        self.assertEqual(result["state"], "OPEN")
        self.assertTrue(result["seen"])
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status,seen_at_utc,seen_by_user_id FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "OPEN")
        self.assertIsNotNone(row["seen_at_utc"])
        self.assertEqual(row["seen_by_user_id"], "USR_HUSBAND")



    def test_v055_stale_ha_action_cannot_reopen_completed_reminder(self):
        self.claim("ha-stale-create", "+60111111111", "remind me on 2 October 2026 at 3pm")
        actor = with_action_key(
            replace(
                self.actor("ha-stale-create", "+60111111111"),
                trusted_text="remind me on 2 October 2026 at 3pm",
            ),
            "ha-stale-create-key",
        )
        reminder = services.create_reminder(
            actor, "closed task", "2026-10-02T16:00:00+08:00"
        )
        services.update_reminder(actor, reminder["reminder_id"], status="complete")
        token = ha_mobile.action_token(
            "ACK", "reminder", reminder["reminder_id"], "USR_HUSBAND"
        )
        result = ha_mobile._process_action_event({
            "event": {
                "data": {"action": token},
                "context": {"id": "ctx-ha-stale-ack"},
            }
        })
        self.assertEqual(result["status"], "closed")
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "COMP")


    def test_v056_scope_policy_is_deterministic_for_reads(self):
        self.claim("scope-read", "+60111111111", "show me notes")
        actor = self.actor("scope-read", "+60111111111")
        self.assertEqual(scope_policy.resolve_read_scope("show me notes"), "family")
        self.assertEqual(scope_policy.resolve_read_scope("show me notes 😊"), "private")
        self.assertEqual(scope_policy.resolve_read_scope("show my private notes"), "private")
        self.assertEqual(scope_policy.resolve_read_scope("show family notes"), "family")
        self.assertEqual(
            services._spaces_sql(replace(actor, read_scope="family"))[1],
            ["FAMILY_SHARED"],
        )
        self.assertEqual(
            services._spaces_sql(replace(actor, read_scope="private"))[1],
            ["HUSBAND_PVT"],
        )

    def test_v056_finance_amount_question_skips_write_preflight(self):
        result = phase2_intent.classify_write_intent(
            "What did I spend RM6.50 on today?"
        )
        self.assertEqual(result["status"], "no_write")
        self.assertFalse(result.get("requires_clarification", False))

    def test_v056_reminder_requires_time_and_validates_weekday(self):
        self.claim(
            "reminder-vague", "+60111111111",
            "Remind me to wash the car this weekend",
        )
        vague_actor = with_action_key(
            replace(
                self.actor("reminder-vague", "+60111111111"),
                trusted_text="Remind me to wash the car this weekend",
            ),
            "reminder-vague-action",
        )
        with self.assertRaisesRegex(ValueError, "REMINDER_NEEDS_TIME"):
            services.create_reminder(
                vague_actor, "wash the car", "2026-10-03T10:00:00+08:00"
            )

        self.claim(
            "reminder-weekday", "+60111111111",
            "Remind me on Saturday at 10:00 AM to wash the car",
        )
        weekday_actor = with_action_key(
            replace(
                self.actor("reminder-weekday", "+60111111111"),
                trusted_text="Remind me on Saturday at 10:00 AM to wash the car",
            ),
            "reminder-weekday-action",
        )
        with self.assertRaisesRegex(ValueError, "DATE_WEEKDAY_MISMATCH"):
            services.create_reminder(
                weekday_actor, "wash the car", "2026-10-04T10:00:00+08:00"
            )

    def test_v056_leave_cancel_readd_portion_does_not_hit_tombstone(self):
        self.claim(
            "leave-v056", "+60111111111",
            "I'm on full-day annual leave on 6 October 2026.",
        )
        actor = with_action_key(
            replace(
                self.actor("leave-v056", "+60111111111"),
                trusted_text="I'm on full-day annual leave on 6 October 2026.",
            ),
            "leave-v056-full",
        )
        first = phase2.set_leave_record(
            actor, "2026-10-06", leave_type="ANNUAL_LEAVE", portion="FULL"
        )
        self.assertEqual(first["portion"], "FULL")
        actor_half = with_action_key(actor, "leave-v056-half")
        half = phase2.set_leave_record(
            actor_half, "2026-10-06", leave_type="ANNUAL_LEAVE",
            portion="HALF_AFTERNOON",
        )
        self.assertEqual(half["previous"]["portion"], "FULL")
        cancel_actor = with_action_key(actor, "leave-v056-cancel")
        phase2.set_leave_record(
            cancel_actor, "2026-10-06", leave_type="ANNUAL_LEAVE",
            portion="HALF_AFTERNOON", status="CANCELLED",
        )
        restore_actor = with_action_key(actor, "leave-v056-restore")
        restored = phase2.set_leave_record(
            restore_actor, "2026-10-06", leave_type="ANNUAL_LEAVE",
            portion="FULL",
        )
        self.assertEqual(restored["portion"], "FULL")

    def test_v056_goal_reference_strips_possessive(self):
        self.assertEqual(
            phase2_finance._normalized_ref("my Europe fund"),
            "europe fund",
        )

    def test_v056_reply_resolver_accepts_deterministic_alex_id(self):
        self.claim("quote-source", "+60111111111", "show transport")
        oid = db.queue_outbound(
            "60111111111@s.whatsapp.net", "TEXT",
            text="Transport: MYR 30.49",
            source_message_id="quote-source",
            context_kind="REPORT",
            context_id='{"kind":"finance_query","spec":{"category":"transport"}}',
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages SET delivery_status='SENT'
                   WHERE outbound_id=?""",
                (oid,),
            )
            conn.commit()
        finally:
            conn.close()
        quoted_id = "ALEX" + hashlib.sha256(oid.encode("utf-8")).hexdigest().upper()[:28]
        result = db.resolve_quoted_context(
            "60111111111@s.whatsapp.net", quoted_id, "+60111111111"
        )
        self.assertEqual(result["context_kind"], "REPORT")
        self.assertEqual(
            result["report_context"]["spec"]["category"], "transport"
        )

    def test_v056_voice_media_is_saved_without_transcription(self):
        self.claim("voice-v056", "+60111111111", "")
        media_ids, context_lines, vision_parts = media.process_payload_media({
            "message_id": "voice-v056",
            "audio_data": base64.b64encode(b"fake-audio").decode("ascii"),
            "audio_mime_type": "audio/ogg",
        })
        self.assertEqual(len(media_ids), 1)
        saved = media.get_media(media_ids[0])
        self.assertEqual(saved["media_type"], "AUDIO")
        self.assertEqual(saved.get("transcript_text") or "", "")
        meta = json.loads(saved.get("transcript_meta_json") or "{}")
        self.assertEqual(meta.get("mode"), "deferred_voice_inbox")
        self.assertFalse(meta.get("command_execution", True))
        self.assertEqual(context_lines, [])
        self.assertEqual(vision_parts, [])

    def actor_for_context(self):
        self.claim("context-policy", "+60111111111", "context")
        return self.actor("context-policy", "+60111111111")



    def test_v057_baileys_pin_contract_and_self_target(self):
        bridge_path = os.path.join(
            os.path.dirname(__file__), "..", "alex-mcp", "app", "connect.js"
        )
        bridge = open(bridge_path, encoding="utf-8").read()
        self.assertIn("pin: targetKey", bridge)
        self.assertIn("type: payload.kind === 'pin' ? 1 : 2", bridge)
        self.assertIn("fromMe: Boolean(payload.target_from_me)", bridge)

        row = {
            "provider_message_id": "wa-own-message",
            "outbound_id": "outbound-1",
            "conversation_id": "60111111111@s.whatsapp.net",
        }
        sent = []
        with patch.object(
            outbox, "_send",
            side_effect=lambda payload: (sent.append(payload) or True, "{}"),
        ):
            self.assertTrue(outbox._control_outbound(row, "pin"))
        self.assertTrue(sent[0]["target_from_me"])
        self.assertEqual(sent[0]["target_message_id"], "wa-own-message")

    def test_v057_empty_read_reply_uses_tool_evidence_not_done(self):
        class FakeFunction:
            def __init__(self, name, arguments="{}"):
                self.name = name
                self.arguments = arguments

        class FakeCall:
            def __init__(self, call_id, name):
                self.id = call_id
                self.function = FakeFunction(name)

        class FakeMessage:
            def __init__(self, content="", calls=None):
                self.content = content
                self.tool_calls = calls or []

            def model_dump(self, exclude_none=True):
                payload = {"role": "assistant", "content": self.content}
                if self.tool_calls:
                    payload["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in self.tool_calls
                    ]
                return payload

        class FakeResponse:
            def __init__(self, message):
                self.choices = [type("Choice", (), {"message": message})()]
                self.usage = None

        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(
                        FakeMessage(calls=[FakeCall("read-1", "query_finances")])
                    )
                return FakeResponse(FakeMessage(content=""))

        fake_client = type(
            "FakeClient",
            (),
            {"chat": type("FakeChat", (), {"completions": FakeCompletions()})()},
        )()
        route = {
            "provider": "grok",
            "model": "stub",
            "reasoning_effort": "low",
            "role": "manual",
        }

        async def fake_call_mcp(actor, name, args, action_key):
            return ({
                "spending_totals": {"MYR": 30.49},
                "income_totals": {},
                "net_outflow": {"MYR": 30.49},
                "category_totals": [],
                "count": 4,
                "returned_records": 4,
                "latest_record": None,
                "records": [],
            }, [])

        self.claim("v057-empty-read", "+60111111111", "show transport expenses")
        actor = self.actor("v057-empty-read", "+60111111111")
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=fake_client), \
             patch.object(brain, "_call_mcp", new=fake_call_mcp):
            reply, attachments = asyncio.run(
                brain.respond(actor, "show transport expenses")
            )
        self.assertEqual(attachments, [])
        self.assertNotEqual(reply.strip().casefold().rstrip(".!"), "done")
        self.assertIn("MYR 30.49", reply)
        self.assertIn("4 matching finance records", reply)


    def test_v057_finance_followup_overrides_model_dates_and_drops_old_search(self):
        self.claim("v057-fin-sep", "+60111111111", "spent RM15.67 on snacks")
        sep_actor = with_action_key(
            replace(
                self.actor("v057-fin-sep", "+60111111111"),
                trusted_text="spent RM15.67 on snacks",
                read_scope="family",
            ),
            "v057-fin-sep-action",
        )
        services.log_expense(
            sep_actor, "September family snacks", 15.67, "food",
            currency="MYR", event_date_local="2026-09-30T12:00:00+08:00",
        )
        self.claim("v057-fin-oct", "+60111111111", "spent RM18.50 on lunch")
        oct_actor = with_action_key(
            replace(
                self.actor("v057-fin-oct", "+60111111111"),
                trusted_text="spent RM18.50 on lunch",
                read_scope="family",
            ),
            "v057-fin-oct-action",
        )
        services.log_expense(
            oct_actor, "October lunch", 18.50, "food",
            currency="MYR", event_date_local="2026-10-01T12:00:00+08:00",
        )

        conversation = self.actor("v057-fin-sep", "+60111111111").conversation_id
        phase2_reports.remember_active_report(
            "USR_HUSBAND", conversation, "finance_query",
            {"count": 1}, period="2026-09",
            spec={
                "period": "2026-09",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "search": "transport",
                "scope": "family",
            },
        )
        self.claim(
            "v057-fin-follow", "+60111111111",
            "Give me a breakdown on food expenses next",
        )
        follow = replace(
            self.actor("v057-fin-follow", "+60111111111"),
            trusted_text="Give me a breakdown on food expenses next",
            read_scope="family",
        )
        result = mcp_server.query_finances(
            follow,
            start_date="2026-10-01", end_date="2026-10-01",
            category="food", search="transport",
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["spending_totals"]["MYR"], 15.67)
        self.assertIn("2026-09-30", result["records"][0]["date_local"])
        self.assertEqual(result["records"][0]["description"], "September family snacks")

    def test_v057_quoted_report_overrides_model_period_and_active_context(self):
        class FakeFunction:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments

        class FakeCall:
            def __init__(self):
                self.id = "export-1"
                self.function = FakeFunction(
                    "report_export",
                    json.dumps({
                        "format": "csv",
                        "period": "2026-10",
                        "report_type": "finance",
                        "search": "pocket cash",
                    }),
                )

        class FakeMessage:
            def __init__(self, content="", calls=None):
                self.content = content
                self.tool_calls = calls or []

            def model_dump(self, exclude_none=True):
                payload = {"role": "assistant", "content": self.content}
                if self.tool_calls:
                    payload["tool_calls"] = [{
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    } for call in self.tool_calls]
                return payload

        class FakeResponse:
            def __init__(self, message):
                self.choices = [type("Choice", (), {"message": message})()]
                self.usage = None

        class FakeCompletions:
            def __init__(self):
                self.n = 0
            def create(self, **kwargs):
                self.n += 1
                if self.n == 1:
                    return FakeResponse(FakeMessage(calls=[FakeCall()]))
                return FakeResponse(FakeMessage(content="Here it is."))

        fake_client = type(
            "FakeClient", (),
            {"chat": type("FakeChat", (), {"completions": FakeCompletions()})()},
        )()
        captured = {}

        async def fake_call(actor, name, args, action_key):
            captured.update(args)
            return ({"status": "ready", "format": "csv"}, [])

        self.claim("v057-quote-export", "+60111111111", "Send this as a csv")
        actor = replace(
            self.actor("v057-quote-export", "+60111111111"),
            trusted_text="Send this as a csv",
            read_scope="family",
        )
        route = {
            "provider": "grok", "model": "stub",
            "reasoning_effort": "low", "role": "manual",
        }
        quoted = {
            "context_kind": "REPORT",
            "context_id": "frozen",
            "report_context": {
                "kind": "finance_query",
                "period": "2026-09",
                "spec": {
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-30",
                    "category": "transport",
                    "search": None,
                    "scope": "family",
                },
            },
        }
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=fake_client), \
             patch.object(brain, "_call_mcp", new=fake_call):
            asyncio.run(brain.respond(
                actor, "Send this as a csv", quoted_context=quoted
            ))
        self.assertEqual(captured["period"], "2026-09")
        self.assertEqual(captured["category"], "transport")
        self.assertIsNone(captured["search"])
        self.assertEqual(captured["scope"], "family")
        self.assertEqual(captured["report_type"], "finance")
        self.assertFalse(captured["use_active_context"])
        self.assertFalse(captured["full_report"])

    def test_v057_pending_voice_numbers_bind_play_and_resolve(self):
        self.claim("v057-voice-src", "+60111111111", "")
        media_ids, _, _ = media.process_payload_media({
            "message_id": "v057-voice-src",
            "audio_data": base64.b64encode(b"voice-audio-v057").decode("ascii"),
            "audio_mime_type": "audio/ogg",
        })
        actor = self.actor("v057-voice-src", "+60111111111", media_ids)
        pending = db.create_pending_item(
            actor, "VOICE", media_id=media_ids[0], note="deferred voice"
        )

        self.claim("v057-voice-list", "+60111111111", "Any unresolved voice notes?")
        reader = replace(
            self.actor("v057-voice-list", "+60111111111"),
            trusted_text="Any unresolved voice notes?",
            read_scope="family",
        )
        listed = mcp_server.list_pending_items(reader, "VOICE")
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(
            set(listed["items"][0]),
            {"choice", "kind", "received", "media_type"},
        )
        self.assertEqual(listed["items"][0]["choice"], 1)
        self.assertRegex(listed["items"][0]["received"], r", \d{1,2}:\d{2} (?:AM|PM)$")

        played = services.resolve_numbered_choice(reader, 1)
        self.assertEqual(played["media_type"], "AUDIO")
        self.assertTrue(played["_attachments"])
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (pending["item_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "PENDING")

        resolved = mcp_server.resolve_pending_item(reader, choice=1)
        self.assertEqual(resolved["status"], "resolved")
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (pending["item_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "RESOLVED")

    def test_v057_typed_voice_clarification_resolves_only_after_verified_action(self):
        self.claim("v057-voice-auto-src", "+60111111111", "")
        media_ids, _, _ = media.process_payload_media({
            "message_id": "v057-voice-auto-src",
            "audio_data": base64.b64encode(b"voice-auto-v057").decode("ascii"),
            "audio_mime_type": "audio/ogg",
        })
        source_actor = self.actor(
            "v057-voice-auto-src", "+60111111111", media_ids
        )
        pending = db.create_pending_item(
            source_actor, "VOICE", media_id=media_ids[0],
            note="deferred voice",
        )
        oid = db.queue_outbound(
            source_actor.conversation_id,
            "TEXT",
            text="I saved this voice note for review.",
            source_message_id="v057-voice-auto-src",
            context_kind="PENDING_ITEM",
            context_id=pending["item_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-v057-voice-auto',
                       delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (oid,),
            )
            conn.commit()
        finally:
            conn.close()

        async def successful_typed_action(actor, *args, **kwargs):
            action_key = "v057-voice-auto-action"
            conn = db.connect()
            try:
                conn.execute(
                    """INSERT INTO tool_execution_claims(
                           action_key,tool_name,state,result_json,attachments_json,
                           completed_at_utc
                       ) VALUES(?,?,'COMPLETED','{}','[]',CURRENT_TIMESTAMP)""",
                    (action_key, "save_item"),
                )
                conn.execute(
                    """INSERT INTO tool_audit(
                           audit_id,action_key,source_message_id,user_id,tool_name,
                           arguments_json,result_json,status,latency_ms
                       ) VALUES(?,?,?,?,?,'{}','{}','OK',0)""",
                    (
                        "v057-voice-auto-audit", action_key,
                        actor.source_message_id, actor.user_id, "save_item",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return "Saved that.", []

        payload = {
            "message_id": "v057-voice-auto-clarify",
            "provider": "WHATSAPP",
            "conversation_id": source_actor.conversation_id,
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": "Save this as a note: the spare key is in the drawer.",
            "quoted_message_id": "wa-v057-voice-auto",
        }
        with patch.object(brain, "respond", new=successful_typed_action):
            result = ingress.process(payload)
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (pending["item_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "RESOLVED")

        # A second typed clarification with no verified mutation must stay
        # pending even when the model returns a fluent answer.
        self.claim("v057-voice-fail-src", "+60111111111", "")
        failed_pending = db.create_pending_item(
            self.actor("v057-voice-fail-src", "+60111111111"),
            "VOICE", note="deferred voice",
        )
        failed_oid = db.queue_outbound(
            source_actor.conversation_id,
            "TEXT",
            text="I saved this voice note for review.",
            source_message_id="v057-voice-fail-src",
            context_kind="PENDING_ITEM",
            context_id=failed_pending["item_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-v057-voice-fail',
                       delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (failed_oid,),
            )
            conn.commit()
        finally:
            conn.close()

        async def no_action(*args, **kwargs):
            return "I couldn't complete that request.", []

        failed_payload = {
            "message_id": "v057-voice-fail-clarify",
            "provider": "WHATSAPP",
            "conversation_id": source_actor.conversation_id,
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": "Save this as a note.",
            "quoted_message_id": "wa-v057-voice-fail",
        }
        with patch.object(brain, "respond", new=no_action):
            result = ingress.process(failed_payload)
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (failed_pending["item_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "PENDING")

    def test_v057_cancel_pending_voice_preserves_original_provenance(self):
        self.claim("v057-voice-cancel-src", "+60111111111", "")
        media_ids, _, _ = media.process_payload_media({
            "message_id": "v057-voice-cancel-src",
            "audio_data": base64.b64encode(b"voice-cancel-v057").decode("ascii"),
            "audio_mime_type": "audio/ogg",
        })
        actor = self.actor(
            "v057-voice-cancel-src", "+60111111111", media_ids
        )
        pending = db.create_pending_item(
            actor, "VOICE", media_id=media_ids[0], note="deferred voice"
        )
        listed = mcp_server.list_pending_items(actor, "VOICE")
        choice = next(
            item["choice"] for item in listed["items"]
            if item["kind"] == "VOICE"
        )
        cancelled = mcp_server.cancel_pending_item(actor, choice=choice)
        self.assertEqual(cancelled["status"], "cancelled")
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (pending["item_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "CANCELLED")
        original = services.get_media_original(actor, media_ids[0])
        self.assertEqual(original["media_type"], "AUDIO")
        self.assertTrue(original["_attachments"])

    def test_v057_post_due_reaction_seen_vs_complete(self):
        group_id = "120363575757@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)
        creator = self.group_actor("v057-due-create", "+60111111111")
        creator = with_action_key(
            replace(
                creator,
                trusted_text="remind the family on Saturday at 10 AM about v057 parcel",
            ),
            "v057-due-create-action",
        )
        reminder = services.create_reminder(
            creator, "v057 parcel", "2026-10-03T10:00:00+08:00",
            destination="group", claimable=True,
        )
        oid = db.queue_outbound(
            group_id, "TEXT", text="Reminder: v057 parcel",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE reminders SET status='DUE' WHERE reminder_id=?""",
                (reminder["reminder_id"],),
            )
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-v057-due',delivery_status='SENT',
                       delivered_at_utc=CURRENT_TIMESTAMP,job_pinned_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (oid,),
            )
            conn.commit()
        finally:
            conn.close()

        db.claim_inbound({
            "message_id": "v057-due-react",
            "provider": "WHATSAPP",
            "conversation_id": group_id,
            "conversation_type": "GROUP",
            "sender_phone": "+60111111111",
            "text": "",
        })
        reactor = db.resolve_actor(
            "+60111111111", group_id, "GROUP", "v057-due-react", []
        )
        seen = services.claim_reminder_from_reaction(
            reactor, "wa-v057-due", "👍"
        )
        self.assertEqual(seen["status"], "seen")
        self.assertEqual(seen["state"], "DUE")
        visible = services.list_reminders(reactor)["reminders"]
        self.assertTrue(any(x["task_text"] == "v057 parcel" for x in visible))
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status,seen_at_utc FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "DUE")
        self.assertIsNotNone(row["seen_at_utc"])

        done = services.claim_reminder_from_reaction(
            reactor, "wa-v057-due", "✅"
        )
        self.assertEqual(done["status"], "completed")
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "COMP")

    def test_v057_reminder_task_name_blocks_cross_domain_fallback(self):
        self.claim("v057-reminder-seed", "+60111111111", "create reminder")
        creator = with_action_key(
            replace(
                self.actor("v057-reminder-seed", "+60111111111"),
                trusted_text="Remind me on Saturday at 10 AM: v056 unclaimed test",
            ),
            "v057-reminder-seed-action",
        )
        services.create_reminder(
            creator, "v056 unclaimed test", "2026-10-03T10:00:00+08:00",
            shared=True,
        )
        self.claim("v057-note-collision", "+60111111111", "save v056 note")
        note_actor = with_action_key(
            replace(
                self.actor("v057-note-collision", "+60111111111"),
                trusted_text="save v056 note",
            ),
            "v057-note-action",
        )
        services.save_item(note_actor, "v056 lighter location", "shared collision")

        self.claim(
            "v057-reminder-lookup", "+60111111111",
            "Is v056 unclaimed test still unresolved?",
        )
        actor = replace(
            self.actor("v057-reminder-lookup", "+60111111111"),
            trusted_text="Is v056 unclaimed test still unresolved?",
            read_scope="family",
        )
        self.assertTrue(
            brain._accessible_reminder_name_mentioned(
                actor, "Is v056 unclaimed test still unresolved?"
            )
        )

        exposed = {}
        class FakeResponse:
            def __init__(self):
                self.choices = [
                    type("Choice", (), {
                        "message": type("Msg", (), {
                            "content": "It is still unresolved.",
                            "tool_calls": [],
                            "model_dump": lambda self, exclude_none=True: {
                                "role": "assistant",
                                "content": self.content,
                            },
                        })()
                    })()
                ]
                self.usage = None
        class FakeCompletions:
            def create(self, **kwargs):
                exposed["names"] = {
                    x["function"]["name"] for x in kwargs.get("tools", [])
                }
                return FakeResponse()
        fake_client = type(
            "FakeClient", (),
            {"chat": type("FakeChat", (), {"completions": FakeCompletions()})()},
        )()
        route = {
            "provider": "grok", "model": "stub",
            "reasoning_effort": "low", "role": "manual",
        }
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=fake_client):
            asyncio.run(brain.respond(
                actor, "Is v056 unclaimed test still unresolved?"
            ))
        self.assertIn("list_reminders", exposed["names"])
        self.assertNotIn("search_saved_items", exposed["names"])
        self.assertNotIn("query_finances", exposed["names"])

    def test_v057_reminder_clarification_is_durable_pending_draft(self):
        first = {
            "message_id": "v057-draft-first",
            "provider": "WHATSAPP",
            "conversation_id": "60111111111@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": "Remind me to water the plants this weekend.",
        }
        with patch.object(
            brain, "respond",
            return_value=(
                "What day and time this weekend would you like me to set the reminder for?",
                [],
            ),
        ):
            result = ingress.process(first)
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            draft = conn.execute(
                """SELECT * FROM pending_items
                   WHERE kind='REMINDER_DRAFT' AND status='PENDING'
                   ORDER BY created_at_utc DESC LIMIT 1"""
            ).fetchone()
            outbound = conn.execute(
                """SELECT context_kind,context_id FROM outbound_messages
                   WHERE source_message_id='v057-draft-first'
                   ORDER BY rowid DESC LIMIT 1"""
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(draft)
        self.assertEqual(outbound["context_kind"], "PENDING_ITEM")
        self.assertEqual(outbound["context_id"], draft["item_id"])

        second = {
            "message_id": "v057-draft-second",
            "provider": "WHATSAPP",
            "conversation_id": "60111111111@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": "Saturday at 9 AM",
        }
        async def finish_draft(actor, user_text, media_context=None,
                               vision_parts=None, quoted_context=None):
            self.assertEqual(
                quoted_context["pending_item"]["kind"], "REMINDER_DRAFT"
            )
            self.assertIn(
                "water the plants",
                quoted_context["recent_user_instruction"].casefold(),
            )
            services.create_reminder(
                with_action_key(actor, "v057-draft-final-action"),
                "water the plants", "2026-10-03T09:00:00+08:00",
            )
            return ("OK. I've set the reminder for Saturday at 9:00 AM.", [])

        with patch.object(brain, "respond", new=finish_draft):
            result = ingress.process(second)
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            draft_state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (draft["item_id"],),
            ).fetchone()["status"]
            made = conn.execute(
                """SELECT task_text FROM reminders
                   WHERE source_message_id='v057-draft-second'"""
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(draft_state, "RESOLVED")
        self.assertEqual([row["task_text"] for row in made], ["water the plants"])

    def test_v057_private_search_offer_is_single_use_and_scope_safe(self):
        self.claim("v057-private-offer-src", "+60111111111", "Show me my Philips asset")
        source_actor = replace(
            self.actor("v057-private-offer-src", "+60111111111"),
            trusted_text="Show me my Philips asset",
            read_scope="family",
        )
        pending, reply = ingress._maybe_create_private_search_offer(
            source_actor,
            "Show me my Philips asset",
            "I couldn't locate a specific Philips Air Fryer in the records available to this request.",
            [],
        )
        self.assertIsNotNone(pending)
        self.assertIn("shared records", reply)
        self.assertIn("private records", reply)
        self.assertEqual(
            ingress._private_offer_query(pending),
            "Show me my Philips asset",
        )

        self.claim("v057-private-offer-yes", "+60111111111", "Yes")
        yes_actor = replace(
            self.actor("v057-private-offer-yes", "+60111111111"),
            trusted_text="Yes",
            read_scope="family",
        )
        seen_scope = {}
        async def private_reply(actor, user_text, media_context=None,
                                vision_parts=None, quoted_context=None):
            seen_scope["scope"] = actor.read_scope
            seen_scope["query"] = user_text
            return ("I found the private Philips asset.", [])
        with patch.object(brain, "respond", new=private_reply):
            result = ingress._fulfill_private_search_offer(yes_actor, dict(pending))
        self.assertTrue(result["ok"])
        self.assertEqual(seen_scope["scope"], "private")
        self.assertEqual(seen_scope["query"], "Show me my Philips asset")
        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (pending["item_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "RESOLVED")

    def test_v057_private_asset_is_searchable_without_crossing_family_scope(self):
        phase2_library.create_asset(
            "Philips Air Fryer", "+60111111111", "DIRECT_DM",
            visibility="private", brand="Philips", model="HD9280/90",
            serial_number="V05-12345", purchase_date="2026-09-27",
        )
        self.claim("v057-asset-family", "+60111111111", "show Philips Air Fryer")
        base = self.actor("v057-asset-family", "+60111111111")
        family_actor = replace(base, trusted_text="show Philips Air Fryer", read_scope="family")
        private_actor = replace(base, trusted_text="show Philips Air Fryer 😊", read_scope="private")
        with use_actor(family_actor):
            shared = phase2_library.list_assets(
                base.phone, base.conversation_type, "all", False, "Philips Air Fryer"
            )
        with use_actor(private_actor):
            private = phase2_library.list_assets(
                base.phone, base.conversation_type, "all", False, "Philips Air Fryer"
            )
        self.assertEqual(shared, [])
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0]["model"], "HD9280/90")
        self.assertIsNone(private[0]["warranty_end"])

    def test_v057_saved_item_browse_uses_local_date_and_time(self):
        self.claim("v057-saved-time", "+60111111111", "save lighter location")
        actor = with_action_key(
            replace(
                self.actor("v057-saved-time", "+60111111111"),
                trusted_text="save lighter location",
                read_scope="family",
            ),
            "v057-saved-time-action",
        )
        services.save_item(actor, "Lighter location", "lighter is in the garage")
        found = services.search_saved_items(actor, "lighter")
        self.assertEqual(found["count"], 1)
        self.assertRegex(
            found["matches"][0]["saved_on"],
            r"^\d{1,2} [A-Za-z]+ 2026, \d{1,2}:\d{2} (?:AM|PM)$",
        )


    def test_v057_frozen_filtered_export_stays_inside_quoted_month(self):
        self.claim("v057-exp-sep", "+60111111111", "spent RM10 on transport")
        sep = with_action_key(
            replace(
                self.actor("v057-exp-sep", "+60111111111"),
                trusted_text="spent RM10 on transport",
                read_scope="family",
            ),
            "v057-exp-sep-action",
        )
        services.log_expense(
            sep, "September transport", 10.0, "transport",
            currency="MYR", event_date_local="2026-09-30T08:00:00+08:00",
        )
        self.claim("v057-exp-oct", "+60111111111", "spent RM20 on transport")
        oct_actor = with_action_key(
            replace(
                self.actor("v057-exp-oct", "+60111111111"),
                trusted_text="spent RM20 on transport",
                read_scope="family",
            ),
            "v057-exp-oct-action",
        )
        services.log_expense(
            oct_actor, "October transport", 20.0, "transport",
            currency="MYR", event_date_local="2026-10-01T08:00:00+08:00",
        )
        self.claim("v057-exp-export", "+60111111111", "send this as csv")
        exporter = replace(
            self.actor("v057-exp-export", "+60111111111"),
            trusted_text="send this as csv",
            read_scope="family",
        )
        result = mcp_server.report_export(
            "csv", exporter,
            period="2026-09", report_type="finance",
            category="transport", scope="family",
            use_active_context=False,
        )
        self.assertEqual(result["record_count"], 1)
        payload = open(
            result["_attachments"][0]["path"], encoding="utf-8"
        ).read()
        self.assertIn("September transport", payload)
        self.assertNotIn("October transport", payload)

    def test_v057_initiator_can_privately_nudge_claimant_without_reopening(self):
        group_id = "120363585858@g.us"
        with open(os.path.join(TEST_DIR, "family_group.json"), "w", encoding="utf-8") as handle:
            handle.write('{"group_jid":"%s"}' % group_id)
        creator = self.group_actor("v057-nudge-create", "+60111111111")
        creator = with_action_key(
            replace(
                creator,
                trusted_text="remind the family on Saturday at 10 AM to collect parcel",
            ),
            "v057-nudge-create-action",
        )
        reminder = services.create_reminder(
            creator, "collect parcel", "2026-10-03T10:00:00+08:00",
            destination="group", claimable=True,
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE reminders
                   SET status='DUE',claimed_by_user_id='USR_WIFE',
                       claimed_at_utc=CURRENT_TIMESTAMP
                   WHERE reminder_id=?""",
                (reminder["reminder_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        self.claim("v057-nudge-now", "+60111111111", "remind her again")
        initiator = replace(
            self.actor("v057-nudge-now", "+60111111111"),
            trusted_text="remind her again",
            read_scope="family",
        )
        result = services.nudge_reminder_claimant(
            initiator, reminder["reminder_id"]
        )
        self.assertEqual(result["status"], "nudged")
        conn = db.connect()
        try:
            row = conn.execute(
                """SELECT status,claimed_by_user_id,nudged_at_utc
                   FROM reminders WHERE reminder_id=?""",
                (reminder["reminder_id"],),
            ).fetchone()
            outbound = conn.execute(
                """SELECT conversation_id,context_kind,text_body
                   FROM outbound_messages
                   WHERE context_id=? AND context_kind='REMINDER_CLAIMANT_FOLLOWUP'
                   ORDER BY rowid DESC LIMIT 1""",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "DUE")
        self.assertEqual(row["claimed_by_user_id"], "USR_WIFE")
        self.assertIsNotNone(row["nudged_at_utc"])
        self.assertEqual(outbound["conversation_id"], "60222222222@s.whatsapp.net")
        self.assertIn("still unresolved", outbound["text_body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
