"""Provider probe isolation/redaction mechanics; no real API calls."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

import live_probe


class LiveProbeTests(unittest.TestCase):
    def test_failure_details_read_real_trace_routes_and_tools(self):
        detail = live_probe.failure_detail({'contract':'case', 'source':'voice',
            'attachment_count':1, 'problems':['required capability'],
            'trace':{'turn':{'result':{'routes':['gemini:configured-model']}},
                     'calls':[{'tool':'resolve_numbered_choice','status':'OK'}]}})
        self.assertEqual(detail['routes'], ['gemini:configured-model'])
        self.assertEqual(detail['attachment_count'], 1)
        self.assertEqual(detail['tools'], [{'tool':'resolve_numbered_choice','status':'OK'}])

    def test_compact_result_preserves_failures_and_labels_targeted_run(self):
        summary = live_probe.result_summary({'status':'FAIL', 'exit_code':1,
            'contracts':['case'], 'summary':{'prompt_runs':2,'planned_prompt_runs':2,
            'conversation_runs':0,'planned_conversation_runs':0,'failures':1,
            'budget_stopped':False,'estimated_cost_usd':0.01,'latency_by_domain':{'large':'detail'}}})
        self.assertEqual(summary['scope'], 'targeted')
        self.assertEqual(summary['failures'], 1)
        self.assertNotIn('latency_by_domain', summary)
        self.assertEqual(summary['estimated_cost_usd'], 0.01)

    def test_only_provider_configuration_is_selected(self):
        provider, selected = live_probe.select_options({
            'ai_provider':'gemini', 'gemini_api_key':'synthetic-api-key', 'gemini_model':'configured-model',
            'husband_phone':'real-person', 'tnb_password':'household-secret', 'SUPERVISOR_TOKEN':'supervisor-secret'})
        self.assertEqual(provider, 'gemini')
        self.assertEqual(selected, {'gemini_api_key':'synthetic-api-key', 'gemini_model':'configured-model'})

    def test_exact_credential_redaction_preserves_json_and_other_fields(self):
        secret = 'synthetic-"key\\value'
        value = {'status':'FAIL', 'reply':'upstream rejected '+secret}
        sanitized = live_probe.redact(value, [secret])
        self.assertEqual(sanitized, {'status':'FAIL','reply':'upstream rejected [redacted]'})
        self.assertEqual(value['reply'], 'upstream rejected '+secret)

    def test_live_child_has_no_supervisor_token_and_private_input_is_deleted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'logs').mkdir()
            seen = {}
            def fake_start(command, **kwargs):
                seen['path'] = Path(command[command.index('--source-options')+1])
                self.assertEqual(seen['path'].stat().st_mode & 0o777, 0o600)
                self.assertNotIn('SUPERVISOR_TOKEN', kwargs['env'])
                self.assertNotIn('HTTPS_PROXY', kwargs['env'])
                self.assertEqual(command[-2:], ['--contract', 'synthetic.case'])
                content = json.loads(seen['path'].read_text())
                self.assertNotIn('tnb_password', content)
                (root/'provider-report.json').write_text(json.dumps({'status':'PASS','summary':{}, 'prompt_results':[]}))
                return SimpleNamespace(returncode=0, wait=lambda **kw:0, poll=lambda:0)
            info = {'data':{'options':{'ai_provider':'gemini','gemini_api_key':'synthetic-api-key','tnb_password':'never-copy'}}}
            with patch.object(live_probe.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(info))), \
                 patch.object(live_probe.subprocess, 'Popen', side_effect=fake_start), \
                 patch('builtins.print') as output:
                result = live_probe.run_provider(root, root, '/test/python',
                    {'PATH':'/usr/bin','SUPERVISOR_TOKEN':'never-inherit','HTTPS_PROXY':'http://127.0.0.1:1'}, 1.0, ['synthetic.case'])
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['contracts'], ['synthetic.case'])
            self.assertFalse(seen['path'].exists())
            self.assertNotIn('synthetic-api-key', str(output.call_args_list))
