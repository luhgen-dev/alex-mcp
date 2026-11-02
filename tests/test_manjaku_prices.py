"""Actual public storefront fixtures and authenticated MCP dispatch; no paid AI."""
import asyncio
import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_core as core
import manjaku_prices as prices
from config import Settings
from context import ActorContext, use_actor
from mcp import Client

FIXTURE = json.loads((Path(__file__).parent / "fixtures/manjaku_20261101.json").read_text())
ROWS = {row["handle"]: row for row in FIXTURE["products"]}
DIAPERS = "mimora-tape-nb64-s82-m72-l60-xl50"
TISSUE = "sofyroo-facial-tissue-80s-3ply"
SALE = "mimora-chlorine-free-tape-diapers-nb64_30100324"
ACTOR = ActorContext("test-user", "+600000000", ("FAMILY_SHARED",), "PRIVATE",
                     "test-chat", "DIRECT_DM", "test-message", (), "Asia/Kuala_Lumpur",
                     trusted_text="Check Manjaku diaper prices")


def page(row):
    return ('<script type="application/ld+json">' + json.dumps(row["schema"]) + '</script>'
            '<script>' + ''.join('otProductInfo.' + name + ' = ' + json.dumps(row[name]) + ';\n'
                                for name in ("handle", "title", "variants")) + '</script>')


def suggestions(handles=()):
    return ('<div id="shopline-section-predictive-search"><div class="predictive-search-results">'
            + ''.join('<a class="predictive-search__item link" href="/products/' + h + '">RM0.01</a>' for h in handles)
            + '<button class="predictive-search__term">Search</button></div></div>')


class ManjakuPriceTests(unittest.TestCase):
    def test_all_live_fixture_variants_parse_in_minor_units(self):
        for handle, row in ROWS.items():
            with self.subTest(handle=handle), patch.object(prices, "_get_html", return_value=page(row)):
                item = prices.product(handle)["products"][0]
                self.assertEqual(len(item["variants"]), len(row["variants"]))
                for actual, source in zip(item["variants"], row["variants"]):
                    self.assertEqual(actual["selling_price"], f"{source['price']/100:.2f}")
                    self.assertEqual(actual["available_online"], source["available"])

    def test_sale_price_is_distinct_from_comparison_price(self):
        with patch.object(prices, "_get_html", return_value=page(ROWS[SALE])):
            item = prices.product(SALE)["products"][0]
        variant = item["selected_variant"]
        self.assertEqual(variant["selling_price"], "18.90")
        self.assertEqual(variant["compare_at_price"], "34.90")

    def test_ambiguous_size_never_gets_selected_or_totalled(self):
        with patch.object(prices, "_get_html", return_value=page(ROWS[DIAPERS])):
            item = prices.product(DIAPERS, quantity=2)["products"][0]
        self.assertTrue(item["requires_variant_selection"])
        self.assertNotIn("selected_variant", item)
        self.assertNotIn("listed_items_subtotal", item)

    def test_carton_range_never_becomes_the_wrong_variant_price(self):
        handle = "-carton-bundle-sale-sofyroo-tissue"
        with patch.object(prices, "_get_html", return_value=page(ROWS[handle])):
            ambiguous = prices.product(handle)["products"][0]
            exact = prices.product(handle, "18076521603504230930582080", 2)["products"][0]
        self.assertEqual((ambiguous["price_min"], ambiguous["price_max"]), ("50.00", "290.00"))
        self.assertNotIn("listed_items_subtotal", ambiguous)
        self.assertEqual(exact["selected_variant"]["option"], "FACIAL 80S X 100PCK")
        self.assertEqual(exact["listed_items_subtotal"], "580.00")

    def test_leading_hyphen_in_real_carton_handle_is_valid_in_search(self):
        handle = "-carton-bundle-sale-sofyroo-tissue"
        with patch.object(prices, "_get_html", side_effect=[suggestions([handle]), page(ROWS[handle])]):
            result = prices.search("Sofyroo tissue")
        self.assertEqual(result["products"][0]["handle"], handle)

    def test_sold_out_variant_does_not_inherit_other_size_stock(self):
        with patch.object(prices, "_get_html", return_value=page(ROWS[DIAPERS])):
            item = prices.product(DIAPERS, "18075648584615584122832080")["products"][0]
        self.assertEqual(item["selected_variant"]["option"], "L60")
        self.assertFalse(item["selected_variant"]["available_online"])
        self.assertFalse(item["sufficient_stock"])
        self.assertNotIn("listed_items_subtotal", item)

    def test_exact_size_and_quantity_preserve_stock_warning_and_discount_exclusion(self):
        with patch.object(prices, "_get_html", return_value=page(ROWS[DIAPERS])):
            item = prices.product(DIAPERS, "18075648584615584122612080", 10000)["products"][0]
        self.assertEqual(item["selected_variant"]["option"], "S82")
        self.assertEqual(item["listed_items_subtotal"], "279000.00")
        self.assertFalse(item["sufficient_stock"])
        self.assertIn("promotions", item["subtotal_note"])

    def test_quantity_rule_does_not_make_invalid_subtotal(self):
        row = copy.deepcopy(ROWS[TISSUE])
        row["variants"][0]["quantity_rule"] = {"min": 4, "increment": 2, "max": 8}
        for quantity in (2, 5, 10):
            with self.subTest(quantity=quantity), patch.object(prices, "_get_html", return_value=page(row)):
                item = prices.product(TISSUE, quantity=quantity)["products"][0]
            self.assertFalse(item["quantity_allowed"])
            self.assertNotIn("listed_items_subtotal", item)

    def test_search_refreshes_product_prices_and_omits_partial_brand_pack_matches(self):
        def source(path):
            if path.startswith("/search/"): return suggestions([DIAPERS, SALE])
            return page(ROWS[path.removeprefix("/products/")])
        with patch.object(prices, "_get_html", side_effect=source) as network:
            result = prices.search("Mimora L60")
        self.assertEqual(network.call_count, 3)
        self.assertEqual([p["handle"] for p in result["products"]], [DIAPERS])
        self.assertEqual(result["partial_matches_omitted"], 1)
        self.assertEqual(result["products"][0]["price_min"], "27.90")
        self.assertFalse(result["cached"])
        self.assertIn("+08", result["checked_at_local"])
        self.assertIn("physical-store", result["search_scope"])
        self.assertIn("keyword=", result["search_url"])

    def test_empty_search_is_not_source_failure_or_store_absence(self):
        with patch.object(prices, "_get_html", return_value=suggestions()) as network:
            result = prices.search("Dutch Lady")
        self.assertEqual(result["products"], [])
        self.assertIn("not a complete catalogue", result["search_scope"])
        self.assertEqual(network.call_count, 1)
        with patch.object(prices, "_get_html", return_value="<html>Unavailable</html>"):
            with self.assertRaises(prices.SourceUnavailable): prices.search("milk")

    def test_missing_changed_conflicting_or_duplicate_structured_data_is_rejected(self):
        for kind in ("currency", "price", "availability", "handle", "duplicate", "missing"):
            row = copy.deepcopy(ROWS[TISSUE])
            if kind == "currency": row["schema"]["offers"][0]["priceCurrency"] = "SGD"
            if kind == "price": row["schema"]["offers"][0]["price"] = "1.00"
            if kind == "availability": row["variants"][0]["available"] = False
            if kind == "handle": row["handle"] = "unrelated"
            if kind == "duplicate": row["variants"].append(row["variants"][0])
            if kind == "missing": row["variants"][0].pop("quantity_rule")
            with self.subTest(kind=kind), patch.object(prices, "_get_html", return_value=page(row)):
                with self.assertRaises(prices.SourceUnavailable): prices.product(TISSUE)

    def test_foreign_variant_and_invalid_inputs_are_rejected(self):
        with patch.object(prices, "_get_html", return_value=page(ROWS[TISSUE])):
            with self.assertRaises(ValueError): prices.product(TISSUE, "18075648584615584122832080")
        with patch.object(prices, "_get_html") as network:
            for handle in ("../other", "https://example.com", "//example.com", "x?y", "x/y"):
                with self.assertRaises(ValueError): prices.product(handle)
            for query in ("", "a", "milk\nprivate", "https://example.com", "x"*121):
                with self.assertRaises(ValueError): prices.search(query)
            for quantity in (0, -1, True, 1.5, 10001):
                with self.assertRaises(ValueError): prices.product(TISSUE, quantity=quantity)
            with self.assertRaises(ValueError): prices.search("milk", limit=6)
        network.assert_not_called()

    def test_search_cannot_follow_external_or_malformed_links(self):
        for href in ("https://example.com/products/milk", "/products/../cart", "/products/milk?redirect=yes"):
            html = suggestions([TISSUE]).replace("/products/" + TISSUE, href)
            with self.subTest(href=href), patch.object(prices, "_get_html", return_value=html) as network:
                with self.assertRaises(prices.SourceUnavailable): prices.search("milk")
                self.assertEqual(network.call_count, 1)

    def test_retailer_routing_is_readonly_and_separate_from_mydin(self):
        for text in ("How much are Mimora diapers at Manjaku?", "Harga lampin di Manjaku?", "Manjaku பால் விலை என்ன?", "Check Manjaku wipes prices"):
            with self.subTest(text=text):
                names = core.brain._select_tool_names(text)
                self.assertIn("search_manjaku_prices", names)
                self.assertNotIn("search_mydin_prices", names)
                self.assertFalse(any(core.brain._is_mutating_tool(n) for n in names))
        names = core.brain._select_tool_names("Check Mydin cooking oil price")
        self.assertNotIn("search_manjaku_prices", names)
        self.assertIn("search_mydin_prices", names)

    def test_authenticated_mcp_dispatch_hides_actor_and_respects_disable(self):
        async def exercise():
            for kind in ("DIRECT_DM", "FAMILY_GROUP"):
                with use_actor(replace(ACTOR, conversation_type=kind)), patch.object(core.mcp_server, "get_settings", return_value=Settings()):
                    async with Client(core.mcp) as client:
                        for tool in (await client.list_tools()).tools:
                            if "manjaku" in tool.name:
                                self.assertNotIn("actor", tool.input_schema.get("properties", {}))
                                self.assertFalse(core.brain._is_mutating_tool(tool.name))
                        with patch.object(prices, "_get_html", return_value=page(ROWS[TISSUE])):
                            result = await client.call_tool("get_manjaku_product_price", {"handle": TISSUE, "quantity": 2})
                        self.assertFalse(result.is_error)
                        self.assertEqual(json.loads(result.content[0].text)["products"][0]["listed_items_subtotal"], "5.80")
                        with patch.object(core.mcp_server, "get_settings", return_value=Settings(manjaku_prices_enabled=False)), patch.object(prices, "_get_html") as network:
                            result = await client.call_tool("search_manjaku_prices", {"query": "milk"})
                            self.assertTrue(result.is_error)
                            network.assert_not_called()
                        with patch.object(prices, "_get_html", side_effect=prices.SourceUnavailable("Cannot reach Manjaku now")):
                            result = await client.call_tool("search_manjaku_prices", {"query": "milk"})
                            self.assertTrue(result.is_error)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
