"""Run the real initialization shell script with controlled process failures."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'alex-mcp/rootfs/etc/cont-init.d/00-alex-init'


class StartupGateTests(unittest.TestCase):
    def run_startup(self, failed_stage=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / 'calls'
            python = root / 'python3'
            python.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$1" >> "$TEST_CALLS"\n'
                '[ "$1" != "$TEST_FAIL_STAGE" ] || exit 42\n'
            )
            python.chmod(0o700)
            # Redirect only /app to the disposable directory. The actual
            # initialization script and shell error handling execute unchanged.
            shell = '''
cd() { [ "$1" = /app ] && builtin cd "$TEST_APP"; }
bashio::log.info() { printf 'INITIALIZED: %s\\n' "$*"; }
bashio::log.warning() { printf 'WARNING: %s\\n' "$*"; }
source "$1"
'''
            env = {'PATH': directory + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
                   'TEST_APP': directory, 'TEST_CALLS': str(calls),
                   'TEST_FAIL_STAGE': failed_stage}
            result = subprocess.run(['bash', '-c', shell, 'startup-test', str(SCRIPT)],
                                    env=env, capture_output=True, text=True, timeout=10)
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_database_initialization_failure_stops_before_selftest_and_success_log(self):
        result, calls = self.run_startup('init_db.py')
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(calls, ['init_db.py'])
        self.assertNotIn('INITIALIZED', result.stdout)

    def test_structural_failure_is_fatal_and_never_claims_initialized(self):
        result, calls = self.run_startup('selftest.py')
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(calls, ['init_db.py', 'selftest.py'])
        self.assertNotIn('INITIALIZED', result.stdout)

    def test_success_runs_both_checks_before_success_log(self):
        result, calls = self.run_startup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['init_db.py', 'selftest.py'])
        self.assertIn('INITIALIZED: Alex MCP persistent core initialized.', result.stdout)


if __name__ == '__main__':
    unittest.main()
