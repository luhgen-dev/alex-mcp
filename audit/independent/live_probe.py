"""Operator-assisted provider benchmark; synthetic state, no household delivery.

Imported by the pinned isolated runner, after its selected tests pass. Only API
credentials and model choices are selected from `ha apps info`; source options
are deleted even on failure. Complete reports/logs remain private on HA.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time

KEYS = ('gemini_api_key', 'xai_api_key', 'openai_api_key')
MODELS = ('gemini_model', 'gemini_lite_model', 'grok_model', 'openai_model', 'reasoning_effort')


def select_options(options):
    provider = options.get('ai_provider') or 'auto'
    if provider not in {'auto', 'gemini', 'grok', 'openai'}:
        raise ValueError('unsupported_api_provider_for_isolated_probe')
    selected = {key: options[key] for key in KEYS + MODELS
                if isinstance(options.get(key), str) and options[key]}
    if not any(selected.get(key) for key in KEYS):
        raise ValueError('no_configured_api_provider_key')
    return provider, selected


def redact(value, secrets):
    text = json.dumps(value, ensure_ascii=False)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            # Work at the serialized representation so quotes/backslashes in
            # a credential cannot evade replacement or corrupt the JSON.
            text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[redacted]')
    return json.loads(text)


def run_provider(work, checkout, python, env, budget):
    info = subprocess.run(['ha', 'apps', 'info', '9c6165c2_alex_mcp', '--raw-json'],
                          capture_output=True, text=True, timeout=30, check=True)
    app = json.loads(info.stdout)
    app = app.get('data', app)
    options = app.get('options', {})
    provider, selected = select_options(options)
    secrets = [selected[k] for k in KEYS if selected.get(k)]
    report_path = work/'provider-report.json'
    progress_path = Path(str(report_path)+'.progress.json')
    log_path = work/'logs/provider.log'
    # The test runner's deliberately dead HTTP proxy is removed for this
    # opt-in provider phase. No Supervisor or household credentials are inherited.
    child_env = {k: v for k, v in env.items()
                 if k not in {'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'SUPERVISOR_TOKEN'}}
    child_env['ALEX_HA_API_URL'] = 'http://127.0.0.1:9/certification-no-ha'
    print('LIVE_SCOPE real API provider; synthetic household; simulated HA; no WhatsApp sending', flush=True)
    print('LIVE_PROVIDER', provider, 'ESTIMATED_STOP_THRESHOLD_USD', budget, flush=True)
    if options.get('ai_routing', 'off') != 'off' or options.get('chatgpt_plan_mode', 'off') != 'off':
        print('LIVE_LIMITATION ChatGPT session routing is configured but this probe exercises the API provider path only.', flush=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='provider-input-', dir=work) as private:
        option_path = Path(private)/'options.json'
        option_path.write_text(json.dumps(selected))
        option_path.chmod(0o600)
        command = [python, str(checkout/'alex-mcp/app/behavior_cert.py'), '--mode', 'live',
                   '--provider', provider, '--source-options', str(option_path),
                   '--live-strategy', 'benchmark', '--include-voice-parity',
                   '--max-live-cost-usd', str(budget), '--hard-latency-ms', '60000',
                   '--report', str(report_path)]
        with log_path.open('w') as log:
            process = subprocess.Popen(command, cwd=checkout, env=child_env, stdout=log, stderr=subprocess.STDOUT)
            timed_out = False
            try:
                while True:
                    try:
                        process.wait(timeout=20)
                        break
                    except subprocess.TimeoutExpired:
                        if progress_path.is_file():
                            try:
                                progress = json.loads(progress_path.read_text())
                                print('LIVE_PROGRESS', json.dumps({k: progress.get(k) for k in
                                    ('completed_prompts', 'planned_prompts', 'completed_conversations',
                                     'planned_conversations', 'estimated_cost_usd')}), flush=True)
                            except (OSError, ValueError):
                                pass
                        if time.monotonic()-started > 1800:
                            timed_out = True
                            break
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    # Temporary provider input has now been removed. Sanitize persisted logs
    # using exact configured credentials as well as the rig's own redaction.
    log_path.write_text(redact(log_path.read_text(errors='replace'), secrets))
    if timed_out or not report_path.is_file():
        result = {'status': 'TIMEOUT' if timed_out else 'FAIL', 'exit_code': process.returncode,
                  'reason': 'provider benchmark did not produce a complete report'}
        print('LIVE_RESULT', json.dumps(result), flush=True)
        print('LIVE_PRIVATE_LOG', log_path, flush=True)
        return result
    report = redact(json.loads(report_path.read_text()), secrets)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    expected_code = 0 if report.get('status') == 'PASS' else 1
    result = {'status': report.get('status', 'FAIL') if process.returncode == expected_code else 'FAIL',
              'exit_code': process.returncode, 'summary': report.get('summary', {}),
              'report': str(report_path), 'voice_recognition': report.get('voice_recognition')}
    print('LIVE_RESULT', json.dumps(result, ensure_ascii=False), flush=True)
    failures = [r for r in report.get('prompt_results', []) + report.get('conversation_results', [])
                if r.get('status') != 'PASS']
    for row in failures[:20]:
        detail = {k: row.get(k) for k in ('contract', 'source', 'problems')}
        detail['reply'] = str(row.get('reply', ''))[:400]
        detail['routes'] = (row.get('trace') or {}).get('routes', [])
        if row.get('steps'):
            detail['failed_steps'] = [{k: step.get(k) for k in ('step', 'problems', 'reply')}
                                      for step in row['steps'] if step.get('status') != 'PASS']
        print('LIVE_FAILURE', json.dumps(detail, ensure_ascii=False), flush=True)
    if len(failures) > 20:
        print('LIVE_MORE_FAILURES', len(failures)-20, flush=True)
    print('LIVE_PRIVATE_REPORT', report_path, flush=True)
    return result
