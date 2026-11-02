"""A completed subset must not turn incomplete behavior certification green."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp/app'))
import behavior_cert


class CertificationStatusTests(unittest.TestCase):
    def test_successful_paid_subset_does_not_hide_missing_capability(self):
        self.assertEqual(behavior_cert._live_acceptance_status([], False, 2, 2, 1), 'FAIL')

    def test_empty_and_interrupted_runs_cannot_pass(self):
        self.assertEqual(behavior_cert._live_acceptance_status([], False, 0, 0), 'NOT_RUN')
        self.assertEqual(behavior_cert._live_acceptance_status([], False, 3, 2), 'INCOMPLETE_EXECUTION')
        self.assertEqual(behavior_cert._live_acceptance_status([], True, 3, 2), 'INCOMPLETE_BUDGET')

    def test_complete_success_and_observed_failure_remain_distinct(self):
        self.assertEqual(behavior_cert._live_acceptance_status([], False, 2, 2), 'PASS')
        self.assertEqual(behavior_cert._live_acceptance_status(['wrong stored amount'], False, 2, 2), 'FAIL')

    def test_model_choices_preserved_without_household_configuration(self):
        options = behavior_cert._sandbox_options('gemini', {'gemini_api_key': 'synthetic-key'},
            {'gemini_model': 'configured-model', 'husband_phone': 'real-household-phone',
             'tnb_password': 'household-secret', 'data_path': '/data/live'})
        self.assertEqual(options['gemini_model'], 'configured-model')
        self.assertNotEqual(options['husband_phone'], 'real-household-phone')
        self.assertNotIn('tnb_password', options)
        self.assertNotIn('data_path', options)
