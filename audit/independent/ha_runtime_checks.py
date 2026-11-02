"""Run a pinned checkout's tests in an isolated HA Terminal virtual environment.

Default checks never read Alex options, database, pairing or Supervisor credentials.
The explicit --live-provider phase selects API credentials/model names from app
options for a synthetic provider benchmark. No app upgrade/restart or system-wide
package changes. Tests receive synthetic household options and no inherited credentials.
"""
import argparse
import json
import importlib.util
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


def synthetic_options():
    # These are the fixture identities used by test_v044 and behavior_cert.
    # Supplying {} suppresses their fallback options and makes ingress reject
    # the synthetic sender before the behavior under test can run.
    return {'husband_phone': '+60111111111', 'wife_phone': '+60222222222',
            'timezone': 'Asia/Kuala_Lumpur', 'ai_provider': 'grok',
            'xai_api_key': '', 'ocr_enabled': False, 'context_turns': 8}


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
    content = log.read_text(errors='replace')
    result = {'status': status, 'exit_code': code, 'seconds': round(time.monotonic()-started, 2), 'log': str(log)}
    counts = re.search(r'^Ran (\d+) tests? in ', content, re.M)
    if counts:
        result['test_executions'] = int(counts[1])
    skips = re.search(r'(?:OK|FAILED)\s*\([^\n]*skipped=(\d+)', content)
    result['skipped'] = int(skips[1]) if skips else 0
    result['skip_messages'] = [line[:800] for line in content.splitlines()
                               if re.search(r' \.\.\. skipped ', line)]
    result['failure_messages'] = [line[:800] for line in content.splitlines()
        if re.match(r'^(?:[\w.]*(?:Error|Exception):|FAIL:|ERROR:)', line)][:40]
    return result


def prepare_audio_tools(work, env, logs):
    """Provide real ffmpeg/ffprobe, without changing the Terminal's packages."""
    if all(shutil.which(name, path=env['PATH']) for name in ('ffmpeg', 'ffprobe')):
        return {'status': 'PASS', 'source': 'existing executable'}
    apk = shutil.which('apk', path=env['PATH'])
    if not apk:
        return {'status': 'UNAVAILABLE', 'reason': 'ffmpeg/ffprobe absent; isolated APK installer unavailable'}
    root = work/'audio-sysroot'
    keys = root/'etc/apk/keys'
    keys.parent.mkdir(parents=True)
    shutil.copytree('/etc/apk/keys', keys)
    shutil.copyfile('/etc/apk/repositories', keys.parent/'repositories')
    print('Installing signed FFmpeg packages into the separate test sysroot.', flush=True)
    installed = run([apk, '--root', str(root), '--initdb', '--no-scripts', 'add', '--no-cache', 'ffmpeg'],
                    work, env, logs/'audio-tools.log', 300)
    if installed['status'] != 'PASS':
        return installed
    loaders = list((root/'lib').glob('ld-musl-*.so.1'))
    if len(loaders) != 1:
        return {'status': 'UNAVAILABLE', 'reason': 'isolated musl loader not found'}
    wrappers = work/'audio-bin'
    wrappers.mkdir()
    for name in ('ffmpeg', 'ffprobe'):
        executable = root/'usr/bin'/name
        if not executable.is_file():
            return {'status': 'UNAVAILABLE', 'reason': name+' absent from installed package'}
        path = wrappers/name
        path.write_text('#!/bin/sh\nexec '+shlex.quote(str(loaders[0]))+' --library-path '
            +shlex.quote(str(root/'usr/lib')+':'+str(root/'lib'))+' '+shlex.quote(str(executable))+' "$@"\n')
        path.chmod(0o700)
    env['PATH'] = str(wrappers)+os.pathsep+env['PATH']
    # Exercise the real decoder/encoder chain used by the voice fixture.
    return run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440:duration=0.1', '-c:a', 'libopus', '-y', str(work/'audio-preflight.ogg')],
               work, env, logs/'audio-preflight.log', 30)


def routing_result(path, execution):
    """Retain LIVE_REQUIRED as unfinished; never turn a process failure green."""
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(report, dict) or report.get('mode') != 'offline':
        return None
    status = report.get('status')
    if status not in {'PASS', 'FAIL', 'LIVE_REQUIRED'}:
        return None
    expected_code = 0 if status == 'PASS' else 1
    if execution.get('exit_code') != expected_code:
        return None
    execution['status'] = status
    execution['routing_summary'] = report.get('summary', {})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--reuse', type=Path, help='Reuse the SDK venv from a previous isolated runtime directory')
    parser.add_argument('--only', action='append', default=[], help='Run only a named check (repeatable); compile always runs. This is not a full suite pass.')
    parser.add_argument('--offline-routing', action='store_true', help='Also audit every configured behavior contract and routing variant without provider calls')
    parser.add_argument('--live-provider', action='store_true', help='After successful selected checks, run real configured API providers against synthetic household fixtures')
    parser.add_argument('--live-contract', action='append', default=[], help='Select a live prompt/conversation contract (repeatable); results are labelled targeted')
    parser.add_argument('--live-budget', type=float, default=1.0, help='Estimated provider spend stop threshold in USD (checked between cases)')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.commit):
        parser.error('commit must be a full immutable GitHub commit SHA')
    if args.live_contract and not args.live_provider:
        parser.error('live-contract requires live-provider')
    if not math.isfinite(args.live_budget) or not 0 < args.live_budget <= 5:
        parser.error('live-budget must be greater than zero and at most 5 USD')
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
    options = work/'synthetic-options.json'
    options.write_text(json.dumps(synthetic_options()))
    # No Supervisor token, household credentials or custom package indexes.
    env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
           'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
           'ALEX_DATA_DIR': str(data), 'ALEX_OPTIONS_PATH': str(options)}
    print('WORKSPACE', work, flush=True)
    print('Downloading pinned public source; production Alex stays running.', flush=True)
    archive = work/'source.zip'
    download('https://codeload.github.com/luhgen-dev/alex-mcp/zip/'+args.commit, archive, 100*1024*1024)
    unpack(archive, checkout, args.commit)
    reuse = args.reuse.resolve() if args.reuse else None
    if reuse and (reuse.parent != base.resolve() or not reuse.name.startswith('runtime-')
                  or not (reuse/'venv/bin/python').is_file()
                  or not (reuse/'source/alex-mcp/app/requirements.txt').is_file()):
        raise ValueError('invalid_isolated_runtime_reuse_path')
    venv = reuse/'venv' if reuse else work/'venv'
    setup = {'status': 'PASS', 'reused_from': str(reuse)} if reuse else run(
        [sys.executable, '-m', 'venv', '--without-pip', str(venv)], work, env, logs/'venv.log', 60)
    report = {'commit': args.commit, 'production_changed': False, 'scope': 'isolated tests; no live delivery', 'setup': setup, 'checks': {}}
    def save():
        (work/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
    save()
    if setup['status'] != 'PASS':
        print('CHECKS_BLOCKED virtual environment unavailable; log:', setup['log'], flush=True)
        return 2
    # pip's official zipapp bootstraps only the isolated venv, not system Python.
    python = str(venv/'bin/python')
    if reuse:
        previous = json.loads((reuse/'summary.json').read_text())
        if (previous.get('dependencies', {}).get('status') != 'PASS'
                or (reuse/'source/alex-mcp/app/requirements.txt').read_bytes() != (checkout/'alex-mcp/app/requirements.txt').read_bytes()):
            raise ValueError('cannot_reuse_unverified_or_different_dependencies')
        report['dependencies'] = {'status': 'PASS', 'reused_from': str(reuse)}
        print('Reusing the installed pinned SDK environment.', flush=True)
    else:
        pip = work/'pip.pyz'
        download('https://bootstrap.pypa.io/pip/pip.pyz', pip, 20*1024*1024)
        print('Installing the pinned Alex SDKs in the isolated environment.', flush=True)
        report['dependencies'] = run([python, str(pip), '--isolated', 'install', '--disable-pip-version-check',
            '-r', str(checkout/'alex-mcp/app/requirements.txt'), 'pyflakes'], checkout, env, logs/'dependencies.log', 600)
    save()
    if report['dependencies']['status'] != 'PASS':
        print('CHECKS_BLOCKED dependency installation failed; log:', logs/'dependencies.log', flush=True)
        return 2
    report['audio_tools'] = prepare_audio_tools(work, env, logs)
    save()
    print('AUDIO_TOOLS', report['audio_tools']['status'], flush=True)
    # Tests may mock provider adapters; a missed mock must not reach a live API.
    env.update(HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1', NO_PROXY='127.0.0.1,localhost,::1')
    jobs = [('compile', [python, '-m', 'compileall', '-q', 'alex-mcp/app', 'tests'], 60)]
    jobs += [(p.stem, [python, '-m', 'unittest', 'discover', '-s', 'tests', '-p', p.name, '-v'], 180)
             for p in sorted((checkout/'tests').glob('test_*.py'))]
    jobs += [('selftest', [python, 'alex-mcp/app/selftest.py'], 120),
             ('stress', [python, 'alex-mcp/app/stress_test.py'], 180),
             ('catalog', [python, 'alex-mcp/app/behavior_cert.py', '--mode', 'catalog'], 120)]
    available = {name for name, _, _ in jobs}
    if set(args.only) - available:
        raise ValueError('unknown_check_selection')
    if args.only:
        selected = set(args.only) | {'compile'}
        jobs = [job for job in jobs if job[0] in selected]
    report['complete_unit_suite'] = not bool(args.only)
    report['selected_checks'] = [name for name, _, _ in jobs]
    print('CHECK_SCOPE', 'targeted; previous results retained separately' if args.only else 'complete unit suite', flush=True)
    routing_path = work/'routing-report.json'
    if args.offline_routing:
        jobs.append(('offline_routing', [python, 'alex-mcp/app/behavior_cert.py', '--mode', 'offline',
                                        '--report', str(routing_path)], 300))
    routing = None
    for index, (name, command, timeout) in enumerate(jobs, 1):
        result = run(command, checkout, env, logs/(name+'.log'), timeout)
        if name == 'offline_routing':
            routing = routing_result(routing_path, result)
        report['checks'][name] = result
        save()
        print(f'{index}/{len(jobs)} {name}: {result["status"]} (skipped={result["skipped"]})', flush=True)
    failed = {k: v['status'] for k, v in report['checks'].items() if v['status'] != 'PASS'}
    print('CHECKS_FINISHED', json.dumps({'jobs':len(jobs), 'failed':failed,
        'test_executions': sum(v.get('test_executions', 0) for v in report['checks'].values()),
        'skipped': sum(v.get('skipped', 0) for v in report['checks'].values())}), flush=True)
    for name in failed:
        print('FAILURE_DETAILS', name, flush=True)
        for message in report['checks'][name]['failure_messages']:
            print(message, flush=True)
    for name, result in report['checks'].items():
        for message in result['skip_messages']:
            print('SKIP_DETAILS', name, message, flush=True)
    if routing:
        print('ROUTING_RESULT', json.dumps({'status': routing['status'], **routing.get('summary', {})}), flush=True)
        seen = set()
        for failure in routing.get('failures', []):
            detail = {k: failure[k] for k in ('contract', 'kind', 'detail', 'prompt', 'provider_facing_tools') if k in failure}
            key = json.dumps(detail, ensure_ascii=False, sort_keys=True)
            if key not in seen and len(seen) < 30:
                print('ROUTING_FAILURE', key, flush=True)
            seen.add(key)
        if len(seen) > 30:
            print('ROUTING_MORE', len(seen)-30, 'additional distinct failures in', routing_path, flush=True)
    print('PRIVATE_REPORT', work/'summary.json', flush=True)
    if args.live_provider:
        if failed:
            print('LIVE_BLOCKED selected checks must pass first', flush=True)
        else:
            spec = importlib.util.spec_from_file_location('alex_live_probe', checkout/'audit/independent/live_probe.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            report['live_provider'] = module.run_provider(work, checkout, python, env, args.live_budget, args.live_contract)
            save()
            if report['live_provider']['status'] != 'PASS':
                return 1
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('CHECKS_BLOCKED', type(exc).__name__, flush=True)
        raise SystemExit(2)
