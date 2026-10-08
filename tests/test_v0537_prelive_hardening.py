"""v0.5.37 pre-live hardening: FIFO-adjacent reply binding and diagnostics v2."""

import json
import os
import uuid
import unittest
from unittest.mock import patch

import test_core as core

db = core.db
diagnostics = core.diagnostics
ingress = core.ingress
outbox = core.outbox
import shadow_router

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
        conn = db.connect()
        try:
            conn.execute("DELETE FROM pending_error_reports")
            conn.execute("DELETE FROM user_reported_errors")
            conn.commit()
        finally:
            conn.close()

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


if __name__ == "__main__":
    unittest.main()
