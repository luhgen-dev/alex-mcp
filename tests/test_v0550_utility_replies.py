"""Live date-only reply regression: real ingress, MCP, storage and outbox.

Only AI responses and the remote utility portals are scripted. No household
credentials or real customer records are used by these tests.
"""
import asyncio
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import test_core as core
from test_household_bills import SETTINGS, TNB_HTML, SAJ_HTML, SAJ_HISTORY
import household_bills as bills
import utility_portals as portals
from mcp import Client


class UtilityReplyTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: core.AlexCoreTests.setUpClass())

    def setUp(self):
        core.AlexCoreTests.setUp(self)
        settings = replace(SETTINGS, tnb_portal_enabled=True, saj_portal_enabled=True,
                           tnb_email='test@example.invalid', tnb_password='fixture',
                           saj_email='test@example.invalid', saj_password='fixture')
        settings_patch = patch.object(bills, 'get_settings', return_value=settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.portals = {}

        def portal(provider):
            instance = Mock()
            result = (portals.parse_tnb(TNB_HTML, SETTINGS.tnb_account) if provider == 'tnb'
                      else portals.parse_saj(SAJ_HTML, SAJ_HISTORY, SETTINGS.saj_account))
            instance.read.return_value = (result, TNB_HTML if provider == 'tnb' else SAJ_HTML)
            instance.pdf.return_value = b'%PDF-1.4 synthetic bill'
            self.portals[provider] = instance
            return instance

        self.portal_patch = patch.object(bills, 'Portal', side_effect=portal)
        self.ctor = self.portal_patch.start()
        self.addCleanup(self.portal_patch.stop)

    def run_turn(self, steps, text='Check my tnb and saj bills', mid='utility-0550'):
        payload = {'message_id': mid, 'provider': 'WHATSAPP',
                   'conversation_id': '60111111111@s.whatsapp.net',
                   'conversation_type': 'DIRECT_DM', 'sender_phone': '+60111111111',
                   'text': text}
        return core.AlexCoreTests._v0513_process_with_scripted_provider(self, payload, steps)

    def tearDown(self):
        # Other suites share this fixture database; remove dependent outbox and
        # bill records before their older, narrower table-reset helpers run.
        core.AlexCoreTests.setUp(self)

    def reply(self):
        with core.db.connect() as conn:
            rows = conn.execute("SELECT text_body FROM outbound_messages WHERE kind='TEXT' ORDER BY rowid").fetchall()
        return '\n'.join(row['text_body'] or '' for row in rows)

    def test_both_mixed_case_providers_survive_empty_final_answer(self):
        self.run_turn([
            {'tool': 'check_household_bill', 'args': {'provider': ' TNB '}},
            {'tool': 'check_household_bill', 'args': {'provider': 'SaJ'}},
            {'content': ''},
        ])
        text = self.reply()
        for expected in ('TNB', 'SAJ', '75.30', '66.40', 'Outstanding', '0.00'):
            self.assertIn(expected, text)
        self.assertNotIn('123456789012', text)
        for portal in self.portals.values():
            portal.pdf.assert_not_called()
        with core.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM household_bill_records').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbound_messages WHERE kind='DOCUMENT'").fetchone()[0], 0)

    def test_provider_schema_advertises_supported_values(self):
        async def read():
            async with Client(core.mcp) as client:
                listed = await client.list_tools()
                return next(t for t in listed.tools if t.name == 'check_household_bill').input_schema
        schema = asyncio.run(read())
        self.assertEqual(schema['properties']['provider']['enum'], ['tnb', 'saj'])
        self.assertNotIn('actor', schema['properties'])

    def test_failed_calls_are_not_hidden_by_saved_records_fallback(self):
        self.run_turn([
            {'tool': 'check_household_bill', 'args': {'provider': 'electricity'}},
            {'tool': 'check_household_bill', 'args': {'provider': 'water'}},
            {'tool': 'bills_list', 'args': {}},
            {'content': ''},
        ])
        text = self.reply().lower()
        self.assertIn("couldn't retrieve", text)
        self.assertIn('live balance', text)
        self.assertNotIn('as of date:', text)
        self.ctor.assert_not_called()

    def test_date_only_model_answer_is_replaced_by_both_results(self):
        self.run_turn([
            {'tool': 'check_household_bill', 'args': {'provider': 'tnb'}},
            {'tool': 'check_household_bill', 'args': {'provider': 'saj'}},
            {'content': 'As of date: 2026-11-02'},
        ])
        self.assertIn('75.30', self.reply())
        self.assertIn('66.40', self.reply())

    def test_one_unavailable_provider_keeps_other_result(self):
        original = self.ctor.side_effect

        def fail_saj(provider):
            portal = original(provider)
            if provider == 'saj':
                portal.login.side_effect = portals.PortalUnavailable('Login unavailable')
            return portal

        self.ctor.side_effect = fail_saj
        self.run_turn([
            {'tool': 'check_household_bill', 'args': {'provider': 'tnb'}},
            {'tool': 'check_household_bill', 'args': {'provider': 'saj'}},
            {'content': ''},
        ])
        self.assertIn('TNB', self.reply())
        self.assertIn('75.30', self.reply())
        self.assertIn("SAJ: I couldn't retrieve a live balance", self.reply())
        with core.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM household_bill_records').fetchone()[0], 1)

    def test_explicit_pdf_request_queues_original_document(self):
        self.run_turn([
            {'tool': 'check_household_bill', 'args': {'provider': 'TNB', 'include_pdf': True}},
            {'content': ''},
        ], text='Send me my TNB bill PDF')
        self.portals['tnb'].pdf.assert_called_once()
        with core.db.connect() as conn:
            rows = conn.execute("SELECT * FROM outbound_messages WHERE kind='DOCUMENT'").fetchall()
            self.assertEqual(len(rows), 1)

    def test_unknown_outstanding_is_never_rendered_as_zero(self):
        text = core.brain._tool_evidence_fallback([{
            '_tool_name': 'check_household_bill', 'provider': 'tnb',
            'status': 'refreshed', 'live': True, 'invoice_amount': '12.00',
            'invoice_date': '2026-10-21', 'outstanding': None,
        }])
        self.assertIn('Outstanding: unknown', text)
        self.assertNotIn('RM 0.00', text)

    def test_empty_saved_bills_is_not_date_only_or_zero(self):
        text = core.brain._tool_evidence_fallback([{
            '_tool_name': 'bills_list', 'obligations': [],
            'provider_bills': {'bills': []}, 'as_of_date': '2026-11-02',
        }])
        self.assertIn('No saved bills', text)
        self.assertIn('live balance', text)

    def test_failed_tnb_input_keeps_label_alongside_saj_success(self):
        original = self.ctor.side_effect

        def fail_tnb(provider):
            portal = original(provider)
            if provider == 'tnb':
                portal.login.side_effect = ValueError('Synthetic configuration failure')
            return portal

        self.ctor.side_effect = fail_tnb
        self.run_turn([
            {'tool': 'check_household_bill', 'args': {'provider': 'TNB'}},
            {'tool': 'check_household_bill', 'args': {'provider': 'SAJ'}},
            {'content': ''},
        ])
        self.assertIn("TNB: I couldn't retrieve a live balance", self.reply())
        self.assertIn('66.40', self.reply())

    def test_unrequested_pdf_is_blocked_even_if_model_requests_it(self):
        for i, text in enumerate(('Check my TNB bill', 'Analyse my TNB bill PDF',
                                  "Check my TNB bill, don't send the PDF",
                                  'Show my TNB bill amount', 'Send me the TNB bill balance')):
            with self.subTest(text=text):
                self.run_turn([
                    {'tool': 'check_household_bill', 'args': {'provider': 'tnb', 'include_pdf': True}},
                    {'content': ''},
                ], text=text, mid=f'no-pdf-{i}')
                self.portals['tnb'].pdf.assert_not_called()
        with core.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbound_messages WHERE kind='DOCUMENT'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
