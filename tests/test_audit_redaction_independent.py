"""Credential redaction must be tested independently of provider execution."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp' / 'app'))
import behavior_cert


class ReportRedactionTests(unittest.TestCase):
    def test_bearer_credentials_in_error_text(self):
        result = behavior_cert._redact_report({'error': 'Header: Bearer synthetic-secret-123'})
        self.assertNotIn('synthetic-secret-123', result['error'])
        self.assertIn('[redacted]', result['error'])

    def test_provider_keys_in_exception_text(self):
        for key in ['sk-proj-synthetic123456', 'xai-synthetic123456']:
            with self.subTest(prefix=key[:3]):
                self.assertNotIn(key, behavior_cert._redact_scalar('upstream rejected ' + key))

    def test_inline_api_key_assignments(self):
        for text in ['api_key=syntheticsecret123', 'api-key: syntheticsecret123',
                     'api key syntheticsecret123', '"api_key": "syntheticsecret123"']:
            with self.subTest(separator=text[:8]):
                result = behavior_cert._redact_scalar(text)
                self.assertNotIn('syntheticsecret123', result)
                self.assertNotIn('\\1', result)

    def test_nested_secret_fields(self):
        for key in ['api_key', 'apiKey', 'api-key', 'Authorization',
                    'access_token', 'refresh_token', 'tnb_password', 'client_secret',
                    'tokens', 'access_token_payload', 'api_key_value', 'input_tokens']:
            with self.subTest(key=key):
                result = behavior_cert._redact_report({'nested': [{key: 'syntheticsecret123'}]})
                self.assertEqual(result['nested'][0][key], '[redacted]')

    def test_usage_metrics_are_not_credentials(self):
        usage = {'input_tokens': 10, 'output_tokens': 4, 'cached_input_tokens': 3,
                 'reasoning_tokens': 1, 'model_calls': 2, 'estimated_cost_usd': 0.01}
        self.assertEqual(behavior_cert._redact_report(usage), usage)

    def test_sanitization_is_recursive_and_nonmutating(self):
        original = {'rows': ({'token': 'syntheticsecret123', 'status': 'FAIL'},)}
        result = behavior_cert._redact_report(original)
        self.assertEqual(result, {'rows': ({'token': '[redacted]', 'status': 'FAIL'},)})
        self.assertEqual(original['rows'][0]['token'], 'syntheticsecret123')


if __name__ == '__main__':
    unittest.main()
