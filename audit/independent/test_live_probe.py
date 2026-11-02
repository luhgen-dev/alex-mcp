"""Provider probe isolation/redaction mechanics; no real API calls."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

import live_probe


class LiveProbeTests(unittest.TestCase):
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
                content = json.loads(seen['path'].read_text())
                self.assertNotIn('tnb_password', content)
                (root/'provider-report.json').write_text(json.dumps({'status':'PASS','summary':{}, 'prompt_results':[]}))
                return SimpleNamespace(returncode=0, wait=lambda **kw:0, poll=lambda:0)
            info = {'data':{'options':{'ai_provider':'gemini','gemini_api_key':'synthetic-api-key','tnb_password':'never-copy'}}}
            with patch.object(live_probe.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(info))), \
                 patch.object(live_probe.subprocess, 'Popen', side_effect=fake_start), \
                 patch('builtins.print') as output:
                result = live_probe.run_provider(root, root, '/test/python',
                    {'PATH':'/usr/bin','SUPERVISOR_TOKEN':'never-inherit','HTTPS_PROXY':'http://127.0.0.1:1'}, 1.0)
            self.assertEqual(result['status'], 'PASS')
            self.assertFalse(seen['path'].exists())
            self.assertNotIn('synthetic-api-key', str(output.call_args_list))
