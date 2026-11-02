"""Run the independent local checks; optionally replay them on baseline modules.

No live providers, HA calls, WhatsApp sender or household database are used.
This is a focused suite, not the repository's complete release gate.
"""
from pathlib import Path
import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULES = ('services', 'outbox', 'behavior_cert', 'phase2', 'custom_ledgers', 'multilingual_intent')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', help='Use the audited runtime modules from this Git commit; keep current tests')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.report.with_suffix('.log')
    with tempfile.TemporaryDirectory(prefix='alex-independent-runtime-') as directory:
        options = Path(directory) / 'options.json'
        options.write_text('{}')
        os.environ['ALEX_DATA_DIR'] = directory
        os.environ['ALEX_OPTIONS_PATH'] = str(options)
        os.environ.pop('SUPERVISOR_TOKEN', None)
        sys.path.insert(0, str(ROOT / 'tests'))
        sys.path.insert(0, str(ROOT / 'alex-mcp' / 'app'))
        baseline = None
        hashes = {}
        overlay = Path(directory) / 'baseline-runtime'
        if args.baseline_ref:
            baseline = subprocess.check_output(
                ['git', 'rev-parse', '--verify', '--end-of-options', args.baseline_ref + '^{commit}'],
                cwd=ROOT, text=True).strip()
            overlay.mkdir()
            # Select all baseline modules before importing any of them. Loading
            # services first can cache current helper functions via `from ...
            # import ...`, silently mixing candidate and baseline code.
            sys.path.insert(0, str(overlay))
        for name in MODULES:
            path = 'alex-mcp/app/' + name + '.py'
            source = (subprocess.check_output(['git', 'show', baseline + ':' + path], cwd=ROOT)
                      if baseline else (ROOT / path).read_bytes())
            hashes[path] = hashlib.sha256(source).hexdigest()
            if baseline:
                (overlay / (name + '.py')).write_bytes(source)
        if baseline:
            for name in MODULES:
                module = importlib.import_module(name)
                if Path(module.__file__).resolve() != overlay / (name + '.py'):
                    raise RuntimeError('baseline import did not use the selected module: ' + name)
        for path in sorted((ROOT / 'tests').glob('*independent*.py')):
            hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*_independent.py')
        with log_path.open('w') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        failed = [str(test) for test, _ in result.failures + result.errors]
        report = {
            'scope': 'isolated SQLite/files; mocked worker network adapters; no live integration',
            'baseline_module_ref': baseline,
            'tests_run': result.testsRun,
            'failure_assertions_including_subtests': len(result.failures),
            'errors_including_subtests': len(result.errors),
            'distinct_failing_test_methods': len({name.split(' (')[0] for name in failed}),
            'skipped': len(result.skipped),
            'successful': result.wasSuccessful(),
            'failures': failed,
            'sha256': hashes,
        }
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: v for k, v in report.items() if k not in {'failures', 'sha256'}}, indent=2))
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
