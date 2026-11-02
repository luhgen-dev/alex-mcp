"""A completed subset must not turn incomplete behavior certification green."""
from pathlib import Path
import sys
import unittest
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alex-mcp/app'))
import behavior_cert


class CertificationStatusTests(unittest.TestCase):
    def selection_calls(self):
        return [
            {'tool':'search_saved_items','status':'OK','result':{
                'matches':[{'choice':1,'item_id':'synthetic-saved-item'}]}},
            {'tool':'resolve_numbered_choice','status':'OK',
             'arguments':{'choice':1,'selection_kind':'SAVED_ITEM'},
             'result':{'status':'found','item_id':'synthetic-saved-item'}},
        ]

    def test_saved_selection_is_an_observed_saved_item_read(self):
        self.assertEqual(behavior_cert._saved_selection_capabilities(self.selection_calls()), {'memory.get'})

    def test_selection_must_match_domain_identity_success_and_preceding_search(self):
        original = self.selection_calls()
        for field, value in [('status','ERROR'), ('arguments',{'choice':1,'selection_kind':'RECEIPT'}),
                             ('arguments',{'choice':2,'selection_kind':'SAVED_ITEM'}),
                             ('result',{'status':'found','item_id':'different-item'}),
                             ('result',{'status':'not_found','item_id':'synthetic-saved-item'})]:
            with self.subTest(field=field, value=value):
                calls = deepcopy(original);calls[-1][field] = value
                self.assertEqual(behavior_cert._saved_selection_capabilities(calls), set())
        self.assertEqual(behavior_cert._saved_selection_capabilities(original[::-1]), set())
        self.assertEqual(behavior_cert._saved_selection_capabilities(original[1:]), set())

    def test_later_empty_search_cannot_reuse_an_earlier_saved_match(self):
        search, selected = self.selection_calls()
        empty = {'tool':'search_saved_items','status':'OK','result':{'matches':[]}}
        self.assertEqual(behavior_cert._saved_selection_capabilities([search,empty,selected]), set())

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
