# Independent audit checkpoint — incomplete, not a release certification

Baseline: upstream `17b37cd998b58b926e3ca00fd8f13d8a8bbcb070` (0.5.54, PR #70).
Evidence collected 2026-11-02. This branch is a **draft repair candidate**. It has
not been merged, installed, restarted, or verified on Home Assistant or WhatsApp.
No household database, credentials, pairing files, or unrelated HA services were
changed. No provider calls were made. The speech/transcription implementation and
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

**Voice expense refusal:** exact audio/transcripts and production tool traces have
not been obtained. Do not invent the spoken wording or infer a transcription
failure from a screenshot. The existing tests mock transcription and script tool
calls; they cannot establish spontaneous understanding by the deployed model.

**Quoted expense moved to private:** the registry exposes no expense scope-move
tool. `correct_expense` preserves the existing space. The current phrase requesting
a private destination produces private read scope, which excludes the original
shared expense from a normal search. The independent test reproduces that
search-boundary behavior; it is **not a passing test for the requested move**.

An eventual move must separately authorize the exact quoted source and the
destination, preserve identity/history, deal with correction chains and original
media links, revalidate queued delivery/report/selection context, and avoid
moving another household member's records. Broadening all private reads or
changing a category to `private` is not a repair. The separate historical and
latest expense examples must be traced independently in production.

## Feature-by-feature status

“Local” below means isolated synthetic SQLite/files. None of these rows is an
end-to-end production pass. [tool_inventory.json](tool_inventory.json) enumerates
all 151 statically registered tools, and deliberately marks their actual MCP
execution in this audit `NOT_RUN`. Backend tests do not certify tool discovery,
argument generation, conversational understanding, or phone delivery.

| Capability family | Fresh evidence / current result | Remaining acceptance work |
| --- | --- | --- |
| Identity and conversation | Unknown sender rejected; group actor has only shared membership. Four Node contracts passed for quoted context, reactions, controls, and FIFO. | Real sender/group binding, quote continuity, overlapping requests, delayed follow-ups, and conversation privacy. |
| Multilingual voice and media | Voice implementation reviewed without edits; receipt original-file integrity repaired locally. | Actual English/Tamil/Tanglish/Malay/mixed audio, expense reproduction, STT accuracy, language/amount preservation, disabled/budget/failure behavior on the deployed configuration. |
| Finance, receipts, privacy corrections | Local exact cents, retry identity, append-only history, totals, ordinary/private/spouse/group isolation, missing/corrupt originals. Validation repairs pass. | Requested quoted privacy move is unimplemented; actual screenshot rows and correction chains need production inspection. |
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
| `python-ingress` → `ingress.py` | Source inspection only; MCP/provider dependencies prevent execution. |
| `python-outbox` → `outbox.py` | Real local durable queue states with transport adapter mocked; no WhatsApp delivery. |
| `python-scheduler` → `scheduler.py` | Source inspection only; HA dependency unavailable. |
| `python-ha-mobile` → `ha_mobile.py` | Inventoried; live worker/action verification outstanding. |
| `python-grocery-prices` → `grocery_prices.py` | Local lease/revision/retry state tests with lookup mocked. |
| `python-dot` → `dot_runtime.py` | Separate local DOT bridge/events/onboarding/startup tests partially pass; packaged startup checks skipped, HTTP module times out in this mismatched environment. Keep feature disabled. |

Startup runs `init_db.py` and logs a warning rather than stopping when `selftest.py`
fails. A running service is therefore not evidence that structural checks passed.

## Exact automated evidence and its limits

- [candidate.json](candidate.json), [candidate.log](candidate.log): **46 tests,
  zero failures/errors/skips**. SHA-256 values bind the five changed runtime
  modules and the independent fixture/test files to the results.
- [baseline.json](baseline.json), [baseline.log](baseline.log): the same 46 tests
  against the original five runtime modules produce **35 failed assertions and
  four errors, affecting 21 distinct test methods**. Subtest failures are counted
  separately from test methods. The four errors are the malformed-JSON and three
  numeric cases, not missing dependencies.
- Local Python 3.12.14. Python compilation passed. Four existing Node contracts
  and bridge syntax passed. These are not a container build or a paired session.
- Earlier isolated execution of 53 available test modules: six modules passed,
  46 failed, and one timed out. Forty-five failures encountered missing
  `requests`; another encountered incompatible installed MCP APIs. Passing
  modules accounted for 54 test methods (52 passed, two packaged-runtime skips),
  including nine earlier independent tests. **Do not add this overlapping,
  earlier count to the final 46-test result.**
- The installed MCP package is 1.29.0; the app pins 2.2.0. `requests`, `openai`,
  and `websocket-client` are unavailable. Dependency installation could not reach
  the package index. The full suite, structural/stress checks, current routing
  certification and container build are not passed or waived.
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

The second command passes **seven additional standalone tests**, including copied
WAL replay, source/archive preservation, scope of exported conversation data,
credential filtering, permissions, path traversal and symlink rejection. These
are separate from the 46-test repair suite above. They use synthetic archives.

### Direct-access limitation

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
