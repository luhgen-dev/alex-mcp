"""MYDIN live contract fixtures and authenticated MCP integration (no paid AI)."""
import asyncio
import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_core as core
import mydin_prices as prices
from config import Settings
from context import ActorContext, use_actor
from mcp import Client

FIXTURE = json.loads((Path(__file__).parent / "fixtures/mydin_products_20261101.json").read_text())
ROW = next(p for p in FIXTURE["data"]["products"]["items"] if p["sku"] == "10010176")
ACTOR = ActorContext("test-user", "+600000000", ("FAMILY_SHARED",), "PRIVATE",
                     "test-chat", "DIRECT_DM", "test-message", (), "Asia/Kuala_Lumpur",
                     trusted_text="What is the current price of EcoBrown 2kg at Mydin?")


def product_response(row=ROW):
    return {"data": {"products": {"items": [copy.deepcopy(row)]}}}


def search_response(query="rice"):
    return {"success": True, "data": {"keyword": query, "page": 1, "pageSize": 5,
            "totalItems": 1, "skuCount": 1, "skus": ["10010176"]}}


class MydinPriceTests(unittest.TestCase):
    def test_one_pack_uses_displayed_price_not_search_suggestion_bulk_price(self):
        with patch.object(prices, "_get_json", return_value=product_response()):
            p = prices.product("10010176")["products"][0]
        self.assertEqual(p["selling_price"], "13.60")
        self.assertEqual(p["unit_price_for_quantity"], "13.60")
        self.assertEqual(p["title"], "EcoBrown Unpolished Brown Rice Original (2kg)")

    def test_bulk_tier_requires_quantity_and_reports_insufficient_stock(self):
        with patch.object(prices, "_get_json", return_value=product_response()):
            p = prices.product("10010176", 5)["products"][0]
        self.assertEqual(p["unit_price_for_quantity"], "13.52")
        self.assertEqual(p["total_for_quantity"], "67.60")
        self.assertFalse(p["sufficient_stock"])

    def test_fractional_bulk_rate_is_not_rounded_before_multiplication(self):
        row = copy.deepcopy(ROW)
        row["tier_prices"] = [{"qty": 12, "value": 7.833333}]
        with patch.object(prices, "_get_json", return_value=product_response(row)):
            p = prices.product("10010176", 12)["products"][0]
        self.assertEqual(p["unit_price_for_quantity"], "7.83")
        self.assertEqual(p["total_for_quantity"], "94.00")

    def test_search_refreshes_each_time_and_emits_source_timestamp(self):
        with patch.object(prices, "_get_json", side_effect=[search_response(), product_response()] * 2) as network:
            first = prices.search("rice")
            second = prices.search("rice")
        self.assertEqual(network.call_count, 4)
        self.assertFalse(first["cached"])
        self.assertIn("+08", first["checked_at_local"])
        self.assertGreaterEqual(second["checked_at"], first["checked_at"])
        self.assertEqual(first["products"][0]["url"], "https://mydin.my/product/ecobrown-b-rice-2kg-10010176")

    def test_wrong_sku_currency_slug_and_duplicate_are_rejected(self):
        for field, value in [("sku", "99999999"), ("url_key", "../../other"), ("name", "")]:
            row = copy.deepcopy(ROW)
            row[field] = value
            if field == "name": row["custom_productname"] = ""
            with self.subTest(field=field), patch.object(prices, "_get_json", return_value=product_response(row)):
                with self.assertRaises(prices.SourceUnavailable): prices.product("10010176")
        row = copy.deepcopy(ROW)
        row["price_range"]["minimum_price"]["final_price"]["currency"] = "SGD"
        with self.assertRaises(prices.SourceUnavailable): prices._normalise(row)
        with patch.object(prices, "_get_json", return_value={"data": {"products": {"items": [ROW, ROW]}}}):
            with self.assertRaises(prices.SourceUnavailable): prices.product("10010176")

    def test_nonfinite_zero_negative_or_bool_price_is_rejected(self):
        for value in [None, True, "NaN", "Infinity", -1, 0]:
            with self.subTest(value=value), self.assertRaises(prices.SourceUnavailable): prices._money(value)

    def test_search_empty_is_success_without_product_fetch(self):
        data = search_response()
        data["data"].update(totalItems=0, skuCount=0, skus=[])
        with patch.object(prices, "_get_json", return_value=data) as network:
            result = prices.search("rice")
        self.assertTrue(result["ok"])
        self.assertEqual(result["products"], [])
        self.assertEqual(network.call_count, 1)

    def test_brand_and_pack_search_omits_partial_matches(self):
        exact = copy.deepcopy(FIXTURE["data"]["products"]["items"][1])
        raw = {"data": {"products": {"items": [exact, ROW], "total_count": 100,
              "page_info": {"current_page": 1, "page_size": 5}}}}
        with patch.object(prices, "_get_json", return_value=raw) as network:
            result = prices.search("Myrasa 5 kg")
        self.assertEqual([p["sku"] for p in result["products"]], ["15145643"])
        self.assertEqual(result["partial_matches_omitted"], 1)
        self.assertTrue(result["more_results"])
        self.assertEqual(network.call_count, 1)

    def test_pack_matching_does_not_confuse_1kg_with_10kg(self):
        self.assertFalse(prices._terms("rice 1kg") <= prices._terms("Rice 10kg"))
        self.assertEqual(prices._terms("beras 5 kg"), prices._terms("rice 5kg"))

    def test_query_and_quantity_validation_precedes_network(self):
        with patch.object(prices, "_get_json") as network:
            for query in ["", "a", "milk\nsecret", "https://example.com", "x" * 121]:
                with self.assertRaises(ValueError): prices.search(query)
            for qty in [0, -1, 1.5, True, 10001]:
                with self.assertRaises(ValueError): prices.product("10010176", qty)
            with self.assertRaises(ValueError): prices.product("https://example.com")
        network.assert_not_called()

    def test_configurable_product_never_gets_exact_quantity_quote(self):
        row = copy.deepcopy(ROW)
        row["__typename"] = "ConfigurableProduct"
        row["price_range"]["maximum_price"]["final_price"]["value"] = 20
        row.pop("salable_quantity")
        with patch.object(prices, "_get_json", return_value=product_response(row)):
            p = prices.product("10010176", 5)["products"][0]
        self.assertTrue(p["requires_variant_selection"])
        self.assertNotIn("total_for_quantity", p)
        self.assertIsNone(p["in_stock"])

    def test_schema_drift_and_upstream_errors_fail_closed(self):
        for data in [{}, {"errors": [{"message": "secret upstream detail"}]}, {"data": {"products": {"items": None}}}]:
            with patch.object(prices, "_get_json", return_value=data):
                with self.assertRaises(prices.SourceUnavailable): prices.product("10010176")
        data = search_response("other query")
        with patch.object(prices, "_get_json", return_value=data):
            with self.assertRaises(prices.SourceUnavailable): prices.search("rice")

    def test_readonly_routing_for_english_malay_and_tamil_price_questions(self):
        for text in ["How much is Myrasa 5kg at Mydin?", "Harga susu di Mydin?", "Mydin பால் விலை என்ன?", "Check Mydin cooking oil price"]:
            with self.subTest(text=text):
                names = core.brain._select_tool_names(text)
                self.assertIn("search_mydin_prices", names)
                self.assertFalse(any(core.brain._is_mutating_tool(n) for n in names))

    def test_mcp_real_dispatch_hides_identity_and_preserves_public_read_in_dm_and_group(self):
        async def exercise():
            for kind in ["DIRECT_DM", "FAMILY_GROUP"]:
                with use_actor(replace(ACTOR, conversation_type=kind)):
                    async with Client(core.mcp) as client:
                        listed = await client.list_tools()
                        for name in ["search_mydin_prices", "get_mydin_product_price"]:
                            tool = next(t for t in listed.tools if t.name == name)
                            self.assertNotIn("actor", tool.input_schema.get("properties", {}))
                            self.assertFalse(core.brain._is_mutating_tool(name))
                        with patch.object(prices, "_get_json", return_value=product_response()):
                            result = await client.call_tool("get_mydin_product_price", {"sku": "10010176"})
                        self.assertFalse(result.is_error)
                        payload = json.loads(result.content[0].text)
                        self.assertEqual(payload["products"][0]["selling_price"], "13.60")
        with patch.object(core.mcp_server, "get_settings", return_value=Settings()):
            asyncio.run(exercise())

    def test_kill_switch_and_network_failure_return_honest_tool_error(self):
        async def exercise():
            with use_actor(ACTOR):
                async with Client(core.mcp) as client:
                    with patch.object(core.mcp_server, "get_settings", return_value=Settings(mydin_prices_enabled=False)), patch.object(prices, "_get_json") as network:
                        result = await client.call_tool("search_mydin_prices", {"query": "milk"})
                        self.assertTrue(result.is_error)
                        network.assert_not_called()
                    with patch.object(core.mcp_server, "get_settings", return_value=Settings()), patch.object(prices, "_get_json", side_effect=prices.SourceUnavailable("Cannot reach MYDIN now")):
                        result = await client.call_tool("search_mydin_prices", {"query": "milk"})
                        self.assertTrue(result.is_error)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
