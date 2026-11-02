"""Run final isolated checks, back up Alex, then use HA's normal app update.

No configuration writes, other-app restarts, pairing changes or automatic
rollback. Reports contain version/state, backup slug and option hashes only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

APP = '9c6165c2_alex_mcp'
TARGET = '0.5.55'


def ha(*args, timeout=1800):
    result = subprocess.run(['ha', *args, '--raw-json'], capture_output=True,
                            text=True, timeout=timeout, check=True)
    value = json.loads(result.stdout)
    if value.get('result') == 'error':
        raise RuntimeError('HA command reported failure: ' + ' '.join(args[:2]))
    return value.get('data', value)


def options_hash(info):
    return hashlib.sha256(json.dumps(info['options'], sort_keys=True).encode()).hexdigest()


def install_release(work, before):
    if before['version'] != '0.5.54' or before.get('state') != 'started':
        raise RuntimeError('Expected running Alex 0.5.54; stopped before update')
    fingerprint = options_hash(before)
    ha('store', 'reload')
    current = ha('apps', 'info', APP)
    if current.get('version_latest') != TARGET or current['version'] != before['version']:
        raise RuntimeError('Expected release is not available, or installed version changed; no update attempted')
    if options_hash(current) != fingerprint:
        raise RuntimeError('Configuration changed during checks; no update attempted')
    print('RELEASE_BACKUP creating Alex-only backup', flush=True)
    backup = ha('backups', 'new', '--app='+APP, '--name=Alex before '+TARGET, '--location=.local')
    slug = backup.get('slug', '')
    if not re.fullmatch('[0-9a-f]+', slug):
        raise RuntimeError('No completed backup slug; no update attempted')
    archive = Path('/backup')/(slug+'.tar')
    with tarfile.open(archive) as saved:
        if not saved.getmembers():
            raise RuntimeError('Backup archive is empty; no update attempted')
    report = {'target':TARGET, 'before_version':before['version'], 'backup':slug,
              'options_sha256_before':fingerprint, 'status':'BACKED_UP'}
    report_path = work/'release.json'
    report_path.write_text(json.dumps(report, indent=2)+'\n')
    print('BACKUP_RETAINED', slug, flush=True)
    print('RELEASE_UPDATE building/updating Alex only; this can take several minutes', flush=True)
    ha('apps', 'update', APP)
    after = ha('apps', 'info', APP)
    if after.get('version') == TARGET and after.get('state') == 'stopped':
        ha('apps', 'start', APP)
    for _ in range(24):
        after = ha('apps', 'info', APP)
        if after.get('state') == 'started':
            break
        time.sleep(5)
    report.update(version=after.get('version'), state=after.get('state'),
                  options_unchanged=options_hash(after)==fingerprint)
    passed = report['version']==TARGET and report['state']=='started' and report['options_unchanged']
    report['status'] = 'DEPLOYED' if passed else 'VERIFY_FAILED'
    report_path.write_text(json.dumps(report, indent=2)+'\n')
    print('RELEASE_RESULT', json.dumps(report), flush=True)
    if not passed:
        raise RuntimeError('Update readback failed; backup retained, no automatic rollback')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.commit):
        parser.error('Use the full published commit SHA')
    os.umask(0o077)
    base = Path('/config/alex-audit')
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='release-', dir=base))
    before = ha('apps', 'info', APP)
    if before.get('version') == TARGET:
        print('ALREADY_INSTALLED', json.dumps({'version':TARGET,'state':before.get('state')}))
        return 0
    url = 'https://raw.githubusercontent.com/luhgen-dev/alex-mcp/'+args.commit+'/audit/independent/ha_runtime_checks.py'
    runner = work/'checks.py'
    with urllib.request.urlopen(url, timeout=60) as response:
        runner.write_bytes(response.read())
    print('RELEASE_CHECKS final full suite and offline routing; no paid provider rerun', flush=True)
    command = [sys.executable, str(runner), '--commit', args.commit, '--offline-routing']
    reuse = Path('/config/alex-audit/runtime-7f2195a3')
    if reuse.is_dir():
        command += ['--reuse', str(reuse)]
    subprocess.run(command, check=True)
    install_release(work, before)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never echo captured app options or arbitrary CLI errors containing keys.
        print('RELEASE_STOPPED', type(exc).__name__, 'Backup/data retained; send this line and preceding progress.', flush=True)
        raise SystemExit(1)
