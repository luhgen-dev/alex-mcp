# MYDIN live price integration — 1 November 2026

## Result

The source blocker is resolved in this environment. MYDIN's first-party APIs
returned live prices, and Alex's registered MCP tool successfully fetched a
live promotional product. This is executable integration in the add-on source,
not another standalone connector ZIP. The installed Home Assistant add-on and
the real WhatsApp account have not been updated/tested by this session.

## Evidence

- `browser-comparison.json`: **33/33** different products matched the live
  rendered MYDIN site on iSKU, title/pack size, selling price and stock quantity:
  12 rice products, 8 cooking oils, 12 milk products and 1 promotional product.
- `mcp-live-sale.json`: a real `mcp.Client` call to the registered
  `get_mydin_product_price` tool, with a synthetic authenticated actor and a
  genuine network request. Lava Tamu TK34-25 returned **RM19.90**, regular
  **RM24.90**, stock **46**, matching the browser. Not a WhatsApp delivery test.
- `bulk-live.json`: EcoBrown 2kg costs **RM13.60** for one pack, or **RM13.52**
  at five packs. The product page independently showed both thresholds. Only
  two packs were in stock, and the tool reported insufficient stock for five.
- `final-Myrasa-5kg.json`, `final-Buruh-5kg.json`,
  `final-cooking-oil.json`: final name/pack search implementation verified live.
  The first two return only the matching product at RM17.95 and RM30.90.
- An invented product keyword returned a successful empty result, not a price.
- Saved responses include their actual check timestamps. They are evidence of
  that check, never a fallback catalogue for future live quotes.

## Actual source contract

The public frontend at `https://mydin.my/` loaded
`/_nuxt/entry.70918e67.js`, `/_nuxt/search.acafd563.js`, and
`/_nuxt/useCategoryProducts.22c04336.js`. Its runtime config names
`https://myapi.mydin.my/` as middleware and `https://mymgtbe.mydin.my` as API base.

Read-only operations observed in those files and replayed successfully:

1. `GET https://mymgtbe.mydin.my/rest/V1/vsf/getSearchSkusPaged/{keyword}`
   with `page` and `pageSize`. Returns a JSON-encoded JSON string containing
   `success`, `data.keyword`, counts, page and SKU list.
2. `GET https://myapi.mydin.my/magento/products?body=...`, where `body` is the
   JSON argument array `[parameters, customQuery, null]`. `parameters` carries
   either an exact SKU filter or the frontend's `search` parameter.
   `customQuery.products` is `products-custom-query`, with bounded requested
   fields in `metadata.fields`. The response is `data.products.items`.

The adapter reads `price_range.minimum_price.final_price` and its currency,
compares regular/maximum prices, uses `custom_productname`, validates the URL
key against the returned iSKU, and reads `salable_quantity` and `tier_prices`.
Configurable products are explicitly marked as needing variant selection.

Two real bugs were prevented during source validation:

- `/api/search/fetchSearchResults?q=...` suggestions can quote a bulk tier
  instead of a one-pack price (EcoBrown: RM13.52 versus RM13.60).
- Storefront SKU search uses broad partial matching. “Myrasa 5kg” initially
  returned creamer. Multi-term searches now use Magento's ranked search and
  require every name/size term to match; unrelated partial matches are omitted.
  Pagination remains explicit; an empty filtered page is not claimed to prove
  that the whole catalogue lacks a product.

No API Anything operation was installed or learned. API Anything is optional:
these already verified first-party reads are called directly by a small
standard-library adapter. No site credentials, cookies, account data, personal
shopping lists, arbitrary URL input, write endpoints or catalogue sharing are used.

## Validation and limits

- Full Python suite: **820 tests run, 818 passed, 2 skipped**.
- **15** MYDIN-specific tests cover actual source fixtures, quantity tiers,
  fractional tier precision, pack matching, schema drift, empty results,
  invalid inputs, variant ambiguity, English/Malay/Tamil routing, authenticated
  MCP dispatch in DM/group contexts, source failure and the configuration switch.
- Existing deterministic stress harness: PASS (17 checks).
- Structural self-test: 5/5. Behaviour catalogue: PASS. Python compile and
  undefined-name checks: PASS.
- Final multi-term live searches took approximately **5.2–5.9 seconds** each
  here. Generic storefront searches involved two calls and took roughly
  **11.6–12.6 seconds** in the sample. These are small-sample observations,
  not a 100-call latency benchmark or a seven-day reliability result.
- Items-only quantity totals preserve the source's unrounded tier rate until
  final rounding. They exclude delivery and checkout-specific rounding.

## Deployment acceptance still required

After merging/installing version 0.5.47 in Home Assistant, ask on WhatsApp:

1. “What is the current price of Myrasa 5kg at MYDIN?”
2. “Check Buruh cooking oil 5kg at MYDIN.”
3. “How much for 5 packs of EcoBrown 2kg, iSKU 10010176?”

Confirm the reply contains the product/pack, MYR amount, link and local check
time; the quantity example must state any stock shortage. Confirm ordinary
shopping/expense records remain unchanged. Group requests still use Alex's
existing mention/reply rules. `mydin_prices_enabled: false` is the kill switch.
No HA access, add-on restart, account connection or live WhatsApp delivery is
claimed by this report.
