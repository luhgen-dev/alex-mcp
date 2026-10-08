"""v0.5.37 pre-live hardening: FIFO-adjacent reply binding and diagnostics v2."""

import json
import os
import uuid
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_core as core

db = core.db
diagnostics = core.diagnostics
ingress = core.ingress
outbox = core.outbox
import shadow_router
from context import with_action_key
import services

PHONE = "+60111111111"
DM = "60111111111@s.whatsapp.net"


class V0537Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.initialize()

    def setUp(self):
        # Reuse the canonical fixture reset without inheriting its hundreds of
        # test methods into this focused v0.5.37 class.
        core.AlexCoreTests.setUp(self)
        conn = db.connect()
        try:
            try:
                conn.execute("DELETE FROM alex_shadow_router_log")
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        # Leave the shared unittest database exactly as the canonical core
        # fixture expects. This keeps focused v0.5.37 cases from leaking saved
        # items/selections into later legacy test modules.
        core.AlexCoreTests.setUp(self)

    def claim(self, mid, phone, text=""):
        return core.AlexCoreTests.claim(self, mid, phone, text)

    def actor(self, mid, phone, media_ids=None):
        return core.AlexCoreTests.actor(self, mid, phone, media_ids)

    def sent_alex_message(self, source_id="bad-source", provider_id="WA-BAD",
                          text="Wrong answer"):
        self.claim(source_id, PHONE, "original request")
        oid = db.queue_outbound(
            DM, "TEXT", text=text, source_message_id=source_id
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET delivery_status='SENT',provider_message_id=?,
                       delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (provider_id, oid),
            )
            conn.commit()
        finally:
            conn.close()
        db.finish_inbound(source_id, text)
        return oid

    def payload(self, mid, text, quoted=None):
        return {
            "message_id": mid,
            "provider": "WHATSAPP",
            "conversation_id": DM,
            "conversation_type": "DIRECT_DM",
            "sender_phone": PHONE,
            "text": text,
            "quoted_message_id": quoted,
        }


class ErrorLifecycleTests(V0537Base):
    def test_natural_error_phrases_are_bounded_and_deterministic(self):
        for text in (
            "Mark this as error",
            "mark that as an error",
            "this is an error",
            "This was wrong",
            "report this error",
        ):
            self.assertTrue(ingress._error_report_command(text)[0], text)
        ok, why = ingress._error_report_command(
            "Mark this as error because it sent the reminder to DM"
        )
        self.assertTrue(ok)
        self.assertIn("sent the reminder", why)
        self.assertFalse(ingress._error_report_command("there was an error yesterday")[0])

    def test_pending_error_only_consumes_reply_to_its_own_prompt(self):
        self.sent_alex_message()
        started = ingress.process(self.payload(
            "mark-1", "Mark this as error", quoted="WA-BAD"
        ))
        self.assertTrue(started["ok"])

        conn = db.connect()
        try:
            pending = conn.execute(
                "SELECT * FROM pending_error_reports"
            ).fetchone()
            self.assertIsNotNone(pending)
            prompt = conn.execute(
                """SELECT * FROM outbound_messages
                   WHERE source_message_id='mark-1'
                     AND context_kind='ERROR_REPORT_DRAFT'"""
            ).fetchone()
            self.assertIsNotNone(prompt)
            # Even with a context kind, this immediate response must quote the
            # exact "Mark this as error" request.
            payload = outbox._payload(outbox._joined_row(conn, prompt["outbound_id"]))
            self.assertEqual(payload["reply_to_message_id"], "mark-1")
            conn.execute(
                """UPDATE outbound_messages SET delivery_status='SENT',
                   provider_message_id='WA-ERROR-PROMPT',
                   delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (prompt["outbound_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        # An unrelated message is normal household work; it must not become
        # the explanation merely because a draft exists.
        unrelated = ingress.process(self.payload("other-1", "hello"))
        self.assertTrue(unrelated["ok"])
        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_reported_errors").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM pending_error_reports").fetchone()[0],
                1,
            )
        finally:
            conn.close()

        explained = ingress.process(self.payload(
            "explain-1",
            "I expected the group reminder, but Alex created it in my DM.",
            quoted="WA-ERROR-PROMPT",
        ))
        self.assertTrue(explained["ok"])
        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM pending_error_reports").fetchone()[0],
                0,
            )
            row = conn.execute(
                "SELECT error_id,user_explanation FROM user_reported_errors"
            ).fetchone()
            self.assertTrue(row["error_id"].startswith("ALEX-"))
            self.assertIn("group reminder", row["user_explanation"])
        finally:
            conn.close()

    def test_bound_explanation_that_sounds_like_error_command_still_completes_draft(self):
        self.sent_alex_message("src-bound", "WA-BOUND", "wrong answer")
        started = ingress.process(self.payload(
            "mark-bound", "Mark this as error", quoted="WA-BOUND"
        ))
        self.assertTrue(started["ok"])
        conn = db.connect()
        try:
            prompt = conn.execute(
                """SELECT outbound_id FROM outbound_messages
                   WHERE source_message_id='mark-bound'
                     AND context_kind='ERROR_REPORT_DRAFT'"""
            ).fetchone()
            conn.execute(
                """UPDATE outbound_messages SET delivery_status='SENT',
                   provider_message_id='WA-BOUND-PROMPT',
                   delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (prompt["outbound_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        result = ingress.process(self.payload(
            "explain-bound",
            "This was wrong because it used my DM instead of the group.",
            quoted="WA-BOUND-PROMPT",
        ))
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM pending_error_reports").fetchone()[0],
                0,
            )
            row = conn.execute(
                "SELECT user_explanation FROM user_reported_errors"
            ).fetchone()
            self.assertIn("used my DM", row["user_explanation"])
        finally:
            conn.close()

    def test_inline_second_report_cannot_bypass_existing_unresolved_draft(self):
        oid1 = self.sent_alex_message("src-first", "WA-FIRST", "first wrong")
        self.claim("mark-first", PHONE, "mark")
        actor = self.actor("mark-first", PHONE)
        diagnostics.begin_user_error_report(actor, {"outbound_id": oid1})

        # A second bad message is visible and quotable, but an inline report
        # must not create a second incident while the first prompt is unresolved.
        oid2 = db.queue_outbound(
            DM, "TEXT", text="second wrong", source_message_id="src-first"
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages SET delivery_status='SENT',
                   provider_message_id='WA-SECOND',
                   delivered_at_utc=CURRENT_TIMESTAMP WHERE outbound_id=?""",
                (oid2,),
            )
            conn.commit()
        finally:
            conn.close()

        result = ingress.process(self.payload(
            "mark-second",
            "Mark this as error because this is also wrong",
            quoted="WA-SECOND",
        ))
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM pending_error_reports").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_reported_errors").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_second_unresolved_report_does_not_overwrite_first(self):
        oid1 = self.sent_alex_message("src-1", "WA-1", "first wrong")
        self.claim("mark-direct", PHONE, "mark")
        actor = self.actor("mark-direct", PHONE)
        first = diagnostics.begin_user_error_report(
            actor, {"outbound_id": oid1}
        )
        oid2 = db.queue_outbound(
            DM, "TEXT", text="second wrong", source_message_id="src-1"
        )
        second = diagnostics.begin_user_error_report(
            actor, {"outbound_id": oid2}
        )
        self.assertEqual(second["status"], "already_pending")
        self.assertEqual(second["error_draft_id"], first["error_draft_id"])
        self.assertEqual(second["target_outbound_id"], oid1)

    def test_cancel_requires_bound_error_prompt_reference(self):
        oid = self.sent_alex_message()
        self.claim("mark-cancel", PHONE, "mark")
        actor = self.actor("mark-cancel", PHONE)
        started = diagnostics.begin_user_error_report(actor, {"outbound_id": oid})
        self.assertEqual(
            diagnostics.cancel_user_error_report(
                actor, started["error_draft_id"]
            )["status"],
            "cancelled",
        )
        self.assertIsNone(diagnostics.pending_user_error_report(actor))


class ReplyBindingAndMarkersTests(V0537Base):
    def test_immediate_context_reply_quotes_source_but_delayed_reminder_does_not(self):
        self.claim("reply-src", PHONE, "show my receipts")
        immediate = db.queue_outbound(
            DM, "TEXT", text="1. receipt",
            source_message_id="reply-src",
            context_kind="SELECTION_SET", context_id="receipt:set1",
        )
        delayed = db.queue_outbound(
            DM, "TEXT", text="Reminder due",
            source_message_id="reply-src",
            context_kind="REMINDER_INITIAL", context_id="rem-1",
        )
        conn = db.connect()
        try:
            p1 = outbox._payload(outbox._joined_row(conn, immediate))
            p2 = outbox._payload(outbox._joined_row(conn, delayed))
        finally:
            conn.close()
        self.assertEqual(p1["reply_to_message_id"], "reply-src")
        self.assertIsNone(p2["reply_to_message_id"])

    def test_error_prompt_itself_gets_hourglass_and_pin_then_cleans_up(self):
        target = self.sent_alex_message()
        self.claim("marker-source", PHONE, "mark")
        actor = self.actor("marker-source", PHONE)
        started = diagnostics.begin_user_error_report(
            actor, {"outbound_id": target}
        )
        prompt_oid = db.queue_outbound(
            DM, "TEXT", text="What was wrong?",
            source_message_id="marker-source",
            context_kind="ERROR_REPORT_DRAFT",
            context_id=started["error_draft_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages SET delivery_status='SENT',
                   provider_message_id='WA-PROMPT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (prompt_oid,),
            )
            conn.commit()
        finally:
            conn.close()

        sent = []
        def fake_send(payload):
            sent.append(dict(payload))
            return True, '{"ok":true,"message_id":"control"}'

        with patch.object(outbox, "_send", side_effect=fake_send):
            conn = db.connect()
            try:
                outbox._reconcile_error_report_markers(conn)
            finally:
                conn.close()

        self.assertEqual([x["kind"] for x in sent[:2]], ["reaction", "pin"])
        self.assertEqual(sent[0]["emoji"], "⏳")
        self.assertTrue(sent[0]["target_from_me"])
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT job_reacted_at_utc,job_pinned_at_utc,job_pin_target "
                "FROM outbound_messages WHERE outbound_id=?",
                (prompt_oid,),
            ).fetchone()
            self.assertIsNotNone(row["job_reacted_at_utc"])
            self.assertIsNotNone(row["job_pinned_at_utc"])
            self.assertEqual(row["job_pin_target"], "OUTBOUND")
        finally:
            conn.close()

        diagnostics.cancel_user_error_report(actor, started["error_draft_id"])
        sent.clear()
        with patch.object(outbox, "_send", side_effect=fake_send):
            conn = db.connect()
            try:
                outbox._reconcile_error_report_markers(conn)
            finally:
                conn.close()
        self.assertEqual([x["kind"] for x in sent], ["reaction", "unpin"])
        self.assertEqual(sent[0]["emoji"], "")


class DiagnosticBundleTests(V0537Base):
    def test_bundle_contains_router_trace_timeline_and_is_exportable(self):
        oid = self.sent_alex_message("diag-src", "WA-DIAG", "wrong finance answer")
        shadow_router.record({
            "source_message_id": "diag-src",
            "conversation_type": "DIRECT_DM",
            "message_snippet": "eh how much we burn",
            "keyword_tools": ["query_finances"],
            "called_tools": ["query_finances"],
            "live_outcome": "answered",
            "shadow_tools": ["query_finances"],
            "shadow_clarify": False,
            "shadow_reason": "finance read",
            "status": "ok",
            "model": "gpt-test",
            "latency_ms": 17,
            "ai_routed": 1,
            "semantic_intent": "READ_FINANCES",
            "semantic_reference": "NONE",
            "semantic_slots": {"query": "this week"},
            "semantic_confidence": 0.97,
            "semantic_mode": "live",
        })
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO tool_audit(
                    audit_id,action_key,source_message_id,user_id,tool_name,
                    arguments_json,result_json,status,latency_ms
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), "trace:diag-src", "diag-src",
                    "USR_HUSBAND", "_turn_trace",
                    json.dumps({
                        "source": "text", "conversation_type": "DIRECT_DM",
                        "exposed_tools": ["query_finances"],
                        "history_turns": 2, "quoted_context": False,
                    }),
                    json.dumps({
                        "routes": ["chatgpt:gpt-test"],
                        "tools_called": ["query_finances"],
                        "attachments_queued": 0,
                        "outcome": "answered",
                    }),
                    "OK", 0,
                ),
            )
            conn.execute(
                """INSERT INTO tool_audit(
                    audit_id,action_key,source_message_id,user_id,tool_name,
                    arguments_json,result_json,status,latency_ms
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), "read:diag-src", "diag-src",
                    "USR_HUSBAND", "query_finances", "{}",
                    '{"count":2}', "OK", 4,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        db.record_usage(
            "diag-src", "chatgpt", "gpt-test",
            100, 20, 1, 1200, model_calls=1,
        )

        self.claim("diag-report", PHONE, "report it")
        actor = self.actor("diag-report", PHONE)
        recorded = diagnostics.complete_user_error_report(
            actor,
            "The total was wrong.",
            {"outbound_id": oid},
        )
        report = diagnostics.get_user_reported_error(
            actor, recorded["error_id"]
        )
        bundle = report["bundle"]
        self.assertEqual(bundle["bundle_format"], "alex-diagnostic-v2")
        self.assertEqual(bundle["semantic_router"]["intent"], "READ_FINANCES")
        self.assertEqual(
            bundle["intent_trace"]["input"]["history_turns"], 2
        )
        self.assertEqual(bundle["stages"]["TOOL"]["status"], "OK")
        self.assertTrue(bundle["timeline"])
        self.assertNotEqual(
            bundle["build"]["source_fingerprint"], "unknown"
        )

        exported = diagnostics.export_user_reported_error(
            actor, recorded["error_id"]
        )
        path = exported["_attachments"][0]["path"]
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["format"], "alex-diagnostic-export-v2")
        self.assertEqual(payload["error_id"], recorded["error_id"])


class ScreenshotRegressionTests(V0537Base):
    def test_numbered_fast_path_accepts_wording_alex_teaches(self):
        cases = {
            "Show 1": 1,
            "Show me 1": 1,
            "send me 2": 2,
            "get me number 3": 3,
            "private 5": 5,
            "listen to 6": 6,
            "hear 7": 7,
        }
        for text, expected in cases.items():
            parsed = ingress._numbered_selection_request(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed[0], expected, text)
            self.assertTrue(parsed[1], text)

    def test_private_numbered_selection_keeps_original_scope(self):
        self.claim("sel-private-save", PHONE, "save this privately")
        private_writer = with_action_key(
            replace(
                self.actor("sel-private-save", PHONE),
                trusted_text="save this privately",
                read_scope="private",
            ),
            "sel-private-save-action",
        )
        services.save_item(
            private_writer, "Private selector", "private selector content"
        )

        self.claim("sel-private-list", PHONE, "show my private saved items")
        private_reader = replace(
            self.actor("sel-private-list", PHONE),
            trusted_text="show my private saved items",
            read_scope="private",
        )
        found = services.search_saved_items(private_reader, "Private selector")
        self.assertEqual(found["count"], 1)

        # The next plain command normally defaults to Family Shared. The
        # persisted selection object is the authorization-bearing continuation,
        # so it must still retrieve the exact private item for the same owner/DM.
        self.claim("sel-private-follow", PHONE, "Show me 1")
        plain_follow = replace(
            self.actor("sel-private-follow", PHONE),
            trusted_text="Show me 1",
            read_scope="family",
        )
        result = services.resolve_numbered_choice(plain_follow, 1)
        self.assertEqual(result["title"], "Private selector")

        ctx = services.latest_selection_set_context(plain_follow)
        self.assertEqual(ctx["scope"], "private")

    def test_privacy_emoji_is_control_not_model_subject(self):
        turn = ingress.build_turn(
            {"text": "Show me my 🎂 expenses"},
            [],
        )
        self.assertEqual(turn["read_scope"], "private")
        self.assertEqual(turn["semantic_text"], "Show me my expenses")
        self.assertNotIn("🎂", turn["intent_text"])

    def test_live_brain_receives_text_without_privacy_emoji(self):
        captured = {}

        async def fake_respond(actor, user_text, media_context=None,
                               vision_parts=None, quoted_context=None, **kwargs):
            captured["text"] = user_text
            captured["scope"] = actor.read_scope
            return ("No matching expenses.", [])

        payload = self.payload(
            "emoji-brain-1", "Show me my 🎂 expenses"
        )
        with patch.object(core.brain, "respond", new=fake_respond):
            result = ingress.process(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(captured["text"], "Show me my expenses")
        self.assertEqual(captured["scope"], "private")

    def test_not_configured_dependency_never_becomes_private_search_offer(self):
        self.claim("roster-no-offer", PHONE, "What shift am I working next week")
        actor = replace(
            self.actor("roster-no-offer", PHONE),
            trusted_text="What shift am I working next week",
            read_scope="family",
        )
        candidate = ingress._private_search_offer_candidate(
            actor,
            "What shift am I working next week",
            "Status: not_configured\nDependency: roster\n"
            "Message: I don't have your work roster configured yet.",
            [],
        )
        self.assertFalse(candidate)

    def test_private_structured_miss_cannot_claim_shared_scope_or_offer_retry(self):
        self.claim("private-miss-src", PHONE, "show my receipts ❤️")
        actor = replace(
            self.actor("private-miss-src", PHONE),
            trusted_text="show my receipts ❤️",
            read_scope="private",
        )
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO tool_audit(
                       audit_id,action_key,source_message_id,user_id,tool_name,
                       arguments_json,result_json,status,latency_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), "private-miss-action",
                    actor.source_message_id, actor.user_id, "find_receipts",
                    "{}",
                    json.dumps({
                        "status": "not_found_in_current_scope",
                        "domain": "receipt",
                        "scope": "private",
                        "private_search_available": False,
                        "matches": [],
                    }),
                    "OK", 1,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        pending, reply = ingress._maybe_create_private_search_offer(
            actor, actor.trusted_text,
            "I only checked your shared records. I can check private too.",
            [],
        )
        self.assertIsNone(pending)
        self.assertEqual(reply, "I couldn't find that in your private records.")

    def test_permission_denial_is_user_visible_not_silent(self):
        payload = self.payload("visible-denial", "Show me 1")
        with patch.object(
            services, "resolve_numbered_choice",
            side_effect=PermissionError("private record"),
        ):
            result = ingress.process(payload)
        self.assertTrue(result["ok"])
        self.assertTrue(result["unauthorized"])
        conn = db.connect()
        try:
            row = conn.execute(
                """SELECT text_body FROM outbound_messages
                   WHERE source_message_id='visible-denial'
                   ORDER BY rowid DESC LIMIT 1"""
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIn("can’t use that record", row["text_body"])



if __name__ == "__main__":
    unittest.main()
