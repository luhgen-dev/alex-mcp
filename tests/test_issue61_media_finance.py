"""Issue 61 media/finance regression evidence; synthetic state, never WhatsApp."""
import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_core as core
from test_conversation_reliability import ConversationFixture


class MediaFinanceRepairTests(ConversationFixture):
    def original(self, mid, kind="AUDIO", caption="", spouse=False):
        owner = self.actor(mid, caption, spouse=spouse)
        raw = b"synthetic original " + kind.encode() + b"\x00\xff"
        mime = "audio/ogg" if kind == "AUDIO" else "video/mp4"
        ident = core.media.save_media(mid, kind, mime, base64.b64encode(raw).decode())
        return owner, ident, raw

    def test_MEDIA_01_02_quoted_captioned_original_exact_bytes_and_retry(self):
        for kind in ("AUDIO", "VIDEO"):
            with self.subTest(kind=kind):
                source, media_id, raw = self.original("original-" + kind, kind, "A caption")
                typed = self.actor("save-" + kind, "Save this as original " + kind)
                saved = core.services.save_original_media_request(typed, source.source_message_id)
                self.assertTrue(saved["media_saved"])
                again = core.services.save_original_media_request(
                    self.actor("save-again-" + kind, "Save that as original " + kind),
                    source.source_message_id,
                )
                self.assertEqual(saved["item_id"], again["item_id"])
                fetched = core.services.get_saved_item(typed, saved["item_id"])
                asset = fetched["_attachments"][0]
                self.assertEqual(asset["kind"], kind)
                self.assertEqual(Path(asset["path"]).read_bytes(), raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), core.media.get_media(media_id)["sha256"])

    def test_MEDIA_01_pending_audio_requires_typed_authority(self):
        owner, ident, raw = self.original("pending-audio")
        pending = core.db.create_pending_item(owner, "VOICE", ident, note="synthetic")
        typed = self.actor("typed-save", "Keep this voice note as gardening")
        self.assertIsNone(core.services.save_original_media_request(replace(typed, source="voice")))
        saved = core.services.save_original_media_request(typed)
        self.assertEqual(saved["status"], "saved")
        with core.db.connect() as conn:
            row = conn.execute("SELECT status FROM pending_items WHERE item_id=?", (pending["item_id"],)).fetchone()
        self.assertNotEqual(row["status"], "PENDING")

    def test_MEDIA_01_quote_never_falls_back_to_unrelated_pending_or_other_owner(self):
        owner, ident, _ = self.original("pending-one")
        core.db.create_pending_item(owner, "VOICE", ident, note="synthetic")
        stranger, other, _ = self.original("spouse-media", "VIDEO", spouse=True)
        typed = self.actor("deny-save", "Save this as clip")
        for quoted in ("missing-original", stranger.source_message_id):
            with self.assertRaises(ValueError):
                core.services.save_original_media_request(typed, quoted)
        with self.assertRaises(PermissionError):
            core.services.save_existing_media_item(typed, "clip", "clip", other, True)
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0], 0)

    def test_MEDIA_01_missing_or_corrupt_asset_cannot_claim_saved(self):
        source, ident, _ = self.original("corrupt-audio")
        path = Path(core.media.get_media(ident)["local_path"])
        path.write_bytes(b"altered")
        typed = self.actor("corrupt-save", "Save this as audio")
        with self.assertRaisesRegex(ValueError, "integrity"):
            core.services.save_original_media_request(typed, source.source_message_id)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            core.services.save_original_media_request(typed, source.source_message_id)

    def test_MEDIA_03_empty_requested_type_never_returns_other_notes(self):
        note = replace(self.actor("save-note", "Remember the umbrella"), action_key="note-key")
        core.services.save_item(note, "umbrella", "umbrella", shared=True)
        reader = self.actor("read-video", "Show me saved videos")
        result = core.services.search_saved_items(reader, "saved videos")
        self.assertEqual(result["count"], 0)
        with self.assertRaises(ValueError):
            core.services.search_saved_items(reader, kind="unsupported")

    def test_PDF_01_frozen_finance_view_no_current_planning_enrichment(self):
        actor = replace(self.actor("expense-source", "Spent RM9 on lunch"), action_key="expense-key")
        core.services.log_expense(actor, "Only displayed lunch", 9, "food", "MYR", event_date_local="2026-09-05T12:00:00+08:00")
        reader = self.actor("finance-reader", "show September expenses")
        ledger = core.services.query_finances(reader, start_date="2026-09-01", end_date="2026-09-30")
        core.phase2_reports.remember_active_report(reader.user_id, reader.conversation_id, "finance_query", ledger,
            spec={"period": "2026-09", "scope": "family"}, period="2026-09")
        core.phase2_finance.create_goal("Unrelated planning secret", 777, 10, actor.phone, visibility="family")
        later = replace(self.actor("later-expense", "Spent RM5"), action_key="later-key")
        core.services.log_expense(later, "Later not displayed", 5, "food", "MYR", event_date_local="2026-09-05T13:00:00+08:00")
        with patch.object(core.phase2_finance, "list_goals", side_effect=AssertionError("must not enrich frozen view")):
            result = core.mcp_server.report_export("pdf", reader, report_type="finance")
        payload = Path(result["_attachments"][0]["path"]).read_bytes()
        self.assertTrue(payload.startswith(b"%PDF"))
        self.assertIn(b"Only displayed lunch", payload)
        self.assertNotIn(b"Unrelated planning secret", payload)
        self.assertNotIn(b"Later not displayed", payload)
        self.assertEqual(result["record_count"], 1)

    def test_GOAL_03_target_update_keeps_single_identity_and_creation_fails_closed(self):
        actor = self.actor("goal-create", "Create a shared camera goal")
        created = core.phase2_finance.create_goal("Camera", 120, 10, actor.phone, visibility="family", status="DRAFT")
        changed = core.phase2_finance.update_goal_target(created["goal_id"], 180, actor.phone)
        self.assertEqual(changed["goal_id"], created["goal_id"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            core.phase2_finance.create_goal("the Camera", 180, 10, actor.phone, visibility="family", status="DRAFT")
        editor = self.actor("goal-edit", "Change camera to RM180 instead")
        with core.use_actor(editor), self.assertRaisesRegex(ValueError, "existing goal"):
            core.phase2_finance.create_goal("different model spelling", 180, 10, actor.phone, visibility="family")
        with core.db.connect() as conn:
            rows = conn.execute("SELECT goal_id,target_minor,status FROM alex_phase2_goals").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["goal_id"], created["goal_id"])
        self.assertEqual(rows[0]["target_minor"], 18000)
        spouse = self.actor("goal-private-owner", "private", spouse=True)
        private = core.phase2_finance.create_goal("Private camera", 333, 0, spouse.phone)
        with self.assertRaises(PermissionError):
            core.phase2_finance.update_goal_target(private["goal_id"], 444, actor.phone)

    def test_private_stash_success_atomic_debit_idempotency_no_family_ledger_write(self):
        owner = self.actor("stash-owner", "private stash")
        pool = core.phase2_finance.create_cash_pool("Synthetic private pot", owner.phone,
            visibility="private", opening_balance=100, source_message_id=owner.source_message_id)
        one = core.phase2_finance.record_cash_pool_spend(pool["pool_id"], 15, "2026-09-05", owner.phone,
            category="food", source_message_id="unique-debit")
        again = core.phase2_finance.record_cash_pool_spend(pool["pool_id"], 15, "2026-09-05", owner.phone,
            category="food", source_message_id="unique-debit")
        self.assertEqual(one["balance"], 85)
        self.assertEqual(again["status"], "already_applied")
        with self.assertRaises(ValueError):
            core.phase2_finance.record_cash_pool_spend(pool["pool_id"], 200, "2026-09-05", owner.phone,
                source_message_id="failed-debit")
        with core.db.connect() as conn:
            debits = conn.execute("SELECT * FROM alex_phase2_cash_pool_adjustments WHERE adjustment_kind='SPEND'").fetchall()
            self.assertEqual(len(debits), 1)
            self.assertEqual(debits[0]["amount_minor"], -1500)
            self.assertNotEqual(debits[0]["space_id"], "FAMILY_SHARED")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM financial_events").fetchone()[0], 0)
        self.assertEqual(core.phase2_finance.cash_pool_balance(pool["pool_id"], owner.phone)["balance"], 85)

    def test_MEDIA_01_02_ingress_quoted_and_current_media_skip_provider(self):
        source, ident, raw = self.original("ingress-video", "VIDEO", "captioned video")
        with patch.object(core.brain, "respond", side_effect=AssertionError("deterministic save must not call provider")):
            result = core.ingress.process(self.payload("ingress-save", "Save that video as garden clip",
                quoted_message_id=source.source_message_id))
        self.assertTrue(result["original_media_saved"])
        actor = self.actor("ingress-fetch", "show garden clip")
        fetched = core.services.get_saved_item(actor, result["saved_item_id"])
        self.assertEqual(Path(fetched["_attachments"][0]["path"]).read_bytes(), raw)
        with patch.object(core.brain, "respond", side_effect=AssertionError("deterministic save must not call provider")):
            direct = core.ingress.process(self.payload("ingress-caption-save", "Save this video as another clip",
                video_data=base64.b64encode(b"synthetic captioned video").decode(), video_mime_type="video/mp4"))
        self.assertTrue(direct["original_media_saved"])

    def test_MEDIA_01_ingress_reply_to_pending_ack_and_note_content(self):
        source, ident, raw = self.original("voice-ack-source")
        pending = core.db.create_pending_item(source, "VOICE", ident, note="synthetic")
        oid = core.db.queue_outbound(source.conversation_id, "TEXT", text="Voice received",
            source_message_id=source.source_message_id, context_kind="PENDING_ITEM", context_id=pending["item_id"])
        with core.db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET provider_message_id='ack-message',delivery_status='SENT' WHERE outbound_id=?", (oid,))
        with patch.object(core.brain, "respond", side_effect=AssertionError("deterministic save must not call provider")):
            result = core.ingress.process(self.payload("save-from-ack", "Save this voice note as garden audio", quoted_message_id="ack-message"))
        self.assertTrue(result["original_media_saved"])
        actor = self.actor("typed-content", "Save this as a note: the spare key is in the drawer.")
        self.assertIsNone(core.services.save_original_media_request(actor, "ack-message"))

    def test_MEDIA_original_read_detects_later_corruption(self):
        source, ident, _ = self.original("original-then-corrupt", "VIDEO")
        typed = self.actor("save-before-corruption", "Save this as clip")
        saved = core.services.save_original_media_request(typed, source.source_message_id)
        Path(core.media.get_media(ident)["local_path"]).write_bytes(b"different bytes")
        with self.assertRaisesRegex(ValueError, "integrity"):
            core.services.get_saved_item(typed, saved["item_id"])

    def test_MEDIA_concurrent_retries_have_one_saved_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        source, ident, _ = self.original("concurrent-original", "VIDEO")
        actors = [self.actor("concurrent-save-" + str(i), "Save this as clip") for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda actor: core.services.save_original_media_request(actor, source.source_message_id), actors))
        self.assertEqual(len({row["item_id"] for row in results}), 1)
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM saved_items WHERE media_id=?", (ident,)).fetchone()[0], 1)
