"""Read an Alex-only HA backup locally; never open or change the live database.

Only the SQLite database/WAL and app metadata are selected from the app archive.
Pairing files and original media are never extracted or exported. The resulting
private report includes narrowly selected household conversation evidence.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

APP = '9c6165c2_alex_mcp'
MAX_DB_BYTES = 1024 * 1024 * 1024
TABLES = ('inbound_messages', 'financial_events', 'financial_event_corrections',
          'media_objects', 'saved_items', 'shopping_items', 'reminders',
          'tasks', 'plans', 'diary_events', 'pending_items', 'outbound_messages',
          'tool_audit', 'voice_transcription_jobs', 'custom_ledgers',
          'custom_ledger_entries', 'discussion_journal')


def alias(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:12] if value else None


def sensitive_values(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r'key|token|password|secret|phone|email', key, re.I):
                if isinstance(item, str) and len(item) >= 6:
                    found.append(item)
            found.extend(sensitive_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(sensitive_values(item))
    return found


def clean(text, secrets=(), limit=1000):
    value = str(text or '')
    for secret in sorted(set(secrets), key=len, reverse=True):
        value = value.replace(secret, '[redacted]')
    value = re.sub(r'(?i)\bbearer\s+\S+|\b(?:sk-|xai-|AIza)[A-Za-z0-9._-]{8,}', '[redacted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted]', value)
    value = re.sub(r'(?i)\b(?:api[_ -]?key|password|secret|token)["\x27]?\s*[:=]\s*["\x27]?[^\s,}"\x27]+', '[credential redacted]', value)
    value = re.sub(r'https?://[^\s"<>]+', '[url]', value)
    value = re.sub(r'(?<!\w)\+?\d{9,16}(?:@(?:s\.whatsapp\.net|g\.us))?', '[phone/id]', value)
    return value[:limit]


def safe_name(member):
    path = PurePosixPath(member.name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe_archive_path')
    return str(path)


def extract_database(backup, directory):
    """Copy only fixed-name regular files, without tar.extract/extractall."""
    target = Path(directory)
    metadata = {}
    copied = set()
    with tarfile.open(backup, 'r:*') as outer:
        candidates = [m for m in outer.getmembers()
                      if safe_name(m) in {APP + '.tar.gz', APP + '.tar'}]
        if len(candidates) != 1 or not candidates[0].isfile():
            raise ValueError('alex_app_archive_not_found_or_not_regular')
        with outer.extractfile(candidates[0]) as stream:
            with tarfile.open(fileobj=stream, mode='r|*') as inner:
                for member in inner:
                    name = safe_name(member)
                    dest = {'data/alex_mcp.db': 'alex_mcp.db',
                            'data/alex_mcp.db-wal': 'alex_mcp.db-wal'}.get(name)
                    if name in {'addon.json', 'app.json'}:
                        if not member.isfile() or member.size > 1024 * 1024:
                            raise ValueError('invalid_app_metadata')
                        with inner.extractfile(member) as handle:
                            metadata = json.load(handle)
                    if dest:
                        if dest in copied or not member.isfile() or member.size > MAX_DB_BYTES:
                            raise ValueError('invalid_or_duplicate_database_member')
                        copied.add(dest)
                        with inner.extractfile(member) as source, (target / dest).open('xb') as output:
                            shutil.copyfileobj(source, output)
    if 'alex_mcp.db' not in copied:
        raise ValueError('database_not_found_in_app_backup')
    return metadata, 'alex_mcp.db-wal' in copied


def parsed(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def inspect_database(path, secrets=()):
    # mode=ro still replays the copied WAL. immutable=1 would incorrectly ignore it.
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    try:
        quick = [r[0] for r in conn.execute('PRAGMA quick_check')]
        available = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {table: conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
                  for table in TABLES if table in available}
        result = {'quick_check_ok': quick == ['ok'],
                  'foreign_key_violation_count': sum(1 for _ in conn.execute('PRAGMA foreign_key_check')),
                  'table_counts': counts, 'expenses': [], 'turns': []}
        required = {'financial_events', 'inbound_messages', 'media_objects', 'tool_audit', 'outbound_messages'}
        if not required <= available:
            result['diagnostic_status'] = 'required_tables_missing'
            return result
        events = conn.execute("""SELECT * FROM financial_events
            WHERE amount_minor IN (5690,5960) AND lower(description) LIKE '%starbuck%'
            ORDER BY created_at_utc DESC LIMIT 10""").fetchall()
        sources = set()
        for row in events:
            sources.add(row['source_message_id'])
            result['expenses'].append({k: row[k] for k in
                ('space_id', 'amount_minor', 'currency', 'status', 'event_date_utc', 'created_at_utc')})
            result['expenses'][-1].update(event=alias(row['event_id']), source=alias(row['source_message_id']),
                                           owner=alias(row['owner_id']), category=clean(row['category'], secrets, 80))
        # Also locate typed requests whose claimed expense might never have persisted.
        for row in conn.execute("""SELECT message_id FROM inbound_messages
            WHERE lower(raw_text) LIKE '%starbuck%'
              AND (raw_text LIKE '%56.90%' OR raw_text LIKE '%59.60%')
            ORDER BY received_at_utc DESC LIMIT 10"""):
            sources.add(row[0])
        windows = {}
        for source in sorted(x for x in sources if x):
            anchor = conn.execute('SELECT * FROM inbound_messages WHERE message_id=?', (source,)).fetchone()
            if not anchor:
                continue
            for row in conn.execute("""SELECT * FROM inbound_messages WHERE conversation_id=?
                AND datetime(received_at_utc) BETWEEN datetime(?,'-3 minutes') AND datetime(?,'+3 minutes')
                ORDER BY received_at_utc LIMIT 18""",
                (anchor['conversation_id'], anchor['received_at_utc'], anchor['received_at_utc'])):
                windows[row['message_id']] = row
        for row in sorted(windows.values(), key=lambda r: r['received_at_utc'])[-50:]:
            source = row['message_id']
            turn = {'source': alias(source), 'at': row['received_at_utc'],
                    'channel': row['conversation_type'], 'conversation': alias(row['conversation_id']),
                    'state': row['processing_state'], 'text': clean(row['raw_text'], secrets),
                    'quoted_provider_id': alias(row['quoted_message_id']), 'voice': [], 'tools': [], 'replies': []}
            for media in conn.execute("SELECT * FROM media_objects WHERE source_message_id=? AND media_type='AUDIO'", (source,)):
                meta = parsed(media['transcript_meta_json'])
                entry = {'transcript': clean(media['transcript_text'], secrets),
                         'command_execution': meta.get('command_execution'),
                         'mode': clean(meta.get('mode'), secrets, 80)}
                if 'voice_transcription_jobs' in available:
                    job = conn.execute('SELECT status,duration_seconds,reason FROM voice_transcription_jobs WHERE media_id=?', (media['media_id'],)).fetchone()
                    if job:
                        entry.update(status=job['status'], seconds=job['duration_seconds'], reason=clean(job['reason'], secrets, 100))
                turn['voice'].append(entry)
            for call in conn.execute('SELECT * FROM tool_audit WHERE source_message_id=? ORDER BY created_at_utc,rowid', (source,)):
                args, outcome = parsed(call['arguments_json']), parsed(call['result_json'])
                tool = {'name': clean(call['tool_name'], secrets, 100), 'status': call['status']}
                if call['tool_name'] == '_turn_trace':
                    tool.update(source=args.get('source'), quoted_context=args.get('quoted_context'),
                                exposed_tools=[clean(x, secrets, 100) for x in (args.get('exposed_tools') or [])[:80]],
                                outcome=clean(outcome.get('outcome'), secrets, 100))
                else:
                    tool['arguments'] = {k: clean(args[k], secrets, 200) for k in
                                         ('amount', 'category', 'currency', 'scope', 'search', 'intent') if k in args}
                    tool['result'] = {k: clean(outcome[k], secrets, 100) for k in
                                      ('status', 'count', 'event_id', 'old_event_id') if k in outcome}
                    for key in ('event_id', 'old_event_id'):
                        if key in tool['result']:
                            tool['result'][key] = alias(tool['result'][key])
                turn['tools'].append(tool)
            for reply in conn.execute('SELECT * FROM outbound_messages WHERE source_message_id=? ORDER BY created_at_utc,rowid', (source,)):
                turn['replies'].append({'kind': reply['kind'], 'text': clean(reply['text_body'], secrets),
                                        'delivery': reply['delivery_status'], 'destination': alias(reply['conversation_id']),
                                        'provider_id': alias(reply['provider_message_id']),
                                        'context_kind': reply['context_kind'], 'context_id': alias(reply['context_id'])})
            result['turns'].append(turn)
        result['diagnostic_status'] = 'collected' if sources else 'no_matching_screenshot_anchors'
        return result
    finally:
        conn.close()


def app_info():
    command = subprocess.run(['ha', 'apps', 'info', APP, '--raw-json'],
                             capture_output=True, text=True, timeout=40)
    if command.returncode:
        raise ValueError('ha_app_info_failed')
    raw = json.loads(command.stdout)
    info = raw.get('data', raw)
    options = info.get('options') or {}
    summary = {key: info.get(key) for key in ('slug', 'version', 'state', 'update_available')}
    summary['voice_notes_enabled'] = options.get('voice_notes_enabled') is True
    summary['options_sha256'] = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
    return summary, sensitive_values(options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('/config/alex-audit/snapshot-report.json'))
    args = parser.parse_args()
    old_umask = os.umask(0o077)
    try:
        live, secrets = app_info()
        digest = hashlib.sha256()
        with args.backup.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        with tempfile.TemporaryDirectory(prefix='alex-backup-probe-') as directory:
            metadata, wal = extract_database(args.backup, directory)
            secrets.extend(sensitive_values(metadata))
            report = {'live_app': live, 'backup_sha256': digest.hexdigest(),
                      'backup_bytes': args.backup.stat().st_size, 'copied_wal': wal,
                      'database': inspect_database(Path(directory) / 'alex_mcp.db', secrets),
                      'limits': 'Backup snapshot only. No original-media hash audit or live delivery verification.'}
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(report, output, ensure_ascii=False, indent=2)
        print('APP', json.dumps(live))
        print('BACKUP_READABLE', report['backup_bytes'], 'bytes; database and archive readable')
        print('DATABASE', json.dumps({k: report['database'][k] for k in ('quick_check_ok', 'foreign_key_violation_count')}))
        print('EXPENSES', json.dumps(report['database']['expenses']))
        print('MATCHED_TURNS', len(report['database']['turns']))
        print('PRIVATE_REPORT', str(args.output))
    except Exception as exc:
        # Do not print arbitrary archive, CLI or database exception strings.
        allowed = {'unsafe_archive_path', 'alex_app_archive_not_found_or_not_regular',
                   'invalid_app_metadata', 'invalid_or_duplicate_database_member',
                   'database_not_found_in_app_backup', 'ha_app_info_failed'}
        print('PROBE_FAILED', str(exc) if str(exc) in allowed else type(exc).__name__)
        return 1
    finally:
        os.umask(old_umask)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
