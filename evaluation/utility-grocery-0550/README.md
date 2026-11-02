# Alex 0.5.50 utility and retailer repair

The reported WhatsApp utility query returned only a date after two invalid-provider
tool calls. Product follow-ups drifted into saved-record/finance searches. This
release repairs provider normalization/schema, complete utility evidence replies,
explicit PDF delivery, retailer conversation routing and price grounding.

Validation on 2 November 2026:

- Python suite: 892 tests, 890 passed, 2 optional checks skipped.
- Real ingress → brain → MCP → storage/outbox regressions: 10 utility tests and
  11 retailer/parser tests. AI and portal responses are synthetic; this is not a
  claim that new real WhatsApp messages were sent or received successfully.
- Offline behaviour: 1,362 variants passed, zero failures.
- Deterministic reasoning oracle: 462/462 passed, bound to the reviewed current
  corpus. The 22 changed public packets are preserved in `oracle-delta.json`;
  20 update the utility schema and 2 remove unrelated retailer-query tools.
- Architecture audit passes with explicit external-review deferral. Fresh
  independent model review is unavailable because GitHub Actions is billing
  blocked; deterministic evidence is not presented as independent review.
- `live-source-before.json` and `live-source-after.json` contain public catalogue
  responses only, retrieved using the production adapters. Final checks cover
  MYDIN Fernleaf milk, rice, Apple rice, yogurts, sardine, cooking oils and
  detergents; Manjaku Mimora, baby wipes, diapers, milk and Drypers.
- Empty results for Apple rice, yogurt, Manjaku milk or Drypers do not establish
  catalogue-wide absence. The tested pages supplied no verified relevant product.
- Live TNB/SAJ login, exact account parsing and TNB PDF retrieval were verified
  on the HA host during 0.5.49. Credentials/private bills are not included here.

No data migrations or configuration changes. List writes still queue background
pricing, plain lists still hide prices, and utilities/PDFs remain on demand.
