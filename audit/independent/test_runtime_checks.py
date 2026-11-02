"""Runner mechanics only; no package installation or household access."""
from pathlib import Path
import os
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import ha_runtime_checks as checks


class RuntimeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commit = 'a' * 40

    def test_routing_gate_never_promotes_live_required_or_failed_execution(self):
        path = self.root/'routing.json'
        path.write_text(json.dumps({'mode':'offline', 'status':'LIVE_REQUIRED', 'summary':{'discovery_dependent_checks':2}}))
        result = {'status':'FAIL', 'exit_code':1}
        self.assertIsNotNone(checks.routing_result(path, result))
        self.assertEqual(result['status'], 'LIVE_REQUIRED')
        path.write_text(json.dumps({'mode':'offline','status':'PASS','summary':{}}))
        crashed = {'status':'FAIL', 'exit_code':139}
        self.assertIsNone(checks.routing_result(path, crashed))
        self.assertEqual(crashed['status'], 'FAIL')

    def test_source_archive_accepts_only_the_pinned_root(self):
        archive = self.root/'source.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('alex-mcp-'+self.commit+'/README.md', 'synthetic source')
        checks.unpack(archive, self.root/'good', self.commit)
        self.assertEqual((self.root/'good/README.md').read_text(), 'synthetic source')
        with self.assertRaisesRegex(ValueError, 'invalid_source_archive_path'):
            checks.unpack(archive, self.root/'other', 'b'*40)

    def test_source_archive_rejects_traversal_and_symlinks(self):
        archive = self.root/'source.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('alex-mcp-'+self.commit+'/../escape', 'no')
        with self.assertRaisesRegex(ValueError, 'invalid_source_archive_path'):
            checks.unpack(archive, self.root/'bad', self.commit)
        with zipfile.ZipFile(archive, 'w') as z:
            info = zipfile.ZipInfo('alex-mcp-'+self.commit+'/link')
            info.external_attr = 0o120777 << 16
            z.writestr(info, '/config')
        with self.assertRaisesRegex(ValueError, 'source_archive_symlink'):
            checks.unpack(archive, self.root/'bad', self.commit)

    def test_failure_summary_retains_dotted_exceptions_and_skip_count(self):
        command = [sys.executable, '-c', "print(\"test_container (Fixture.test_container) ... skipped 'HA image-only binary execution'\"); print('ERROR: fixture'); print('sqlite3.OperationalError: missing synthetic table'); print('Ran 3 tests in 0.01s'); print('FAILED (errors=1, skipped=1)'); raise SystemExit(1)"]
        result = checks.run(command, self.root, {'PATH':os.environ.get('PATH','')}, self.root/'test.log', 10)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['test_executions'], 3)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['skip_messages'], ["test_container (Fixture.test_container) ... skipped 'HA image-only binary execution'"])
        self.assertIn('sqlite3.OperationalError: missing synthetic table', result['failure_messages'])

    def test_audio_package_install_targets_only_the_isolated_sysroot(self):
        commands = []
        def fake_run(command, cwd, env, log, timeout):
            commands.append(command)
            if command[0] == '/sbin/apk':
                root = Path(command[command.index('--root')+1])
                (root/'lib').mkdir()
                (root/'lib/ld-musl-x86_64.so.1').write_bytes(b'not an executable')
                (root/'usr/bin').mkdir(parents=True)
                for name in ('ffmpeg','ffprobe'):
                    (root/'usr/bin'/name).write_bytes(b'synthetic')
            return {'status':'PASS'}
        env = {'PATH':'/usr/bin'}
        with patch.object(checks.shutil, 'which', side_effect=lambda name, **kw: '/sbin/apk' if name=='apk' else None), \
             patch.object(checks.shutil, 'copytree'), patch.object(checks.shutil, 'copyfile'), \
             patch.object(checks, 'run', side_effect=fake_run):
            checks.prepare_audio_tools(self.root, env, self.root)
        self.assertIn('--no-scripts', commands[0])
        self.assertNotIn('--allow-untrusted', commands[0])
        self.assertEqual(commands[0][commands[0].index('--root')+1], str(self.root/'audio-sysroot'))
        self.assertEqual(commands[1][0], 'ffmpeg')
        self.assertIn('--library-path', (self.root/'audio-bin/ffmpeg').read_text())
        self.assertTrue(env['PATH'].startswith(str(self.root/'audio-bin')))


if __name__ == '__main__':
    unittest.main()
