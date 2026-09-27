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
from context import use_actor, with_action_key
from mcp import Client
from mcp_server import mcp


class AlexCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.initialize()

    def setUp(self):
        conn = db.connect()
        try:
            for table in (
                "tool_audit", "ai_usage", "outbound_messages", "conversation_turns",
                "event_media_links", "financial_event_corrections", "financial_events",
                "saved_items", "reminders", "savings_goals", "money_buckets", "leave_state", "media_objects",
                "inbound_messages",
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

    def test_private_and_shared_space_isolation(self):
        self.claim("p1", "+60111111111", "coffee")
        husband = with_action_key(self.actor("p1", "+60111111111"), "a-private")
        result = services.log_expense(husband, "Coffee", 8.50, "food")
        self.assertEqual(result["space"], "HUSBAND_PVT")

        self.claim("q-wife", "+60222222222", "how much")
        wife = self.actor("q-wife", "+60222222222")
        self.assertEqual(services.query_finances(wife)["count"], 0)

        self.claim("s1", "+60111111111", "TNB")
        husband_shared = with_action_key(self.actor("s1", "+60111111111"), "a-shared")
        shared = services.log_expense(husband_shared, "TNB electricity bill", 120.00, None)
        self.assertEqual(shared["space"], "FAMILY_SHARED")
        self.assertEqual(services.query_finances(wife, search="TNB")["count"], 1)

    def test_idempotent_action_key(self):
        self.claim("i1", "+60111111111", "lunch 10")
        actor = with_action_key(self.actor("i1", "+60111111111"), "same-action")
        first = services.log_expense(actor, "Lunch", 10, "food")
        second = services.log_expense(actor, "Lunch", 10, "food")
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
        original = services.log_expense(original_actor, "Lunch", 12, "food")

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
                actor, "TNB electricity bill", 100, "utilities", reference=ref
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
        pending = services.log_expense(actor, "Transfer to person", 50, None, reference="REF9")
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
            services.log_expense(actor, f"Lunch {i}", 10, "food")
        result = services.query_finances(base, category="food", limit=5)
        self.assertEqual(result["count"], 25)
        self.assertEqual(result["returned_records"], 5)
        self.assertEqual(result["spending_totals"]["MYR"], 250.0)

    def test_generic_p2p_category_guess_is_rejected(self):
        self.claim("p2p1", "+60111111111", "sent RM50")
        actor = with_action_key(self.actor("p2p1", "+60111111111"), "p2p-action")
        result = services.log_expense(
            actor, "Transfer to PRIYA", 50, "groceries", reference="BANKREF"
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
                        "description": "Lunch", "amount": 9.5, "category": "food"
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
