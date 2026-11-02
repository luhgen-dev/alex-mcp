"""Run a pinned checkout's tests in an isolated HA Terminal virtual environment.

Never reads Alex options, database, pairing or Supervisor credentials. No app
upgrade/restart or system package installation. Downloads public source and
dependencies; tests receive empty options and no inherited credentials.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


def download(url, path, limit):
    with urllib.request.urlopen(url, timeout=45) as response, path.open('xb') as output:
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise ValueError('download_too_large')
            output.write(block)


def unpack(archive, target, commit):
    prefix = 'alex-mcp-' + commit
    total = 0
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != prefix:
                raise ValueError('invalid_source_archive_path')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('source_archive_symlink')
            if member.is_dir():
                continue
            total += member.file_size
            if member.file_size > 50 * 1024 * 1024 or total > 300 * 1024 * 1024:
                raise ValueError('source_archive_too_large')
            output = target.joinpath(*path.parts[1:])
            output.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as reader, output.open('xb') as writer:
                shutil.copyfileobj(reader, writer)


def run(command, cwd, env, log, timeout):
    started = time.monotonic()
    with log.open('w') as output:
        try:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
            status, code = ('PASS' if result.returncode == 0 else 'FAIL'), result.returncode
        except subprocess.TimeoutExpired:
            status, code = 'TIMEOUT', None
        except OSError as exc:
            output.write(type(exc).__name__)
            status, code = 'UNAVAILABLE', None
    return {'status': status, 'exit_code': code, 'seconds': round(time.monotonic()-started, 2), 'log': str(log)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.commit):
        parser.error('commit must be a full immutable GitHub commit SHA')
    os.umask(0o077)
    base = Path('/config/alex-audit')
    base.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(base).free < 750 * 1024 * 1024:
        print('CHECKS_BLOCKED less than 750 MiB free; no installation attempted', flush=True)
        return 2
    work = Path(tempfile.mkdtemp(prefix='runtime-', dir=base))
    logs, checkout, data = work/'logs', work/'source', work/'synthetic-data'
    for path in (logs, checkout, data):
        path.mkdir()
    options = work/'empty-options.json'
    options.write_text('{}')
    # No Supervisor token, household credentials or custom package indexes.
    env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
           'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
           'ALEX_DATA_DIR': str(data), 'ALEX_OPTIONS_PATH': str(options)}
    print('WORKSPACE', work, flush=True)
    print('Downloading pinned public source; production Alex stays running.', flush=True)
    archive = work/'source.zip'
    download('https://codeload.github.com/luhgen-dev/alex-mcp/zip/'+args.commit, archive, 100*1024*1024)
    unpack(archive, checkout, args.commit)
    venv = work/'venv'
    setup = run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], work, env, logs/'venv.log', 60)
    report = {'commit': args.commit, 'production_changed': False, 'scope': 'isolated tests; no live delivery', 'setup': setup, 'checks': {}}
    def save():
        (work/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
    save()
    if setup['status'] != 'PASS':
        print('CHECKS_BLOCKED virtual environment unavailable; log:', setup['log'], flush=True)
        return 2
    # pip's official zipapp bootstraps only the isolated venv, not system Python.
    pip = work/'pip.pyz'
    download('https://bootstrap.pypa.io/pip/pip.pyz', pip, 20*1024*1024)
    python = str(venv/'bin/python')
    print('Installing the pinned Alex SDKs in the isolated environment.', flush=True)
    report['dependencies'] = run([python, str(pip), '--isolated', 'install', '--disable-pip-version-check',
        '-r', str(checkout/'alex-mcp/app/requirements.txt'), 'pyflakes'], checkout, env, logs/'dependencies.log', 600)
    save()
    if report['dependencies']['status'] != 'PASS':
        print('CHECKS_BLOCKED dependency installation failed; log:', logs/'dependencies.log', flush=True)
        return 2
    # Tests may mock provider adapters; a missed mock must not reach a live API.
    env.update(HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1', NO_PROXY='127.0.0.1,localhost,::1')
    jobs = [('compile', [python, '-m', 'compileall', '-q', 'alex-mcp/app', 'tests'], 60)]
    jobs += [(p.stem, [python, '-m', 'unittest', 'discover', '-s', 'tests', '-p', p.name, '-v'], 180)
             for p in sorted((checkout/'tests').glob('test_*.py'))]
    jobs += [('selftest', [python, 'alex-mcp/app/selftest.py'], 120),
             ('stress', [python, 'alex-mcp/app/stress_test.py'], 180),
             ('catalog', [python, 'alex-mcp/app/behavior_cert.py', '--mode', 'catalog'], 120)]
    for index, (name, command, timeout) in enumerate(jobs, 1):
        result = run(command, checkout, env, logs/(name+'.log'), timeout)
        report['checks'][name] = result
        save()
        print(f'{index}/{len(jobs)} {name}: {result["status"]}', flush=True)
    failed = {k: v['status'] for k, v in report['checks'].items() if v['status'] != 'PASS'}
    print('CHECKS_FINISHED', json.dumps({'jobs':len(jobs), 'failed':failed}), flush=True)
    print('PRIVATE_REPORT', work/'summary.json', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('CHECKS_BLOCKED', type(exc).__name__, flush=True)
        raise SystemExit(2)
