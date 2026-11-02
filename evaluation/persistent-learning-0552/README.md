# Persistent household learning — 0.5.52

Explicit chat teaching now persists facts, preferences and per-pack MYR price
references in the scoped household database. Relevant facts/preferences are
recalled independently of recent chat history. Missing online grocery prices
can use a matching taught reference, labelled as an estimate with its source
and date. Teaching alone creates no expense, shopping item or outgoing message.

Corrections retain version history; forgetting removes the memory and stops
its use for open grocery estimates. Purchased historical snapshots are retained.
Personal reply preferences apply automatically only to their author. Private
memory and conversation history remain excluded from shared-scope replies.

## Verification

- Full Python suite: **918 tests run, 916 passed, 2 optional tests skipped**.
- The 25 learning tests include actual ingress/brain/MCP/database/outbox paths
  with scripted provider replies, a fresh Python process against the same
  database, cleared conversation history, privacy, correction/deletion,
  product/pack identity, price provenance and background estimates.
- Offline behavior suite: **1,389 variants passed, zero failures**.
- Public reasoning corpus: **471/471 deterministic decisions passed**.
  All 462 pre-existing public packets are unchanged; nine new packets cover
  teaching and recall. The accompanying delta contains only synthetic examples.
- Architecture audit: **109 checks passed**, with independent external review
  explicitly deferred because GitHub Actions billing is unavailable.
- Stress checks, compilation, focused static analysis and diff whitespace
  checks passed.

The deterministic oracle is regression evidence, not an independent model
review. Scripted-provider tests do not certify every natural-language phrasing
or a fresh live WhatsApp interaction. No synthetic prices or memories were
written to the household database. Build/install results are recorded in the
release pull request after the isolated Home Assistant package check.

## Upgrade

The additive migration creates two memory tables and a nullable scope column
on conversation history. Existing household settings, bills, shopping entries
and WhatsApp pairing are preserved. Take a current backup before upgrading.
Memory supplies facts/preferences within existing capabilities; it cannot
install integrations, grant access or activate unsupported automations.
