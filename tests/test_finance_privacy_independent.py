"""Quoted visibility changes with real SQLite and unchanged archived bytes."""
from dataclasses import replace
from pathlib import Path
import hashlib
import json

import independent_fixture
import db
import finance_privacy
import services
import outbox
import phase2_reports
from unittest.mock import patch
import select
import subprocess
import sys


class ExpensePrivacyTests(independent_fixture.HouseholdFixture):
    def test_finance_read_keeps_one_snapshot_during_a_visibility_commit(self):
        self.expense()
        reader = self.actor('reader', 'Show shared finances', wife=True)
        mover = self.mover()
        connections = []
        moved = []

        class InterleavedConnection:
            def __init__(self):
                self.conn = db.connect()
                connections.append(self.conn)

            def execute(self, sql, *args):
                if 'SELECT event_type,currency' in sql and not moved:
                    # A real second SQLite connection commits the move after
                    # version selection, but before aggregate/record reads.
                    moved.append(finance_privacy.move_quoted_expense(mover))
                return self.conn.execute(sql, *args)

            def close(self):
                self.conn.close()

        with patch.object(services, 'connect', InterleavedConnection):
            old = services.query_finances(reader)
        self.assertEqual(moved[0]['status'], 'moved')
        self.assertEqual(old['_finance_privacy_version'], 0)
        self.assertEqual(old['count'], 1)
        self.assertEqual(old['spending_totals'], {'MYR': 56.90})
        self.assertEqual(len(old['records']), 1)
        fresh = services.query_finances(reader)
        self.assertEqual(fresh['_finance_privacy_version'], 1)
        self.assertEqual(fresh['count'], 0)

    def test_cached_finance_payload_is_removed_after_move_and_fresh_read_is_scoped(self):
        self.expense()
        reader = self.actor('reader', 'Show shared finances', wife=True)
        ledger = services.query_finances(reader)
        self.assertEqual(ledger['count'], 1)
        self.assertEqual(ledger['_finance_privacy_version'], 0)
        for kind, payload in [('finance_query', ledger), ('monthly_finance', {'ledger': ledger})]:
            with self.subTest(kind=kind):
                phase2_reports.remember_active_report(reader.user_id, kind, kind, payload, spec={'scope': 'family'})
                self.assertEqual(phase2_reports.load_active_report(reader.user_id, kind)['payload'], payload)
        finance_privacy.move_quoted_expense(self.mover())
        for kind in ('finance_query', 'monthly_finance'):
            cached = phase2_reports.load_active_report(reader.user_id, kind)
            self.assertTrue(cached['_privacy_stale'])
            self.assertIsNone(cached['payload'])
            self.assertEqual(cached['spec'], {'scope': 'family'})
        fresh = services.query_finances(reader)
        self.assertEqual(fresh['count'], 0)
        self.assertEqual(fresh['records'], [])
        self.assertEqual(fresh['_finance_privacy_version'], 1)
        phase2_reports.remember_active_report(reader.user_id, reader.conversation_id, 'finance_query', fresh)
        self.assertEqual(phase2_reports.load_active_report(reader.user_id, reader.conversation_id)['payload'], fresh)

    def test_old_unversioned_finance_cache_invalidates_but_other_report_survives(self):
        self.expense()
        reader = self.actor('reader', wife=True)
        for kind in ('finance_query', 'snapshot'):
            phase2_reports.remember_active_report(reader.user_id, kind, kind, {'count': 1})
        finance_privacy.move_quoted_expense(self.mover())
        self.assertIsNone(phase2_reports.load_active_report(reader.user_id, 'finance_query')['payload'])
        self.assertEqual(phase2_reports.load_active_report(reader.user_id, 'snapshot')['payload'], {'count': 1})

    def test_report_read_before_move_but_audited_after_move_is_blocked(self):
        self.expense()
        reader = self.actor('reader', 'Show shared finances', wife=True)
        old = services.query_finances(reader)
        finance_privacy.move_quoted_expense(self.mover())
        base = dict(source_message_id='reader', conversation_id=reader.conversation_id,
                    kind='TEXT', text_body='Old totals', context_kind=None, context_id=None, local_path=None)
        for name, payload in [('query_finances', old), ('finance_report', {'ledger': old}),
                              ('report_export', {'source_report_kind': 'finance_query', '_finance_privacy_version': 0})]:
            with self.subTest(tool=name), db.connect() as conn:
                conn.execute('DELETE FROM tool_audit')
                conn.execute('''INSERT INTO tool_audit(audit_id,action_key,source_message_id,user_id,tool_name,
                    arguments_json,result_json,status,created_at_utc) VALUES('read','read','reader',?,?,'{}',?,'OK','2099-01-01')''',
                    (reader.user_id, name, finance_privacy.audit_result_json(payload)))
                with self.assertRaises(finance_privacy.PrivacyDeliveryError):
                    finance_privacy.validate_outbound(conn, base)
                # A read of the current generation remains deliverable, even
                # with the same second timestamp as the visibility change.
                conn.execute("UPDATE tool_audit SET result_json=?,created_at_utc='2000-01-01'",
                             (json.dumps({'_finance_privacy_version': 1}),))
                finance_privacy.validate_outbound(conn, base)

    def test_large_audit_keeps_valid_json_and_privacy_generation(self):
        for payload in ({'_finance_privacy_version': 12, 'records': ['\\\"தமிழ்'] * 20000},
                        {'ledger': {'_finance_privacy_version': 13, 'records': ['x'] * 20000}}):
            encoded = finance_privacy.audit_result_json(payload)
            self.assertLessEqual(len(encoded), 30000)
            decoded = json.loads(encoded)
            self.assertTrue(decoded['_audit_truncated'])
            self.assertEqual(finance_privacy.snapshot_version(decoded), finance_privacy.snapshot_version(payload))

    def test_visibility_guard_blocks_a_separate_delivery_process(self):
        code = '''import sys
sys.path.insert(0, sys.argv[1])
import db, finance_privacy
db.DB_PATH = sys.argv[2]
print('ready', flush=True)
with finance_privacy.delivery_guard():
    print('acquired', flush=True)
'''
        app = str(Path(__file__).resolve().parents[1] / 'alex-mcp/app')
        with finance_privacy.delivery_guard():
            process = subprocess.Popen([sys.executable, '-c', code, app, db.DB_PATH], stdout=subprocess.PIPE, text=True)
            self.addCleanup(lambda: process.poll() is None and process.kill())
            self.addCleanup(process.stdout.close)
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertFalse(select.select([process.stdout], [], [], 0.1)[0])
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        self.assertEqual(process.stdout.readline().strip(), 'acquired')
        self.assertEqual(process.wait(timeout=5), 0)

    def expense(self, key='expense', **actor_options):
        actor = self.actor(key, 'I spent 56.90 in Starbucks', **actor_options)
        result = services.log_expense(actor, 'Synthetic coffee', 56.90, 'food')
        with db.connect() as conn:
            conn.execute('''INSERT INTO outbound_messages(outbound_id,conversation_id,kind,source_message_id,
                text_body,delivery_status,provider_message_id) VALUES(?,?,'TEXT',?,'Recorded','SENT',?)''',
                (key+'-out', actor.conversation_id, key, key+'-provider'))
        return actor, result['event_id']

    def mover(self, key='move', quote='expense-provider', **options):
        actor = self.actor(key, 'Actually save that to my private', **options)
        with db.connect() as conn:
            conn.execute('UPDATE inbound_messages SET quoted_message_id=? WHERE message_id=?', (quote, key))
        return actor

    def stored(self, event):
        with db.connect() as conn:
            return dict(conn.execute('SELECT * FROM financial_events WHERE event_id=?', (event,)).fetchone())

    def media(self, actor, event):
        path = Path(self.directory.name) / 'original.jpg'
        path.write_bytes(b'synthetic original receipt bytes')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with db.connect() as conn:
            conn.execute('''INSERT INTO media_objects(media_id,source_message_id,media_type,mime_type,sha256,local_path)
                VALUES('receipt',?,'IMAGE','image/jpeg',?,?)''', (actor.source_message_id, digest, str(path)))
            conn.execute("INSERT INTO event_media_links VALUES(?,'receipt')", (event,))
        return path, digest

    def test_exact_quote_moves_only_that_expense_and_retry_is_idempotent(self):
        _, event = self.expense()
        _, other = self.expense('other')
        before = self.stored(event)
        actor = self.mover()
        self.assertEqual(actor.read_scope, 'private')
        result = finance_privacy.move_quoted_expense(actor)
        self.assertEqual(result['event_id'], event)
        self.assertEqual(result['status'], 'moved')
        self.assertEqual(self.stored(event), {**before, 'space_id': actor.private_space})
        self.assertEqual(self.stored(other)['space_id'], 'FAMILY_SHARED')
        self.assertEqual(finance_privacy.move_quoted_expense(actor)['status'], 'already_private')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_visibility_changes').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 2)

    def test_correction_chain_and_receipt_become_private_without_changing_bytes(self):
        owner, original = self.expense()
        path, digest = self.media(owner, original)
        corrected = services.correct_expense(self.actor('correct', 'Change amount to 59.60'), original, amount=59.60)['event_id']
        # A durable individual confirmation may identify an older chain member.
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET context_kind='FINANCIAL_EVENT',context_id=? WHERE outbound_id='expense-out'", (original,))
        actor = self.mover()
        result = finance_privacy.move_quoted_expense(actor)
        self.assertEqual(result['event_id'], corrected)
        self.assertEqual(self.stored(original)['space_id'], actor.private_space)
        self.assertEqual(self.stored(corrected)['space_id'], actor.private_space)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(services.get_receipt(actor, 'receipt')['status'], 'found')
        for reader in (self.actor('spouse', 'Show receipts', wife=True), self.actor('group', 'Show receipts', group=True)):
            with self.subTest(reader=reader.user_id), self.assertRaises(PermissionError):
                services.get_receipt(reader, 'receipt')
            with self.assertRaises(PermissionError):
                services.get_media_original(reader, 'receipt')

    def test_other_owner_and_foreign_conversation_quotes_are_denied(self):
        _, event = self.expense(wife=True)
        actor = self.mover()
        with self.assertRaises(ValueError):
            finance_privacy.move_quoted_expense(actor)
        # Even a same-chat forwarded durable context is no ownership grant.
        with db.connect() as conn:
            conn.execute("UPDATE outbound_messages SET conversation_id=?,context_kind='FINANCIAL_EVENT',context_id=? WHERE outbound_id='expense-out'",
                         (actor.conversation_id, event))
        with self.assertRaises(PermissionError):
            finance_privacy.move_quoted_expense(actor)
        self.assertEqual(self.stored(event)['space_id'], 'FAMILY_SHARED')

    def test_negation_and_group_do_not_write(self):
        _, event = self.expense()
        actor = self.mover()
        for text in ("Don't save that to private", 'How do I move that to private?', 'If I move it to private', 'Show my private expenses'):
            with self.subTest(text=text), self.assertRaises(PermissionError):
                finance_privacy.move_quoted_expense(replace(actor, trusted_text=text))
        with self.assertRaises(PermissionError):
            finance_privacy.move_quoted_expense(self.mover('group-move', group=True))
        self.assertEqual(self.stored(event)['space_id'], 'FAMILY_SHARED')

    def test_old_shared_quote_does_not_reveal_updated_private_metadata(self):
        _, event = self.expense()
        viewer = self.actor('viewer', 'Show details', wife=True)
        with db.connect() as conn:
            conn.execute('''INSERT INTO outbound_messages(outbound_id,conversation_id,kind,source_message_id,
                text_body,delivery_status,provider_message_id) VALUES('old-copy',?,'TEXT','expense',
                'Old already delivered summary','SENT','old-provider')''', (viewer.conversation_id,))
        finance_privacy.move_quoted_expense(self.mover())
        quote = db.resolve_quoted_context(viewer.conversation_id, 'old-provider', viewer.phone)
        self.assertNotIn('financial_event', quote)
        self.assertEqual(quote['quoted_alex_text'], 'Old already delivered summary')

    def test_shared_receipt_dependency_prevents_partial_move(self):
        owner, event = self.expense()
        self.media(owner, event)
        _, other = self.expense('other')
        with db.connect() as conn:
            conn.execute("INSERT INTO event_media_links VALUES(?,'receipt')", (other,))
        with self.assertRaisesRegex(ValueError, 'also linked'):
            finance_privacy.move_quoted_expense(self.mover())
        self.assertEqual(self.stored(event)['space_id'], 'FAMILY_SHARED')
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_visibility_changes').fetchone()[0], 0)

    def test_queued_receipt_and_old_financial_reply_cannot_leak_after_move(self):
        owner, event = self.expense()
        path, _ = self.media(owner, event)
        reader = self.actor('reader', 'Show finances', wife=True)
        with db.connect() as conn:
            conn.execute('''INSERT INTO tool_audit(audit_id,action_key,source_message_id,user_id,tool_name,
                arguments_json,result_json,status,created_at_utc) VALUES('read','read','reader',?,
                'query_finances','{}','{}','OK','2000-01-01 00:00:00')''', (reader.user_id,))
        finance_privacy.move_quoted_expense(self.mover())
        base = dict(outbound_id='queued', source_message_id='reader', conversation_id=reader.conversation_id,
                    kind='TEXT', text_body='Old shared summary', context_kind=None, context_id=None, local_path=None)
        with self.assertRaises(finance_privacy.PrivacyDeliveryError):
            outbox._payload(base)
        with self.assertRaises(finance_privacy.PrivacyDeliveryError):
            outbox._payload({**base, 'source_message_id': None, 'kind': 'IMAGE', 'local_path': str(path)})
        # An unrelated non-financial response is unaffected.
        self.assertEqual(outbox._payload({**base, 'source_message_id': None})['text'], 'Old shared summary')
        self.assertEqual(outbox._payload({**base, 'conversation_id': owner.conversation_id})['to'], owner.conversation_id)

    def test_outbox_marks_blocked_delivery_failed_then_sends_one_safe_notice(self):
        owner, event = self.expense()
        path, _ = self.media(owner, event)
        reader = self.actor('reader', 'Send receipt', wife=True)
        db.queue_outbound(reader.conversation_id, 'IMAGE', local_path=str(path),
                          source_message_id='reader', mime_type='image/jpeg')
        finance_privacy.move_quoted_expense(self.mover())
        with patch.object(outbox, '_send', return_value=(True, '{"message_id":"synthetic-delivery"}')) as send:
            outbox.sweep()
            send.assert_not_called()
            outbox.sweep()
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0]['text'], finance_privacy.EXPIRED_NOTICE)
        with db.connect() as conn:
            rows = conn.execute("SELECT kind,delivery_status FROM outbound_messages WHERE source_message_id='reader' ORDER BY rowid").fetchall()
            self.assertEqual([tuple(r) for r in rows], [('IMAGE', 'FAILED'), ('TEXT', 'SENT')])
