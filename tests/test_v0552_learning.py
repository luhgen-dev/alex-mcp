"""Persistent teaching through real storage, MCP/ingress, and grocery workers.

Only model and retailer network responses are scripted. No real account data.
"""
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import test_core as core
import learned_memory as lm
import grocery_prices as gp
import services
import db
import brain
import mydin_prices
from context import with_action_key


class PersistentLearningTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: core.AlexCoreTests.setUpClass())
    claim = core.AlexCoreTests.claim
    actor = core.AlexCoreTests.actor

    def setUp(self):
        core.AlexCoreTests.setUp(self)
        self.n = 0

    def tearDown(self):
        core.AlexCoreTests.setUp(self)

    def user(self, text='Remember Huggies diaper L 52 pcs RM45 at Manjaku', phone='+60111111111'):
        self.n += 1
        mid = 'learning-' + str(self.n)
        self.claim(mid, phone, text)
        return with_action_key(replace(self.actor(mid, phone), trusted_text=text,
                                      read_scope=core.scope_policy.resolve_read_scope(text)), mid + '-key')

    def price(self, actor=None, **kw):
        return lm.remember_price(actor or self.user(), kw.pop('product', 'Huggies diaper'),
                                 kw.pop('pack_description', 'L 52 pcs'), kw.pop('unit_price', '45'),
                                 kw.pop('retailer', 'Manjaku'), **kw)

    def test_repeat_action_and_new_version_have_one_memory_with_history(self):
        actor = self.user()
        first = self.price(actor)
        self.assertEqual(self.price(actor)['status'], 'already_saved')
        updated = self.price(self.user('Update Huggies diaper L 52 pcs reference to RM47 at Manjaku'), unit_price='47')
        self.assertEqual(updated['item_id'], first['item_id'])
        self.assertEqual(updated['version'], 2)
        history = lm.recall(actor, 'Huggies', True)['memories'][0]['history']
        self.assertEqual([h['data']['unit_price'] for h in history], ['47.00', '45.00'])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM shopping_items').fetchone()[0], 0)

    def test_new_python_process_recalls_without_conversation_history(self):
        self.price()
        code = "import learned_memory,json; print(json.dumps(learned_memory.reference(['FAMILY_SHARED'],'Huggies',2)))"
        env = {**os.environ, 'PYTHONPATH': os.path.dirname(lm.__file__),
               'ALEX_DATA_DIR': os.path.dirname(db.DB_PATH), 'ALEX_OPTIONS_PATH': core.OPTIONS}
        result = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        quote = json.loads(result.stdout)
        self.assertEqual(quote['line_total'], '90.00')
        self.assertEqual(quote['source_kind'], 'learned_reference')

    def test_new_chat_reuses_fact_and_preference_without_old_turns(self):
        actor = self.user('Remember my usual coffee is decaf')
        lm.remember(actor, 'Luhgen usual coffee', 'Decaf coffee', topics=['coffee', 'drinks'])
        other = replace(actor, conversation_id='another-dm', trusted_text='Which coffee do I use?')
        self.assertEqual(lm.context_for_turn(other, other.trusted_text)[0]['content'], 'Decaf coffee')
        self.assertEqual(lm.context_for_turn(other, 'Tell me the weather'), [])
        prefs = self.user('Remember I prefer short bullet replies')
        lm.remember(prefs, 'Reply style', 'Short bullets', 'preference', ['replies'])
        self.assertIn('Short bullets', [x['content'] for x in lm.context_for_turn(other, 'What is on today?')])

    def test_existing_saved_text_is_automatically_recalled(self):
        actor = self.user('Remember the spare key is in the blue drawer')
        services.save_item(actor, 'Spare key location', 'Blue drawer')
        self.assertEqual(lm.context_for_turn(actor, 'Where is the spare key?')[0]['content'], 'Blue drawer')

    def test_private_fact_price_and_preference_never_enter_shared_read(self):
        actor = self.user('Remember privately my Huggies L 52 pcs price is RM45')
        self.price(actor)
        private = self.user('Remember privately my favourite coffee is a secret')
        lm.remember(private, 'Favourite coffee', 'Secret private coffee', 'preference', ['replies'])
        shared = replace(private, read_scope='family', trusted_text='Coffee price')
        self.assertEqual(lm.context_for_turn(shared, 'Coffee price'), [])
        self.assertEqual(lm.recall(shared)['memories'], [])
        self.assertIsNone(lm.price_fallback(shared, 'Huggies'))
        self.assertEqual(lm.price_fallback(private, 'Huggies')['unit_price'], '45.00')
        spouse = self.user('Huggies price privately', '+60222222222')
        self.assertIsNone(lm.price_fallback(spouse, 'Huggies'))
        with self.assertRaises(PermissionError):
            lm.context_for_turn(replace(private, conversation_type='GROUP'), 'coffee')

    def test_shared_reference_available_to_spouse(self):
        self.price()
        spouse = self.user('Huggies price', '+60222222222')
        self.assertEqual(lm.price_fallback(spouse, 'Huggies')['unit_price'], '45.00')

    def test_private_recall_does_not_leak_back_through_recent_chat_history(self):
        import conversation_state
        actor = self.user('Where is my private coffee?')
        db.add_turn(actor.user_id, actor.conversation_id, 'user', 'Where is coffee?', read_scope='private')
        db.add_turn(actor.user_id, actor.conversation_id, 'assistant', 'Private secret location.', read_scope='private')
        shared = replace(actor, read_scope='family', trusted_text='Where is coffee?')
        self.assertEqual(conversation_state.recent_turns(shared), [])
        self.assertIn('Private secret', conversation_state.recent_turns(actor)[1]['content'])

    def test_teaching_does_not_hijack_finance_and_bill_updates(self):
        for text in ['Update my expense to RM45', 'Save my TNB bill RM45', 'Remember I paid RM45 at Manjaku', 'Update me on Huggies prices']:
            self.assertEqual(lm.teaching_tools(text), set())
        self.assertIn('search_mydin_prices', brain._select_tool_names('Update me on Huggies prices'))

    def test_pack_and_retailer_mismatch_never_substituted(self):
        self.price()
        for query in ['Huggies M', 'Huggies XL', 'Huggies 64 pcs', 'Pampers', 'Huggies wipes']:
            self.assertIsNone(lm.reference(['FAMILY_SHARED'], query), query)
        self.assertIsNone(lm.reference(['FAMILY_SHARED'], 'Huggies', retailer='MYDIN'))
        self.price(self.user('Remember Huggies M 64 pcs at Manjaku is RM40'),
                   pack_description='M 64 pcs', unit_price='40')
        self.assertEqual(lm.reference(['FAMILY_SHARED'], 'Huggies')['status'], 'needs_choice')
        self.assertEqual(lm.reference(['FAMILY_SHARED'], 'Huggies L')['unit_price'], '45.00')

    def test_explicit_alias_reused_without_making_up_a_brand(self):
        self.price(self.user('Remember our usual diapers means Huggies L52, RM45'), aliases=['usual diapers'])
        self.assertEqual(lm.reference(['FAMILY_SHARED'], 'usual diapers')['unit_price'], '45.00')
        self.assertIsNone(lm.reference(['FAMILY_SHARED'], 'Pampers'))

    def test_no_expiry_for_a_budget_reference(self):
        self.price(observed_on='2025-01-01')
        quote = lm.reference(['FAMILY_SHARED'], 'Huggies')
        self.assertEqual(quote['observed_on'], '2025-01-01')
        self.assertIn('Not a live', lm.render_reference(quote))

    def test_invalid_money_and_invented_price_are_rejected(self):
        for amount in ['NaN', 'Infinity', '-1', '0', '100001', 'wrong', '99', '52']:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.price(unit_price=amount)
        with self.assertRaises(ValueError):
            self.price(pack_description='')
        with self.assertRaises(ValueError):
            self.price(observed_on='2099-01-01')
        with self.assertRaises(ValueError):
            self.price(self.user('Remember Huggies L 52 pcs SGD45'), unit_price='45')

    def test_shared_style_preference_does_not_change_spouse_style(self):
        actor = self.user('Remember I prefer short replies')
        lm.remember(actor, 'Luhgen reply style', 'Short replies', 'preference', ['replies'])
        spouse = self.user('Explain rainbows', '+60222222222')
        self.assertEqual(lm.context_for_turn(spouse, spouse.trusted_text), [])
        self.assertEqual(len(lm.recall(spouse)['memories']), 1)
        spouse = self.user('Remember I prefer detailed replies', '+60222222222')
        lm.remember(spouse, 'Luhgen reply style', 'Detailed replies', 'preference', ['replies'])
        self.assertEqual(lm.context_for_turn(actor, 'Explain rainbows')[0]['content'], 'Short replies')
        self.assertEqual(lm.context_for_turn(spouse, 'Explain rainbows')[0]['content'], 'Detailed replies')

    def test_correction_and_forgetting_work_for_other_taught_facts(self):
        actor = self.user('Remember my usual coffee is decaf')
        first = lm.remember(actor, 'Luhgen usual coffee', 'Decaf', topics=['coffee'])
        actor = self.user('Update my remembered usual coffee to dark roast')
        updated = lm.remember(actor, 'Luhgen usual coffee', 'Dark roast', topics=['coffee'])
        self.assertEqual(first['item_id'], updated['item_id'])
        self.assertEqual(lm.context_for_turn(actor, 'Which coffee?')[0]['content'], 'Dark roast')
        services.remove_saved_item(actor, first['item_id'])
        self.assertEqual(lm.context_for_turn(actor, 'Which coffee?'), [])

    def test_read_only_or_document_instruction_cannot_teach(self):
        for text in ['What is the Huggies price RM45?', "Don't remember Huggies RM45", 'How would I remember Huggies RM45?']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.price(self.user(text))
        with self.assertRaises(ValueError):
            self.price(replace(self.user(), source='voice'))

    def test_grocery_worker_and_totals_use_reference_without_messages_or_expenses(self):
        self.price()
        actor = self.user('Add Huggies L 52 pcs to grocery list')
        services.add_shopping_item(actor, 'Huggies L 52 pcs', '2')
        with patch.object(gp, '_lookup_online', return_value={'status': 'unavailable'}):
            self.assertTrue(gp.process_one())
        self.assertNotIn('price', services.list_shopping_items(actor)['items'][0])
        priced = services.list_shopping_items(actor, include_prices=True)
        self.assertEqual(priced['estimated_total'], '90.00')
        self.assertFalse(priced['items'][0]['price']['stale'])
        self.assertEqual(gp.monthly_summary(actor)['pending_list_estimate'], '90.00')
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM outbound_messages').fetchone()[0], 0)

    def test_live_quote_takes_priority_over_estimate(self):
        self.price()
        online = {'status': 'priced', 'unit_price': '42.00'}
        with patch.object(gp, '_lookup_online', return_value=online):
            self.assertEqual(gp.lookup({'item_name': 'Huggies', 'quantity': '1', 'notes': None,
                                       'space_id': 'FAMILY_SHARED'}), online)

    def test_preference_can_disable_and_enable_fallback_persistently(self):
        self.price()
        actor = self.user('Remember: use online prices only for groceries')
        lm.remember(actor, 'Pricing', 'Online only', 'preference', ['groceries'], False)
        self.assertIsNone(lm.reference(['FAMILY_SHARED'], 'Huggies'))
        actor = self.user('Update my grocery price preference: remembered estimates are fine')
        lm.remember(actor, 'Other wording', 'Use known estimates', 'preference', ['groceries'], True)
        self.assertEqual(lm.reference(['FAMILY_SHARED'], 'Huggies')['unit_price'], '45.00')

    def test_forgetting_and_correction_invalidates_open_quotes(self):
        saved = self.price()
        actor = self.user('Add Huggies to grocery list')
        services.add_shopping_item(actor, 'Huggies', '1')
        with patch.object(gp, '_lookup_online', return_value={'status': 'unavailable'}):
            gp.process_one()
            self.price(self.user('Update Huggies price to RM47'), unit_price='47')
            self.assertFalse(services.list_shopping_items(actor, include_prices=True)['complete'])
            gp.process_one()
            self.assertEqual(services.list_shopping_items(actor, include_prices=True)['estimated_total'], '47.00')
            services.remove_saved_item(actor, saved['item_id'])
            gp.process_one()
        self.assertIsNone(lm.reference(['FAMILY_SHARED'], 'Huggies'))
        self.assertFalse(services.list_shopping_items(actor, include_prices=True)['complete'])

    def test_bought_history_frozen_when_price_updated(self):
        with patch.object(core.runtime_clock, 'now_utc', return_value=datetime(2026, 10, 20, tzinfo=timezone.utc)):
            self.price()
            actor = self.user('Add Huggies')
            item = services.add_shopping_item(actor, 'Huggies', '2')['item_id']
            with patch.object(gp, '_lookup_online', return_value={'status': 'unavailable'}):
                gp.process_one()
            services.update_shopping_item(actor, item, status='bought')
        self.price(self.user('Update Huggies RM47'), unit_price='47')
        self.assertEqual(gp.monthly_summary(actor, '2026-10')['purchased_list_reference_estimate'], '90.00')


class LearningConversationTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: core.AlexCoreTests.setUpClass())
    setUp = PersistentLearningTests.setUp
    tearDown = PersistentLearningTests.tearDown
    claim = PersistentLearningTests.claim
    actor = PersistentLearningTests.actor
    user = PersistentLearningTests.user
    price = PersistentLearningTests.price
    def run_turn(self, mid, text, steps, phone='+60111111111'):
        payload = {'message_id': mid, 'provider': 'WHATSAPP',
                   'conversation_id': phone.replace('+', '') + '@s.whatsapp.net',
                   'conversation_type': 'DIRECT_DM', 'sender_phone': phone, 'text': text}
        exposure = []
        core.AlexCoreTests._v0513_process_with_scripted_provider(self, payload, steps, exposure)
        with db.connect() as c:
            reply = '\n'.join(r[0] or '' for r in c.execute('SELECT text_body FROM outbound_messages WHERE source_message_id=?', (mid,)))
        return reply, exposure

    def test_teach_then_later_price_query_uses_real_memory(self):
        _, exposed = self.run_turn('teach-price', 'Remember Huggies diaper L 52 pcs RM45 per pack at Manjaku', [
            {'tool': 'remember_product_price', 'args': {'product': 'Huggies diaper', 'pack_description': 'L 52 pcs',
                                                     'unit_price': '45', 'retailer': 'Manjaku'}},
            {'content': 'Saved your price reference.'}])
        self.assertNotIn('log_expense', exposed[0])
        with db.connect() as c:
            c.execute('DELETE FROM conversation_turns')
        with patch.object(mydin_prices, 'search', return_value={'ok': True, 'products': []}):
            reply, _ = self.run_turn('later-price', 'Huggies price', [
                {'tool': 'search_mydin_prices', 'args': {'query': 'Huggies'}},
                {'content': 'It costs RM999'}])
        self.assertIn('approximately RM 45.00', reply)
        self.assertIn('Manjaku', reply)
        self.assertIn('Not a live', reply)
        self.assertNotIn('999', reply)

    def test_current_only_request_does_not_apply_fallback(self):
        self.price()
        with patch.object(mydin_prices, 'search', return_value={'ok': True, 'products': []}):
            reply, _ = self.run_turn('live-only', 'Huggies current online price only', [
                {'tool': 'search_mydin_prices', 'args': {'query': 'Huggies'}}, {'content': ''}])
        self.assertNotIn('45.00', reply)

    def test_price_history_and_update_use_persistent_tools_not_live_catalogue(self):
        self.price()
        reply, exposed = self.run_turn('read-history', 'Show Huggies price history', [
            {'tool': 'recall_knowledge', 'args': {'query': 'Huggies', 'include_history': True}},
            {'content': 'Your recorded reference is RM45 per pack.'}])
        self.assertNotIn('search_mydin_prices', exposed[0])
        self.run_turn('update-reference', 'Update the remembered Huggies price to RM47', [
            {'tool': 'recall_knowledge', 'args': {'query': 'Huggies'}},
            {'tool': 'remember_product_price', 'args': {'product': 'Huggies diaper',
                 'pack_description': 'L 52 pcs', 'retailer': 'Manjaku', 'unit_price': '47'}},
            {'content': 'Updated your price reference to RM47.'}])
        self.assertEqual(lm.reference(['FAMILY_SHARED'], 'Huggies')['unit_price'], '47.00')

    def test_failed_catalogue_search_can_still_return_remembered_estimate(self):
        self.price()
        with patch.object(mydin_prices, 'search', side_effect=ValueError('Synthetic source unavailable')):
            reply, _ = self.run_turn('offline-ref', 'Huggies price', [
                {'tool': 'search_mydin_prices', 'args': {'query': 'Huggies'}}, {'content': ''}])
        self.assertIn('approximately RM 45.00', reply)
        self.assertIn('unavailable', reply)

    def test_preference_is_actually_offered_to_model_after_new_chat_context(self):
        self.run_turn('teach-pref', 'Remember I always prefer short bullet replies', [
            {'tool': 'remember_knowledge', 'args': {'subject': 'Reply style', 'content': 'Use short bullets',
                                                 'kind': 'preference', 'topics': ['replies']}},
            {'content': 'Saved your reply preference.'}])
        with db.connect() as c:
            c.execute('DELETE FROM conversation_turns')
        messages = []
        original = brain._completion_kwargs
        def inspect(*args, **kwargs):
            messages.extend(args[1])
            return original(*args, **kwargs)
        with patch.object(brain, '_completion_kwargs', side_effect=inspect):
            self.run_turn('later-pref', 'Explain how rainbows form', [{'content': 'Light refracts through droplets.'}])
        context = [m for m in messages if isinstance(m.get('content'), str) and m['content'].startswith('Relevant persistent')]
        self.assertTrue(context)
        self.assertIn('Use short bullets', context[0]['content'])


if __name__ == '__main__':
    unittest.main()
