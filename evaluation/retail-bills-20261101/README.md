# Combined retail and bills candidate: 0.5.48

This extends the held Manjaku branch based on MYDIN 0.5.47. It implements the
three grocery paths, durable price/history records, manual two-line Digi bill
capture and monthly owner prompt, on-demand utility adapters, and matching
chat routing/MCP/menu/diagnostic contracts. No running HA state was modified.

## Local evidence

- 870 Python tests: 868 passed, 2 pre-existing skips.
- Offline behaviour: 171 contracts, 1,362 variant checks passed, zero failures
  and zero discovery-dependent cases; six-tool exposure budget preserved.
- Blind packet integrity: 462 packets, no gaps/forbidden exposure.
- Deterministic reasoning oracle: 462/462 passed, fingerprint
  `fddfd018ba5e9cbbcddb8d674911d5750f7174c4c2bce22c0ded85e390f0485e`.
  This is explicitly **not** an independent-model review.
- Structural self-test: 5/5; stress harness: passed; architecture: 109/109
  using the existing explicit unavailable-reviewer fallback.
- Python compilation/undefined-name checks; WhatsApp control, reaction,
  quote and conversation-FIFO contracts; new s6 service shell syntax passed.
- New tests cover transaction-only enqueueing, restart leases, renamed/removed/
  purchased item races, product/quantity ambiguity, private scope, stale quote
  fallback, purchase-time history and truncation; bill charge/balance separation,
  duplicates and conflicting screenshots, account changes, redirects, failed
  login, PDF validation/analysis-vs-send, scheduling/idempotence and scope.

## External evidence and remaining acceptance

The existing MYDIN/Manjaku source evidence remains in their evaluation folders.
No new retailer API, login, dependency, purchase or cart action was introduced.
The utility HTML field/button contracts were observed on first-party TNB/SAJ
pages, but backend credential login was not run: a cloud-browser session is
not a credential for the installed HA service. Synthetic fixtures are clearly
separate from live-account verification.

TNB/SAJ portal flags default off. Private HA setup and the first live on-demand
amount checks are required. TNB PDF has a download/extraction path; SAJ PDF is
still unsupported/unverified. Failed/changed responses preserve prior records
and report unavailable, never zero. No TLS or bot-check bypass is implemented.

The earlier GitHub Actions runs were blocked by the account billing lock.
Fresh hosted checks are requested on publication. The exact current corpus
requires 462 independent-review judgments in 10 shards; no previous snapshot
has been relabelled as a fresh pass. The existing final-audit fallback records
external review as deferred when infrastructure is unavailable.
Docker is unavailable in this development environment, so a full HA image build
must be verified by hosted CI or during installation. No image dependencies
changed; the existing COPY/chmod rules package the new worker.

See `alex-mcp/DOCS.md` for the single-update installation/configuration and
external acceptance steps. The normal add-on backup remains the rollback path.
