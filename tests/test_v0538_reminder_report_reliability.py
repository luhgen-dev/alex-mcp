"""v0.5.38 reminder pin and PDF reliability regressions."""

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_core as core

db = core.db
outbox = core.outbox
services = core.services
mcp_server = core.mcp_server
phase2_reports = core.phase2_reports
from context import with_action_key
import finance_privacy
from pathlib import Path
import json

PHONE = "+60111111111"
DM = "60111111111@s.whatsapp.net"


class V0538ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.AlexCoreTests.setUpClass()

    def setUp(self):
        core.AlexCoreTests.setUp(self)

    def claim(self, mid, text="fixture"):
        return core.AlexCoreTests.claim(self, mid, PHONE, text)

    def actor(self, mid):
        return core.AlexCoreTests.actor(self, mid, PHONE)

    def test_cached_export_refuses_visibility_change_and_repeat_exports_keep_records(self):
        self.claim('export-expense', 'I spent 56.90 in Starbucks')
        owner = with_action_key(self.actor('export-expense'), 'export-expense')
        event = services.log_expense(owner, 'Export fixture coffee', 56.90, 'food')['event_id']
        self.claim('query-finance', 'Show shared expenses')
        reader = self.actor('query-finance')
        mcp_server.query_finances(actor=reader, scope='family')
        exports = [mcp_server.report_export(actor=reader, format='json') for _ in range(2)]
        for exported in exports:
            data = json.loads(Path(exported['_attachments'][0]['path']).read_text())
            self.assertEqual(data['ledger']['records'][0]['event_id'], event)
            self.assertEqual(exported['record_count'], 1)
        db.queue_outbound(DM, 'TEXT', text='Recorded', source_message_id='export-expense',
                          context_kind='FINANCIAL_EVENT', context_id=event)
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET delivery_status='SENT',provider_message_id='expense-quote' WHERE source_message_id='export-expense'")
        self.claim('move-private', 'Actually save that to my private')
        with db.connect() as conn:
            conn.execute("UPDATE inbound_messages SET quoted_message_id='expense-quote' WHERE message_id='move-private'")
        mover = replace(self.actor('move-private'), trusted_text='Actually save that to my private')
        finance_privacy.move_quoted_expense(mover)
        with self.assertRaisesRegex(ValueError, 'Financial visibility changed'):
            mcp_server.report_export(actor=reader, format='json')
        mcp_server.query_finances(actor=reader, scope='family')
        fresh = mcp_server.report_export(actor=reader, format='json')
        data = json.loads(Path(fresh['_attachments'][0]['path']).read_text())
        self.assertEqual(data['ledger']['records'], [])
        self.assertEqual(fresh['record_count'], 0)

    def test_reminder_pin_is_not_blocked_by_hourglass_failure(self):
        self.claim("v0538-rem-pin", "remind me to test pin")
        actor = with_action_key(self.actor("v0538-rem-pin"), "v0538-rem-pin-action")
        reminder = services.create_reminder(
            actor, "test pin", "2026-10-09T10:00:00+08:00"
        )
        oid = db.queue_outbound(
            DM, "TEXT", text="Reminder set: test pin.",
            source_message_id=actor.source_message_id,
            context_kind="REMINDER_CREATED",
            context_id=reminder["reminder_id"],
        )
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE outbound_messages
                   SET delivery_status='SENT',provider_message_id='WA-V0538-PIN',
                       delivered_at_utc=CURRENT_TIMESTAMP
                   WHERE outbound_id=?""",
                (oid,),
            )
            conn.commit()
            row = outbox._joined_row(conn, oid)

            calls = []
            def controls(_conn, _row, kind, emoji=None, outbound=False):
                calls.append((kind, emoji, outbound))
                return kind == "pin"

            with patch.object(outbox, "_attempt_control", side_effect=controls):
                outbox._ensure_claim_setup_markers(conn, row)

            state = conn.execute(
                """SELECT job_pinned_at_utc,job_reacted_at_utc,job_pin_target
                   FROM outbound_messages WHERE outbound_id=?""",
                (oid,),
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(calls)
        self.assertEqual(calls[0][0], "pin")
        self.assertIsNotNone(state["job_pinned_at_utc"])
        self.assertEqual(state["job_pin_target"], "OUTBOUND")
        self.assertIsNone(state["job_reacted_at_utc"])

    def test_acknowledgement_does_not_resolve_or_remove_reminder(self):
        self.claim("v0538-ack-create", "remind me")
        creator = with_action_key(
            self.actor("v0538-ack-create"), "v0538-ack-action"
        )
        reminder = services.create_reminder(
            creator, "keep pinned", "2026-10-09T10:00:00+08:00"
        )
        self.claim("v0538-ack-turn", "acknowledge keep pinned")
        result = services.update_reminder(
            self.actor("v0538-ack-turn"),
            reminder["reminder_id"],
            status="ack",
        )
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status,acknowledged_at_utc FROM reminders WHERE reminder_id=?",
                (reminder["reminder_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(result["status"], "seen")
        self.assertEqual(row["status"], "OPEN")
        self.assertIsNotNone(row["acknowledged_at_utc"])

    def test_finance_pdf_falls_back_and_is_structurally_valid(self):
        self.claim("v0538-pdf", "Generate my September 2026 finance report as PDF")
        actor = self.actor("v0538-pdf")
        services.log_expense(
            with_action_key(actor, "v0538-pdf-expense"),
            "PDF reliability meal", 12.34, "food", currency="MYR",
            event_date_local="2026-09-10T10:00:00+08:00",
        )

        with patch.object(
            phase2_reports, "premium_finance_pdf",
            side_effect=RuntimeError("simulated premium renderer edge case"),
        ):
            exported = mcp_server.report_export(
                "pdf", actor, period="2026-09", report_type="finance"
            )

        self.assertEqual(exported["status"], "ready")
        self.assertEqual(exported["pdf_renderer"], "fallback_text")
        path = exported["_attachments"][0]["path"]
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.getsize(path), exported["byte_size"])
        self.assertGreater(exported["byte_size"], 350)
        with open(path, "rb") as handle:
            payload = handle.read()
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertIn(b"/Type /Catalog", payload)
        self.assertIn(b"xref", payload)
        self.assertIn(b"trailer", payload)
        self.assertIn(b"%%EOF", payload[-256:])
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_normal_finance_pdf_passes_validation(self):
        report = {
            "period": "2026-09",
            "ledger": {
                "records": [{
                    "date_local": "2026-09-10",
                    "category": "food",
                    "scope": "family",
                    "description": "Lunch",
                    "currency": "MYR",
                    "amount": 12.34,
                }],
                "spending_totals": {"MYR": 12.34},
                "income_totals": {},
                "net_outflow": {"MYR": 12.34},
                "count": 1,
            },
            "category_totals": [{
                "category": "food", "label": "Food & Drink",
                "currency": "MYR", "amount": 12.34, "count": 1,
            }],
            "obligations": [],
            "goals": [],
            "display": {"title": "September 2026 Finance Report"},
        }
        payload, renderer = phase2_reports.render_finance_pdf(report)
        self.assertEqual(renderer, "premium")
        self.assertGreater(len(payload), 350)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", payload[-256:])


if __name__ == "__main__":
    unittest.main()
