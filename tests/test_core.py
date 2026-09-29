import asyncio
import base64
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

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
import phase2
import diagnostics
import profile_config
import phase2_finance
import phase2_work
import phase2_library
import phase2_delegation
import phase2_monitor
import phase2_presence
import phase2_reports
import brain
from context import use_actor, with_action_key
from config import Settings
from mcp import Client
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
                "alex_phase2_goal_contributions", "alex_phase2_goal_period_targets",
                "alex_phase2_goal_baseline_versions", "alex_phase2_obligation_instances",
                "alex_phase2_evidence_facts", "alex_phase2_cash_events",
                "alex_phase2_cash_pools", "alex_phase2_plan_reserves", "alex_phase2_goals",
                "alex_phase2_work_events", "alex_phase2_delegations",
                "alex_profile_config_versions",
                "tool_audit", "tool_execution_claims", "ai_usage", "diagnostic_runs", "monitor_notifications",
                "outbound_messages", "conversation_turns", "selection_sets", "reminder_events",
                "task_reminder_links", "task_events", "tasks",
                "diary_reminder_links", "plan_diary_links", "schedule_conflicts", "diary_events", "plans",
                "leave_records", "work_roster", "cashflow_baselines",
                "event_media_links", "financial_event_corrections", "financial_events",
                "saved_items", "shopping_items", "reminders", "savings_goals", "money_buckets",
                "leave_state", "media_objects", "inbound_messages",
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

    def test_dm_receipt_media_does_not_widen_private_finance_scope(self):
        self.claim("scope-receipt-private", "+60111111111", "log this receipt")
        raw = base64.b64encode(b"generic-receipt").decode("ascii")
        receipt_media = media.save_media(
            "scope-receipt-private", "IMAGE", "image/jpeg", raw
        )
        actor = with_action_key(
            self.actor(
                "scope-receipt-private", "+60111111111", [receipt_media],
                trusted_text="log this receipt",
            ),
            "scope-receipt-private-a",
        )
        result = services.log_expense(
            actor, "Personal purchase", 15, "personal", currency="MYR"
        )
        self.assertEqual(result["space"], "HUSBAND_PVT")

    def test_sensitive_finance_categories_default_private_but_family_can_be_explicit(self):
        self.claim("scope-pharmacy-private", "+60111111111", "pharmacy medicine")
        actor = with_action_key(
            self.actor(
                "scope-pharmacy-private", "+60111111111",
                trusted_text="pharmacy medicine",
            ),
            "scope-pharmacy-private-a",
        )
        private = services.log_expense(
            actor, "Pharmacy medicine", 20, "pharmacy", currency="MYR"
        )
        self.assertEqual(private["space"], "HUSBAND_PVT")

        self.claim(
            "scope-pharmacy-family", "+60111111111",
            "share with the family pharmacy medicine",
        )
        family_actor = with_action_key(
            self.actor(
                "scope-pharmacy-family", "+60111111111",
                trusted_text="share with the family pharmacy medicine",
            ),
            "scope-pharmacy-family-a",
        )
        family = services.log_expense(
            family_actor, "Pharmacy medicine", 20, "pharmacy", currency="MYR"
        )
        self.assertEqual(family["space"], "FAMILY_SHARED")

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

    def actor_for_context(self):
        self.claim("context-policy", "+60111111111", "context")
        return self.actor("context-policy", "+60111111111")



if __name__ == "__main__":
    unittest.main(verbosity=2)
