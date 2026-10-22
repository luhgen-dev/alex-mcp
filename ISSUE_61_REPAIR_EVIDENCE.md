# Issue 61 repair evidence (candidate — not live certification)

Baseline: `d82ece52d3cea1edb25b8f13ccd83446b5c2fbc7`, add-on 0.5.45.
This matrix retains every stable handover ID, including incidental defects.
All fixtures are synthetic. Passing Python/socket tests do not prove WhatsApp
rendering, original-media codec playability on a phone, or physical HA operation.

| IDs | Root correction | Reproduction/regression |
|---|---|---|
| GOAL-01/02/03 | Required action survives tool cap; frozen owner/chat-bound numbered goal list; target update retains identity; create rejects editing/activation and same-scope duplicates | `test_issue61_routing_goals.py`, `test_issue61_media_finance.py` |
| PDF-01 | Frozen ledger export does not independently query goals/planning; active and quoted scope checked | `test_issue61_media_finance.py`, existing report privacy tests |
| TASK-01/02 | Immutable inbound provenance plus task event history; deterministic original-instruction rendering; no reminder-domain substitution | `test_issue61_routing_goals.py`, `test_issue61_tasks_reminders.py` |
| TASK-03/04/05 | Scoped durable task-reminder clarification retains task/time/anchor; closed-choice recovery; no clarification pins; unrelated current requests win | `test_issue61_tasks_reminders.py`, conversation pending tests |
| NL-01 | Indirect inability to attend asks cancellation/rescheduling clarification without mutation; explicit follow-up reaches diary tools | `test_issue61_routing_goals.py` |
| REM-01/02/03 | Typed quoted claim uses canonical reminder; obsolete clarification pins cleaned; fixed pre-due claim scheduling; full local date/time/timezone | `test_issue61_tasks_reminders.py`, existing lifecycle tests |
| MEDIA-01/02/03 | Typed current/quoted/pending media exact binding, integrity-checked original retrieval, scoped idempotency and media-kind filter | `test_issue61_media_finance.py`, `test_issue61_media_upgrade.py` |
| WORK-01/02 | Authenticated own-work DM read scope, atomic private profile import, effective-dated roster/OT versions, six entitlement categories | `test_issue61_work_brief.py` |
| BRIEF-01/02/03/04 | Runtime local calendar, owner profile ContextVar, labeled due/overdue versus total-open tasks, one canonical acknowledgement across regenerated versions | `test_issue61_work_brief.py`, `test_issue61_tasks_reminders.py` |
| MENU-01/02 | Natural menu handled before pending reminder; registry-backed enabled-capability navigation | menu and capability tests |
| EAR-01 / Journal | Durable capture/stop fallback, dedicated indexed journal recall, scoped frozen numbered navigation, owners/status/history, explicit same-scope promotion links | `test_discussion_journal.py` |
| Additional stash gap | Successful private debit/expense atomicity, retry and insufficient funds leave no partial writes | `test_issue61_media_finance.py` |
| Additional delivery gaps | Claimed pre-due scheduling, post-due group release and original media bytes | task/reminder and media tests; phone transport still live-only |

| Progressive fallback | Authenticated swipe-source binding; immediate guarded recovery; successful tool evidence plus explicit consent; scoped/versioned exact-context hints, collisions, history; software errors separately reported | `test_progressive_teaching.py`, `test_issue61_teaching_ingress.py` |

## Verification status

Release candidate: **0.5.46**. Local evidence at first PR publication:

- Python suite: 805 tests run, 2 intentionally skipped, no errors/failures (21.214s;
  803 passed; two packaged-runtime cases require CI image).
- Offline certification: 164 contracts, 152 prompt contracts, 12 conversations,
  33 conversation-step checks, 1305 variant passes, zero failures and zero discovery-dependent cases. Tool exposure cap remains six.
- Public human-AI corpus: 443 packets; zero routing gaps/forbidden exposure.
- External Lab unit/self-test/Tier-A stress/catalog/offline checks passed. The
  deterministic public-packet oracle was reviewed and recertified: 443/443 pass.
  This is not an independent model test.
- Four JavaScript WhatsApp payload/FIFO contracts passed; changed Python modules
  compile and targeted pyflakes/diff whitespace checks passed.
- CI #1057 built the HA image, verified media binaries and passed the packaged
  startup/socket smoke. Final candidate/post-merge CI must reconfirm the exact SHA.

### External independent-review blocker (not a pass)

CI #1057 and External Lab #697 recorded GitHub Copilot **monthly quota exhausted**
at the first isolated review shard. The original independent snapshot is retained
unchanged and is stale for this corpus. Artifact evidence explicitly records
`DEFERRED_EXTERNAL_REVIEW`, reason `github_copilot_monthly_quota`, and
`not_claimed_as_pass: true`. No provider restriction was bypassed.

The existing baseline release policy (`final_audit.py`, both workflows) permits
this documented deferral only with an exact-corpus deterministic oracle and all
other gates passing. It still fails closed on the stale snapshot when independent
review resumes. This repair does not change or weaken that policy. Once the
provider quota is restored, the existing isolated review must generate and verify
fresh evidence; it remains an external acceptance item, not completed engineering
evidence from this run.

The final PR/CI records are authoritative for exact tested commit and packaging.
Do not treat a green deterministic simulation as live certification.

Journal extraction remains a reviewable AI interpretation. Source IDs, participants
and cited date evidence are validated; this is not proof that every semantic
omission or paraphrase is correct. No journal entry automatically schedules an
external action. Review decisions/actions before explicit promotion.

## Migration and latency

Startup expands old restrictive AUDIO/VIDEO SQLite constraints transactionally,
retaining originals and relationships; new journal, reference and teaching tables
are additive. Upgrade fixtures prove retry/restart and rollback on migration error.
Back up add-on data before installation; restore the matching pre-update backup
for rollback rather than running old code against a migrated database.

Normal teaching lookup is one local SELECT, with no provider call or per-turn DDL.
A synthetic 100-sample no-match lookup measured median 1.247ms / p95 2.348ms in
the cloud test runner; this is not HA hardware or end-to-end WhatsApp latency.
Correction recovery uses the existing guarded reasoning flow; learning approval is
local/deterministic. Ear structured stop uses one tool-free provider request and
persists a durable fallback if unavailable. End-to-end phone latency is **unmeasured**.


## Live-only acceptance (not performed by the cloud test runner)

- Install the released HA update, restart, inspect add-on startup logs.
- Actual WhatsApp voice/video typed and swipe-reply save, phone playback.
- Full local reminder date; typed claim; claimant DM at due time; post-due release
  yields a fresh group claim card with correct lifecycle markers.
- Regenerate today's private brief and acknowledge the second version.
- Ear group capture/stop/restart/retrieval and action updates from real devices.
- Privately provision the owner's profile, verify actual effective dates and
  balances. Do not publish private values in GitHub or test artifacts.
- Swipe-reply a genuinely misunderstood request, verify the recovered result, then
  explicitly approve the interpretation; confirm it does not cross owner/chat/scope.
- Spouse onboarding and real multi-participant delivery remain untested.

Issue 61 remains open until external acceptance is recorded or explicitly
acknowledged as blocked. An implementation PR alone is not live completion.
