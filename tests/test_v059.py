import json
import unittest
from unittest.mock import patch

from tests import test_core as core


db = core.db
services = core.services
ingress = core.ingress
outbox = core.outbox
brain = core.brain
with_action_key = core.with_action_key


class V059LiveRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.initialize()

    def setUp(self):
        conn = db.connect()
        try:
            for table in (
                # v0.5.37 diagnostics reference the exact outbound/inbound turn,
                # so clear those dependent rows before the legacy v0.5.9 fixture
                # removes their parents.
                "pending_error_reports", "user_reported_errors",
                "outbound_messages", "reminder_handoffs", "reminder_claim_events",
                "reminder_events", "reminders", "pending_items", "inbound_messages",
                "tool_audit", "tool_execution_claims",
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

    def actor(self, mid, phone):
        return db.resolve_actor(
            phone, phone.replace("+", "") + "@s.whatsapp.net",
            "DIRECT_DM", mid, [],
        )

    def test_direct_dm_completion_reaction_closes_due_reminder(self):
        self.claim("v059-dm-create", "+60111111111")
        actor = with_action_key(
            self.actor("v059-dm-create", "+60111111111"),
            "v059-dm-create-action",
        )
        reminder = services.create_reminder(
            actor, "v059 dm reaction", "2026-10-03T13:00:00+08:00"
        )
        oid = db.queue_outbound(
            actor.conversation_id, "TEXT", text="Reminder: v059 dm reaction",
            context_kind="REMINDER_INITIAL", context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE reminders SET status='DUE' WHERE reminder_id=?",
                (reminder["reminder_id"],),
            )
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-v059-dm-due',delivery_status='SENT',
                       delivered_at_utc=CURRENT_TIMESTAMP,
                       job_pinned_at_utc=CURRENT_TIMESTAMP,
                       job_pin_target='OUTBOUND'
                   WHERE outbound_id=?""",
                (oid,),
            )
            conn.commit()
        finally:
            conn.close()

        result = ingress.process({
            "message_id": "v059-dm-reaction-event",
            "provider": "WHATSAPP",
            "conversation_id": actor.conversation_id,
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "event_kind": "REACTION",
            "reaction_target_message_id": "wa-v059-dm-due",
            "reaction_text": "✅",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["reaction"]["status"], "completed")

        conn = db.connect()
        try:
            state = conn.execute(
                "SELECT status FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state, "COMP")

    def test_reminder_draft_pins_outbound_without_hourglass_then_unpins_on_cancel(self):
        first = {
            "message_id": "v059-draft-first",
            "provider": "WHATSAPP",
            "conversation_id": "60111111111@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": "Remind me to test the lamp",
        }
        with patch.object(
            brain, "respond",
            return_value=("What time would you like to be reminded?", []),
        ):
            self.assertTrue(ingress.process(first)["ok"])

        conn = db.connect()
        try:
            draft = conn.execute(
                """SELECT * FROM pending_items
                   WHERE source_message_id='v059-draft-first'
                     AND kind='REMINDER_DRAFT'"""
            ).fetchone()
            outbound = conn.execute(
                """SELECT outbound_id FROM outbound_messages
                   WHERE source_message_id='v059-draft-first'
                     AND context_kind='PENDING_ITEM'"""
            ).fetchone()
            conn.execute(
                """UPDATE outbound_messages
                   SET provider_message_id='wa-v059-draft',
                       delivery_status='SENT',delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (outbound["outbound_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        sent = []
        conn = db.connect()
        try:
            with patch.object(
                outbox, "_send",
                side_effect=lambda payload: (sent.append(payload) or True, "{}"),
            ):
                outbox._reconcile_pending_item_markers(conn)
            markers = conn.execute(
                """SELECT job_reacted_at_utc,job_pinned_at_utc,job_pin_target
                   FROM outbound_messages WHERE outbound_id=?""",
                (outbound["outbound_id"],),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNone(markers["job_reacted_at_utc"])
        self.assertIsNotNone(markers["job_pinned_at_utc"])
        self.assertEqual(markers["job_pin_target"], "OUTBOUND")
        self.assertEqual([payload["kind"] for payload in sent], ["pin"])
        self.assertTrue(sent[0]["target_from_me"])
        self.assertEqual(sent[0]["target_message_id"], "wa-v059-draft")

        second = {
            "message_id": "v059-draft-cancel",
            "provider": "WHATSAPP",
            "conversation_id": "60111111111@s.whatsapp.net",
            "conversation_type": "DIRECT_DM",
            "sender_phone": "+60111111111",
            "text": "cancel",
        }
        cancelled = ingress.process(second)
        self.assertTrue(cancelled["ok"])
        self.assertTrue(cancelled["reminder_draft_cancelled"])

        sent.clear()
        conn = db.connect()
        try:
            with patch.object(
                outbox, "_send",
                side_effect=lambda payload: (sent.append(payload) or True, "{}"),
            ):
                outbox._reconcile_pending_item_markers(conn)
            draft_state = conn.execute(
                "SELECT status FROM pending_items WHERE item_id=?",
                (draft["item_id"],),
            ).fetchone()["status"]
            cleanup = conn.execute(
                """SELECT job_unpinned_at_utc FROM outbound_messages
                   WHERE outbound_id=?""",
                (outbound["outbound_id"],),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(draft_state, "CANCELLED")
        self.assertIsNotNone(cleanup["job_unpinned_at_utc"])
        self.assertEqual([payload["kind"] for payload in sent], ["unpin"])
        self.assertTrue(sent[0]["target_from_me"])


if __name__ == "__main__":
    unittest.main()
