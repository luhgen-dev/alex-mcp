"""Voice regressions must verify persisted actions and actual file delivery.

Scripted model replies exercise the real ingress/MCP boundary; they are not
evidence of live model understanding or speech recognition accuracy.
"""
import asyncio
import base64
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_core as core
import multilingual_intent
import reminder_time
import test_multilingual_voice as voice_fixture


class VoiceParityPolicyTests(unittest.TestCase):
    def test_family_reminder_is_explicit_and_plan_followup_is_an_update(self):
        import phase2_intent
        self.assertTrue(phase2_intent.reminder_write_signal(
            "Remind the family tomorrow at 9am to collect the parcel; anyone can claim it."))
        add, block = core.brain._contextual_tool_hints(
            "Make it kid-friendly and keep it as a draft.",
            "Start a draft family day trip to Malacca next month. Don't lock it yet.")
        self.assertIn("update_plan", add)
        self.assertIn("create_plan", block)

    def test_status_card_delivery_and_unspecified_automation(self):
        self.assertTrue(core.brain._attachment_delivery_authorized("Send me the home status card.", "ha_home_report"))
        self.assertFalse(core.brain._attachment_delivery_authorized("Don't send the home status card.", "ha_home_report"))
        self.assertTrue(core.brain._ha_draft_missing_details("Draft an automation for the hall light."))
        self.assertFalse(core.brain._ha_draft_missing_details("Draft an automation to turn the hall light on at sunset."))

    def test_saved_memory_read_is_not_a_new_save(self):
        self.assertFalse(multilingual_intent.requests_action("நான் சேமிக்க சொன்னதை காட்டு"))
        self.assertTrue(multilingual_intent.requests_action("இந்த தகவலை சேமி"))

    def test_native_reminder_keeps_pending_clarification(self):
        phrase = "நாளைக்கு மருந்து எடுக்க ரிமைண்ட் பண்ணு"
        self.assertTrue(core.ingress._is_reminder_request(phrase))
        self.assertFalse(multilingual_intent.requests_reminder("ரிமைண்ட் பண்ணாத"))

    def test_colloquial_date_preserves_source_and_canonical_meaning(self):
        for token in ("tmrw", "tomorow"):
            phrase = f"remind me {token} 9am to pay the bill"
            result = reminder_time.validate(dict(source_text=f"{token} 9am",kind="LOCAL_CLOCK",
                reference="CURRENT_MESSAGE",date_text=token,time_text="09:00",needs_clarification=False),current_text=phrase)
            self.assertEqual(result["canonical_text"],"tomorrow 09:00")
    def test_observed_capability_denials_trigger_recovery(self):
        for reply in (
            "I cannot log new expenses directly as I do not have write access to your financial ledger.",
            "I cannot directly set a new reminder for you with the tools currently available.",
            "I cannot generate a PDF file, but I have listed the transactions below.",
            "I am unable to attach the receipt image.",
        ):
            with self.subTest(reply=reply):
                self.assertTrue(core.brain._looks_like_false_capability_denial(reply))

    def test_colloquial_actions_expose_the_actual_mutator(self):
        for text, expected in (
            ('எனக்கு ரெண்டு நிமிஷத்துல ரிமைண்ட் பண்ணி', 'create_reminder'),
            ('நான் 56 ரூபாய் ஸ்பெண்ட் பண்ணேன் என் லெட்ஜர்ல லாக் பண்ணு', 'log_expense'),
        ):
            with self.subTest(text=text):
                names = {s['function']['name'] for s in asyncio.run(core.brain._tool_specs(text))}
                self.assertIn(expected, names)
                self.assertTrue(core.brain._trusted_mutation_requested(text))

    def test_native_original_file_requests_authorize_delivery(self):
        for phrase in (
            "கிரெடிட் கார்டு கட்டிய ரசீதை அனுப்பு",
            "அந்த ரசீதை காட்டு",
            "அந்த ரிசீட் எனக்கு காமி",
            "அந்த புகைப்படத்தை அனுப்புங்கள்",
            "Tolong hantar resit bayaran kad kredit itu",
            "Tunjukkan gambar resit itu",
            "antha photo anuppu",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(core.brain._attachment_delivery_authorized(phrase, "get_receipt"))

    def test_negated_original_file_requests_never_send(self):
        for phrase in (
            "Don't send the receipt, just tell me the amount",
            "Jangan hantar resit itu, beritahu jumlah sahaja",
            "அந்த ரசீதை அனுப்ப வேண்டாம்",
            "photo anuppa vendam",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(core.brain._attachment_delivery_authorized(phrase, "get_receipt"))

    def test_native_reminder_imperatives_can_discover_creation(self):
        for phrase in (
            "நாளைக்கு காலை ஏழு மணிக்கு மருந்து சாப்பிட ஞாபகப்படுத்து",
            "நாளை காலை 7 மணிக்கு ரிமைண்டர் வை",
            "இன்னும் ஒரு அஞ்சு நிமிஷத்துல மருந்து எடுக்க ரிமைண்ட் பண்ணு",
            "naalaiku kaalaila 7 manikku nyabaga paduthunga",
            "Tolong ingatkan saya makan ubat esok pukul tujuh pagi",
        ):
            with self.subTest(phrase=phrase):
                specs = asyncio.run(core.brain._discover_tool_specs(
                    "create reminder take medicine tomorrow at 7 AM", original_user_text=phrase))
                self.assertIn("create_reminder", {s["function"]["name"] for s in specs})

    def test_unclassified_native_requests_resolve_domain_before_reads(self):
        for phrase in ("எனக்கு என்னென்ன ரிமைண்டர் இருக்கு", "போன மாதம் செலவு PDFல அனுப்பு"):
            specs = asyncio.run(core.brain._tool_specs(phrase))
            self.assertEqual([s["function"]["name"] for s in specs], ["discover_alex_tools"])

    def test_colloquial_tamil_duration_is_source_checked(self):
        phrase = "இன்னும் ஒரு அஞ்சு நிமிஷத்துல மருந்து எடுக்க ரிமைண்ட் பண்ணு"
        spec = dict(source_text=phrase, kind="RELATIVE", reference="CURRENT_MESSAGE",
                    relative_minutes=5, needs_clarification=False)
        self.assertEqual(reminder_time.validate(spec, current_text=phrase)["relative_minutes"], 5)
        with self.assertRaises(ValueError):
            reminder_time.validate({**spec, "relative_minutes": 50}, current_text=phrase)

    def test_tamil_suffixed_export_format_and_negation(self):
        self.assertTrue(core.brain._attachment_delivery_authorized("செலவுகள் PDFல அனுப்பு", "report_export"))
        self.assertFalse(core.brain._attachment_delivery_authorized("PDFல அனுப்ப வேண்டாம்", "report_export"))


class VoiceParityIngressTests(unittest.TestCase):
    claim = core.AlexCoreTests.claim
    actor = core.AlexCoreTests.actor
    payload = voice_fixture.MultilingualVoiceTests.payload
    setUpClass = classmethod(voice_fixture.MultilingualVoiceTests.setUpClass.__func__)
    setUp = voice_fixture.MultilingualVoiceTests.setUp
    tearDown = voice_fixture.MultilingualVoiceTests.tearDown

    def run_voice(self, phrase, steps, mid="parity-voice", **extra):
        self.api.return_value = SimpleNamespace(text=phrase, languages=[{"code": "ta"}])
        return core.AlexCoreTests._v0513_process_with_scripted_provider(
            self, self.payload(mid, **extra), steps)

    def seed_receipt(self):
        phone = "+60111111111"
        self.claim("parity-receipt", phone, "Log shared fixture purchase RM42.50")
        raw = b"synthetic receipt bytes; not the incoming voice recording"
        media_id = core.media.save_media("parity-receipt", "IMAGE", "image/jpeg", base64.b64encode(raw).decode())
        core.media._update_text(media_id, "ocr_text", "Fixture shop receipt RM42.50")
        actor = replace(self.actor("parity-receipt", phone, [media_id]), source="image",
                        trusted_text="Log shared fixture purchase RM42.50", action_key="parity-seed")
        result = core.services.log_expense(actor, "Fixture purchase", 42.50, "food", "MYR",
                                          event_date_local="2026-11-01T10:00:00+08:00")
        return media_id, raw, result

    def test_colloquial_voice_expense_persists_and_quoted_private_move_needs_no_model(self):
        text = 'நான் 56.90 ரூபாய் ஸ்பெண்ட் பண்ணேன் என் லெட்ஜர்ல லாக் பண்ணு'
        self.run_voice(text, [
            {'tool': 'log_expense', 'args': {'description': 'Synthetic coffee', 'amount': 56.90, 'category': 'food'}},
            {'content': 'Recorded the expense.'},
        ], mid='colloquial-expense')
        with core.db.connect() as conn:
            transcript = conn.execute("SELECT transcript_text FROM media_objects WHERE source_message_id='colloquial-expense' AND media_type='AUDIO'").fetchone()[0]
            self.assertEqual(transcript, text)
            row = conn.execute("SELECT * FROM financial_events WHERE source_message_id='colloquial-expense'").fetchone()
            self.assertEqual(row['amount_minor'], 5690)
            self.assertEqual(row['space_id'], 'FAMILY_SHARED')
            event_id = row['event_id']
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id='quoted-coffee' WHERE source_message_id='colloquial-expense' AND kind='TEXT'")
        payload = {'message_id': 'move-coffee', 'conversation_id': '60111111111@s.whatsapp.net',
                   'conversation_type': 'DIRECT_DM', 'sender_phone': '+60111111111',
                   'text': 'Actually save that to my private', 'quoted_message_id': 'quoted-coffee'}
        with patch.object(core.brain, 'respond', side_effect=AssertionError('Move must use durable quote binding')):
            result = core.ingress.process(payload)
        self.assertTrue(result['expense_moved_private'])
        with core.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT space_id FROM financial_events WHERE event_id=?', (event_id,)).fetchone()[0], 'HUSBAND_PVT')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 1)

    def test_tamil_receipt_retrieval_delivers_original_once(self):
        mid, raw, _ = self.seed_receipt()
        phrase = "அந்த ரிசீட் எனக்கு காமி"
        result = self.run_voice(phrase, [
            {"tool": "discover_alex_tools", "args": {"intent": "show original receipt"}},
            {"tool": "find_receipts", "args": {"query": "Fixture"}},
            {"tool": "get_receipt", "args": {"media_id": mid}},
            {"content": "Here is the original receipt."},
        ])
        self.assertTrue(result["ok"])
        with core.db.connect() as c:
            files = c.execute("SELECT * FROM outbound_messages WHERE source_message_id='parity-voice' AND kind='IMAGE'").fetchall()
        self.assertEqual(len(files), 1)
        self.assertEqual(Path(files[0]["local_path"]).read_bytes(), raw)
        self.assertTrue(core.ingress.process(self.payload("parity-voice"))["duplicate"])
        self.assertEqual(self.api.call_count, 1)

    def test_voice_save_of_quoted_photo_preserves_photo_not_command_audio(self):
        mid, raw, _ = self.seed_receipt()
        result = self.run_voice("Save this photo as fixture reference", [],quoted_message_id="parity-receipt")
        self.assertTrue(result["original_media_saved"])
        with core.db.connect() as c:
            rows = c.execute("SELECT media_id FROM saved_items").fetchall()
        self.assertEqual([r["media_id"] for r in rows], [mid])

    def test_voice_memory_note_does_not_require_command_recording(self):
        result = self.run_voice("Remember that our code word is cobalt", [
            {"tool": "save_item", "args": {"title": "Code word", "content": "cobalt"}},
            {"content": "I've saved the code word cobalt."},
        ])
        self.assertTrue(result["ok"])
        with core.db.connect() as c:
            row = c.execute("SELECT content,media_id FROM saved_items").fetchone()
            reply = c.execute("SELECT text_body FROM outbound_messages WHERE source_message_id='parity-voice' AND kind='TEXT'").fetchone()[0]
        self.assertEqual(row["content"], "cobalt")
        self.assertIsNone(row["media_id"])
        self.assertNotIn("couldn't verify", reply)

    def test_plan_and_task_natural_names_require_unique_scoped_match(self):
        import phase2
        from context import with_action_key
        phone = "+60111111111"
        self.claim("parity-plan", phone, "create family plan")
        actor = replace(self.actor("parity-plan",phone), trusted_text="create family plan")
        plan = phase2.create_plan(with_action_key(actor,"plan-one"),"Family Day Trip to Malacca")
        self.assertEqual(phase2.resolve_plan_reference(actor,plan_name="Malacca trip"),plan["plan_id"])
        task = phase2.create_task(with_action_key(actor,"task-one"),"Check passport expiry dates")
        self.assertEqual(phase2.resolve_task_reference(actor,task_name="passport-check"),task["task_id"])
        phase2.create_task(with_action_key(actor,"task-two"),"Check passport renewal status")
        with self.assertRaisesRegex(ValueError,"ambiguous"):
            phase2.resolve_task_reference(actor,task_name="passport-check")

    def test_appointment_read_keeps_calendar_tools_when_reminder_name_matches(self):
        self.claim("parity-dentist", "+60111111111", "Remind me of Dentist Appointment on 1 January 2030 at 3pm")
        actor = replace(self.actor("parity-dentist", "+60111111111"),
                        trusted_text="Remind me of Dentist Appointment on 1 January 2030 at 3pm", action_key="parity-dentist")
        core.services.create_reminder(actor,"Dentist Appointment","2030-01-01T15:00:00+08:00")
        result = self.run_voice("When is my dentist appointment?", [
            {"tool":"get_agenda", "args":{"start_date":"2030-01-01","end_date":"2030-01-01"}},
            {"content":"I found your reminder; no appointment has been recorded."},
        ])
        self.assertTrue(result["ok"])
        with core.db.connect() as c:
            trace = json.loads(c.execute("SELECT arguments_json FROM tool_audit WHERE source_message_id='parity-voice' AND tool_name='_turn_trace'").fetchone()[0])
        self.assertTrue({"get_agenda", "get_agenda_range"} & set(trace["exposed_tools"]))

    def test_agenda_relative_weekday_uses_actual_calendar(self):
        self.claim("parity-calendar", "+60111111111", "What's on my agenda next Thursday?")
        actor=replace(self.actor("parity-calendar", "+60111111111"),trusted_text="What's on my agenda next Thursday?")
        with patch.object(core.runtime_clock,"today",return_value=datetime(2026,9,29).date()):
            agenda=core.phase2.get_agenda(actor,"2026-10-02","2026-10-02")
        self.assertEqual((agenda["start_date"],agenda["end_date"]),("2026-10-01","2026-10-01"))

    def test_tamil_pdf_request_delivers_valid_report(self):
        self.seed_receipt()
        result = self.run_voice("என்னோட செலவுகள் PDFல அனுப்பு", [
            {"tool": "discover_alex_tools", "args": {"intent": "export November 2026 finance report as PDF"}},
            {"tool": "report_export", "args": {"format": "pdf", "period": "2026-11", "report_type": "finance"}},
            {"content": "Here is your report."},
        ])
        self.assertTrue(result["ok"])
        with core.db.connect() as c:
            files = c.execute("SELECT * FROM outbound_messages WHERE source_message_id='parity-voice' AND kind='DOCUMENT'").fetchall()
        self.assertEqual(len(files), 1)
        self.assertTrue(Path(files[0]["local_path"]).read_bytes().startswith(b"%PDF"))

    def test_colloquial_reminder_creates_one_real_due_row(self):
        phrase = "இன்னும் ஒரு அஞ்சு நிமிஷத்துல மருந்து எடுக்க ரிமைண்ட் பண்ணு"
        fixed = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        with patch.object(core.runtime_clock, "now_utc", return_value=fixed):
            result = self.run_voice(phrase, [
                # The recognized imperative now offers create_reminder in the
                # initial six-tool set. Discovery is only a spare-slot fallback.
                {"tool": "create_reminder", "args": {"task": "Take medicine", "due_local": "2026-11-02T20:05:00+08:00",
                    "time_interpretation": {"source_text": phrase, "kind": "RELATIVE", "reference": "CURRENT_MESSAGE",
                                            "relative_minutes": 5, "needs_clarification": False}}},
                {"content": "Reminder set for 8:05 PM."},
            ], sent_at_ms=int(fixed.timestamp() * 1000))
        self.assertTrue(result["ok"])
        with core.db.connect() as c:
            rows = c.execute("SELECT * FROM reminders").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["due_at_utc"], "2026-11-02T12:05:00+00:00")

    def test_pdf_denial_after_ledger_read_recovers_to_actual_file(self):
        self.seed_receipt()
        result = self.run_voice("Show November 2026 expenses as PDF", [
            {"tool": "query_finances", "args": {"start_date": "2026-11-01", "end_date": "2026-11-30"}},
            {"content": "I cannot generate a PDF file, but I have listed the transactions below."},
            {"tool": "report_export", "args": {"format": "pdf", "report_type": "finance"}},
            {"content": "Here is your PDF."},
        ])
        self.assertTrue(result["ok"])
        with core.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbound_messages WHERE source_message_id='parity-voice' AND kind='DOCUMENT'").fetchone()[0], 1)

    def test_certification_voice_fixture_reaches_authorized_brain(self):
        import behavior_cert
        observed = []
        async def respond(actor, text, *args, **kwargs):
            observed.append((actor.source, actor.voice_authorized, text))
            return "Fixture checked.", []
        with patch.object(core.brain, "respond", side_effect=respond):
            result = behavior_cert._run_ingress_turn(mid="rig-voice", conv="60111111111@s.whatsapp.net",
                prompt="Show my reminders", source="voice", phone="+60111111111", conversation_type="DIRECT_DM")
        self.assertTrue(result["ok"])
        self.assertEqual(observed, [("voice", True, "Show my reminders")])
        self.api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
