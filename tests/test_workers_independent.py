"""Real queue/state transitions with network adapters replaced at the boundary.

These tests prove local worker behavior, not catalogue accuracy or phone delivery.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import unittest

import independent_fixture
import db
import grocery_prices
import services
import outbox


class WorkerStateTests(independent_fixture.HouseholdFixture):
    def setUp(self):
        super().setUp()
        with db.connect() as conn:
            conn.executescript(grocery_prices.SCHEMA)
        active = patch.object(outbox, '_STARTUP_MANAGED_JOB_RECONCILED', False)
        active.start()
        self.addCleanup(active.stop)

    def item(self):
        return services.add_shopping_item(self.actor('add', 'Add milk'), 'Synthetic milk', '2')['item_id']

    def job(self, item):
        with db.connect() as conn:
            row = conn.execute('SELECT * FROM grocery_price_jobs WHERE item_id=?', (item,)).fetchone()
            return dict(row) if row else None

    def run_price_job(self, lookup):
        with patch.object(grocery_prices, 'get_settings', return_value=SimpleNamespace(grocery_background_prices=True)), \
                patch.object(grocery_prices, 'lookup', side_effect=lookup):
            return grocery_prices.process_one()

    def test_grocery_add_retry_does_not_duplicate_item_job_or_history(self):
        item = self.item()
        services.add_shopping_item(self.actor('retry', 'Add milk'), 'Synthetic milk', '2')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM shopping_items').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grocery_price_jobs').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grocery_item_history').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)
        self.assertEqual(self.job(item)['state'], 'QUEUED')

    def test_grocery_completion_after_rename_is_discarded(self):
        item = self.item()
        def slow_lookup(row, choice):
            services.update_shopping_item(self.actor('rename', 'Change milk to bread'), item, item='Synthetic bread')
            return {'status': 'available', 'synthetic': 'old milk result'}
        self.assertTrue(self.run_price_job(slow_lookup))
        self.assertEqual(self.job(item)['state'], 'QUEUED')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grocery_price_quotes').fetchone()[0], 0)

    def test_grocery_completion_after_purchase_is_discarded(self):
        item = self.item()
        def slow_lookup(row, choice):
            services.update_shopping_item(self.actor('buy', 'Mark milk purchased'), item, status='purchased')
            return {'status': 'available'}
        self.run_price_job(slow_lookup)
        self.assertIsNone(self.job(item))
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grocery_price_quotes').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)

    def test_grocery_network_failure_has_bounded_retries_and_no_exception_body(self):
        item = self.item()
        for attempt in range(1, 4):
            self.run_price_job(lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic-secret-in-error')))
            job = self.job(item)
            self.assertEqual(job['attempts'], attempt)
            self.assertEqual(job['state'], 'QUEUED' if attempt < 3 else 'DONE')
            with db.connect() as conn:
                for row in conn.execute('SELECT payload_json FROM grocery_price_quotes'):
                    self.assertNotIn('synthetic-secret-in-error', row[0])
                conn.execute("UPDATE grocery_price_jobs SET next_try='2000-01-01T00:00:00+00:00'")

    def queue(self, kind='TEXT', path=None):
        with db.connect() as conn:
            conn.execute('''INSERT INTO outbound_messages(outbound_id,conversation_id,kind,text_body,local_path)
                            VALUES('outbound','synthetic@s.whatsapp.net',?,'Synthetic message',?)''', (kind, path))

    def outbound(self):
        with db.connect() as conn:
            return dict(conn.execute("SELECT * FROM outbound_messages WHERE outbound_id='outbound'").fetchone())

    def test_outbox_transport_retry_preserves_message_identity(self):
        self.queue()
        with patch.object(outbox, '_send', return_value=(False, 'synthetic transport failure')) as send:
            outbox.sweep()
            first_payload = send.call_args.args[0]
        self.assertEqual(self.outbound()['delivery_status'], 'PENDING')
        self.assertIsNone(self.outbound()['delivered_at_utc'])
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET next_attempt_at_utc=NULL")
        with patch.object(outbox, '_send', return_value=(True, '{"message_id":"synthetic-provider-id"}')) as send:
            outbox.sweep()
            self.assertEqual(send.call_args.args[0]['message_id'], first_payload['message_id'])
        self.assertEqual(self.outbound()['delivery_status'], 'SENT')
        self.assertEqual(self.outbound()['provider_message_id'], 'synthetic-provider-id')
        with patch.object(outbox, '_send') as send:
            outbox.sweep()
            send.assert_not_called()

    def test_outbox_corrupt_original_is_failed_before_transport(self):
        actor = self.actor('media')
        original = b'synthetic original'
        path = Path(self.directory.name) / 'receipt.pdf'
        path.write_bytes(b'changed bytes')
        with db.connect() as conn:
            conn.execute('''INSERT INTO media_objects(media_id,source_message_id,media_type,mime_type,sha256,local_path)
                            VALUES('media',?,'PDF','application/pdf',?,?)''',
                         (actor.source_message_id, hashlib.sha256(original).hexdigest(), str(path)))
        self.queue('DOCUMENT', str(path))
        with patch.object(outbox, '_send') as send:
            outbox.sweep()
            send.assert_not_called()
        row = self.outbound()
        self.assertEqual(row['delivery_status'], 'FAILED')
        self.assertIsNone(row['delivered_at_utc'])
        self.assertIsNone(row['next_attempt_at_utc'])
        self.assertIn('integrity', row['last_error'])
        with db.connect() as conn:
            notices = conn.execute("SELECT text_body FROM outbound_messages WHERE kind='TEXT'").fetchall()
            self.assertEqual(len(notices), 1)
            self.assertIn("couldn't deliver", notices[0][0])


if __name__ == '__main__':
    unittest.main()
