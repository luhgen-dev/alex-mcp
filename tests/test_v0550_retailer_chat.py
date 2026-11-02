"""Reported product follow-ups through ingress, real MCP and the outbox."""
import copy
import unittest
from unittest.mock import patch

import test_core as core
import mydin_prices
import manjaku_prices
import retailer_chat
import catalog_keywords
from test_mydin_prices import ROW, search_response, product_response
from test_manjaku_prices import ROWS, SALE, DIAPERS, page, suggestions


class RetailerChatTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: core.AlexCoreTests.setUpClass())

    def setUp(self):
        core.AlexCoreTests.setUp(self)
        self.mydin = patch.object(mydin_prices, 'search', side_effect=self.result).start()
        self.manjaku = patch.object(manjaku_prices, 'search', side_effect=lambda *a, **k: self.result(*a, source='Manjaku Online', **k)).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        core.AlexCoreTests.setUp(self)

    @staticmethod
    def result(query, *args, source='MYDIN Online', **kwargs):
        return {'ok': True, 'source': source, 'checked_at_local': '02 Nov 2026, 10:40 AM +08',
                'products': [{'title': query + ' fixture 1kg', 'selling_price': '12.34',
                              'currency': 'MYR', 'in_stock': True,
                              'url': ('https://mydin.my/product/' if source == 'MYDIN Online' else 'https://www.manjaku.com/products/') + 'fixture-10012345'}]}

    def run_turn(self, mid, text, steps):
        payload = {'message_id': mid, 'provider': 'WHATSAPP',
                   'conversation_id': '60111111111@s.whatsapp.net',
                   'conversation_type': 'DIRECT_DM', 'sender_phone': '+60111111111', 'text': text}
        exposed = []
        core.AlexCoreTests._v0513_process_with_scripted_provider(self, payload, steps, exposed)
        with core.db.connect() as conn:
            rows = conn.execute("SELECT text_body FROM outbound_messages WHERE source_message_id=? AND kind='TEXT'", (mid,)).fetchall()
        return '\n'.join(row['text_body'] or '' for row in rows), exposed

    def test_entire_screenshot_sequence_keeps_live_mydin_search(self):
        cases = [("What's the price of fernleaf milk?", 'fernleaf milk'),
                 ('What about rice price? 10kg. What brands do i have options?', 'rice 10kg'),
                 ('Do we have apple rice?', 'apple rice'),
                 ('What about yogurts?', 'yogurts'), ('Sardine price', 'sardine'),
                 ('What about cooking oils?', 'cooking oils'), ('What about detergents?', 'detergents')]
        for i, (text, query) in enumerate(cases):
            reply, exposed = self.run_turn('price-'+str(i), text, [
                {'tool': 'search_mydin_prices', 'args': {'query': query}},
                {'content': 'Unverified model price RM999.99'},
            ])
            self.assertIn('RM 12.34', reply)
            self.assertIn('https://mydin.my/product/', reply)
            self.assertNotIn('999.99', reply)
            self.assertNotIn('query_finances', exposed[0])
            self.assertNotIn('search_saved_items', exposed[0])
        self.assertEqual(self.mydin.call_count, len(cases))
        self.manjaku.assert_not_called()
        with core.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)

    def test_manjaku_followup_and_explicit_retailer_switch(self):
        for i, (text, tool) in enumerate([
            ('Check Manjaku diaper prices', 'search_manjaku_prices'),
            ('What about baby wipes?', 'search_manjaku_prices'),
            ('Wipes price at Mydin?', 'search_mydin_prices'),
            ('What about detergents?', 'search_mydin_prices'),
        ]):
            reply, exposed = self.run_turn('switch-'+str(i), text, [
                {'tool': tool, 'args': {'query': 'wipe'}}, {'content': ''},
            ])
            self.assertIn('RM 12.34', reply)
            other = 'search_mydin_prices' if 'manjaku' in tool else 'search_manjaku_prices'
            self.assertNotIn(other, exposed[0])

    def test_model_must_lookup_instead_of_inventing_or_reusing_prices(self):
        reply, _ = self.run_turn('must-fetch', 'Sardine price', [
            {'content': 'Sardines cost RM999.99'},
            {'tool': 'search_mydin_prices', 'args': {'query': 'sardine'}},
            {'content': 'RM999.99'},
        ])
        self.assertIn('12.34', reply)
        self.assertNotIn('999.99', reply)

    def test_long_manjaku_sequence_survives_generic_price_and_bounded_history(self):
        turns = ['Check Manjaku diaper prices', 'What about baby wipes?',
                 'Diaper price', 'What about milk?', 'What about bottles?',
                 'What about tissue?', 'What about lotion?', 'What about shampoo?']
        for i, text in enumerate(turns):
            reply, exposed = self.run_turn('long-manjaku-'+str(i), text, [
                {'tool': 'search_manjaku_prices', 'args': {'query': 'mimora'}},
                {'content': ''},
            ])
            self.assertIn('Manjaku Online', reply)
            self.assertNotIn('search_mydin_prices', exposed[0])
        self.mydin.assert_not_called()

    def test_model_never_fetches_fails_closed_at_call_limit(self):
        reply, _ = self.run_turn('no-fetch', 'Sardine price', [{'content': 'RM999.99'}]*4)
        self.assertIn('No live catalogue result', reply)
        self.assertNotIn('999.99', reply)

    def test_store_failure_is_not_replaced_with_old_price(self):
        self.mydin.side_effect = mydin_prices.SourceUnavailable('Fixture source unavailable')
        reply, _ = self.run_turn('failed-price', 'Sardine price', [
            {'tool': 'search_mydin_prices', 'args': {'query': 'sardine'}},
            {'content': 'Sardines cost RM999.99'},
        ])
        self.assertIn('current prices are unavailable', reply)
        self.assertNotIn('999.99', reply)

    def test_new_topics_do_not_inherit_catalogue_context(self):
        prior = [{'role': 'user', 'content': 'Check MYDIN rice price'}]
        for text in ('How much did I spend on rice?', 'Add milk to my grocery list',
                     'Show my grocery list with prices', 'What about my salary?',
                     'What about TNB bills?', 'What about tomorrow?', 'What about calories?',
                     'Monthly grocery amount', 'Do we have milk at home?'):
            with self.subTest(text=text):
                self.assertEqual(retailer_chat.context(text, prior), set())
        self.assertEqual(retailer_chat.context('What about milk?', prior + [
            {'role': 'user', 'content': 'Show my agenda for tomorrow'}]), set())

    def test_empty_result_reports_search_scope_not_catalogue_absence(self):
        self.mydin.side_effect = None
        self.mydin.return_value = {'ok': True, 'products': [], 'more_results': True}
        reply, _ = self.run_turn('not-found', 'Apple rice price', [
            {'tool': 'search_mydin_prices', 'args': {'query': 'apple rice'}},
            {'content': 'MYDIN does not sell Apple rice'},
        ])
        self.assertIn('does not prove', reply)
        self.assertNotIn('does not sell', reply)


class CatalogueRepairTests(unittest.TestCase):
    def test_plurals_and_food_accessories_keep_meaning(self):
        for original, expected in [('yogurts', 'yogurt'), ('cooking oils', 'cooking oil'),
                                   ('detergents', 'detergent'), ('Drypers', 'drypers')]:
            self.assertEqual(catalog_keywords.normalize(original), expected)
        for query, title in [('yogurts', 'Pureen Yogurt Shampoo'), ('milk', 'Milk Powder Case'),
                             ('milk', 'Baby Hippo Head to Toe Oat Milk & Rice'),
                             ('diapers', 'MommyMora Diaper Bag')]:
            self.assertFalse(catalog_keywords.relevant(query, title))
        self.assertTrue(catalog_keywords.relevant('milk powder case', 'Milk Powder Case'))
        self.assertTrue(catalog_keywords.relevant('milk', 'Fernleaf Full Cream Milk'))

    def test_manjaku_current_price_survives_stale_lower_compare_price(self):
        row = copy.deepcopy(ROWS[SALE])
        row['variants'][0]['compare_at_price'] = 100
        with patch.object(manjaku_prices, '_get_html', return_value=page(row)):
            product = manjaku_prices.product(SALE)['products'][0]
        self.assertEqual(product['selected_variant']['selling_price'], '18.90')
        self.assertIsNone(product['selected_variant']['compare_at_price'])
        self.assertTrue(product['selected_variant']['comparison_price_unverified'])

    def test_one_bad_product_does_not_discard_verified_siblings(self):
        with patch.object(manjaku_prices, '_get_html', return_value=suggestions([SALE, DIAPERS])), \
             patch.object(manjaku_prices, '_read_product', side_effect=[
                 manjaku_prices.SourceUnavailable('bad product'),
                 {'handle': DIAPERS, 'title': 'MIMORA DIAPERS', 'variants': []},
             ]):
            result = manjaku_prices.search('Mimora')
        self.assertEqual(len(result['products']), 1)
        self.assertEqual(result['unavailable_products'], 1)

    def test_single_term_mydin_filters_unrelated_search_hits(self):
        row = copy.deepcopy(ROW)
        row['custom_productname'] = 'Chocolate Jelly'
        with patch.object(mydin_prices, '_get_json', side_effect=[search_response('yogurt'), product_response(row)]):
            result = mydin_prices.search('yogurts')
        self.assertEqual(result['products'], [])
        self.assertEqual(result['partial_matches_omitted'], 1)
