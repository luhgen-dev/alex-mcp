"""No network or real credentials: exercise production startup composition."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'alex-mcp'/'app'))
from config import Settings
import db
import dot_runtime as runtime

class StartupTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('ALEX_TEST_PACKAGED_RUNTIME') == '1', 'HA image-only binary execution')
    def test_packaged_binary_executes_without_network_or_credentials(self):
        import subprocess
        runtime.validate_runtime()
        result = subprocess.run(['/usr/local/bin/tunnel-client-runtime', '--version'],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('0.0.16', result.stdout + result.stderr)

    def test_production_private_startup_composes_real_app_and_ledger(self):
        s = Settings(ai_provider='dot', dot_enabled=True, dot_transport='private_tunnel',
                     dot_tunnel_id='tunnel_'+'a'*32, dot_tunnel_runtime_key='test-only-'+'x'*40)
        with tempfile.TemporaryDirectory() as d, patch.object(runtime, 'DATA_DIR', d), \
             patch.object(db, 'DB_PATH', os.path.join(d, 'alex.db')), \
             patch.object(runtime, 'get_settings', return_value=s), \
             patch.object(runtime.subprocess, 'Popen') as process, \
             patch('threading.Thread.start'), patch('uvicorn.Server.run') as server:
            # In the HA image, validate the real packaged ELF; on a host without
            # that artifact, only this packaging check is replaced.
            check = contextlib.nullcontext() if os.environ.get('ALEX_TEST_PACKAGED_RUNTIME') == '1' else patch.object(runtime, 'validate_runtime')
            with check:
                runtime.main()
            self.assertTrue(process.called)
            self.assertTrue(server.called)
            command = process.call_args.args[0]
            self.assertIn('--mcp.server-url=http://127.0.0.1:5002/mcp/', command)
            self.assertNotIn(s.dot_tunnel_runtime_key, repr(process.call_args))
            self.assertTrue(Path(d, 'dot_events.db').exists())

    def test_diagnostic_never_emits_exception_values_or_arbitrary_class_names(self):
        for error in [ValueError('sk-DO-NOT-LOG secret config'), type('SecretClassName', (Exception,), {})('secret')]:
            with tempfile.TemporaryDirectory() as d, patch.object(runtime, 'DATA_DIR', d), \
                 patch.object(runtime, 'main', side_effect=error), patch.object(runtime.time, 'sleep'), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                runtime.run_service()
                status = json.loads(Path(d, 'dot_status.json').read_text())
                all_output = output.getvalue() + json.dumps(status)
                for forbidden in ['sk-DO-NOT-LOG', 'secret', 'SecretClassName']:
                    self.assertNotIn(forbidden, all_output)
                self.assertEqual(status['diagnostic']['code'], 'DOT_STARTUP')

    def test_specific_stage_preserved_without_message(self):
        with self.assertRaises(runtime.StartupFailure) as caught:
            runtime.checked('DOT_PRIVATE_CONFIG', lambda: (_ for _ in ()).throw(ValueError('secret')))
        self.assertEqual(caught.exception.code, 'DOT_PRIVATE_CONFIG')
        self.assertEqual(caught.exception.error_type, 'ValueError')
        self.assertNotIn('secret', str(caught.exception))

    def test_runtime_missing_architecture_and_format(self):
        with patch.object(runtime.platform, 'system', return_value='Linux'), patch.object(runtime.platform, 'machine', return_value='aarch64'):
            with self.assertRaises(runtime.StartupFailure) as error: runtime.validate_runtime('/absent')
            self.assertEqual(error.exception.code, 'DOT_RUNTIME_ARCH')
        with tempfile.TemporaryDirectory() as d, patch.object(runtime.platform, 'system', return_value='Linux'), patch.object(runtime.platform, 'machine', return_value='x86_64'):
            path = Path(d, 'runtime')
            with self.assertRaises(runtime.StartupFailure) as error: runtime.validate_runtime(str(path))
            self.assertEqual(error.exception.code, 'DOT_RUNTIME_MISSING')
            path.write_bytes(b'wrong format'); path.chmod(0o700)
            with self.assertRaises(runtime.StartupFailure) as error: runtime.validate_runtime(str(path))
            self.assertEqual(error.exception.code, 'DOT_RUNTIME_FORMAT')

if __name__ == '__main__': unittest.main()
