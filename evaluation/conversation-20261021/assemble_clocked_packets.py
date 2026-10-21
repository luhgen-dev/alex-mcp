import hashlib
import json
from pathlib import Path
import random
import secrets

root = Path('/tmp/alex-conversation-evidence')
fixed = '2026-10-21T08:00:00+00:00'
entries, provenance = [], {}
old_ids = {p['packet_id'] for p in json.loads((root/'actual-runtime-blind-packets.json').read_text())['packets']}
for label in ('baseline', 'candidate'):
    source = json.loads((root/f'clocked-runtime-capture-{label}-private.json').read_text())
    assert source['clock_env']['ALEX_CERT_NOW'] == fixed
    provenance[label] = {key: source[key] for key in ('repo', 'revision', 'runtime_source_sha256')}
    assert len(source['rows']) == 10
    for row in source['rows']:
        assert row['actor_received_at_utc'] == row['runtime_clock_before_capture'] == row['runtime_clock_after_capture'] == fixed
        assert row['seeded_fixture_timestamps_aligned']
        public = {'packet_id': 'pkt_' + secrets.token_hex(12),
                  'messages': row['request']['messages'], 'tools': row['request'].get('tools', [])}
        assert public['packet_id'] not in old_ids
        state = None
        for message in public['messages']:
            content = message.get('content', '')
            if isinstance(content, str) and '{"conversation_state"' in content:
                state = json.loads(content[content.index('{"conversation_state"'):])['conversation_state']
        if label == 'candidate':
            assert state['current_time']['received_at_utc'] == fixed
            assert state['current_time']['local_datetime'] == '2026-10-21T16:00:00+08:00'
            assert state['active']['id'] == row['fixture_pending_record']['item_id']
        private = {
            'packet_id': public['packet_id'], 'revision_label': label,
            'variation_index': row['variation'], 'input_phrase': row['phrase'],
            'packet_sha256': hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'seeded_pending_record': row['fixture_pending_record'],
            'actor_received_at_utc': row['actor_received_at_utc'],
            'runtime_clock_before_capture': row['runtime_clock_before_capture'],
            'runtime_clock_after_capture': row['runtime_clock_after_capture'],
            'sqlite_wall_clock_at_seed': row['sqlite_wall_clock_at_seed'],
            'provider_parameters': {k: v for k, v in row['request'].items() if k not in {'messages', 'tools'}},
        }
        entries.append((public, private))
random.SystemRandom().shuffle(entries)
public_value = {'schema_version': 1, 'packet_count': len(entries), 'packets': [x[0] for x in entries]}
public_path = root/'clocked-runtime-blind-packets.json'
raw = (json.dumps(public_value, ensure_ascii=False, indent=2) + '\n').encode()
for bad in (b'candidate', b'revision_label', b'test_followup_variation', b'test_conversation_reliability',
            b'ConversationFixture', b'aeb3b74', b'/workspace/', b'/tmp/'):
    assert bad not in raw, bad
for packet in public_value['packets']:
    assert set(packet) == {'packet_id', 'messages', 'tools'}
    text = json.dumps(packet, ensure_ascii=False)
    assert text.count('baseline') == 1 and 'explicit reserves as the baseline.' in text
public_path.write_bytes(raw)
private_value = {
    'schema_version': 1,
    'evidence_kind': 'actual first direct-brain request capture with fixed clock; no fresh model evaluation or domain actions',
    'public_file': str(public_path), 'public_file_sha256': hashlib.sha256(raw).hexdigest(),
    'provenance': provenance,
    'clock_setup': {
        'environment_variable': 'ALEX_CERT_NOW', 'value': fixed, 'set_before_app_imports': True,
        'retained_through_fixture_seed_and_capture': True, 'matches_actor_received_at_utc': True,
        'candidate_current_local_datetime': '2026-10-21T16:00:00+08:00',
        'verified_all20_before_after_clocks': True,
        'verified_all10_candidate_state_pending_ids_equal_seeded_record_ids': True,
        'sqlite_boundary': 'SQLite CURRENT_TIMESTAMP retains the wall clock. Only disposable seeded rows are explicitly timestamped at the frozen instant before capture. No SQL clock override, runtime/test source change, domain action, pending cleanup or post-action timestamp rewrite occurs.',
        'seed_columns_aligned_identically': {
            'pending_items': 'created_at_utc', 'conversation_turns': 'created_at_utc',
            'outbound_messages': 'created_at_utc', 'inbound_messages': 'received_at_utc',
        },
    },
    'capture_conditions': 'Separate processes and disposable databases; unchanged ConversationFixture.capture_request; live semantic router disabled; ingress bypassed; scripted provider response only after first-request capture.',
    'blind_integrity': 'New shuffled opaque IDs; full unmodified messages/schemas; no revision labels, expected answers, test names or paths. The original shared financial policy uses the ordinary word baseline identically in every packet.',
    'packets': [{**x[1], 'public_position': i} for i, x in enumerate(entries, 1)],
}
private_path = root/'clocked-runtime-blind-mapping-private.json'
private_path.write_text(json.dumps(private_value, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'public_file': str(public_path), 'private_mapping': str(private_path),
                  'packet_count': len(entries), 'public_sha256': private_value['public_file_sha256'],
                  'clock': fixed, 'verified_candidate_pending_ids': 10}, indent=2))
