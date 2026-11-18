"""Exercise WhatsApp ingress, durable evidence, overnight decisions and the next real brain turn."""

import json
from contextlib import closing
from datetime import datetime
from unittest.mock import patch
from dataclasses import replace

from test_conversation_reliability import ConversationFixture
import test_core as core
import overnight_review as review
import daily_brief
import review_rules as rules

NIGHT = datetime.fromisoformat("2026-11-19T03:02:00+08:00")
STAMP = "2026-11-18 10:30:00"


class OvernightReviewTests(ConversationFixture):
    def setUp(self):
        super().setUp()
        self.clear_review()
        self.addCleanup(self.clear_review)

    def clear_review(self):
        with closing(core.db.connect()) as c, c:
            for t in (
                "review_enrollment",
                "review_rules",
                "review_runs",
                "review_lookup_hints",
                "review_preferences",
                "review_turns",
            ):
                c.execute("DELETE FROM " + t)

    def exchange(
        self, mid, text, reply=None, *, spouse=False, files=(), tools=(), state="COMPLETED", status="SENT"
    ):
        a = self.actor(mid, text, spouse=spouse)
        review.register(a)
        if reply:
            core.db.queue_outbound(a.conversation_id, "TEXT", text=reply, source_message_id=mid)
        for file in files:
            core.db.queue_outbound(a.conversation_id, file, source_message_id=mid)
        for name, provider in tools:
            core.brain._audit(a, name, {"provider": provider}, {"provider": provider}, True, 2, mid + name)
        with closing(core.db.connect()) as c, c:
            c.execute(
                "UPDATE inbound_messages SET received_at_utc=?,processing_state=? WHERE message_id=?",
                (STAMP, state, mid),
            )
            c.execute(
                "UPDATE outbound_messages SET delivery_status=?,delivered_at_utc=? WHERE source_message_id=?",
                (status, STAMP, mid),
            )
        return a

    def nightly(self, reviewer=None):
        review.run_due(now=NIGHT, reviewer=reviewer or (lambda packet: ("ok", "")))

    def test_maintenance_queue_contains_only_fixed_owner_evidence(self):
        self.assertEqual(review.maintenance_queue(NIGHT)["state"], "awaiting first review")
        self.exchange("private-message-id", "PRIVATE_NOTE", "PRIVATE_REPLY",
                      tools=[("find_receipts", "PRIVATE_ARGUMENT"), ("SECRET_TOOL", "OTHER_SECRET")])
        self.exchange("wife-only-id", "WIFE_PRIVATE", "WIFE_REPLY", spouse=True)
        with closing(core.db.connect()) as conn, conn:
            conn.execute("UPDATE review_turns SET finding='no_final',reviewed_on='2026-11-19',assessment='SECRET'")
            conn.execute("INSERT INTO review_runs VALUES('2026-11-19','2026-11-19')")
            conn.execute("INSERT OR REPLACE INTO review_enrollment VALUES('USR_WIFE',1)")
            conn.execute("INSERT OR REPLACE INTO review_turns(message_id,owner_id,finding,reviewed_on) "
                         "VALUES('wife-only-id','USR_WIFE','receipt_delivery','2026-11-19')")
        result = review.maintenance_queue(NIGHT)
        self.assertEqual(result["last_review"], "2026-11-19")
        self.assertEqual(result["cases"], [
            {"day":"2026-11-19","finding":"no_final","status":"investigate",
             "tools":["find_receipts (ok)", "other tool (ok)"],"count":1}
        ])
        self.assertNotIn("SECRET", json.dumps(result))
        with closing(core.db.connect()) as conn, conn:
            conn.execute("UPDATE review_turns SET repair='empty_promise' WHERE owner_id='USR_HUSBAND'")
        self.assertEqual(review.maintenance_queue(NIGHT)["cases"][0]["status"], "local repair applied")
        with closing(core.db.connect()) as conn, conn:
            conn.execute("UPDATE review_turns SET reviewed_on='2026-11-01'")
        self.assertEqual(review.maintenance_queue(NIGHT)["cases"], [])
        with closing(core.db.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO review_enrollment VALUES('USR_HUSBAND',0)")
        self.assertEqual(review.maintenance_queue(NIGHT), {"state":"disabled","cases":[]})

    def test_partial_bills_reproduced_and_next_turn_retry_uses_real_mcp(self):
        a = self.exchange(
            "pastbill", "Check my TNB and SAJ bills", "TNB RM22.", tools=[("check_household_bill", "tnb")]
        )
        self.nightly()
        self.assertIn("utility_coverage", review.active_rules(a.user_id))
        with patch(
            "household_bills.refresh",
            side_effect=[
                {"provider": "tnb", "status": "refreshed", "live": True, "outstanding": "22.00"},
                {"provider": "saj", "status": "unavailable", "live": False},
            ],
        ) as portal:
            result, reqs = self.scripted(
                [
                    {"content": "I’ll check."},
                    {
                        "calls": [
                            {"tool": "check_household_bill", "args": {"provider": "tnb"}},
                            {"tool": "check_household_bill", "args": {"provider": "saj"}},
                        ]
                    },
                    {"content": "Here is TNB only."},
                ],
                payload=self.payload("newbill", "Please check both my SAJ and TNB bills"),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(portal.call_count, 2)
        with closing(core.db.connect()) as c:
            reply = c.execute(
                "SELECT cached_response FROM inbound_messages WHERE message_id='newbill'"
            ).fetchone()[0]
        self.assertIn("TNB", reply)
        self.assertIn("SAJ", reply)
        self.assertIn("unconfirmed", reply)

    def test_receipt_without_delivery_and_successful_slow_reply(self):
        a = self.exchange("lost", "Send me the original receipt", "Here it is.")
        self.exchange("good", "Show my original receipt", "Here it is.", files=["IMAGE"])
        self.nightly()
        self.assertEqual(review.active_rules(a.user_id), {"receipt_delivery"})
        with closing(core.db.connect()) as c:
            self.assertEqual(
                c.execute("SELECT finding FROM review_turns WHERE message_id='good'").fetchone()[0], None
            )
        result, _ = self.scripted(
            [{"content": "Attached."}, {"content": "Attached."}],
            payload=self.payload("receipt-next", "Fetch my original receipt"),
        )
        self.assertTrue(result["ok"])
        with closing(core.db.connect()) as c:
            self.assertIn(
                "couldn't retrieve",
                c.execute(
                    "SELECT cached_response FROM inbound_messages WHERE message_id='receipt-next'"
                ).fetchone()[0],
            )

    def test_brief_contains_only_invitation_report_owner_only_undo_respected(self):
        owner = self.exchange("secret", "Show the original receipt for BLUEWIDGET", "Here it is.")
        self.nightly()
        payload = daily_brief.create_or_get(owner, "2026-11-19")
        self.assertEqual(payload["text"].count(review.NOTICE), 1)
        self.assertNotIn("BLUEWIDGET", payload["text"])
        self.assertTrue(payload["actionable"])
        self.assertNotIn(
            review.NOTICE,
            daily_brief.build(self.actor("sp", "daily brief", spouse=True), "2026-11-19")["text"],
        )
        self.assertIn("BLUEWIDGET", review.handle(replace(owner, trusted_text="show my diagnostic report")))
        self.assertNotIn(
            "BLUEWIDGET",
            review.handle(replace(owner, trusted_text="diagnostic report", conversation_type="GROUP")),
        )
        self.assertNotIn("BLUEWIDGET", review.report("USR_WIFE"))
        review.handle(replace(owner, trusted_text="undo diagnostic repair 2"))
        self.exchange("again", "Send my original receipt", "Attached.")
        review.run_due(now=NIGHT.replace(day=20), reviewer=lambda p: ("ok", ""))
        self.assertFalse(review.active_rules(owner.user_id))

    def test_feedback_is_acknowledged_and_applied_only_in_this_owners_chat(self):
        self.assertTrue(
            core.ingress.process(self.payload("style", "Next time, keep your replies short"))[
                "overnight_review"
            ]
        )
        with closing(core.db.connect()) as c, c:
            c.execute("UPDATE inbound_messages SET received_at_utc=? WHERE message_id='style'", (STAMP,))
            c.execute("UPDATE outbound_messages SET delivery_status='SENT' WHERE source_message_id='style'")
        self.nightly()
        self.assertTrue(review.response_preference("USR_HUSBAND", "60111111111@s.whatsapp.net"))
        self.assertFalse(review.response_preference("USR_HUSBAND", "family@g.us"))
        self.assertFalse(review.response_preference("USR_WIFE", "60111111111@s.whatsapp.net"))
        self.assertIsNone(review.explicit_preference("Actually save that to my private"))

    def test_rephrase_learns_only_existing_same_scope_tool_without_physical_replay(self):
        a = self.exchange("lookup1", "Check price Huggies XL tape Manjaku", "I’m working on that.")
        self.exchange(
            "lookup2",
            "Show Huggies XL tape price in Manjaku",
            "Manjaku Online — RM50",
            tools=[("search_manjaku_prices", "")],
        )
        self.nightly()
        offered = {"search_manjaku_prices"}
        self.assertEqual(
            review.lookup_hint(a, "How much is Huggies XL tape at Manjaku?", offered), "search_manjaku_prices"
        )
        self.assertIsNone(review.lookup_hint(a, "How much is Huggies XL tape at Manjaku?", set()))
        self.assertIsNone(review.lookup_hint(replace(a, read_scope="private"), a.trusted_text, offered))
        self.assertIsNone(review.lookup_hint(a, "Check the TNB bill", offered))

    def test_overdue_notice_once_and_never_counts_as_delivered_result(self):
        self.exchange("missing", "Check my TNB and SAJ bills", state="FAILED")
        now = datetime.fromisoformat("2026-11-18T11:00:00+00:00")
        with patch.object(core.runtime_clock, "now_utc", return_value=now):
            review.check_unfinished()
            review.check_unfinished()
        with closing(core.db.connect()) as c, c:
            rows = c.execute(
                "SELECT context_kind FROM outbound_messages WHERE source_message_id='missing'"
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["REVIEW_STATUS"])
            c.execute("UPDATE outbound_messages SET delivery_status='SENT'")
        self.nightly()
        self.assertIn("Processing failed", review.report("USR_HUSBAND"))
        self.assertFalse(review.active_rules("USR_HUSBAND"))

    def test_model_cannot_install_code_rules_or_review_another_owner(self):
        self.exchange("one", "Show my receipt", "Here it is.")
        self.exchange("two", "My secret marker SPOUSE_ONLY", "Noted.", spouse=True)
        packets = []

        def reviewer(p):
            packets.append(p)
            return "mismatch", "Needs investigation."

        self.nightly(reviewer)
        self.assertNotIn("SPOUSE_ONLY", str(packets[0]))
        count = len(packets)
        self.nightly(reviewer)
        self.assertEqual(len(packets), count)
        self.assertFalse(review.active_rules("USR_WIFE"))

    def test_promise_and_blanket_instructions_are_not_authority(self):
        self.assertFalse(rules.applicable("Don't check TNB and SAJ bills"))
        self.assertFalse(rules.applicable("If I ask you to check TNB and SAJ bills"))
        p = rules.evaluate("Check prices for rice", "I’m working on that and will get it to you shortly…")
        self.assertTrue(
            rules.replay_verified(
                p,
                "Check prices for rice",
                "I’m working on that and will get it to you shortly…",
                set(),
                False,
            )
        )

    def test_ordinary_feedback_applies_tested_bill_fix_without_matching_historical_failure(self):
        text = "Next time if a bill fails, send me the available bill and tell me which one failed."
        self.assertTrue(core.ingress.process(self.payload("feedback-bill", text))["overnight_review"])
        with closing(core.db.connect()) as c, c:
            c.execute(
                "UPDATE inbound_messages SET received_at_utc=? WHERE message_id='feedback-bill'", (STAMP,)
            )
        self.nightly()
        self.assertIn("utility_coverage", review.active_rules("USR_HUSBAND"))
        self.assertIsNone(
            rules.feedback_repair("Next time if a bill fails, send me the other person's private bill.")
        )

    def test_brief_yes_and_late_notice_dont_require_a_swipe_or_notify_twice(self):
        a = self.exchange("private-result", "Fetch my original receipt", "It is attached.")
        early = daily_brief.create_or_get(a, "2026-11-19")
        self.assertNotIn(review.NOTICE, early["text"])
        self.nightly()
        brief = daily_brief.create_or_get(a, "2026-11-19")
        self.assertIn(review.NOTICE, brief["text"])
        oid = core.db.queue_outbound(
            a.conversation_id,
            "TEXT",
            text=brief["text"],
            context_kind="DAILY_BRIEF",
            context_id=brief["brief_id"],
        )
        with closing(core.db.connect()) as c, c:
            c.execute(
                "UPDATE outbound_messages SET delivery_status='SENT',delivered_at_utc=? WHERE outbound_id=?",
                ("2026-11-19 00:00:00", oid),
            )
        yes = self.actor("say-yes", "yes")
        self.assertIn("Diagnostic report", review.handle(yes))
        self.assertFalse(review.notice_date(a.user_id, "2026-11-20"))
        self.assertTrue(review.notice_date(a.user_id, "2026-11-19"))

    def test_failed_model_is_labelled_and_does_not_stop_local_review(self):
        self.exchange("p", "Check price of Huggies XL", "I’ll check.")
        fail = lambda _: (_ for _ in ()).throw(RuntimeError("unavailable"))
        self.nightly(fail)
        report = review.report("USR_HUSBAND")
        self.assertIn("independent review unavailable", report)
        self.assertIn("empty_promise", review.active_rules("USR_HUSBAND"))

    def test_healthy_sample_can_reveal_a_semantic_error(self):
        import hashlib

        mid = next(
            str(i)
            for i in range(500)
            if int.from_bytes(hashlib.sha256(str(i).encode()).digest()[:2], "big") % 25 == 0
        )
        self.exchange(mid, "What is the XL tape price at Manjaku?", "The M pants cost RM65.")
        self.nightly(lambda packet: ("mismatch", "Different size and product type."))
        self.assertIn("Different size and product type.", review.report("USR_HUSBAND"))
        self.assertFalse(review.active_rules("USR_HUSBAND"))

    def test_model_call_is_tool_free_and_usage_accounted(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        route = {"provider": "openai", "model": "test", "reasoning_effort": "low"}
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content='{"verdict":"ok","reason":"Delivered."}'))
            ],
            usage=None,
        )
        with (
            patch.object(core.brain, "_provider_routes", return_value=[route]),
            patch.object(core.brain, "_client_for", return_value=client),
            patch.object(core.brain, "_record_usage_buckets") as usage,
        ):
            self.assertEqual(
                review.model_review({"request": "test", "delivered_answer": "ok"}), ("ok", "Delivered.")
            )
            self.assertNotIn("tools", client.chat.completions.create.call_args.kwargs)
            usage.assert_called_once()

    def test_spouses_private_chat_requires_her_own_enrollment(self):
        spouse = self.exchange("other-member", "Send my receipt", "Attached.", spouse=True)
        with closing(core.db.connect()) as c:
            self.assertIsNone(
                c.execute(
                    "SELECT message_id FROM review_turns WHERE owner_id=?", (spouse.user_id,)
                ).fetchone()
            )
        self.assertIn(
            "private chat",
            review.handle(replace(spouse, trusted_text="enable overnight review", conversation_type="GROUP")),
        )
        self.assertIn("enabled", review.handle(replace(spouse, trusted_text="enable overnight review")))
        review.register(spouse)
        self.assertIn("disabled", review.handle(replace(spouse, trusted_text="disable overnight review")))
        with closing(core.db.connect()) as c:
            self.assertIsNone(
                c.execute(
                    "SELECT message_id FROM review_turns WHERE owner_id=?", (spouse.user_id,)
                ).fetchone()
            )
