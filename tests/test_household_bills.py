"""Synthetic portal contracts + authenticated screenshot/scheduler integration."""
import asyncio
import base64
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import test_core as core
import db
import media
import household_bills as bills
import utility_portals as portals
from config import Settings
from context import with_action_key, use_actor
from mcp import Client

TNB_HTML = '''<p>1 Accounts Home 123456789012</p><div>Billing Summary Bill Date 21-Oct-2026 My Previous Balance RM 0.00 My Current Charges RM 75.32 Rounding Adjustment -RM 0.02 I've cleared all bills RM 0.00</div><div>Payment History LAST PAYMENT AMOUNT RM 75.30 LAST PAYMENT DATE 23-Oct-2026</div><button onclick="PrintPDF('ABCD1234')">View Bill</button><script>function PrintPDF(x){ var IsOwnedAccount = "False"; }</script>'''
SAJ_HTML = '''<main>Home Owner <div class="bill-account-id">12345678-N1234567</div> Water Usage (m³) 16 Pay Before 06 Nov 2026 Due In 4 days Amount Due (RM) 0.00 Recent Payments Date 23/10/2026 Reference No. R001 Amount RM 66.40</main>'''
SAJ_HISTORY = '''<main>Home Owner <div class="bill-account-id">12345678-N1234567</div> Date Type of Transaction Bill No. Reference No. Amount Action Oct 2026 06/10/2026 Bill B26102 - RM 66.40 Sep 2026 05/09/2026 Bill B26091 - RM 70.55</main>'''
SETTINGS = Settings(husband_phone='+60111111111', wife_phone='+60222222222', digi_husband_line='+60123456789', digi_wife_line='+60129876543', tnb_account='123456789012', saj_account='12345678-N1234567')

class PortalContractTests(unittest.TestCase):
    def test_tnb_paid_bill_separates_charges_rounding_and_balance(self):
        p = portals.parse_tnb(TNB_HTML,'123456789012')
        self.assertEqual(p['invoice_amount'],'75.30'); self.assertEqual(p['outstanding'],'0.00')
        self.assertIsNone(p['due_date']); self.assertEqual(p['last_payment_date'],'2026-10-23')
    def test_tnb_unknown_zero_account_mismatch_multicard_and_expired_fail_closed(self):
        for html, account in ((TNB_HTML.replace("I've cleared all bills",'Unknown balance'),'123456789012'),(TNB_HTML,'other'),(TNB_HTML.replace('1 Accounts','2 Accounts'),'123456789012'),(TNB_HTML+' You have been logged out','123456789012')):
            with self.subTest(html=html[:20]), self.assertRaises(portals.PortalUnavailable): portals.parse_tnb(html,account)
    def test_saj_zero_due_is_not_zero_invoice(self):
        p = portals.parse_saj(SAJ_HTML,SAJ_HISTORY,'12345678-N1234567')
        self.assertEqual(p['invoice_amount'],'66.40'); self.assertEqual(p['outstanding'],'0.00')
        self.assertEqual(p['due_date'],'2026-11-06'); self.assertEqual(len(p['history']),2)
        self.assertFalse(p['pdf_supported'])
    def test_saj_does_not_mix_accounts_or_accept_empty_history(self):
        for history in ('',SAJ_HISTORY.replace('12345678-N1234567','other')):
            with self.assertRaises(portals.PortalUnavailable): portals.parse_saj(SAJ_HTML,history,'12345678-N1234567')
    def test_redirect_cannot_exfiltrate_credentials(self):
        p = portals.Portal('saj'); r = Mock(status_code=307,headers={'Location':'https://attacker.invalid/steal'})
        with patch.object(p.session,'request',return_value=r) as request:
            with self.assertRaises(portals.PortalUnavailable): p.html('POST',portals.SAJ+'/customer/account/loginPost/',data={'password':'secret'})
            self.assertEqual(request.call_count,1)
        p.close()
    def test_same_origin_redirect_then_evil_url_is_rejected_before_request(self):
        p = portals.Portal('tnb'); r = Mock(status_code=302,headers={'Location':'https://evil.invalid/'})
        with patch.object(p.session,'request',return_value=r) as request:
            with self.assertRaises(portals.PortalUnavailable): p.html('GET',portals.TNB+'/AccountManagement/IndividualDashboard')
            self.assertEqual(request.call_count,1)
        p.close()
    def test_pdf_requires_pdf_bytes_and_unambiguous_button(self):
        p = portals.Portal('tnb')
        with patch.object(p,'html',return_value='ABCDEF123456'), patch.object(p,'request',return_value=b'<html>login</html>'):
            with self.assertRaises(portals.PortalUnavailable): p.pdf(TNB_HTML)
        p.close()
    def test_secrets_not_in_settings_repr(self):
        self.assertNotIn('top-secret',repr(replace(SETTINGS,tnb_password='top-secret',saj_password='top-secret')))

class HouseholdBillTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: core.AlexCoreTests.setUpClass())
    claim = core.AlexCoreTests.claim
    actor = core.AlexCoreTests.actor
    def setUp(self):
        core.AlexCoreTests.setUp(self)
        clock = patch.object(bills.runtime_clock,'now_utc',return_value=datetime(2026,11,1,12,tzinfo=timezone.utc))
        clock.start(); self.addCleanup(clock.stop)
        self.claim('bill','+60111111111','Here is my Digi bill screenshot')
        mid = media.save_media('bill','IMAGE','image/png',base64.b64encode(b'synthetic-image').decode())
        self.a = with_action_key(self.actor('bill','+60111111111',[mid]),'bill-save')
        with db.connect() as c: c.execute('UPDATE media_objects SET ocr_text=? WHERE media_id=?',('CelcomDigi 0123456789 Check Bill Details Current bill RM 454.72 Total due RM 0.00',mid))
        self.patch = patch.object(bills,'get_settings',return_value=SETTINGS); self.patch.start(); self.addCleanup(self.patch.stop)
    def save(self, **kw):
        return bills.record_digi(self.a,'husband','2026-10-16','454.72',outstanding='0.00',due_date='2026-11-15',**kw)
    def test_screenshot_idempotent_and_not_expense(self):
        first,second = self.save(),self.save()
        self.assertEqual(first['record_id'],second['record_id'])
        with db.connect() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM financial_events').fetchone()[0],0)
        out = bills.list_records(self.a,'2026-10')
        self.assertEqual(out['bills'][0]['invoice_amount'],'454.72')
        self.assertEqual(out['bills'][0]['outstanding'],'0.00')
        self.assertFalse(out['bills'][0]['live'])
        self.assertIn({'provider':'digi','person':'wife'},out['missing'])
    def test_wrong_line_unreadable_missing_media_and_sms_rejected(self):
        with self.assertRaises(ValueError): bills.record_digi(self.a,'wife','2026-10-16','454.72')
        with self.assertRaises(ValueError): self.save(readable=False)
        with self.assertRaises(ValueError): bills.record_digi(replace(self.a,media_ids=()),'husband','2026-10-16','454.72')
        with db.connect() as c: c.execute("UPDATE media_objects SET ocr_text='SMS 2901 CelcomDigi'")
        with self.assertRaises(ValueError): self.save()
    def test_private_bill_does_not_leak_to_shared(self):
        self.a = replace(self.a,trusted_text='private Digi bill',read_scope='private')
        self.save()
        out = bills.list_records(replace(self.a,read_scope='family'),'2026-10')
        self.assertEqual(out['bills'],[])
    def test_account_reconfiguration_does_not_relabel_old_bill(self):
        self.save()
        with patch.object(bills,'get_settings',return_value=replace(SETTINGS,digi_husband_line='+60125555555')):
            out = bills.list_records(self.a,'2026-10','digi')
        self.assertFalse(out['bills'][0]['matches_current_account'])
        self.assertIn({'provider':'digi','person':'husband'},out['missing'])
    def test_current_missing_month_not_replaced_with_previous_month(self):
        self.save(); out=bills.list_records(self.a,'2026-11','digi')
        self.assertEqual(out['bills'],[]); self.assertEqual(len(out['missing']),2)
    def test_prompt_once_after_16th_local_time_survives_restarts_and_no_spouse(self):
        for when,expected in [('2026-11-16T10:59:00+00:00',0),('2026-11-16T11:00:00+00:00',1),('2026-11-17T11:00:00+00:00',0)]:
            with patch.object(bills.runtime_clock,'now_utc',return_value=datetime.fromisoformat(when)):
                self.assertEqual(bills.queue_digi_prompt(),expected)
        with db.connect() as c:
            rows=c.execute("SELECT * FROM outbound_messages WHERE context_kind='DIGI_SCREENSHOT_PROMPT'").fetchall()
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['conversation_id'],'60111111111@s.whatsapp.net')
    def test_disabled_prompt_never_queues(self):
        with patch.object(bills,'get_settings',return_value=replace(SETTINGS,digi_screenshot_reminder_enabled=False)):
            self.assertEqual(bills.queue_digi_prompt(),0)
    def test_portal_refresh_saves_amount_and_only_downloads_when_requested(self):
        settings=replace(SETTINGS,tnb_portal_enabled=True,tnb_email='test@example.invalid',tnb_password='secret')
        with patch.object(bills,'get_settings',return_value=settings), patch.object(bills,'Portal') as ctor:
            portal=ctor.return_value; portal.read.return_value=(portals.parse_tnb(TNB_HTML,SETTINGS.tnb_account),TNB_HTML)
            result=bills.refresh(self.a,'tnb')
            self.assertTrue(result['live']); portal.pdf.assert_not_called()
            self.assertEqual(bills.list_records(self.a,'2026-10','tnb')['bills'][0]['invoice_amount'],'75.30')
    def test_refresh_failure_does_not_write_zero(self):
        settings=replace(SETTINGS,tnb_portal_enabled=True,tnb_email='test@example.invalid',tnb_password='secret')
        with patch.object(bills,'get_settings',return_value=settings), patch.object(bills,'Portal') as ctor:
            ctor.return_value.login.side_effect=portals.PortalUnavailable('Login unavailable')
            result=bills.refresh(self.a,'tnb')
            self.assertFalse(result['live']); self.assertEqual(result['status'],'unavailable')
        with db.connect() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM household_bill_records').fetchone()[0],0)
    def test_authenticated_monthly_tool_includes_digi_and_grocery_without_double_count(self):
        self.save()
        async def run():
            with use_actor(self.a):
                async with Client(core.mcp) as client:
                    out=await client.call_tool('household_payments_summary',{'period':'2026-10','refresh_utilities':False})
                    return out
        out=asyncio.run(run())
        self.assertFalse(out.is_error)
        result=out.structured_content or json.loads(out.content[0].text)
        self.assertIn('groceries',result); self.assertIsNone(result['combined_total'])
        self.assertEqual(result['provider_bills']['bills'][0]['invoice_amount'],'454.72')

    def test_screenshot_cannot_silently_change_a_readable_amount(self):
        self.save()
        with self.assertRaises(ValueError): bills.record_digi(self.a,'husband','2026-10-16','999.99')
        with db.connect() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM household_bill_records').fetchone()[0],1)
    def test_pdf_review_does_not_send_document_unless_requested(self):
        settings=replace(SETTINGS,tnb_portal_enabled=True,tnb_email='test@example.invalid',tnb_password='secret')
        with patch.object(bills,'get_settings',return_value=settings), patch.object(bills,'Portal') as ctor, patch.object(bills.subprocess,'run',return_value=Mock(stdout=b'Item charges: sample evidence')):
            portal=ctor.return_value; portal.read.return_value=(portals.parse_tnb(TNB_HTML,SETTINGS.tnb_account),TNB_HTML)
            portal.pdf.return_value=b'%PDF-1.4 sample statement'
            result=bills.refresh(self.a,'tnb',analyse_pdf=True)
            self.assertIn('statement_text_untrusted',result); self.assertNotIn('_attachments',result)
            result=bills.refresh(self.a,'tnb',include_pdf=True)
            self.assertEqual(result['_attachments'][0]['mime_type'],'application/pdf')
    def test_private_scope_cannot_start_shared_portal_login(self):
        with patch.object(bills,'Portal') as ctor:
            with self.assertRaises(PermissionError): bills.refresh(replace(self.a,read_scope='private'),'tnb')
            ctor.assert_not_called()
