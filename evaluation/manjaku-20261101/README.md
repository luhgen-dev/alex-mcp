# Manjaku Online price lookup — held for the combined update

Validated on 1 November 2026. This branch builds on merged MYDIN version
0.5.47. Manjaku is an unreleased addition: no version bump, main-branch merge,
Home Assistant update, restart or WhatsApp delivery is claimed.

## Verified source

Manjaku's current public store is Shopline at https://www.manjaku.com/.
The storefront's `component-predictive-search.js` uses the read endpoint
`/search/suggest?q=...&field=title&resource_type=product&limit=...&available_type=show&section_id=predictive-search`.
The full search UI uses `/search?keyword=...`, not `?q=...`.

The adapter reads only links from the verified predictive-search section,
then refreshes the product pages. Each page exposes literal JSON assignments
for `otProductInfo.handle`, `title` and `variants`, plus Product structured
offers. Price, MYR currency, variant URL and availability must agree. Variant
prices are integer sen; they are converted to MYR using Decimal. The code
decodes JSON literals without running JavaScript. It never quotes a suggestion
card's minimum as a selected variant's exact price.

There are no cookies, credentials, cart operations, API Anything installation,
new dependencies, database migration or background polling. Network calls are
fixed-host public GETs, with no redirects, 12-second request timeout, 2 MB page
cap, at most five search products and three concurrent product reads. The
`manjaku_prices_enabled` switch blocks both tool paths before networking.

## Live evidence

Nine products / 21 variants were captured in the reduced public-data fixture.
Eleven visible browser price observations across those nine products matched
the separately fetched product data. These include sale and comparison prices,
three carton variants, and a sold-out diaper size. Not every one of the 21
variants was selected in the browser.

Examples observed during this session:

| Product / option | Listed price | Detail |
|---|---:|---|
| Mimora Tape NB64 | RM18.90 | Comparison price RM34.90 |
| Mimora Tape S82 | RM27.90 | L60 separately marked sold out |
| Baby Hippo wipes 3×30s Milk | RM10.90 | One sale unit |
| Sofyroo facial tissue 80s 3-ply | RM2.90 | Single pack |
| Sofyroo carton: facial 250s × 24 packs | RM140.00 | Exact carton option |
| Sofyroo carton: facial 80s × 100 packs | RM290.00 | Exact carton option |
| Sofyroo carton: pocket 10×8s × 50 packs | RM50.00 | Starting listing price belongs to this option |

`browser-comparison.json` records the visible observations. `mcp-live-carton.json`
records a real authenticated local MCP call (synthetic actor) that fetched the
RM290 option and calculated RM580 for two cartons in about 5.3 seconds. Search
samples took approximately 10.9–13.8 seconds when refreshing products; empty
searches took about 5.7–6.1 seconds. These are small samples, not a reliability
or percentile latency benchmark.

## Scope and limitations

- [Manjaku's FAQ](https://www.manjaku.com/pages/faq) explicitly distinguishes
  online prices and stock from physical stores. The MYDIN retail-price reference
  assumption is not silently applied to Manjaku.
- Search is the top suggestions (default three, maximum five), not an exhaustive
  catalogue. Every query term must match the returned product title. Empty
  results mean no verified top matches, not absence from every store.
- Dutch Lady returned zero results in the current full browser search and the
  adapter. Older search-engine listings are not used as live prices.
- Product variants are separate sale units. A multi-variant product needs an
  explicit choice before a subtotal is returned. Stock shortages, sold-out
  variants and quantity rules remain explicit.
- The browser also showed cart-applied promotion banners. Their final terms
  were not verified and no cart/checkout was created. Listed-price subtotals
  exclude cart, bundle, member and voucher discounts and delivery. An explicitly
  sold carton variant is still quoted at its own listed price.

## Validation

- Full Python suite: **835 run, 833 passed, 2 skipped**; 15 Manjaku tests include
  actual fixtures, range/variant traps, sale prices, quantity rules, malformed
  sources, empty results, unsafe links, cross-retailer routing, DM/group MCP
  dispatch, source failures and the kill switch. Existing MYDIN tests pass.
- Offline behaviour certification: **166 contracts, 1,323 variants, no failures**.
- Deterministic reasoning oracle: **449/449**. All 446 previous public packets
  are unchanged; three Manjaku requests correctly select its own search tool.
  `oracle-delta.json` and `oracle-report.json` bind this review to the corpus.
- Final architecture audit: **109/109** using the repository's existing
  unavailable-reviewer fallback. Fresh independent review is **deferred** because
  GitHub Actions is blocked by the account billing lock observed in PR #63.
  The original independent snapshot and CI workflow are not changed.
- Structural self-test: **5/5**. Existing deterministic stress harness:
  **17 checks passed**. Compile and changed-file lint checks pass.

Hosted CI/container verification, fresh independent-model review and real
installed HA/WhatsApp acceptance remain pending. The eventual combined release
should smoke-test generic search, a selected size, the RM50/RM140/RM290 carton
choices, the sold-out L60 option and a no-result milk search.
