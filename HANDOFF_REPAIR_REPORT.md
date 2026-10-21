# Alex 0.5.44: conversation-to-reminder execution repair

## Decision and scope

The known handoff defect has been repaired in code. Clear time interpretations
can now reach the reminder service through a bounded, source-checked argument;
a verified write completes the exact pending task. This does not depend on Alex
gradually learning from household use.

The release version is **0.5.44**. Local and independent evidence is recorded
below; [PR #59](https://github.com/luhgen-dev/alex-mcp/pull/59) records the remote
CI and publication status. Installation and targeted live acceptance are the
user's next step. No live household messages, reminders, devices or configured
provider requests were used for this repair.

This change applies to the existing Python conversation brain and semantic-router
path (`auto`, `grok`, `gemini`, or `openai`). The optional `dot` shopping bridge
has a separate execution path. No database schema migration or new persistent
memory store is required.

The baseline is main commit `aeb3b74f64510103134a4268ca3e5e98c465646d`, version
0.5.43. The preceding candidate and its failures remain in
[`CONVERSATION_CANDIDATE_REPORT.md`](CONVERSATION_CANDIDATE_REPORT.md) and
`evaluation/conversation-20261021/`. The repair's evidence is under
`evaluation/handoff-20261021/`.

## Why the earlier candidate was insufficient

The previous blind review demonstrated that a model could see the pending task
and propose a sensible reminder. Actual backend replay still rejected six of
eight proposals with a missing-time question. The backend reparsed the original
wording without a reliable handoff from the model's interpretation. In two
full-ingress probes, an unclear continuation also caused the original task to be
replaced by its time fragment; the preceding candidate fixed that retention
failure but did not make those time expressions schedulable.

The broader independent gate also had ten disagreements. Its public inputs
omitted relevant runtime evidence: receipt OCR, visible pending choices, and
policies about household-record lookup and automatic attachment delivery. The
repair supplies that actual synthetic evidence and runtime policy text. It does
not edit old independent answers or change the private scoring rules.

## What the repair changes

### 1. A concrete time handoff

`create_reminder` and `update_reminder` accept optional `time_interpretation`.
It contains a verbatim user source span, a relative delay or local date/clock,
a current-message/active-draft relationship, and an ambiguity flag. It contains
no actor ID, recipient, privacy scope, destination or pending-item ID.

The backend checks field names and types, source provenance, stated quantities,
date/weekday constraints, clocks, AM/PM/dayparts, ranges and contradictions.
Source membership alone is not treated as proof that the proposed meaning is
correct. Recognizable constraints in the original wording must support the
interpretation. An unsupported expression or missing material detail still
produces a focused question.

An existing ingress semantic hint is adapted through the same validation. A
separate time-only actor carries the canonical time to the existing date
validator. Raw user text continues to determine permission, scope, recipient
and delivery routing. No additional model round is introduced by this backend
handoff. The existing narrow interpreter can still run where wording needs
normalization; universal reductions in model calls, cost or latency are not
claimed.

### 2. Exact pending completion

Ingress supplies the exact pending ID when it has grounded a continuation. If
the narrow interpreter is uncertain but the main brain recognizes a continuation,
the brain may request `ACTIVE_DRAFT`. Runtime code resolves that relationship
only through the frozen, owner/chat-bound state and rereads the exact pending row.
The service independently checks owner, conversation, kind and pending status.

Successful create receipts carry that locally validated ID. Ingress requires an
actual reminder row from the current authenticated message and either its own
exact prior binding or the successful durable tool receipt before resolving a
draft. It never falls back to the newest pending request. Rejected continuation
proposals retain the task; a separate, fully specified new reminder leaves the
prior draft alone.

Natural references such as “set that reminder for tomorrow” are treated as
references to a grounded task. Merely containing the word “reminder” no longer
forces that phrase to become a separate new task. Explicit new-task wording
still wins.

### 3. Stable relative times

Relative delays start from the authenticated message timestamp. Model or queue
delay does not restart the countdown. If the intended deadline has already
elapsed, Alex asks for a future time. A newer time-bearing correction supersedes
an older fragment.

Older pending text does not store a transport timestamp for each fragment. A
bare “yes” to an old relative duration therefore requests a fresh duration;
the backend does not invent the original anchor. Legacy internal callers with
no transport timestamp retain their processing-time fallback. Normal ingress
supplies the timestamp.

## Verification

The full offline suite completed **692 tests: 690 passed, 2 skipped, no failures
or errors**. This includes **26 new full-ingress tests** and **23 new backend
time tests**, plus packet-evidence and independent-review provenance tests.

The 26 integration cases execute ingress, brain orchestration, real in-process
MCP tools, backend validation, database writes, pending completion and outbound
binding. Provider decisions and the narrow continuation interpreter are
scripted. They verify all six previously rejected natural phrases, genuine
ambiguities, exact/competing/foreign/closed references, a cancellation race,
new-task separation, group/private/spouse routing, duplicate inbound delivery,
forged source evidence, incorrect model date arithmetic and receipt-time delay.

The first broad run found a compatibility regression: an empty current message
with a locally authorized quoted request could lose its raw time constraints.
The legacy path now retains that authenticated raw quote context, with a direct
regression check. Three existing accumulated-time checks and the original
pending lifecycle tests remain intact. The first new integration run also
exposed the omitted “from the current time” phrase; that source grammar was
repaired and the entire integration suite passed afterward.

A later full-suite run exposed an import-order error in a new fixture test:
it loaded application settings before the existing disposable-household setup.
Importing that setup first restored isolation; all 681 tests then completed
successfully. This was a test harness repair, not a change to household access.

### Broader review corrections

The first fresh 388-packet review scored **369/388**, and its original raw
answers, imported snapshot and score are preserved. Its 19 disagreements exposed
a mixture of runtime gaps and incomplete fixtures. They were not replaced with
expected answers.

Named saved-item deletion now exposes the authorized search needed to obtain an
exact item ID. Picture retrieval descriptions distinguish search metadata from
retrieving the original attachment. Runtime policies explicitly cover tentative
shopping questions, sender-only private scope, empty zero-balance stash creation
with the tool's built-in MYR default, and automation drafts without deployment or
immediate device control. Public packets export those same runtime instructions.

Successful bill-payment fixtures now state RM120; reminder fixtures state AM/PM;
availability fixtures state an interval; and automation fixtures state both a
trigger and action. Claimed-reminder handoff scenarios include visible synthetic
context identifying the claimed reminder. The departure tool now explains that
missing travel time returns before the roster is read, so an explicit shift
question still needs its roster lookup.

Original incomplete requests remain covered. Six new public packets require
clarification for incomplete automation and ambiguous English/Tamil times.
Focused regressions retain missing-amount bills, broad availability and unbound
handoff requests, accepting a direct question or safe lookup followed by a
question. The existing scorer cannot express both first-decision choices for
those cases, so it was not weakened. Actual attempted writes and state changes
remain rejected by the existing observation judge.

The subsequent full public-only review scored **392/394**. Two reasonable
clarifications remained: “Move my dentist reminder to 3pm” omitted the date,
and “Plan my departure for tomorrow's shift” omitted travel duration. The
successful reschedule now states tomorrow; both departure positives now state
a 30-minute commute. Their original incomplete wording remains in focused
lookup-or-clarification coverage. No runtime behavior was changed for these
final fixture corrections.

The final independent result is **394/394**. A verified v6 snapshot carries
**391 exactly unchanged public packets and their original raw-source decisions**
from that full review, plus **3 fresh independent decisions** for the changed
packets. It is not represented as another fresh full-corpus pass. The importer
embeds the complete parent snapshot/public bundle and raw delta sources,
reconstructs all decisions on every load, and rejects edits, stale evidence,
missing/extra coverage or carried-packet drift. Original failures and rationales
remain available. A separate review also caught and repaired an unsupported
snapshot-version bypass in the architecture audit; the scoring gate already
rejected unsupported versions.

| Gate | Completed local result |
| --- | --- |
| Original identical 50 baseline/candidate contracts | Baseline 15/50; repaired candidate 50/50; no regressions in that set |
| Existing offline behavior certification | 1,158 variations across 140 contracts, plus 31 conversation steps; pass |
| Public reasoning packet structural audit | 394 packets; zero routing gaps or forbidden exposure |
| Deterministic reasoning regression oracle | 394/394; pass; separate from model evidence |
| Architecture/release audit | 107 checks; pass, including supported-version and raw v6 evidence verification |
| Structural self-test and stress harness | Pass |
| Python compile and undefined-name check | Pass |
| WhatsApp syntax, control, reaction, quote and FIFO contracts | Pass |
| Independent capability review | 394/394; 391 verified unchanged judgments plus 3 fresh blind judgments |
| Fresh full-ingress blind runtime replay | 10/10 exact first-request matches; 8 actual creations and exact draft completions; 2 clarifications retain their original drafts |
| Remote CI/container/lab | Required before merge; authoritative results are recorded on PR #59 |

The original 50-case comparison is an engineering contract comparison, not an
estimate of live language accuracy. Its new API and direct-brain checks must
not be presented as newly working WhatsApp conversations: baseline ingress
already supplied context in many of those examples.

In its 23 paired provider-packet checks, both versions made 30 scripted main-brain
calls. The repair's median increase in serialized message length was **3,445
characters**, due to the added grounded context and instructions. These are not
token or price measurements. The fresh ten-case runtime capture also invokes
the existing narrow interpreter in eight cases, controlled to return `UNCLEAR`.
The evidence supports a reliability repair; it does not establish lower cost,
fewer total model calls or faster end-to-end interaction.

The final runtime replay executed the independent reviewer's unchanged first
responses through actual ingress, brain orchestration, MCP tools and services.
All eight created records preserve the original `water the plants` task, owner,
DM destination and ordinary Family Shared scope. Their due times match the
user's wording, including receipt-relative delays. Both unclear times remain
pending, with no premature reminder or extra/orphan draft. There were zero
worker errors. The frozen source hashes and first-request bytes match the final
code; the actual receipts, rows and outbound bindings are preserved alongside
the raw reviewer responses.

## Remaining limits

Live configured-provider accuracy, model tokens/cost, end-to-end latency,
WhatsApp delivery/pins and installed HA behavior remain unmeasured. The refreshed
independent corpus tests capability selection from synthetic interpreted input;
its source labels do not certify raw voice authorization or transcription.
The actual runtime replay uses an independent reviewer, not Alex's configured
provider. The known ten phrases are regression examples, not unseen holdouts.

The time grammar is bounded. Compound duration/range expressions, unsupported
calendar phrases such as “the day after tomorrow,” missing AM/PM and ambiguous
“next Friday” can still require clarification. These are explicit limits of this
release, not cases counted as completed actions.
Correct Tamil capability selection does not prove that raw Tamil time wording
passes the bounded backend validator; multilingual live execution remains
unverified.

The existing single-active-draft policy is unchanged: an explicit new reminder
that itself needs clarification creates its own draft and supersedes the prior
unfinished draft. This release does not add a queue of simultaneous unfinished
reminder conversations. It prevents a mere time answer or completed separate
request from accidentally replacing the original task.

Automatic procedural learning is unchanged. Alex's existing explicit memory,
durable pending state and bounded conversation context are not a runtime process
that automatically turns repeated success/failure into reusable learned skills.
The separate [learning audit](LEARNING_CAPABILITY_AUDIT.md) remains applicable.

## Targeted live acceptance after the HA update

Confirm HA reports version **0.5.44** and record the selected AI provider. Test
one conversation at a time with a distinctive temporary task name:

1. Ask for a reminder without its time. Reply “Around seven tomorrow evening
   would work for me.” Verify one reminder for the original task, tomorrow at
   7 PM, and completion of the original pending prompt.
2. Repeat with a different task and “Roughly half an hour from the current time
   please.” Compare the stored due time with the sent-message time.
3. Give “Tomorrow around 7ish.” Verify that Alex asks AM or PM and creates no
   premature reminder. Supply the missing detail and verify the same task.
4. While a missing-time draft is pending, ask for a clearly separate new reminder
   with a full time. Verify the new task is created and the old draft is retained.
5. After the DM cases pass, repeat the continuation in the family group and, if
   useful, for a named recipient. Verify destination, assignment and pending
   pin/unpin behavior from actual WhatsApp and HA observations.

Record the exact messages, timestamps, final reply, reminder task/time/destination,
pending state and any duplicate. Clean up the deliberately created test reminders
after observing their state. Live results determine acceptance and any next repair;
offline passing results alone do not establish production reliability.
