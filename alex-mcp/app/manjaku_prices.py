"""Read-only Manjaku Online prices from its public Shopline storefront.

Use the site's predictive search, then refresh each product's embedded variant
JSON and cross-check its MYR structured offers. Never execute page JavaScript.
Cart/member promotions and physical-store prices are not verified here.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

BASE = "https://www.manjaku.com"
MAX_BYTES = 2_000_000
TIMEOUT = 12
HANDLE = re.compile(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,239}\Z")
VARIANT = re.compile(r"[0-9]{5,32}\Z")
NOTICE = ("Manjaku Online listed prices only; physical-store prices/stock can differ. "
          "Cart, bundle, member and voucher discounts and delivery are not verified.")


class SourceUnavailable(ValueError):
    """An unavailable or changed public source, without raw upstream details."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceUnavailable("Manjaku redirected the request; current prices unavailable.")


def _get_html(path: str) -> str:
    request = Request(BASE + path, headers={"User-Agent": "Alex-ManjakuPrice/1.0 (read-only)",
                                           "Accept": "text/html"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
            if response.status != 200 or response.headers.get_content_type() != "text/html":
                raise SourceUnavailable("Manjaku returned an unexpected page response.")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SourceUnavailable("Manjaku page exceeded the size limit.")
        return data.decode("utf-8")
    except HTTPError as exc:
        raise SourceUnavailable(f"Manjaku returned HTTP {exc.code}; current prices unavailable.") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise SourceUnavailable("Cannot reach Manjaku now; current prices unavailable.") from exc
    except UnicodeError as exc:
        raise SourceUnavailable("Manjaku returned an invalid page.") from exc


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.links = []
        self.search_root = False
        self.search_results = False
        self.search_button = False
        self._script = None
        self._data = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "script":
            self._script = attrs
            self._data = []
        self.search_root |= attrs.get("id") == "shopline-section-predictive-search"
        self.search_results |= "predictive-search-results" in classes
        self.search_button |= tag == "button" and "predictive-search__term" in classes
        if tag == "a" and "predictive-search__item" in classes:
            self.links.append(attrs.get("href", ""))

    def handle_data(self, data):
        if self._script is not None:
            self._data.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append((self._script, "".join(self._data)))
            self._script = None


def _handle(value: str) -> str:
    if not isinstance(value, str) or not HANDLE.fullmatch(value) or ".." in value:
        raise ValueError("Use an exact Manjaku product handle returned by search.")
    return value


def _text(value, maximum=300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Invalid catalogue text")
    return " ".join(value.split())


def _integer(value, minimum=0, maximum=10_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid catalogue integer")
    return value


def _money(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Invalid price")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid price") from exc
    if not amount.is_finite() or not 0 < amount <= 100000 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Invalid price")
    return amount


def _assignment(scripts, name):
    # Decode only the JSON literal after the observed assignment, never JS.
    pattern = re.compile(r"\botProductInfo\." + re.escape(name) + r"\s*=\s*")
    values = []
    for _, source in scripts:
        for match in pattern.finditer(source):
            value, end = json.JSONDecoder().raw_decode(source[match.end():])
            if not source[match.end() + end:].lstrip().startswith(";"):
                raise ValueError("Non-literal product assignment")
            values.append(value)
    if len(values) != 1:
        raise ValueError("Missing or ambiguous product data")
    return values[0]


def _read_product(handle: str) -> dict:
    page = _Page()
    page.feed(_get_html("/products/" + handle))
    try:
        schemas = []
        for attrs, source in page.scripts:
            if attrs.get("type") == "application/ld+json":
                value = json.loads(source)
                if isinstance(value, dict) and value.get("@type") == "Product":
                    schemas.append(value)
        if len(schemas) != 1:
            raise ValueError("Product schema missing")
        schema = schemas[0]
        url = BASE + "/products/" + handle
        title = _text(schema["name"])
        if schema["url"] != url or _assignment(page.scripts, "handle") != handle:
            raise ValueError("Product identity mismatch")
        if _text(_assignment(page.scripts, "title")) != title:
            raise ValueError("Product title mismatch")
        rows = _assignment(page.scripts, "variants")
        offers = schema["offers"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100 or not isinstance(offers, list):
            raise ValueError("Invalid variants")
        by_url = {o["url"]: o for o in offers}
        if len(by_url) != len(rows) or len(offers) != len(rows):
            raise ValueError("Offer count mismatch")
        variants = []
        seen = set()
        for row in rows:
            variant_id = row["id"]
            if not isinstance(variant_id, str) or not VARIANT.fullmatch(variant_id) or variant_id in seen:
                raise ValueError("Invalid variant id")
            seen.add(variant_id)
            variant_url = url + "?sku=" + variant_id
            if row["url"] != "/products/" + handle + "?sku=" + variant_id:
                raise ValueError("Variant link mismatch")
            offer = by_url[variant_url]
            price = Decimal(_integer(row["price"], 1)) / 100
            if offer["priceCurrency"] != "MYR" or _money(offer["price"]) != price:
                raise ValueError("Currency or price mismatch")
            available = row["available"]
            if type(available) is not bool:
                raise ValueError("Unknown availability")
            expected = "InStock" if available else "OutOfStock"
            if offer["availability"] not in {"http://schema.org/" + expected, "https://schema.org/" + expected}:
                raise ValueError("Availability mismatch")
            managed = row["inventory_management"]
            if type(managed) is not bool or row["inventory_policy"] not in {"deny", "continue"}:
                raise ValueError("Unknown stock policy")
            stock = _integer(row["inventory_quantity"], -10_000_000) if managed else None
            rule = row["quantity_rule"]
            minimum = _integer(rule["min"], 1, 10000)
            increment = _integer(rule["increment"], 1, 10000)
            maximum = _integer(rule["max"], minimum, 10000) if rule["max"] is not None else None
            compare = row["compare_at_price"]
            compare_price = Decimal(_integer(compare)) / 100 if compare is not None else Decimal(0)
            if compare_price and compare_price < price:
                raise ValueError("Invalid comparison price")
            variants.append({"variant_id": variant_id, "sku": _text(row["sku"], 100) if row["sku"] else None,
                             "option": _text(row["title"]) if row["title"] else "Default",
                             "selling_price": f"{price:.2f}",
                             "compare_at_price": f"{compare_price:.2f}" if compare_price else None,
                             "available_online": available, "stock_quantity": stock,
                             "allows_backorder": row["inventory_policy"] == "continue",
                             "quantity_minimum": minimum, "quantity_increment": increment,
                             "quantity_maximum": maximum, "url": variant_url})
        prices = [Decimal(v["selling_price"]) for v in variants]
        return {"handle": handle, "title": title, "url": url, "currency": "MYR",
                "price_min": f"{min(prices):.2f}", "price_max": f"{max(prices):.2f}",
                "requires_variant_selection": len(variants) > 1, "variants": variants}
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation) as exc:
        raise SourceUnavailable("Manjaku product data could not be verified; current price unavailable.") from exc


def _envelope(products: list, started: float, local_timezone: str) -> dict:
    now = datetime.now(timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(local_timezone))
    except (ValueError, KeyError):
        local = now.astimezone(ZoneInfo("Asia/Kuala_Lumpur"))
    return {"ok": True, "source": "Manjaku Online", "source_kind": "live_first_party_storefront",
            "currency": "MYR", "cached": False, "checked_at": now.isoformat(),
            "checked_at_local": local.isoformat(), "elapsed_ms": round((time.monotonic() - started) * 1000),
            "price_scope_note": NOTICE, "products": products}


def _terms(text: str) -> set[str]:
    text = re.sub(r"(\d)\s+(kg|g|ml|l)\b", r"\1\2", text.casefold())
    aliases = {"lampin": "diaper", "diapers": "diaper", "wipes": "wipe", "tisu": "tissue", "tissues": "tissue"}
    return {aliases.get(t, t) for t in re.findall(r"[a-z0-9]+", text)}


def search(query: str, limit: int = 3, local_timezone: str = "Asia/Kuala_Lumpur") -> dict:
    if (not isinstance(query, str) or not 2 <= len(query.strip()) <= 120
            or any(ord(c) < 32 for c in query) or "://" in query or not _terms(query)):
        raise ValueError("Use 2–120 characters of product, brand or pack-size keywords.")
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Limit must be from 1 to 5.")
    query = query.strip()
    started = time.monotonic()
    params = {"q": query, "field": "title", "resource_type": "product", "limit": limit,
              "available_type": "show", "section_id": "predictive-search"}
    page = _Page()
    page.feed(_get_html("/search/suggest?" + urlencode(params)))
    if not (page.search_root and page.search_results and page.search_button) or len(page.links) > limit:
        raise SourceUnavailable("Manjaku search response could not be verified.")
    handles = []
    for link in page.links:
        parts = urlsplit(link)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or not parts.path.startswith("/products/"):
            raise SourceUnavailable("Manjaku returned an unexpected product link.")
        try:
            handle = _handle(parts.path[len("/products/"):])
        except ValueError as exc:
            raise SourceUnavailable("Manjaku returned an invalid product link.") from exc
        if handle in handles:
            raise SourceUnavailable("Manjaku returned duplicate products.")
        handles.append(handle)
    # A small bounded group of public GETs; preserve the storefront result order.
    with ThreadPoolExecutor(max_workers=3) as pool:
        products = list(pool.map(_read_product, handles))
    matching = [p for p in products if _terms(query) <= _terms(p["title"])]
    result = _envelope(matching, started, local_timezone)
    result.update(query=query, source_candidates=len(handles), partial_matches_omitted=len(products) - len(matching),
                  search_scope="Top storefront suggestions only; not a complete catalogue or physical-store inventory.",
                  may_have_more_results=len(handles) == limit, search_url=BASE + "/search?" + urlencode({"keyword": query}))
    return result


def product(handle: str, variant_id: str | None = None, quantity: int = 1,
            local_timezone: str = "Asia/Kuala_Lumpur") -> dict:
    _handle(handle)
    if variant_id is not None and (not isinstance(variant_id, str) or not VARIANT.fullmatch(variant_id)):
        raise ValueError("Use an exact variant_id returned by Manjaku search.")
    if type(quantity) is not int or not 1 <= quantity <= 10000:
        raise ValueError("Quantity must be a whole number of sale units from 1 to 10000.")
    started = time.monotonic()
    item = _read_product(handle)
    variants = item["variants"]
    selected = next((v for v in variants if v["variant_id"] == variant_id), None) if variant_id else (variants[0] if len(variants) == 1 else None)
    if variant_id and selected is None:
        raise ValueError("That variant does not belong to this Manjaku product.")
    if selected:
        valid_quantity = (quantity >= selected["quantity_minimum"]
                          and quantity % selected["quantity_increment"] == 0
                          and (selected["quantity_maximum"] is None or quantity <= selected["quantity_maximum"]))
        stock = selected["stock_quantity"]
        sufficient = stock >= quantity if stock is not None and not selected["allows_backorder"] else None
        if not selected["available_online"]:
            sufficient = False
        item.update(requires_variant_selection=False, selected_variant=selected,
                    requested_quantity=quantity, quantity_allowed=valid_quantity, sufficient_stock=sufficient)
        if valid_quantity and selected["available_online"]:
            item["listed_items_subtotal"] = f"{Decimal(selected['selling_price']) * quantity:.2f}"
            item["subtotal_note"] = "Listed-price arithmetic only; cart promotions and delivery excluded; not a checkout quote."
    return _envelope([item], started, local_timezone)
