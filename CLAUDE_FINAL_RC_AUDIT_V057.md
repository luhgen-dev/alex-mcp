# Claude Final RC Audit — Alex MCP v0.5.7

**Date:** 2 October 2026  
**Repository:** https://github.com/luhgen-dev/alex-mcp  
**Production/live-tested v0.5.6 baseline:** `af28a8182113f3cf5f132bd0e00934ea13dd7fe6`  
**Exact v0.5.7 code candidate to audit:** `e08153141963343b7d18c3dbb98b1dfedd00cef9`  
**Repair branch:** `repair/v0.5.7-live-regression`  
**Draft PR:** https://github.com/luhgen-dev/alex-mcp/pull/20

> Important: this document commit is documentation-only and will sit after the code candidate on the branch. Audit the exact code candidate SHA above, plus compare it to the baseline. Do not assume the branch tip is the code SHA.

## 1. Audit purpose

This is the final independent engineering review before I consider v0.5.7 ready to merge and live-test.

Your previous audit of baseline `af28a8182113f3cf5f132bd0e00934ea13dd7fe6` and early repair head `12e8ad7` identified six root-cause groups:
- G1 empty model reply → misleading “Done.”
- G2 conversational objects not deterministically bound
- G3 ACK implemented as lifecycle status
- G4 pending items lacked namespace/verbs
- G5 malformed Baileys pin payload
- G6 scope-correct misses looked like data loss

The repair was reworked around those findings rather than continuing the early patches blindly.

Please independently verify that the exact RC `e08153141963343b7d18c3dbb98b1dfedd00cef9` closes the demonstrated defects **without breaking the previously healthy behavior**.

This is read-only. Do not modify the repository, PR, Home Assistant or production data.

## 2. Current certification evidence

At exact code SHA `e08153141963343b7d18c3dbb98b1dfedd00cef9`:

- **Alex MCP CI:** PASS — run 36963676762
- **External Alex Lab:** PASS — run 36963676746
- **Core tests:** **269/269 PASS**
- **External Lab:** status PASS, offline_failures=0, failure_packets=0
- structural self-test: PASS
- Tier-A stress: PASS
- final architecture audit: PASS
- Tier-B catalog: PASS
- Tier-B offline: PASS
- real-AI reasoning snapshot: PASS
- Node syntax / Home Assistant container build: PASS

The previous audit correctly noted that the first repair attempt had added no tests. That is no longer true: v0.5.7 now contains dedicated live-regression contracts, including pin transport, generic read finalization, finance context, quoted export precedence, pending voice selection/resolution, reminder reaction semantics, durable reminder drafts, private search offers, private asset scope, local saved-item time, filtered exports, reminder claimant nudges, and verified typed voice-action resolution.

Please do not treat passing tests as proof. Read the implementation and try to break the assumptions.

## 3. Locked product behavior

### Privacy

- Plain DM reads → **Family Shared**.
- Plain DM writes → **Family Shared**.
- Explicit word `private` or **any emoji in the current command** → owner-private.
- User never needs to say “shared.”
- Group never displays private content.
- Private group intent hands off to authenticated owner DM.
- One spouse never reads the other spouse’s private space.
- No domain-specific stash exception: Pocket Cash follows the same global read rule.

### Private discovery

A plain shared lookup that misses may say only that shared records were checked and offer a private search.

It must **not probe or reveal private-match existence before authorization**.

A direct “Yes” to that specific offer authorizes a **single-use private re-run** of the same query. In a group, the result goes to owner DM.

Direct private wording/emoji searches private immediately.

### Finance source-of-truth

Finance is release-critical.

- Active report period/category/scope must remain stable across natural drill-downs.
- User’s explicit current period wins.
- Explicit WhatsApp quote/swipe-reply to a report must outrank mutable/recent report context.
- Quoted export must preserve exact canonical dataset; model can choose requested file format, not silently substitute dates/filters.
- Exported PDF/CSV/JSON must represent the same dataset being discussed.
- Never silently shift month/category because another record exists.

### Voice

Voice is preserved evidence, **not a trusted command interface**.

- incoming voice → original preserved + PENDING;
- pending is marked ⏳ and actually pinned;
- list shows numbered local date+time, no raw internal IDs;
- `Play 1` / `Listen 1` retrieves audio and stays PENDING;
- `Resolve 1` / `Cancel 1` are real backend verbs;
- successful typed clarification may resolve **only after the requested typed action has a verified completed mutating tool claim**;
- fluent text with no successful mutation must not resolve;
- original audio remains retrievable after RESOLVED/CANCELLED.

### Family reminder accountability

- pre-due any emoji reaction may claim a claimable family reminder;
- due reminder goes to claimant DM if claimed, otherwise original group;
- due reminder is pinned while unresolved;
- post-due ordinary emoji = seen metadata only, lifecycle stays DUE;
- ✅/✔️/☑️ or explicit completion text → COMP;
- claimant can relinquish/reopen;
- initiator can privately nudge claimant;
- scheduler first follows claimant, then privately escalates to initiator;
- original family group remains reopen destination;
- one logical reminder ID is preserved;
- resolving/cancelling unpins Alex-managed reminder only;
- HA ACK follows the same seen-not-completed semantic.

## 4. What changed after your previous audit

### G5 — WhatsApp pins

`connect.js` now sends Baileys rc14 control payload in the expected shape:

- top-level `pin: targetKey`
- type 1 for pin
- type 2 for unpin
- `fromMe` derived from `target_from_me`
- Alex’s own outbound reminder pins pass `target_from_me:true`

A Node-contract regression test asserts the payload.

A one-time schema migration clears the v0.5.6 false-positive `job_pinned_at_utc` flags so still-unresolved items can be reconciled with real pins after upgrade.

Please verify payload/key semantics, reconciliation, false-success handling, group-participant behavior and whether manual pins are untouched.

### G1 — generic finalization

The goal-only fallback was removed as the sole solution.

When the final model answer is empty:
- attachment retrieval → “Here it is.”
- verified committed mutation may use “Done.”
- reads render conservative tool evidence/display values
- internal IDs are filtered
- errors do not become success prose

Please try multiple read tools, not only goals.

### F2 — frozen quoted reports

Outbound report attachments now carry REPORT context.

When `report_export` is called from trusted quoted REPORT context, `brain.respond`:
- sets `use_active_context=False`
- overrides model-supplied period/category/search/scope/start/end/currency/source with the quoted frozen spec
- fixes report_type from the quoted kind
- forces `full_report=False`
- leaves file format as the requested output choice

Please specifically try:
1. quote September report after an intervening Pocket Cash query;
2. model guesses October;
3. quote filtered September transport report;
4. active report slot contains unrelated data.

### F3 — finance drill-down

Recognised natural follow-up with no explicit period in **trusted user text** deterministically inherits canonical active month, even if the model supplies wrong dates.

When the user changes category, stale `search` does not poison the new category query.

Please try:
- September transport → “food next,” model passes today;
- transport represented via search string → food category;
- explicit October in current text must override September.

### F5 — reminder clarification

Reminder clarification is now a durable `pending_items(kind='REMINDER_DRAFT')` object rather than relying only on previous-user-turn history.

The clarification reply can recover the original request even after an intervening message and should create exactly one reminder.

Please check:
- immediate reply;
- swipe reply;
- intervening unrelated message;
- duplicate/replayed answer;
- stale/expired draft.

### F6 / G4 — pending voice namespace and verbs

There is now:
- `pending_selection_sets`
- numbered pending selection mapping
- `resolve_pending_item`
- `cancel_pending_item`
- local display time
- hidden IDs in normal presentation

The final follow-up repair adds:
- `db.has_completed_mutation(source_message_id)`
- ingress `_resolve_voice_pending_after_success`

A typed voice clarification closes the VOICE item only when the exact current inbound turn has an OK `tool_audit` joined to a `tool_execution_claims.state='COMPLETED'` mutation.

Please scrutinize this especially for:
- reads accidentally resolving;
- failed/uncertain mutations resolving;
- file/media retrieval resolving;
- explicit resolve/cancel behavior;
- multiple tool calls where one mutation commits but another requested action fails;
- idempotent cached mutations;
- private-group handoff path;
- original audio provenance after closure.

### F7 — seen is not lifecycle state

Post-due non-completion WhatsApp reactions leave reminder state unchanged and write seen/ack metadata.

`services.update_reminder(status='ack')` also leaves lifecycle state unchanged, so HA ACK should not stop listing/escalation/pins.

Explicit completion reaction transitions to COMP.

Please verify scheduler, list query, pin reconciliation, HA actions and completion/cancel behavior agree.

### F8 — domain isolation

Existing accessible reminder task names are used as deterministic hints so wording such as:

> Is v056 unclaimed test still unresolved?

stays in reminder reads and blocks unrelated saved-memory fallback even though the word “reminder” is absent.

Check false positives against legitimate saved-note queries.

### F4 / F9 / G6 — scope miss and private offer

Assets now accept natural scoped query filtering.

Plain Family-scope miss must not imply global absence or create a duplicate automatically.

Private search offer is represented as a pending, single-use owner object with a bounded age. A “Yes” re-runs the exact original query privately.

Check that:
- private match existence is not probed/revealed before permission;
- stale “Yes” cannot authorize an unrelated search;
- one spouse cannot consume the other’s offer;
- group fulfillment returns result only in owner DM;
- plain Pocket Cash follows global Family default, not a hidden private exception.

### Claimed reminder accountability

Additive reminder fields track:
- seen
- nudge
- initiator notification
- relinquishment

Existing DUE state remains the unresolved lifecycle state; CLAIMED is represented by claimant metadata.

Scheduler:
- claimed due → claimant DM
- unresolved → claimant follow-up
- still unresolved → initiator DM escalation
- explicit initiator nudge supported
- release/reopen supported

Please inspect race/idempotency behavior, especially claim/complete/release near scheduler sweeps.

## 5. Regression tests added

Please inspect their quality rather than just their presence.

Current named v0.5.7 tests include at least:

- `test_v057_baileys_pin_contract_and_self_target`
- `test_v057_empty_read_reply_uses_tool_evidence_not_done`
- `test_v057_finance_followup_overrides_model_dates_and_drops_old_search`
- `test_v057_quoted_report_overrides_model_period_and_active_context`
- `test_v057_pending_voice_numbers_bind_play_and_resolve`
- `test_v057_typed_voice_clarification_resolves_only_after_verified_action`
- `test_v057_cancel_pending_voice_preserves_original_provenance`
- `test_v057_post_due_reaction_seen_vs_complete`
- `test_v057_reminder_task_name_blocks_cross_domain_fallback`
- `test_v057_reminder_clarification_is_durable_pending_draft`
- `test_v057_private_search_offer_is_single_use_and_scope_safe`
- `test_v057_private_asset_is_searchable_without_crossing_family_scope`
- `test_v057_saved_item_browse_uses_local_date_and_time`
- `test_v057_frozen_filtered_export_stays_inside_quoted_month`
- `test_v057_initiator_can_privately_nudge_claimant_without_reopening`

Please identify missing adversarial cases.

## 6. Real-AI certification note

The v0.5.6 passing human-AI corpus and v0.5.7 corpus each contain 384 packet IDs.

For v0.5.7:
- 262 packets were byte-identical to the previously reviewed corpus.
- 122 differed only in `available_tools`.
- prompt/history/actor/source/conversation type were unchanged.
- every prior chosen tool remained available.
- the 122 changed tool surfaces were re-reviewed.
- four deterministic-oracle route differences were manually reviewed and the existing human-AI decision was retained:
  1. original recordings → `find_media`
  2. pending expense approval → `list_pending_expenses + confirm_expense`
  3. warranties → `warranty_expiring`
  4. saved pictures → `search_saved_items`
- no human-AI decision changes were required.
- snapshot and deterministic oracle are now fingerprint-bound to the exact current 384-packet corpus.

Please independently check whether this compatibility review is defensible and whether any newly introduced tool surface should have forced a fresh full-corpus model review.

## 7. Persistence / migration review

Please pay special attention to upgrades from a real v0.5.6 `/data/alex_mcp.db`.

New schema pieces are additive and `db.initialize()` uses `_ensure_column` where required.

A `schema_migrations` table was added to guard the one-time false-pin reset.

Please check:
- fresh install;
- upgrade from v0.5.6;
- repeated startup idempotency;
- no destructive transformation of household data;
- pending/reminder selection tables creation;
- reminder CHECK constraints remain compatible;
- false-pin reset runs once only.

## 8. Previously healthy behavior that must remain intact

Do not approve a repair that regresses:

- leave re-add after cancelled tombstone;
- emoji-private group receipt → owner DM;
- RM6.50 finance description lookup;
- Saturday calendar resolution;
- September shared PDF and privacy;
- Pocket Cash owner-private data integrity;
- pre-due family reminder claim and claimant DM due delivery;
- safe voice defer/no command execution;
- receipt/media provenance;
- private exact saved-note boundary;
- private browse and natural Family Shared browse;
- direct filtered September Transport PDF = MYR30.49 / 4 tx;
- unclaimed family reminder fires in group;
- September Food & Drink = MYR15.67 reconciliation;
- central scope policy;
- first-winner claim atomicity;
- final-call tool-call guard.

## 9. Specific questions

Please answer all:

1. Is any original F1–F10 defect still reproducible on exact RC?
2. Is any early repair still overfit to the live wording rather than the underlying mechanism?
3. Does the quoted-report implementation truly make the quoted dataset immutable against model/active-context substitution?
4. Does finance follow-up logic ever override an **explicit current user period** incorrectly?
5. Can any pending voice item resolve without a verified intended action?
6. Conversely, can a successful legitimate typed clarification remain stuck pending?
7. Does the Baileys pin/unpin implementation now match rc14, including own-message keys?
8. Can seen/ACK still accidentally stop reminder escalation or pin retention anywhere?
9. Is the new claimed-reminder escalation safe under duplicate scheduler runs and concurrent reactions?
10. Can private-search offers leak private existence, be replayed, cross users, or authorize a different query?
11. Are schema migrations safe on a v0.5.6 persistent DB?
12. Are the 269 tests materially covering the live failures, or are there important missing assertions?
13. Do you see any adjacent defect introduced by the repair diff that was not part of F1–F10?

## 10. Deliverable

Return:

- exact SHA audited;
- full-source access confirmation;
- tests/static/reproductions performed;
- **GO / HOLD** recommendation for merge (with reasons);
- F1–F10 closure table;
- any new findings, with DEMONSTRATED / STRONGLY SUPPORTED / SPECULATIVE labels;
- exact files/functions;
- minimum patch for any HOLD item;
- tests to add for every new finding;
- migration assessment;
- privacy assessment;
- finance source-of-truth assessment;
- reminder-accountability assessment;
- voice-lifecycle assessment;
- whether another audit is necessary after any requested patch.

Please be adversarial. A clean “GO” is useful only if you genuinely tried to break the RC.
