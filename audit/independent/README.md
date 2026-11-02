# Independent audit checkpoint — incomplete, not a release certification

Baseline: upstream `17b37cd998b58b926e3ca00fd8f13d8a8bbcb070` (0.5.54, PR #70).
Evidence collected 2026-11-02. This branch is a **draft repair candidate**. It has
not been merged, installed, restarted, or verified as the running Alex app.
An operator-assisted isolated SDK test run on HA Terminal has now completed;
its partial result and limitations are recorded below.
No household database, credentials or pairing files were changed. The operator
restarted Terminal & SSH to cancel a malformed mobile paste, then restored its
temporary Python installation. Alex and other household services kept running.
No provider calls were made. The speech/transcription implementation and
provider configuration are unchanged.

The original assignment remains open. The attached screenshots are evidence of
reported failures, not an exhaustive test plan and not proof that records were
written. The requested comprehensive audit and deployment are **not complete**.

## Verified findings and repairs

| Finding | Reproduction and consequence | Candidate change | Evidence |
| --- | --- | --- | --- |
| Expense confirmation accepts invalid category | Confirm a pending expense with `private`, `general`, or whitespace: baseline activates it; aliases are also left unnormalized. | Use the normal category validator; keep incomplete records pending with exact missing-field names. | `test_finance_validation_independent.py`: invalid confirmation and normalization tests. |
| Correction bypasses pending review | Rename a pending expense with no amount: baseline creates an ACTIVE successor. A privacy word can also become its category. | Require an ACTIVE parent and valid amount/category before append-only correction. | Pending bypass, category, and original/history/total tests. |
| Receipt APIs can return missing/corrupted originals | Delete or replace the original file after archiving; three retrieval APIs return attachment success on baseline. | Verify archived SHA-256 and existence at retrieval. | Exact-byte positive tests; missing/corrupt negative tests for all three APIs. |
| Original changes while queued | Retrieve a valid original, replace its bytes, then build the outbound payload: baseline encodes the replacement. | Hash the exact bytes encoded at egress; permanently fail corrupt originals and queue a document failure notice. | Retrieval-to-egress tamper test and real outbox-state test with a mocked sender. |
| Plan listing ignores current read scope | Ordinary DM listing includes owner-private plans; explicit private listing includes shared plans. Private group request silently returns shared plans. | Use centralized `read_spaces` policy instead of all memberships. | Shared/private/all, spouse, group, and private-in-group cases. |
| Custom-ledger oversized data becomes invalid JSON | A 21,000-character note is sliced after JSON encoding, persisted, and cannot be decoded on read. | Reject oversized/invalid JSON before insertion; preserve valid content exactly. | Large-data readback test. |
| Custom-ledger nonfinite/oversized amounts crash internally | NaN, infinity, and an amount beyond SQLite's integer range cause unhandled numeric errors. | Reject nonfinite/out-of-range amounts with actionable domain errors. | Invalid-amount tests plus exact cents/idempotency/zero-write checks. |
| Audit redaction misses credentials | Double-escaped regexes fail to remove bearer and provider credentials from error strings; several key spellings are missed. | Correct patterns; conservatively redact credential keys while retaining explicitly recognized numeric usage counters. | Six independent test methods, with nested fields, variants, and nonmutation checks. All secrets in these tests are synthetic. |
| Attachment judge can falsely pass | Correct pathname with wrong/missing bytes passes baseline; audio/video are not treated as attachments. | Freeze independent fixture digests; check queued file readability/content/hash as well as identity and count. Include audio/video. | Six judge mutation probes: positive original, wrong bytes, missing file, empty file, missing digest, and audio/video. |
| Product documentation contradicts current voice implementation | The voice section still describes voice as never executable. Historical certification counts can be mistaken for current acceptance. | Document 0.5.54 transcript authority/fallback and explicitly label historical certification. | Source/document review; no claim of live speech verification. |

## Screenshot failures still unresolved

**Voice expense refusal:** operator-supplied production traces show authorized
Tamil transcription, omitted write capabilities, a failed capability recovery,
and tool calls discarded at the final model-call limit. Terminal font rendering
does not preserve every Tamil character clearly; constructed regression phrases
are not labelled verbatim transcripts. Colloquial reminder/logging imperatives
now retain their required mutator before schema capping. The spoken Tamil loanword
for "private" also selects owner-private storage; a real SQLite test checks it.
The false-denial detector
also recognizes "cannot log" and "write access", and an exhausted recovery may
not repeat an unsupported lack-of-capability claim. New SDK/ingress tests are
written. The second isolated SDK run executed voice ingress with real Ogg decoding
and scripted provider responses; its new expense-to-private regression passed.
One reminder test still failed because its script requested discovery before the
now-directly-offered mutator. That script is repaired; the strengthened harness
now preserves provider assertions through application error handling. Full rerun
and actual provider/phone acceptance remain pending.

**Quoted expense moved to private:** both captured corrections failed with
"event not found in your accessible spaces". An explicit English move/save-to-private
request quoting one expense now uses a deterministic ingress operation. It binds
the stored provider quote, verifies ownership, and moves the entire correction
chain in one transaction while preserving IDs, amounts and original files.
Another person's record, a group request, or a receipt still shared through a
different record is refused without a partial change. Linked custom-ledger and
saved-file references are checked as well. This is an ingress capability, not a
new model-facing MCP tool, and general read scopes are unchanged.

New local checks cover stored moves/retries, correction history, original receipt
bytes, owner/spouse/group boundaries, shared-file dependencies, quoted metadata,
and queued delivery. A cross-process file lock serializes the final outbox
check/send with a visibility commit. Stale finance snapshots for other recipients
are conservatively replaced by a safe request for a fresh result. Already sent
WhatsApp messages cannot be erased by this operation. Correction/confirmation
authorization now occurs inside a write transaction to prevent stale shared-scope
decisions across a concurrent move. Full ingress, migration, provider, report-cache
and production acceptance remain pending; this is not a deployed fix.

**Cached finance privacy and repeated exports:** a cached shared ledger could
survive a move to private. Finance queries now read their totals, records and
privacy generation in one SQLite snapshot. Cached finance payloads from an older
generation are withheld and an exact cached export asks for a fresh report.
The final delivery check also rejects old generations when a tool audit was
written after the move, closing the read/commit/audit race. Large audit results
retain valid JSON and the privacy generation rather than truncating that evidence.
Real SQLite checks cover the interleaved commit, old and new cache formats,
fresh scoped reads, and delayed delivery. An SDK regression also checks repeated
exports: a remembered canonical report must not be nested as its own ledger.
That SDK regression ran and exposed an incomplete synthetic actor: without
ingress's trusted text, its seed expense used the legacy private fallback.
The fixture now sets trusted text, verifies the stored shared row before export,
and exercises the spouse's report before/after the owner's private move. No
application privacy boundary was widened. The corrected test awaits rerun.

## Feature-by-feature status

“Local” below means isolated synthetic SQLite/files. None of these rows is an
end-to-end production pass. [tool_inventory.json](tool_inventory.json) enumerates
all 151 statically registered tools. SDK tests now exercise some real MCP calls,
but their execution has not been individually attributed across that inventory.
Backend tests do not certify every tool's discovery,
argument generation, conversational understanding, or phone delivery.

| Capability family | Fresh evidence / current result | Remaining acceptance work |
| --- | --- | --- |
| Identity and conversation | Unknown sender rejected; group actor has only shared membership. Four Node contracts passed for quoted context, reactions, controls, and FIFO. | Real sender/group binding, quote continuity, overlapping requests, delayed follow-ups, and conversation privacy. |
| Multilingual voice and media | Voice implementation reviewed without edits; receipt original-file integrity repaired locally. | Actual English/Tamil/Tanglish/Malay/mixed audio, expense reproduction, STT accuracy, language/amount preservation, disabled/budget/failure behavior on the deployed configuration. |
| Finance, receipts, privacy corrections | Local exact cents, retries, append-only history, totals, isolation, original integrity and quoted private-move storage/egress checks pass. Operator traces confirm both reported moves failed in 0.5.54. | Full ingress/MCP, migration, concurrent provider turns, report caches and deployed acceptance. |
| Reports and exports | Certification file judge now rejects wrong/missing/empty originals. | Full/filtered totals, date windows, PDF/image/XLSX content and decoding, report/selection scope, cached report invalidation, delivered bytes. |
| Shopping and prices | Real queue tests: duplicate add, rename/purchase during lookup, bounded retries, sanitized errors, no accidental expense. | Live Mydin/Manjaku data, matching/pack quantities, remembered-price preference, provenance/estimates and monthly summaries. Network lookup is mocked in local worker tests. |
| Saved memory and teaching | Saved-note scope/emoji content checks pass. | Teaching revisions, learned preferences/prices, recall injection boundaries, deleting/forgetting and original media lifecycle. |
| Reminders and HA companion | Outbox transport retry preserves one stable provider message key; missing/corrupt document is not delivered. | Scheduler due time, presence, repeated/escalated reminders, claim/handoff/reaction lifecycle, actual companion actions and phone delivery. |
| Diary and availability | Source paths inspected, but no new diary/availability behavioral pass. | Conflicts, date-only/time-known states, end-before-start validation, recurrence, spouse busy-only boundary and linked reminder changes. |
| Tasks and plans | Task create/complete/reopen/cancel history, retry, no implicit plan/reminder, spouse/group isolation; plan list scope repaired. | Conversational discovery, confirmation/materialized diary lifecycle, cross-record atomicity, linked reminder edits, plan sharing and follow-ups. |
| Goals, cash and stashes | Goal read-scope/model-argument boundary, private goal denial, exact pool spending, retry and overdraft rollback pass locally. | Contributions/allocations/baselines/projections/reserves, evidence matching, cross-ledger double counting, conversation lifecycle. |
| Bills, utilities and Digi | Not behaviorally verified in this session. | Portal credentials/status handling, payment evidence, monthly/due logic, screenshot prompts, duplicates, correct account and delivery. |
| Work and leave | Source/test inventory only. | Actual configured roster, leave ranges/balances, OT/salary, commute/departure and private-work boundaries. |
| Daily brief | Worker and delivery-marker paths identified. | Current household facts, scope, stale data, schedule/DST, once-per-day queue/delivery and pin cleanup. |
| Ear/listening and journal | Source/test inventory only. | Start/stop/resume, addressed versus captured messages, private exclusions, durable summaries and injection resistance. |
| Assets and monitoring | Asset read scope and owner/spouse/group edit isolation pass locally. | Warranty dates, document originals, monitor scheduling/cancellation, candidate deduplication and actual delivery. |
| HA tools and custom ledgers | Custom-ledger exact cents, scope, classification gate, retry, no accidental main-ledger entries, numeric/JSON repairs pass. | Live entity reads/control, automation drafts, physical state readback, custom-ledger attachments/lifecycle and integration tests. |
| Operations, upgrades and recovery | Source verified against upstream blobs; compile and focused checks pass. Full suite is blocked. | Fresh HA backup, current settings/version/process readback, real dependency/container build, migration/restart/rollback, pairing preservation, CI and production smoke. |

## Worker inventory

All seven packaged services were enumerated from `rootfs/etc/services.d`:

| Worker | Evidence in this session |
| --- | --- |
| `node-bridge` → `connect.js` | Four local payload/FIFO contracts and syntax check; no paired bridge run. |
| `python-ingress` → `ingress.py` | Isolated SDK ingress checks now execute, including real Ogg decoding and quoted private moves; model/STT/transport responses are scripted. Full run still has two test failures described below. |
| `python-outbox` → `outbox.py` | Real local durable queue states with transport adapter mocked; no WhatsApp delivery. |
| `python-scheduler` → `scheduler.py` | Core and reminder-lifecycle module checks pass in isolated HA testing; live due-time scheduling and actual delivery remain unverified. |
| `python-ha-mobile` → `ha_mobile.py` | Inventoried; live worker/action verification outstanding. |
| `python-grocery-prices` → `grocery_prices.py` | Local lease/revision/retry state tests with lookup mocked. |
| `python-dot` → `dot_runtime.py` | DOT modules passed in the first isolated HA run. Two image-only startup/socket checks are guarded by `ALEX_TEST_PACKAGED_RUNTIME`; current aggregate shows two skips and the next runner prints their names. No packaged binary or live bridge certification. Keep feature disabled. |

Startup runs `init_db.py` and logs a warning rather than stopping when `selftest.py`
fails. A running service is therefore not evidence that structural checks passed.

## Exact automated evidence and its limits

- [candidate.json](candidate.json), [candidate.log](candidate.log): **64 tests,
  zero failures/errors/skips**. SHA-256 values bind eight tested runtime modules,
  the independent fixture/test files, and the additional wiring/schema sources.
  The latter hashes do not certify execution of the SDK-dependent ingress/brain.
- [baseline.json](baseline.json), [baseline.log](baseline.log): the earlier 48 tests
  against the original six runtime modules produce **35 failed assertions and
  five errors, affecting 22 distinct test methods**. Subtest failures are counted
  separately from test methods. The errors are malformed JSON, three numeric
  cases and rejected colloquial Tamil time evidence, not missing dependencies.
  The runner selects all baseline modules before importing any, and checks
  their loaded paths. Incremental replacement had cached candidate helper
  functions in baseline dependants and initially masked the new time regression.
  Those historical baseline logs remain tied to their recorded test hashes at
  commit `959d419b843ab58974ff71c610fa653ab513fc3a`; they are not a comparison
  of the expanded 64-test suite. The current runner refuses a baseline missing
  the new privacy module instead of silently importing candidate code.
- Local Python 3.12.14. Python compilation passed. Four existing Node contracts
  and bridge syntax passed. These are not a container build or a paired session.
- Earlier isolated execution of 53 available test modules: six modules passed,
  46 failed, and one timed out. Forty-five failures encountered missing
  `requests`; another encountered incompatible installed MCP APIs. Passing
  modules accounted for 54 test methods (52 passed, two packaged-runtime skips),
  including nine earlier independent tests. **Do not add this overlapping,
  earlier count to the current 64-test result.**
- The installed MCP package is 1.29.0; the app pins 2.2.0. `requests`, `openai`,
  and `websocket-client` are unavailable. Dependency installation could not reach
  the package index in this executor. The subsequent HA Terminal run below used
  the pinned dependencies successfully. The full suite, current routing
  certification and container build are not passed or waived.
- Operator-assisted HA Terminal run of `4cfbbcf24ea5e48abbad26daeb5a5343a1a3c706`:
  **58/64 jobs passed; six modules failed**. Compile, selftest, stress and tool
  catalog passed; 54/60 test modules passed. These are job counts, not test-case
  counts. The old runner did not report skips, so passed modules may include
  skipped cases. Evidence is user-provided terminal screenshots and error output;
  full logs remain at `/config/alex-audit/runtime-7f2195a3` on HA.
  [ha-runtime-observation.json](ha-runtime-observation.json) records the failures.
  The missing Phase 2 tables were traced to two fixtures calling only
  `db.initialize()` before using the broader core reset. They now initialize the
  complete core fixture. Empty injected options suppressed the synthetic phone
  configuration in older tests, explaining unauthorized ingress and missing
  `quoted`/`voice_pending` results; the runner now supplies synthetic identities.
  FFmpeg is present in the app Dockerfile but absent from Terminal. The revised
  runner installs signed Alpine packages into a private test sysroot and verifies
  actual Opus encoding. Subsequent operator runs report `AUDIO_TOOLS PASS` and
  execute real Ogg decode tests successfully.
- Five local runner-mechanics tests pass (including mocked private APK setup and
  a check that `LIVE_REQUIRED` or a crashed process cannot become a routing pass),
  and eight earlier backup-probe tests passed. Neither count certifies the app.
  No application assertion, schema requirement, or release gate was removed to
  address the six module failures. The revised complete SDK run remains pending.
- Second operator-assisted HA run at `c1815adeb9f7ed0407d6a2a022de76db8969c67d`:
  **62/64 jobs passed; 1,033 test executions; two skips**. Both remaining failing
  modules report one failing test each: `test_v0538_reminder_report_reliability`
  (cached export fixture, `IndexError`) and `test_voice_feature_parity` (unused
  scripted provider responses). The synthetic-identity/schema problems and
  missing FFmpeg are resolved. These counts include overlapping imported test
  cases and are not 1,033 unique feature scenarios. Screenshots 315245/247/249
  are operator evidence; complete logs remain on HA.
  The corrected tests still assert persisted rows, exact records, private scope,
  export content and reminder due time. The provider harness now records missing
  tool/exhausted-reply assertions outside the application's exception handler,
  so an invalid final scripted call cannot silently turn into a passing test.
  A new negative regression proves that harness behavior. The application code
  is unchanged since `c1815ade`; the complete suite must run with this stricter
  harness before accepting the result. The runner also prints skipped test names.
- Third operator-assisted HA run at `c34ba271005baad79b86b59357432fb62691f787`:
  **63/64 jobs passed; 1,034 test executions; two skips**. The stricter core
  provider harness, negative harness regression, complete voice-parity module,
  migrations, selftest, stress and catalog passed. The single remaining test
  reached the expected cached-export privacy refusal, but asserted `ValueError`
  instead of the MCP boundary's `ToolError`. The assertion now uses the SDK's
  exception class; fresh scoped export assertions after it are still required.
  The two skipped tests are exactly `test_packaged_binary_executes_without_network_or_credentials`
  and `test_real_egress_and_dot_sockets_coexist`, both in `test_dot_startup` and
  requiring the packaged HA image. They are not waived.
  Evidence: screenshots 315261/263/265/267. Python 3.14.10 was restored in the
  Terminal app after its restart; the public source and isolated test data were
  recreated while the verified SDK environment was reused. App code is unchanged.
  The voice expense fixture now says 56.90, matching its scripted amount, and
  asserts that exact transcript was stored. It still does not certify live ASR.
  The next run targets these two edited modules and the broader offline routing
  matrix; it is explicitly not another complete unit-suite run.
- Upstream CI run `41451020384` was observed failed with zero job steps executed.
  The prior release records a billing lock; this session has not independently
  verified the account-level billing cause.

The source was obtained through the GitHub connector because shell network
access is blocked. All 178 non-`evaluation/` files were checked against upstream
Git blob IDs. Historical evaluation artifacts were not copied into the local
checkout. Its synthetic baseline commit is
`d386cec57cdfdb313d58d26140e955c99764ef32`; this is **not an upstream revision**.
The five baseline runtime blobs match upstream `17b37cd`. Publication uses the
actual upstream tree/parent, retaining its historical artifacts.

Reproduce in a normal checkout with the project's real dependencies installed:

```sh
python audit/independent/run_checks.py --report /tmp/alex-candidate.json
python audit/independent/run_checks.py --baseline-ref 17b37cd998b58b926e3ca00fd8f13d8a8bbcb070 --report /tmp/alex-baseline.json
```

The baseline command is expected to exit 1. It loads only the five original
runtime modules while keeping the new test corpus and unchanged dependency
source. The fixture creates synthetic tables directly: **it does not test schema
migrations**. Worker network adapters are mocked at the boundary; durable queue
and file behavior remain real. No stub SDK is used to make the suite pass.

## Test-suite gaps

1. The attachment judge previously accepted a pathname as identity evidence;
   mutation probes now demonstrate that corrupt/missing bytes fail.
2. Existing “voice” suites generate a tone, mock transcription, and script the
   provider. They exercise boundaries, not real multilingual recognition or
   spontaneous model routing. Live fixtures with actual speech are still needed.
3. Shared setup imports `test_core` and the full integration stack in many domain
   tests. Missing one SDK prevents broad execution. New isolated fixtures reduce
   that coupling for this subset; they do not replace integration tests.
4. Fresh-schema tests cannot establish upgrades, restart recovery, household-data
   preservation or deployment. Those gates remain explicit.
5. Historical prompt counts are not a full final-candidate rerun. Structural tool
   presence/routing and a successful tool response are not persisted effects.
6. Synthetic receipt fixture bytes are sufficient for identity checks, but are
   not valid media-decoder/phone-rendering evidence. File validity and actual
   delivery require separate checks.

## Genuine blockers and continuation

### Operator-assisted backup inspection

`ha_snapshot_probe.py` is a standalone standard-library diagnostic for use in
the existing HA Terminal when direct remote control is unavailable. It uses
`ha apps info` for a filtered current state/options fingerprint, hashes a local
backup, and copies only the database/WAL to a temporary directory. It reads the
copy with `mode=ro` (including copied WAL transactions), reports integrity/counts,
and selects the two known expense amounts/merchant and nearby turns in the same
conversation. This is retrospective evidence, not a live reproduction.

The report stays on HA under `/config/alex-audit/snapshot-report.json`, mode 0600.
It contains selected household conversation text: keep it private and never
commit it. Known configured secrets, common credential patterns, URLs and phone
numbers are filtered. No pairing files or original media are extracted/exported;
original-media integrity and restore tests remain separate. The script makes no
live database, configuration, pairing or release changes.

```sh
python3 audit/independent/ha_snapshot_probe.py --backup /backup/REPLACE_WITH_BACKUP_SLUG.tar
python -m unittest discover -s audit/independent -p test_ha_snapshot_probe.py -v
```

The second command passes **eight additional standalone tests**, including copied
WAL replay, source/archive preservation, scope of exported conversation data,
credential filtering, permissions, path traversal and symlink rejection. These
are separate from the 59-test repair suite above. They use synthetic archives.
The collector retains redacted tool errors and routing decisions, hashes input
event IDs, and preserves zero/false values instead of converting them to blanks.

### Additional Tamil time evidence

The numeric time validator now accepts the colloquial Tamil `ரெண்டு` as two,
alongside `இரண்டு`. Two independent tests use constructed Tamil inputs and real
isolated SQLite: both forms persist the same two-minute due time and explicit
private scope; a model interpretation of twenty minutes is rejected without a
write. The speech/transcription implementation is unchanged. These tests do not
certify model tool exposure, voice ingress, reminder delivery, or resolution of
the reported voice refusal. Those remain open production checks.

### Direct-access limitation

`ha_runtime_checks.py --commit <full-published-SHA>` supplies an operator-assisted
test route when this workspace cannot install the pinned SDKs. It downloads only
that public source snapshot and public dependencies, creates a private isolated
venv, empty data and synthetic household options under `/config/alex-audit/runtime-*`, and runs each
test module in a separate bounded subprocess. Inherited Supervisor/provider
credentials are excluded. Provider HTTP destinations are blocked by the test
environment proxy; localhost remains available for local HTTP contract tests.
The script does not access production Alex data or restart/update an HA app.
It preserves private logs and a summary for failures; it does not claim a Docker
build or actual provider/phone delivery. `--reuse` can reuse the prior SDK venv
only if installation succeeded and pinned requirement bytes are unchanged.
All source, data, audio packages and logs for the new run use a new directory.
The revised runner reports actual test exceptions, execution counts and skips
directly. Its local mechanics checks do not replace the pending SDK rerun.
`--only <check>` is repeatable and marks the result as a targeted run; compilation
still runs. `--offline-routing` additionally checks all configured prompt and
conversation contracts. Its `LIVE_REQUIRED` results remain unfinished, and
known routing failures are printed with their contract and prompt for repair.

The current shell cannot reach its network proxy; requests do not reach HA.
Permission requests stall without an approval control visible to the user. The
managed-environment status reports unrestricted network policy, but this does
not make the shell transport usable. No authenticated browser or remote terminal
tool is available. The existing HA credential has **not** been proven valid or
invalid against the server; asking for another token would not fix this.

Continue in an execution session that can make approved HTTPS requests. First
perform a read-only HA API check, inspect current Alex version/settings/processes,
and take a fresh backup before any production change. Retrieve exact private
voice/quote traces, complete remaining domain/worker/privacy tests and the scope
move implementation, rerun the real dependency/container suite, then release and
deploy. Verify actual database/file/delivery state and pairing after restart.
No additional approval is needed for those already-authorized routine actions.

## Short phone acceptance test — after completion and deployment

1. Speak one genuine new expense in the usual mixed language. Confirm amount,
   currency, merchant and date; independently verify exactly one stored row.
2. Quote its confirmation: “Actually save that to my private.” Verify the exact
   same record/history is owner-private and absent from shared/spouse/group reads.
3. Request an existing receipt and open the delivered original. Compare the
   queued/delivered bytes against its archived hash, not just the caption.
4. Create a reminder due in two minutes, receive it on the intended phone/channel,
   complete it, and confirm no later duplicate/follow-up remains.
5. Add then complete a household task; confirm its history and that no expense,
   plan, or reminder was created unintentionally.

These steps have not been performed on this candidate. Spouse acceptance also
requires the spouse's configured identity; the handover reported only one number.
