"""Probe safety tests using synthetic HA-format archives and real SQLite/WAL."""
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
import unittest

import ha_snapshot_probe as probe


class SnapshotProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.db'
        self.conn = sqlite3.connect(self.source)
        self.addCleanup(self.conn.close)
        schema = Path(__file__).resolve().parents[2] / 'alex-mcp/app/schema.sql'
        self.conn.executescript(schema.read_text())
        self.conn.execute("INSERT INTO users VALUES('u','Synthetic')")
        self.conn.execute("INSERT INTO spaces VALUES('FAMILY_SHARED','Synthetic')")
        self.conn.execute("""INSERT INTO inbound_messages(message_id,conversation_id,sender_phone,raw_text,received_at_utc)
            VALUES('text','private-chat','+60111111111','I spent 56.90 in starbucks','2026-11-02T13:50:00+00:00')""")
        self.conn.execute("""INSERT INTO financial_events(event_id,action_key,source_message_id,space_id,
            created_by,owner_id,event_type,category,amount_minor,event_date_utc,timezone_name,description)
            VALUES('expense','action','text','FAMILY_SHARED','u','u','Expense','food',5690,
            '2026-11-02T13:50:00+00:00','Asia/Kuala_Lumpur','Starbucks')""")
        self.conn.execute("""INSERT INTO inbound_messages(message_id,conversation_id,sender_phone,received_at_utc)
            VALUES('voice','private-chat','+60111111111','2026-11-02T13:49:00+00:00')""")
        self.conn.execute("""INSERT INTO media_objects(media_id,source_message_id,media_type,mime_type,sha256,local_path,transcript_text)
            VALUES('audio','voice','AUDIO','audio/ogg','synthetic-hash','/data/private-audio.ogg',
            'Log my expense. synthetic-api-credential https://private.example Bearer another-secret')""")
        self.conn.execute("""INSERT INTO voice_transcription_jobs(media_id,status,duration_seconds,reserved_usd,created_at_utc)
            VALUES('audio','DONE',8,0,'2026-11-02T13:49:00+00:00')""")
        self.conn.execute("""INSERT INTO inbound_messages(message_id,conversation_id,sender_phone,raw_text,received_at_utc)
            VALUES('other','another-private-chat','+60222222222','unrelated private note','2026-11-02T13:49:00+00:00')""")
        self.conn.commit()

    @staticmethod
    def member(archive, name, content):
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

    def backup(self, extra=None, database=True, wal=None):
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode='w:gz') as archive:
            self.member(archive, 'addon.json', json.dumps({'options': {'api_key': 'synthetic-api-credential'}}).encode())
            if database:
                self.member(archive, './data/alex_mcp.db', self.source.read_bytes())
            if wal:
                self.member(archive, './data/alex_mcp.db-wal', wal.read_bytes())
            self.member(archive, 'data/wa_auth/creds.json', b'pairing-secret-never-extract')
            if extra:
                extra(archive)
        path = self.root / 'backup.tar'
        with tarfile.open(path, 'w') as outer:
            self.member(outer, probe.APP + '.tar.gz', inner.getvalue())
        return path

    def destination(self):
        path = self.root / 'extracted'
        path.mkdir()
        return path

    def test_only_database_is_extracted_and_source_is_unchanged(self):
        path, destination = self.backup(), self.destination()
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata, wal = probe.extract_database(path, destination)
        self.assertFalse(wal)
        self.assertEqual([x.name for x in destination.iterdir()], ['alex_mcp.db'])
        report = probe.inspect_database(destination / 'alex_mcp.db', probe.sensitive_values(metadata))
        self.assertTrue(report['quick_check_ok'])
        self.assertEqual(report['foreign_key_violation_count'], 0)
        self.assertEqual(report['expenses'][0]['amount_minor'], 5690)
        rendered = json.dumps(report)
        for forbidden in ['pairing-secret', 'synthetic-api-credential', 'another-secret',
                          'private.example', '+60111111111', 'unrelated private note']:
            self.assertNotIn(forbidden, rendered)
        voice = next(t for t in report['turns'] if t['voice'])
        self.assertEqual(voice['voice'][0]['seconds'], 8)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash)

    def test_copied_wal_is_replayed(self):
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA wal_autocheckpoint=0')
        self.conn.execute("UPDATE financial_events SET amount_minor=5960 WHERE event_id='expense'")
        self.conn.commit()
        wal = Path(str(self.source) + '-wal')
        self.assertTrue(wal.exists())
        destination = self.destination()
        metadata, copied = probe.extract_database(self.backup(wal=wal), destination)
        self.assertTrue(copied)
        report = probe.inspect_database(destination / 'alex_mcp.db')
        self.assertEqual(report['expenses'][0]['amount_minor'], 5960)

    def test_audit_errors_zero_counts_and_routes_are_retained_and_redacted(self):
        rows = [
            ('error', 'correct_expense', {'event_id': 'expense'},
             {'error': 'Denied synthetic-api-credential Bearer hidden-secret'}, 'ERROR'),
            ('zero', 'query_finances', {}, {'count': 0, 'status': False}, 'OK'),
            ('trace', '_turn_trace', {'exposed_tools': ['query_finances']},
             {'routes': ['self_repair:capability_denial'], 'outcome': 'answered'}, 'OK'),
        ]
        for key, name, args, result, status in rows:
            self.conn.execute('''INSERT INTO tool_audit(audit_id,action_key,source_message_id,user_id,
                tool_name,arguments_json,result_json,status) VALUES(?,?,'text','u',?,?,?,?)''',
                (key, key, name, json.dumps(args), json.dumps(result), status))
        self.conn.commit()
        report = probe.inspect_database(self.source, ['synthetic-api-credential'])
        turn = next(t for t in report['turns'] if t['source'] == probe.alias('text'))
        calls = {t['name']: t for t in turn['tools']}
        self.assertEqual(calls['correct_expense']['arguments']['event_id'], probe.alias('expense'))
        self.assertEqual(calls['correct_expense']['result']['error'], 'Denied [redacted] [redacted]')
        self.assertEqual(calls['query_finances']['result'], {'count': '0', 'status': 'False'})
        self.assertEqual(calls['_turn_trace']['routes'], ['self_repair:capability_denial'])
        self.assertNotIn('hidden-secret', json.dumps(report))

    def test_traversal_member_is_rejected(self):
        archive = self.backup(extra=lambda tar: self.member(tar, '../escaped', b'no'))
        with self.assertRaisesRegex(ValueError, 'unsafe_archive_path'):
            probe.extract_database(archive, self.destination())
        self.assertFalse((self.root / 'escaped').exists())

    def test_database_symlink_is_rejected(self):
        def link(archive):
            info = tarfile.TarInfo('data/alex_mcp.db')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_database_member'):
            probe.extract_database(self.backup(extra=link, database=False), self.destination())

    def test_missing_database_fails_without_exporting_pairing(self):
        destination = self.destination()
        with self.assertRaisesRegex(ValueError, 'database_not_found'):
            probe.extract_database(self.backup(database=False), destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_command_writes_private_filtered_report(self):
        backup = self.backup()
        output = self.root / 'reports/result.json'
        stdout = io.StringIO()
        live = {'version': '0.5.54', 'state': 'started', 'voice_notes_enabled': True}
        with patch.object(probe, 'app_info', return_value=(live, [])), \
                patch('sys.argv', ['probe', '--backup', str(backup), '--output', str(output)]), \
                redirect_stdout(stdout):
            self.assertEqual(probe.main(), 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-api-credential', output.read_text() + stdout.getvalue())
        self.assertIn('BACKUP_READABLE', stdout.getvalue())

    def test_cli_failure_does_not_print_response_or_credentials(self):
        from types import SimpleNamespace
        with patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='secret', stderr='secret')):
            with self.assertRaisesRegex(ValueError, '^ha_app_info_failed$'):
                probe.app_info()


if __name__ == '__main__':
    unittest.main()
