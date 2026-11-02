"""Independent ledger tests: real SQLite, no provider, MCP, HA or sender.

This fixture deliberately imports no test_core helpers. Assertions read the
authoritative rows, so a plausible reply or tool status cannot make them pass.
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp' / 'app'))
import db
import services
import scope_policy
import outbox


import independent_fixture


class IndependentLedgerTests(independent_fixture.HouseholdFixture):
    def row(self, event_id):
        with db.connect() as conn:
            return dict(conn.execute('SELECT * FROM financial_events WHERE event_id=?',
                                     (event_id,)).fetchone())

    def receipt(self, *, private=False):
        actor = self.actor('receipt-source', 'Log private lunch RM10' if private else 'Log lunch RM10')
        raw = b'%PDF-1.4\nSynthetic original receipt bytes\n%%EOF'
        path = Path(self.directory.name) / 'original.pdf'
        path.write_bytes(raw)
        with db.connect() as conn:
            conn.execute('''INSERT INTO media_objects
                (media_id,source_message_id,media_type,mime_type,sha256,local_path)
                VALUES(?,?,'PDF','application/pdf',?,?)''',
                ('receipt', actor.source_message_id, hashlib.sha256(raw).hexdigest(), str(path)))
        event = services.log_expense(replace(actor, media_ids=('receipt',)), 'Synthetic lunch', 10, 'food')
        return actor, event['event_id'], path, raw

    def test_default_write_is_shared_and_private_is_owner_only(self):
        shared = services.log_expense(self.actor('shared'), 'Synthetic lunch', 10, 'food')
        private = services.log_expense(self.actor('private', 'Log private lunch RM12'),
                                       'Synthetic private lunch', 12, 'food')
        self.assertEqual(self.row(shared['event_id'])['space_id'], 'FAMILY_SHARED')
        self.assertEqual(self.row(private['event_id'])['space_id'], 'HUSBAND_PVT')
        for group, wife in [(False, False), (False, True), (True, False)]:
            reader = self.actor(f'read-{group}-{wife}', 'Show spending', group=group, wife=wife)
            records = services.query_finances(reader)['records']
            self.assertEqual([x['event_id'] for x in records], [shared['event_id']])
        owner = self.actor('private-read', 'Show my private spending')
        self.assertEqual([x['event_id'] for x in services.query_finances(owner)['records']],
                         [private['event_id']])

    def test_private_destination_does_not_authorize_shared_source_read(self):
        event = services.log_expense(self.actor('source'), 'Synthetic coffee', 56.90, 'food')
        correction = self.actor('move', 'Actually save that to my private')
        self.assertEqual(services.query_finances(correction)['count'], 0)
        self.assertEqual(self.row(event['event_id'])['amount_minor'], 5690)
        self.assertEqual(self.row(event['event_id'])['space_id'], 'FAMILY_SHARED')

    def test_retry_has_one_exact_row(self):
        actor = self.actor('retry')
        first = services.log_expense(actor, 'Synthetic coffee', 59.60, 'food')
        again = services.log_expense(actor, 'Synthetic coffee', 59.60, 'food')
        self.assertEqual(first['event_id'], again['event_id'])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 1)
        self.assertEqual(self.row(first['event_id'])['amount_minor'], 5960)

    def test_correction_preserves_original_and_exact_total(self):
        old = services.log_expense(self.actor('original'), 'Synthetic lunch', 10, 'food')
        changed = services.correct_expense(self.actor('correction', 'Actually RM12'), old['event_id'], amount=12)
        self.assertEqual(self.row(old['event_id'])['status'], 'SUPERSEDED')
        self.assertEqual(self.row(old['event_id'])['amount_minor'], 1000)
        self.assertEqual(self.row(changed['event_id'])['amount_minor'], 1200)
        report = services.query_finances(self.actor('read'))
        self.assertEqual(report['count'], 1)
        self.assertEqual(report['spending_totals']['MYR'], 12)

    def test_invalid_confirmation_category_cannot_activate_pending_record(self):
        for category in ['private', 'general', '   ']:
            with self.subTest(category=category):
                pending = services.log_expense(self.actor('pending-' + category), 'Unclear purchase', 10)
                result = services.confirm_expense(self.actor('confirm-' + category), pending['event_id'], category=category)
                self.assertEqual(result['status'], 'still_needs_information')
                self.assertEqual(self.row(pending['event_id'])['status'], 'PENDING_HUMAN_REVIEW')

    def test_confirmation_normalizes_category(self):
        pending = services.log_expense(self.actor('pending'), 'Unclear purchase', 10)
        services.confirm_expense(self.actor('confirm'), pending['event_id'], category='food_drink')
        self.assertEqual(self.row(pending['event_id'])['category'], 'food')

    def test_correction_cannot_bypass_pending_confirmation(self):
        pending = services.log_expense(self.actor('pending'), 'Unclear purchase', None)
        with self.assertRaises(ValueError):
            services.correct_expense(self.actor('correct', 'Rename this expense'),
                                     pending['event_id'], description='Renamed purchase')
        self.assertEqual(self.row(pending['event_id'])['status'], 'PENDING_HUMAN_REVIEW')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 1)

    def test_correction_rejects_scope_as_category(self):
        original = services.log_expense(self.actor('original'), 'Synthetic lunch', 10, 'food')
        with self.assertRaises(ValueError):
            services.correct_expense(self.actor('correct', 'Change category'), original['event_id'], category='private')
        self.assertEqual(self.row(original['event_id'])['category'], 'food')
        self.assertEqual(self.row(original['event_id'])['status'], 'ACTIVE')

    def test_unsupported_currency_and_nonfinite_amount_leave_no_rows(self):
        result = services.log_expense(self.actor('currency'), 'Synthetic lunch', 10, 'food', 'USD')
        self.assertEqual(result['status'], 'clarification_required')
        for i, amount in enumerate([float('nan'), float('inf'), -1, 0]):
            with self.assertRaises(ValueError):
                services.log_expense(self.actor(f'amount-{i}'), 'Synthetic lunch', amount, 'food')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)

    def test_receipt_apis_return_exact_original_bytes(self):
        actor, event, path, raw = self.receipt()
        for result in [services.get_receipt(actor, 'receipt'),
                       services.get_receipt_for_event(actor, event),
                       services.get_media_original(actor, 'receipt')]:
            self.assertEqual(Path(result['_attachments'][0]['path']).read_bytes(), raw)
            self.assertEqual(result['_attachments'][0]['mime_type'], 'application/pdf')

    def test_receipt_apis_reject_corrupt_or_missing_original(self):
        actor, event, path, raw = self.receipt()
        calls = [lambda: services.get_receipt(actor, 'receipt'),
                 lambda: services.get_receipt_for_event(actor, event),
                 lambda: services.get_media_original(actor, 'receipt')]
        for damaged in ['changed', 'missing']:
            path.write_bytes(b'wrong file') if damaged == 'changed' else path.unlink()
            for i, call in enumerate(calls):
                with self.subTest(damaged=damaged, api=i), self.assertRaises(ValueError):
                    call()
        self.assertEqual(self.row(event)['status'], 'ACTIVE')

    def test_private_receipt_is_not_available_to_spouse_or_group(self):
        actor, event, path, raw = self.receipt(private=True)
        for wife, group in [(True, False), (False, True)]:
            reader = self.actor(f'forbidden-{wife}-{group}', wife=wife, group=group)
            with self.assertRaises(PermissionError):
                services.get_receipt(reader, 'receipt')
            with self.assertRaises(PermissionError):
                services.get_receipt_for_event(reader, event)
            with self.assertRaises(PermissionError):
                services.get_media_original(reader, 'receipt')

    def test_egress_rechecks_original_bytes_after_retrieval(self):
        actor, event, path, raw = self.receipt()
        attachment = services.get_receipt_for_event(actor, event)['_attachments'][0]
        row = dict(kind='DOCUMENT', outbound_id='synthetic-outbound',
                   conversation_id=actor.conversation_id, context_kind=None,
                   local_path=attachment['path'], mime_type=attachment['mime_type'],
                   text_body='Synthetic receipt', filename='receipt.pdf')
        payload = outbox._payload(row)
        import base64
        self.assertEqual(base64.b64decode(payload['file_b64']), raw)
        path.write_bytes(b'changed after retrieval')
        with self.assertRaises(ValueError):
            outbox._payload(row)


if __name__ == '__main__':
    unittest.main()
