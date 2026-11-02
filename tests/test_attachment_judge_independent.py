"""Mutation probes for the certification judge's file assertions."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp' / 'app'))
import behavior_cert


class AttachmentJudgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='alex-file-judge-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'original.bin'
        self.path.write_bytes(b'synthetic original bytes')
        fixtures = {'vinyl_path': str(self.path), 'original_attachment_sha256': {
            str(self.path.resolve()): hashlib.sha256(self.path.read_bytes()).hexdigest()}}
        active = patch.object(behavior_cert, 'CERT_FIXTURES', fixtures)
        active.start()
        self.addCleanup(active.stop)
        self.contract = SimpleNamespace(expect_attachment=True, expect_attachment_of='vinyl')

    def check(self, kind='IMAGE'):
        return behavior_cert._attachment_identity_problems(self.contract,
            [{'kind': kind, 'local_path': str(self.path)}])

    def test_original_exact_bytes_pass(self):
        self.assertEqual(self.check(), [])

    def test_same_path_wrong_bytes_fails(self):
        self.path.write_bytes(b'different file at the correct path')
        self.assertTrue(self.check())

    def test_same_path_missing_file_fails(self):
        self.path.unlink()
        self.assertTrue(self.check())

    def test_empty_generated_attachment_fails(self):
        self.contract.expect_attachment_of = None
        self.path.write_bytes(b'')
        self.assertTrue(self.check('DOCUMENT'))

    def test_missing_frozen_digest_is_not_success(self):
        behavior_cert.CERT_FIXTURES['original_attachment_sha256'].clear()
        self.assertTrue(self.check())

    def test_audio_and_video_are_checked_as_attachments(self):
        for kind in ['AUDIO', 'VIDEO']:
            with self.subTest(kind=kind):
                self.assertEqual(self.check(kind), [])
        self.path.write_bytes(b'corrupt')
        self.assertTrue(self.check('AUDIO'))
        self.assertTrue(self.check('VIDEO'))


if __name__ == '__main__':
    unittest.main()
