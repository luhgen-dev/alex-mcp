"""Queue, stale-result races, privacy and historical totals; no external requests."""
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
import test_core as core
import grocery_prices as gp
import services
import db
from config import Settings
from context import with_action_key

PRICE = {"status": "priced", "line_total": "27.20", "unit_price": "13.60", "retailer": "mydin", "estimate": True}

class GroceryEnrichmentTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: core.AlexCoreTests.setUpClass())
    claim = core.AlexCoreTests.claim
    actor = core.AlexCoreTests.actor
    def setUp(self):
        core.AlexCoreTests.setUp(self)
        self.claim("grocery", "+60111111111", "add rice to grocery list")
        self.a = with_action_key(self.actor("grocery", "+60111111111"), "grocery-add")
    def add(self, name="Rice brand 2kg", quantity="2"):
        return services.add_shopping_item(self.a, name, quantity)["item_id"]
    def count(self, table):
        with db.connect() as c:
            return c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    def test_add_and_edit_never_call_network_and_plain_list_hides_price(self):
        with patch.object(gp, "lookup", side_effect=AssertionError("network")):
            item = self.add()
            services.update_shopping_item(self.a, item, quantity="3")
        self.assertEqual(self.count("grocery_price_jobs"), 1)
        self.assertEqual(self.count("grocery_item_history"), 2)
        self.assertNotIn("price", services.list_shopping_items(self.a)["items"][0])
    def test_retry_of_add_is_idempotent(self):
        self.add(); self.add()
        self.assertEqual(self.count("grocery_item_history"), 1)
    def test_worker_saves_quote_without_expense_or_message(self):
        self.add()
        with patch.object(gp, "lookup", return_value=PRICE):
            self.assertTrue(gp.process_one())
        out = services.list_shopping_items(self.a, include_prices=True)
        self.assertEqual(out["estimated_total"], "27.20")
        self.assertEqual(self.count("financial_events"), 0)
        self.assertEqual(self.count("outbound_messages"), 0)
    def test_edit_during_network_discards_old_quote(self):
        item = self.add()
        def raced(*a):
            services.update_shopping_item(self.a, item, item="Milk 1L")
            return PRICE
        with patch.object(gp, "lookup", side_effect=raced):
            gp.process_one()
        self.assertEqual(self.count("grocery_price_quotes"), 0)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT state FROM grocery_price_jobs").fetchone()[0], "QUEUED")
    def test_purchase_during_network_discards_and_stops_job(self):
        item = self.add()
        def raced(*a):
            services.update_shopping_item(self.a, item, status="bought")
            return PRICE
        with patch.object(gp, "lookup", side_effect=raced): gp.process_one()
        self.assertEqual(self.count("grocery_price_quotes"), 0)
        self.assertEqual(self.count("grocery_price_jobs"), 0)
    def test_expired_worker_lease_resumes_after_restart(self):
        self.add()
        with db.connect() as c:
            c.execute("UPDATE grocery_price_jobs SET state='RUNNING',lease_until='2020-01-01'")
        with patch.object(gp, "lookup", return_value=PRICE): self.assertTrue(gp.process_one())
    def test_disabled_background_is_noop(self):
        self.add()
        with patch.object(gp, "get_settings", return_value=Settings(grocery_background_prices=False)), patch.object(gp, "lookup") as lookup:
            self.assertFalse(gp.process_one()); lookup.assert_not_called()
    def test_generic_ambiguous_never_gets_an_exact_price(self):
        row = {"item_name": "rice", "quantity": "1", "notes": None}
        with patch.object(gp.mydin_prices, "search", return_value={"products": [{"title": "Any rice"}], "checked_at": "now"}), patch.object(gp, "get_settings", return_value=Settings(manjaku_prices_enabled=False)):
            self.assertEqual(gp.lookup(row)["status"], "needs_choice")
        for quantity in ("2kg", "half", "0", "1.5", "-2"):
            self.assertIsNone(gp.pack_count(quantity))
    def test_private_quotes_not_visible_in_family(self):
        private = replace(self.a, trusted_text="private add rice", read_scope="private")
        item = services.add_shopping_item(private, "Private rice", "1")["item_id"]
        with self.assertRaises(PermissionError): gp.select_product(replace(self.a, read_scope="family"), item, "mydin", "123")
        self.assertEqual(services.list_shopping_items(replace(self.a, read_scope="family"), include_prices=True)["items"], [])
    def test_purchase_history_not_repriced_or_counted_again_next_month(self):
        with patch.object(gp.runtime_clock, "now_utc", return_value=datetime(2026,10,20,tzinfo=timezone.utc)):
            item = self.add()
            with patch.object(gp, "lookup", return_value=PRICE): gp.process_one()
            services.update_shopping_item(self.a, item, status="bought")
        october = gp.monthly_summary(self.a, "2026-10")
        self.assertEqual(october["purchased_list_reference_estimate"], "27.20")
        self.assertEqual(october["recorded_actual_spending"], [])
        self.assertEqual(gp.monthly_summary(self.a, "2026-11")["purchased_list_reference_estimate"], "0.00")
    def test_failed_refresh_preserves_labelled_old_reference(self):
        self.add()
        with patch.object(gp, "lookup", return_value=PRICE): gp.process_one()
        with db.connect() as c:
            c.execute("UPDATE grocery_price_jobs SET state='QUEUED',next_try='2000-01-01'")
        with patch.object(gp, "lookup", return_value={"status":"unavailable"}): gp.process_one()
        price = services.list_shopping_items(self.a, include_prices=True)["items"][0]["price"]
        self.assertEqual(price["line_total"], "27.20")
        self.assertEqual(price["refresh_error"], "source_unavailable")

    def test_routing_distinguishes_list_quote_monthly_and_bill_capture(self):
        for prompt, expected in (("Show grocery list", "list_shopping_items"),("Show grocery list with prices", "list_shopping_items"),("Check rice price", "search_mydin_prices"),("What's the grocery amount this month?", "grocery_monthly_summary"),("Check TNB bill", "check_household_bill"),("What are this month's payments?", "household_payments_summary")):
            with self.subTest(prompt=prompt): self.assertIn(expected,core.brain._select_tool_names(prompt))
        selected=core.brain._select_tool_names("Save my Digi bill screenshot",["CelcomDigi Check Bill Details"])
        self.assertIn("record_digi_bill",selected); self.assertNotIn("log_expense",selected)

    def test_priced_list_truncation_cannot_claim_complete_total(self):
        self.add()
        services.add_shopping_item(with_action_key(self.a,'grocery-second'),'Milk 1L','1')
        with patch.object(gp,'lookup',return_value=PRICE): gp.process_one(); gp.process_one()
        out=services.list_shopping_items(self.a,limit=1,include_prices=True)
        self.assertTrue(out['list_truncated']); self.assertFalse(out['complete'])
    def test_editing_purchase_note_does_not_erase_purchase_time_reference(self):
        with patch.object(gp.runtime_clock,'now_utc',return_value=datetime(2026,10,20,tzinfo=timezone.utc)):
            item=self.add()
            with patch.object(gp,'lookup',return_value=PRICE): gp.process_one()
            services.update_shopping_item(self.a,item,status='bought')
        with patch.object(gp.runtime_clock,'now_utc',return_value=datetime(2026,10,21,tzinfo=timezone.utc)):
            services.update_shopping_item(self.a,item,notes='stored in pantry')
        self.assertEqual(gp.monthly_summary(self.a,'2026-10')['purchased_list_reference_estimate'],'27.20')
