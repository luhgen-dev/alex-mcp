"""Read-only MYDIN Online prices, using the requests made by its public frontend.

Search suggestions contain quantity-tier prices. Resolve search SKUs through
Magento and use final_price for one pack; never quote the suggestion feed.
No cookies, account credentials, shopping records, or cached prices are used.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo
import catalog_keywords

SEARCH_BASE = "https://mymgtbe.mydin.my/rest/V1/vsf/getSearchSkusPaged/"
PRODUCTS_URL = "https://myapi.mydin.my/magento/products"
MAX_BYTES = 1_500_000
TIMEOUT = 12
SKU = re.compile(r"[0-9]{5,20}\Z")
# Fields and products-custom-query are present in MYDIN's public JS bundle.
FIELDS = """items { sku name custom_productname url_key
 price_range {
  minimum_price { final_price { currency value } regular_price { currency value } }
  maximum_price { final_price { currency value } regular_price { currency value } }
 }
 tier_prices { qty value }
 ... on SimpleProduct { quantity salable_quantity }
} """


class SourceUnavailable(ValueError):
    """Expected upstream failure; contains no response body or credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceUnavailable("MYDIN redirected the request; current prices unavailable.")


def _get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Alex-MydinPrice/1.0 (read-only)",
                                    "Accept": "application/json"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise SourceUnavailable("MYDIN did not return a successful price response.")
            if response.headers.get_content_type() != "application/json":
                raise SourceUnavailable("MYDIN returned an unexpected response type.")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SourceUnavailable("MYDIN response exceeded the size limit.")
        value = json.loads(data)
        # MYDIN's search endpoint currently returns a JSON-encoded JSON string.
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("object expected")
        return value
    except HTTPError as exc:
        raise SourceUnavailable(f"MYDIN returned HTTP {exc.code}; current prices unavailable.") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise SourceUnavailable("Cannot reach MYDIN now; current prices unavailable.") from exc
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, SourceUnavailable):
            raise
        raise SourceUnavailable("MYDIN returned an invalid price response.") from exc


def _money(value, fractional: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise SourceUnavailable("Invalid MYDIN price.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise SourceUnavailable("Invalid MYDIN price.") from exc
    if (not number.is_finite() or not 0 < number <= 100000
            or (not fractional and number != number.quantize(Decimal("0.01")))):
        raise SourceUnavailable("Invalid MYDIN price.")
    return number


def _integer(value, minimum=0, maximum=1_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SourceUnavailable("Invalid MYDIN quantity or result count.")
    return value


def _sku(value) -> str:
    if not isinstance(value, str) or not SKU.fullmatch(value):
        raise ValueError("A valid MYDIN iSKU is required.")
    return value


def _normalise(row: dict) -> dict:
    try:
        sku = _sku(row["sku"])
        slug = row["url_key"]
        title = row.get("custom_productname") or row["name"]
        if (not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,240}", slug)
                or not slug.endswith("-" + sku)):
            raise ValueError("product link mismatch")
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValueError("invalid title")
        prices = row["price_range"]
        minimum = prices["minimum_price"]
        maximum = prices["maximum_price"]
        amounts = [minimum["final_price"], maximum["final_price"], minimum["regular_price"]]
        if any(x["currency"] != "MYR" for x in amounts):
            raise ValueError("wrong currency")
        low, high, regular = [_money(x["value"]) for x in amounts]
        if high < low or regular < low:
            raise ValueError("invalid price range")
        simple = row.get("__typename") == "SimpleProduct"
        if simple and low != high:
            raise ValueError("ambiguous simple product price")
        stock = row.get("salable_quantity")
        if stock is not None:
            stock = _integer(stock)
        tiers = []
        # Parent/configurable tiers cannot be applied to an unspecified variant.
        if simple:
            for tier in row.get("tier_prices") or []:
                qty = _integer(tier["qty"], 1)
                price = _money(tier["value"], fractional=True)
                if price <= low:
                    tiers.append({"minimum_quantity": qty,
                                  "unit_price": str(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                                  "unit_price_unrounded": str(price)})
        return {"sku": sku, "title": title.strip(), "currency": "MYR",
                "selling_price": f"{low:.2f}", "maximum_price": f"{high:.2f}",
                "regular_price": f"{regular:.2f}", "on_sale": low < regular,
                "requires_variant_selection": not simple,
                "stock_quantity": stock, "in_stock": stock > 0 if stock is not None else None,
                "quantity_tiers": sorted(tiers, key=lambda t: t["minimum_quantity"]),
                "url": "https://mydin.my/product/" + slug}
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SourceUnavailable):
            raise
        raise SourceUnavailable("MYDIN product data could not be verified.") from exc


def _products(skus: list[str]) -> list[dict]:
    if not skus:
        return []
    if len(skus) > 20:
        raise ValueError("At most 20 products per lookup.")
    for sku in skus:
        _sku(sku)
    body = [{"filter": {"sku": {"in": skus}}, "pageSize": len(skus), "currentPage": 1},
            {"products": "products-custom-query", "metadata": {"fields": FIELDS}}, None]
    result = _get_json(PRODUCTS_URL + "?" + urlencode({"body": json.dumps(body, separators=(",", ":"))}))
    try:
        if result.get("errors"):
            raise ValueError("upstream errors")
        rows = result["data"]["products"]["items"]
        if not isinstance(rows, list) or len(rows) > len(skus):
            raise ValueError("unexpected product set")
        products = [_normalise(row) for row in rows]
        by_sku = {p["sku"]: p for p in products}
        if len(by_sku) != len(products) or set(by_sku) - set(skus):
            raise ValueError("mismatched or duplicate SKU")
        return [by_sku[sku] for sku in skus if sku in by_sku]
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SourceUnavailable):
            raise
        raise SourceUnavailable("MYDIN product response could not be verified.") from exc


def _envelope(products, started, local_timezone):
    now = datetime.now(timezone.utc)
    return {"ok": True, "source": "MYDIN Online", "source_kind": "first_party_live_api",
            "checked_at": now.isoformat(),
            "checked_at_local": now.astimezone(ZoneInfo(local_timezone)).strftime("%d %b %Y, %I:%M %p %Z"),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "cached": False, "currency": "MYR",
            "reference": "MYDIN online selling prices, used as the Mutiara Rini shopping reference.",
            "products": products}


def _terms(text: str) -> set[str]:
    text = catalog_keywords.normalize(text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(kg|gm|g|ml|litres?|liters?|l)\b", r"\1\2", text.casefold())
    aliases = {"beras": "rice", "susu": "milk", "minyak": "oil", "masak": "cooking"}
    return {aliases.get(t, t) for t in re.findall(r"\d+(?:\.\d+)?(?:kg|gm|g|ml|l)\b|[^\W_]+", text)}


def _specific_search(query, limit, page, started, local_timezone):
    # The storefront SKU search is loose OR matching: "Myrasa 5kg" starts
    # with creamer. Its Magento search ranks the combined phrase first. Use
    # that documented frontend parameter and require every name/size term.
    fields = "total_count page_info { current_page page_size total_pages } " + FIELDS
    body = [{"search": query, "pageSize": limit, "currentPage": page},
            {"products": "products-custom-query", "metadata": {"fields": fields}}, None]
    raw = _get_json(PRODUCTS_URL + "?" + urlencode({"body": json.dumps(body, separators=(",", ":"))}))
    try:
        if raw.get("errors"):
            raise ValueError("upstream errors")
        data = raw["data"]["products"]
        rows = data["items"]
        total = _integer(data["total_count"])
        if (not isinstance(rows, list) or len(rows) > limit or total < len(rows)
                or data["page_info"]["current_page"] != page
                or data["page_info"]["page_size"] != limit):
            raise ValueError("unexpected search page")
        normalised = [_normalise(row) for row in rows]
        if len({p["sku"] for p in normalised}) != len(normalised):
            raise ValueError("duplicate SKU")
        wanted = _terms(query)
        matches = [p for p, row in zip(normalised, rows)
                   if wanted <= _terms(p["title"] + " " + row.get("name", ""))
                   and catalog_keywords.relevant(query, p['title'])]
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUnavailable("MYDIN search response could not be verified.") from exc
    response = _envelope(matches, started, local_timezone)
    response.update(query=query, page=page, source_total_results=total,
                    matching_results_on_page=len(matches), more_results=page * limit < total,
                    partial_matches_omitted=len(rows) - len(matches),
                    search_note="All product-name/pack-size terms must match. No matches on this page does not prove catalogue-wide absence; use the next page or refine the product name.")
    return response


def search(query: str, limit: int = 5, page: int = 1,
           local_timezone: str = "Asia/Kuala_Lumpur") -> dict:
    """Public catalogue search. No personal records or user identity sent upstream."""
    if (not isinstance(query, str) or not 2 <= len(query.strip()) <= 120
            or any(ord(c) < 32 for c in query) or "://" in query):
        raise ValueError("Use a product name, brand and pack size (2–120 characters).")
    if type(limit) is not int or not 1 <= limit <= 20 or type(page) is not int or not 1 <= page <= 100:
        raise ValueError("limit must be 1–20 and page must be 1–100.")
    started = time.monotonic()
    query = catalog_keywords.normalize(query)
    if len(_terms(query)) > 1:
        return _specific_search(query, limit, page, started, local_timezone)
    result = _get_json(SEARCH_BASE + quote(query, safe="") + "?" + urlencode({"page": page, "pageSize": limit}))
    try:
        if result.get("success") is not True:
            raise ValueError("search unsuccessful")
        data = result["data"]
        skus = data["skus"]
        total = _integer(data["totalItems"])
        if (not isinstance(skus, list) or len(skus) > limit or len(set(skus)) != len(skus)
                or data["page"] != page or data["pageSize"] != limit or data["keyword"] != query
                or data["skuCount"] != len(skus) or total < len(skus)):
            raise ValueError("unexpected search result")
        for sku in skus:
            _sku(sku)
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUnavailable("MYDIN search response could not be verified.") from exc
    verified_products = _products(skus)
    products = [p for p in verified_products if _terms(query) <= _terms(p['title'])
                and catalog_keywords.relevant(query, p['title'])]
    response = _envelope(products, started, local_timezone)
    response.update(query=query, page=page, total_results=total,
                    more_results=page * limit < total,
                    partial_matches_omitted=len(verified_products)-len(products),
                    unavailable_skus=[sku for sku in skus if sku not in {p["sku"] for p in verified_products}])
    return response


def product(sku: str, quantity: int = 1, local_timezone: str = "Asia/Kuala_Lumpur") -> dict:
    _sku(sku)
    if type(quantity) is not int or not 1 <= quantity <= 10000:
        raise ValueError("Quantity must be a whole number of packs from 1 to 10000.")
    started = time.monotonic()
    products = _products([sku])
    if products and not products[0]["requires_variant_selection"]:
        p = products[0]
        price = min([Decimal(p["selling_price"])] + [Decimal(t["unit_price_unrounded"])
                    for t in p["quantity_tiers"] if t["minimum_quantity"] <= quantity])
        p.update(requested_quantity=quantity,
                 unit_price_for_quantity=str(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                 total_for_quantity=str((price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                 total_note="Items-only estimate using the unrounded tier rate; checkout rounding and delivery excluded.",
                 sufficient_stock=p["stock_quantity"] >= quantity if p["stock_quantity"] is not None else None)
    result = _envelope(products, started, local_timezone)
    result["found"] = bool(products)
    return result
