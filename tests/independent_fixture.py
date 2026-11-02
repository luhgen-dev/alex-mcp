"""Isolated real SQLite fixture, without importing test_core or provider SDKs.

This exercises the shipped schema, not upgrades/migrations or ingress.
"""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp' / 'app'))
import db
import scope_policy


class HouseholdFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='alex-independent-')
        self.addCleanup(self.directory.cleanup)
        for name, value in [('DATA_DIR', self.directory.name),
                            ('DB_PATH', str(Path(self.directory.name) / 'test.db'))]:
            active = patch.object(db, name, value)
            active.start()
            self.addCleanup(active.stop)
        with db.connect() as conn:
            conn.executescript(Path(db.SCHEMA_PATH).read_text())
            conn.executemany('INSERT INTO users VALUES(?,?)',
                             [('USR_HUSBAND', 'Synthetic A'), ('USR_WIFE', 'Synthetic B')])
            conn.executemany('INSERT INTO spaces VALUES(?,?)',
                             [('FAMILY_SHARED', 'Shared'), ('HUSBAND_PVT', 'A'), ('WIFE_PVT', 'B')])
            conn.executemany('INSERT INTO memberships VALUES(?,?)',
                             [('USR_HUSBAND', 'FAMILY_SHARED'), ('USR_HUSBAND', 'HUSBAND_PVT'),
                              ('USR_WIFE', 'FAMILY_SHARED'), ('USR_WIFE', 'WIFE_PVT')])
            conn.executemany('INSERT INTO user_phone_history(history_id,user_id,phone_number) VALUES(?,?,?)',
                             [('a', 'USR_HUSBAND', '+60111111111'), ('b', 'USR_WIFE', '+60222222222')])

    def actor(self, key, text='Spent RM10 on lunch', *, wife=False, group=False):
        phone = '+60222222222' if wife else '+60111111111'
        chat = 'synthetic@g.us' if group else phone[1:] + '@s.whatsapp.net'
        kind = 'GROUP' if group else 'DIRECT_DM'
        db.claim_inbound(dict(message_id=key, conversation_id=chat,
                              conversation_type=kind, sender_phone=phone, text=text))
        return replace(db.resolve_actor(phone, chat, kind, key), action_key=key,
                       trusted_text=text, intent_text=text,
                       read_scope=scope_policy.resolve_read_scope(text),
                       received_at_utc='2026-11-02T13:50:00+00:00')
