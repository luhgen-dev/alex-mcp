# Alex conversation reliability candidate

> **Historical candidate report.** The decision and failed replay below describe
> the experiment before the interpretation-to-execution repair. They are retained
> as original evidence. See [the v0.5.44 handoff repair report](HANDOFF_REPAIR_REPORT.md)
> for the subsequent implementation, final verification and live-test scope.

## Decision

**Keep the reliability fixes for review; hold the installed upgrade.** The
implementation improves concrete context and execution contracts. However,
replaying a blind reviewer's eight reminder proposals through the actual backend
created only two reminders: the other six asked for the time again. Better
context alone is insufficient to deliver the intended conversational upgrade.
The broader independent model-review gate also has ten disagreements. Live
provider accuracy, cost, latency and WhatsApp delivery remain unmeasured.

Prepared 21 October 2026 against `luhgen-dev/alex-mcp` main commit
`aeb3b74f64510103134a4268ca3e5e98c465646d` (v0.5.43), on
`experiment/conversation-reliability-20261021`. There is no version bump or
deployment in this candidate.

The candidate changes the existing Python `brain.respond` and semantic router.
The optional `ai_provider=dot` shopping bridge bypasses this path. The installed
Home Assistant version and provider configuration were not inspected. The
implementation therefore establishes a benefit for the Python path, subject to
the tests and limitations below.

## What changed and why it helps

1. **One grounded view of the current conversation.** The brain and semantic
   router now receive bounded, actor- and chat-scoped context: the pending
   question, its exact record ID, a validated quoted item, an existing numbered
   list and the received time. This is a read-only projection of existing
   durable records. A reply such as “around seven tomorrow evening would work”
   can be interpreted with the reminder task it answers.
2. **More reliable continuation and topic changes.** Short or novel wording
   retains a small amount of relevant history. A fresh shopping or finance
   request does not acquire reminder mutation tools merely because a reminder
   draft is pending. Closed, foreign and unresolved quoted records cannot
   silently become a different current record.
3. **Exact numbered selections.** `resolve_numbered_choice` accepts optional
   list-kind and list-ID bindings. A quote to an older displayed list stays
   bound across unrelated reads and repeated choices. A stale or invalid quote
   produces clarification instead of selecting from a newer list. Existing
   backend permission checks still validate the selected record.
4. **Tighter action execution.** A provider tool call must have been offered in
   that model request. Discovery cannot authorize a second, previously
   unoffered call in the same response. Coordinated negation and hypothetical
   requests are filtered after all routing overrides. Semantic suggestions
   retain the original required tools when the exposure cap is reached.
5. **Reuse a validated interpretation.** When ingress has already supplied a
   validated reminder interpretation, the brain reuses it instead of invoking
   the live semantic router again. A focused regression test verifies that
   branch; no live latency saving is claimed.
6. **Keep the task when a reminder attempt needs clarification.** If a proposed
   reminder is rejected for its date/time and the current turn is not an
   explicitly new reminder, ingress can retain the exact validated pending
   draft. It rechecks the ID, owner, chat, kind and pending status before binding
   the outgoing question. It neither creates a reminder nor rewrites or resolves
   the old draft. The pre-action conversation state is retained as a detached
   trace snapshot, so later shadow review and this recovery use the same state.

The principal runtime files are `conversation_state.py`, `brain.py`,
`shadow_router.py`, `ingress.py` and `mcp_server.py` under `alex-mcp/app/`. Identity, ACLs,
delivery destinations, time validation and idempotency remain backend-owned.

## Before/after evidence

The same 50 new contract checks ran in separate processes against the unchanged
baseline and the candidate, using disposable databases and scripted model
responses.

| Evidence path | Baseline | Candidate | What it establishes |
| --- | ---: | ---: | --- |
| New conversation-state API | 0/12 | 12/12 | The new API provides the specified bounded bindings. Its absence in the baseline is not evidence of a live incident. |
| Direct brain prompt/context | 2/16 | 16/16 | The captured request contains the required context and tool exposure. These calls bypass ingress. |
| Routing helpers | 10/12 | 12/12 | Required original tools survive semantic merging and contextual history is retained. |
| Direct brain with real MCP/backend and a scripted provider | 0/7 | 7/7 | The specified binding and tool-execution protections operate against a real disposable database. |
| Ingress through brain and backend, with a scripted provider | 3/3 | 3/3 | Existing shopping interruption, exact expense correction and duplicate-inbound behavior remain correct. |
| **Total** | **15/50** | **50/50** | **35 improved contracts; no regressions within this set.** |

The result is a contract comparison, not a percentage estimate of natural
language accuracy. In particular, the three ingress cases already passed on
the baseline. Failures in direct-brain fixtures do not prove that those same
messages failed through the deployed WhatsApp ingress path.

One concrete backend example: a scripted provider attempted an unoffered
shopping-item removal during a read request. The baseline executed it; the
candidate rejected the call. Other executed cases confirm that quoted list IDs
stay bound and that an invalid old list does not fall back to an unrelated new
one.

Full case outcomes, source fingerprints and paired request metrics are in
[`comparison.json`](evaluation/conversation-20261021/comparison.json).

## Blind review of actual runtime requests

Ten natural reminder follow-up phrases were captured from each revision's
actual first `brain.respond` model request, including its full messages and
offered tool schemas. The 20 packets were shuffled and given opaque IDs. A fresh
isolated reviewer saw only those packets, without revision labels, test
expectations, repository source or earlier answers.

| Reviewer response | Baseline packets | Candidate packets |
| --- | ---: | ---: |
| Proposed a reminder with the grounded task | 0 | 8 |
| Asked for the missing task/action | 10 | 0 |
| Asked to resolve a genuinely ambiguous time | 0 | 2 |

The two candidate clarifications concerned “tomorrow around 7ish” without AM/PM
and “tonight, sometime after eight.” Relative delays used the explicit
`received_at_utc` instruction. The corrected capture aligns the Python clock,
actor receipt time and seeded fixture rows at the same instant. The eight
proposed calls use the visible watering task; their schema has no pending-item-ID
argument, so the proposals alone do not prove that the exact draft was completed.

This supports the causal explanation that supplying grounded state makes these
requests answerable. It remains a small, fixture-derived reasoning review.
The reviewer was not Alex's configured provider. Captures bypassed ingress and
disabled the live router. These phrases are not an unseen held-out benchmark.

**Normal ingress already supplies much of this context on the baseline.** A
separate disposable-database probe recovered the original question and draft ID
for eight of the ten phrases with zero semantic-interpreter calls. The other
two have an existing semantic-rescue path and recover when that interpreter is
stubbed with a positive result. These direct-brain results must therefore not
be presented as eight newly working WhatsApp conversations. See
`baseline-ingress-continuation-probe.json` for that qualification.

An initial capture used a fixed actor receipt time but the real processing
clock, making three proposed relative times already past. Its raw packets and
review are retained with the `preliminary-` prefix. That run is superseded for
time evaluation by the clock-aligned capture. SQLite's default clock was not
overridden: only the explicitly seeded fixture rows were timestamped to match
the fixed Python clock before capture. Subsequent backend execution is assessed
separately rather than assuming proposed calls succeeded.

### Actual backend replay of the fresh proposals

Each fresh decision was then injected as the provider's next response into the
same disposable direct-brain fixture. The full first request matched the
reviewed packet byte-for-byte in all 20 cases. The replay preserved the actual
tool receipts and database state. A fixed neutral terminal response completed
tool turns; it was not a second independent reasoning step or a success claim.

| Backend outcome | Baseline | Candidate |
| --- | ---: | ---: |
| Reminder created | 0 | 2 |
| Proposed create returned a time clarification | 0 | 6 |
| Reviewer directly asked for clarification | 10 | 2 |
| Worker errors | 0 | 0 |

Both created reminders had the watering task, expected due time, authenticated
owner, current source message and DM destination. They came from “Later at
11.30 in the evening please” and “Forty minutes later would be alright.” All
drafts remained pending in this direct-brain replay; normal draft completion
belongs to ingress after a successful creation.

The other six proposals reached `REMINDER_NEEDS_TIME` in the existing backend
validator. For example, the reviewer interpreted “Around seven tomorrow evening
would work for me” as tomorrow at 19:00, but the backend still returned “What
time should I remind you?” The candidate did not weaken that validator or count
the rejected proposal as a completed action.

**This is the main remaining conversational bottleneck exposed by the
experiment:** useful model interpretation is not consistently handed to the
existing time-validation path. Improving the grounded semantic handoff and
testing its durable effects is a more direct next step toward natural,
reliable interaction than adding more persistent memory alone. This is an
inference from the replay; it does not establish a live failure rate.

The packet bytes, raw independent decisions, unblinding provenance and paired
analysis are preserved under `evaluation/conversation-20261021/` as
`runtime-packets.json`, `runtime-review-decisions.json`,
`runtime-review-provenance.json` and `runtime-review-summary.json`. The complete
backend replays are `runtime-replay-baseline.json` and
`runtime-replay-candidate.json`.

### A draft-loss regression found and repaired

Three additional scripted probes exercised the full ingress, brain and real
MCP/backend path. Two forced the existing continuation interpreter to return
`UNCLEAR` for the relative-time fragments “Forty minutes from the message I just
sent please” and “Roughly half an hour from the current time please.” Their
provider calls used the fresh reviewer's proposals, with the relative due time
translated to the probe's current aligned clock. These are separate scripted
lifecycle checks, not blindly reviewed full-ingress requests.

Before the retention fix, the backend rejected both time phrasings and ingress
cancelled the original watering draft, replacing it with a draft containing
only the time fragment. That was a real candidate regression discovered during
testing. The original failed runs are preserved in
`ingress-lifecycle-before-retention-fix.json`.

After the narrow fix, both probes leave the entire original draft unchanged and
`PENDING`, with the outgoing question bound to its exact ID. They create no new
draft or reminder. The third probe creates an explicitly new dentist reminder
while leaving the watering draft unchanged. All three final probes have zero
worker errors. Six additional tests cover these paths plus exact owner/chat/
status validation and the requirement for an actual rejected-create trace.

The same ten candidate direct-brain proposals were also replayed after the fix.
Every first request still matched the original reviewed messages and schemas
exactly; the original decisions were not edited. The outcome remains two
reminders, six backend time clarifications and two reviewer clarifications.
See `ingress-lifecycle-after-retention-fix.json` and
`runtime-postfix-summary.json` for the final verification. The repair prevents
task loss; it does not make the unsupported time wording schedulable.

## Existing regression and stress coverage

| Gate | Result |
| --- | --- |
| Full candidate unit suite | 597 run; 595 passed, 2 skipped; no failures or errors |
| Full baseline unit suite | 520 run; 518 passed, 2 skipped; no failures or errors |
| Existing offline behavior certification | 1,147 variation checks across 137 contracts, plus 31 conversation steps; no failures on either revision |
| Router context regressions | 15 tests, including 300 seeded malformed-state cases; pass |
| Public reasoning packet structural audit | 388 packets; zero routing gaps or forbidden tool exposures |
| Existing deterministic reasoning oracle | 388/388 pass; deterministic engineering evidence |
| Structural self-test, stress harness, catalog and compile | Pass |
| Final architecture audit | 106 checks pass |
| WhatsApp bridge syntax and control/reaction/quote/FIFO contracts | Pass |
| Independent frozen model-review gate | **378/388 agree with the scorer; 10 disagreements; FAIL** |

The existing stress harness exercises repeated and concurrent writes, privacy
scope, receipts, durable reminders, destination routing, memory selections,
diary conflicts, goals, assets, monitoring, tool caps and database integrity.
The new tests concentrate on pending follow-ups, topic switches, negation,
stale/foreign/closed references, multiple numbered choices, schema sanitization,
tool exposure and interpretation reuse. Voice/image/PDF labels in the offline
corpus exercise interpreted inputs, not real transcription or phone rendering.

The candidate's 77 additional unit tests consist of the 50 comparison checks,
15 router-context tests, four review-provenance tests, two interpretation-reuse/
trace tests and six draft-retention tests. These counts overlap the table above
and must not be summed as independent samples. The full unit suite, paired
comparison, behavior certification, packet audit, both reasoning scorers,
architecture audit and compilation were rerun after the last runtime change.
The earlier stress, catalog and bridge runs are retained with their provenance;
the bridge source did not change.

The local test environment required unrestricted execution for the in-process
MCP worker/thread handoff: the identical baseline call hung inside the managed
sandbox and passed outside it. Both revisions used the same Python 3.12 virtual
environment, installed requirements and disposable fixtures. These runs did
not use live provider credentials or household services. Container-image CI
has not been claimed as a local pass.

## The remaining model-review gate

Adding optional exact list binding changed 59 of the 388 legacy public packets;
329 were byte-identical. A fresh isolated reviewer evaluated only the 59 changed
packets, with no private contracts, expected answers or earlier decisions. Its
raw answers were retained, including disagreements. The other 329 retain their
original decisions and original, mixed provenance.

Fresh agreement is **49/59**. Overall agreement after carrying the identical
packets is **378/388**. This is not a fresh independent review of all 388.

| Disagreement group | Count | Interpretation |
| --- | ---: | --- |
| Receipt requests without visible receipt content/currency | 3 | Reviewer requested missing information; legacy scorer expected a finance write. |
| Bare numbered answers without the corresponding list/conflict context | 3 | Reviewer requested context; scorer expected a resolver. |
| Numbered retrieval with unnecessary discovery | 3 | Reviewer selected discovery as well as an already offered resolver; an efficiency/policy disagreement. |
| A codeword visible in chat history | 1 | Reviewer answered from history; scorer required a durable-record lookup. The public packet omits the full brain policy for household records. |

These outcomes include packet-context limitations and real policy/efficiency
disagreements. They should not all be labelled runtime regressions or dismissed
as harness defects. The legacy scorer also does not verify exact tool arguments
or durable effects; the new real-backend tests provide separate evidence there.

Snapshot v4 records the fresh and inherited provenance explicitly. The
deterministic oracle's fingerprint was updated for the schema change, with its
expected-decision logic unchanged. The independent review's failing result was
not rewritten to match expectations, and the CI gate was not disabled. A draft
PR is therefore expected to fail at this gate until it is resolved.

Raw changed packets, independent decisions and the scored report are preserved
as `legacy-changed-public-packets.json`, `legacy-independent-decisions.json` and
`legacy-model-review.json` in the evidence directory.

## Efficiency and practical limits

Across the 23 cases that reached the model on both revisions, the scripted
provider call count was **30 before and 30 after**. Median first-request message
size rose from **15,414 to 16,846 characters**, approximately **9.3%**; the median
paired increase was 1,432 characters. These are serialized message characters,
excluding tool-schema size, and do not measure tokens, billing or latency. The
candidate has three additional recorded packet cases because some baseline
checks stopped before reaching their capture point; aggregate unpaired call
counts should not be compared.

The candidate adds bounded context and avoids a duplicate router call on the
validated ingress branch. It does not yet establish a net live speed or cost
improvement. Normal greetings and health-check fast paths remain covered by
the existing tests.

A separate 24-scenario, 48-turn holdout specification and an export/scoring
harness were prepared. **All 48 turns remain unexecuted.** Harness self-checks
verify that private expectations are excluded, later turns need actual prior
observations, and missing execution cannot be reported as a pass. Its presence
adds test design, not successful model evidence.

Natural language has unbounded variations. This campaign provides broad
regression coverage plus targeted adversarial cases; it cannot certify every
possible message or a universal error rate.

## Hermes-style learning

The source audit found explicit saved facts, bounded conversation history,
pending state, versioned configuration, transaction-correction provenance and
router telemetry. It found **no runtime pipeline that turns corrections or
successful workflows into persistent reusable behavioral skills**. Engineering
changes currently supply that improvement loop.

Hermes documents agent-created and editable skills for reusable procedures, and
its `/learn` implementation instructs the agent to save or extend such skills.
Those mechanisms store reusable instructions; they do not change model weights.
Primary sources checked on 21 October 2026:

- [Hermes skills documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes persistent memory documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Hermes learn prompt source](https://github.com/NousResearch/hermes-agent/blob/main/agent/learn_prompt.py)

See [`LEARNING_CAPABILITY_AUDIT.md`](LEARNING_CAPABILITY_AUDIT.md) for the exact
Alex source anchors. No automatic learning feature was added in this candidate.
A later reviewed correction-to-example workflow could be useful after the
interpreter is measured, with explicit owner scope and a distinction between
changing one record and learning a general preference.

## Before an installed upgrade

1. Address the observed interpretation-to-time-validation failures, then resolve
   the ten model-review disagreements using complete runtime packets and
   declared policies. Preserve independent decisions and score exact arguments
   and backend effects. Preserve the backend's time and ownership validation;
   a model's proposed timestamp is insufficient evidence by itself.
2. Execute the prepared multi-turn holdout through the real ingress and
   configured provider against a disposable backend. Include mixed-language
   wording, interruptions, restarts, ambiguous recipients, duplicate deliveries
   and uncertain write outcomes; measure first-response and completion latency,
   tokens and cost. The current environment has no live provider credentials
   for that campaign.
3. Confirm the installed version/provider and review the resulting evidence
   before selecting an upgrade. Real WhatsApp voice, quote metadata, reminder
   delivery and Home Assistant actions need their own controlled live checks.

## Reproduce the offline gates

From the candidate checkout, with Python 3.12 and
`alex-mcp/app/requirements.txt` installed:

```sh
python -m unittest discover -s tests -v
git worktree add --detach /tmp/alex-baseline aeb3b74f64510103134a4268ca3e5e98c465646d
python tools/conversation_comparison.py --baseline /tmp/alex-baseline --candidate . --report /tmp/conversation-comparison.json
python alex-mcp/app/behavior_cert.py --mode offline --phase all --report /tmp/alex-behavior.json
python alex-mcp/app/human_ai_lab.py --mode selftest --phase all --packets /tmp/alex-packets.jsonl --report /tmp/alex-packets-report.json
python alex-mcp/app/real_ai_snapshot_review.py --report /tmp/alex-independent-review.json
python alex-mcp/app/chatgpt_reasoning_review.py
python alex-mcp/app/final_audit.py
```

Run the commands individually: the independent snapshot command currently exits
nonzero with the ten preserved disagreements. Re-scoring saved independent
decisions reproduces that score, not the original fresh reasoning pass. The
runtime capture worker is preserved in the evidence directory for reproducing
the first-request extraction; fresh reviews require a new isolated reviewer.

Evidence identities are recorded in `gates.json` and `manifest.json` alongside
the saved reports. The comparison was captured before committing, so both Git
revision fields name the base commit. The differing runtime source SHA-256
values identify the tested source bytes, including untracked candidate modules.
The final runtime source SHA-256 is
`0287e3ef6a2b018cd7cd2897a18b4013b54891936e56250c950e837682833639`.
The evidence directory's `README.md` explains the saved failures, final replays
and reproduction commands.
