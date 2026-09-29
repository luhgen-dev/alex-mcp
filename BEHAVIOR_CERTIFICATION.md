# Alex Behaviour Certification Rig — Independent Review Handover

## Purpose

Alex already had a strong deterministic Phase-3 stress harness in
`alex-mcp/app/stress_test.py`. That remains **Tier A** and is intentionally not
replaced.

The v0.4.4 WhatsApp smoke test exposed a different failure class: the backend
could support a capability correctly while Alex behaved differently depending
on ordinary phrasing, conversation context, modality, privacy scope, or object
focus. The new rig therefore adds **Tier B behavioural certification**.

The release path is now:

1. **Tier A — deterministic invariants**
2. **Tier B — user-facing language/conversation behaviour**
3. **Explicit manual gates — only external WhatsApp/physical-device facts**
4. **Short final production acceptance**

A capability is not certified because one wording worked. Equivalent ordinary
wordings, required state effects, identity, privacy, and conversational
continuity must agree.

## Files

- `alex-mcp/app/behavior_contracts.py` — owner-visible behavioural contracts
- `alex-mcp/app/behavior_capabilities.py` — stable capability -> current MCP adapter
- `alex-mcp/app/behavior_cert.py` — catalog/offline/live certification runner
- `alex-mcp/app/runtime_clock.py` — one injectable Alex runtime clock
- `alex-mcp/app/phase_certify.py` — one-command phase gate
- `tests/test_behavior_cert.py` — rig self-tests and mutation tests
- `alex-mcp/app/stress_test.py` — existing Tier-A stress harness
- `.github/workflows/ci.yml` — rig/catalog/offline exercise and normal CI

The current catalog contains **106 prompt contracts**, **10 multi-turn
conversation contracts**, and **8 explicit external manual gates** across the
historical Phase 1, Phase 2, and Phase 3 capability set.

## Changes made after Claude's first independent review

Claude's first verdict was **REQUIRES MATERIAL CHANGES**. The following changes
directly address those findings rather than weakening contracts to make v0.4.4
green.

### 1. The judge now proves effects, not merely tool calls

Contracts can assert:

- exact durable rows and field values;
- exact row deltas;
- substring/date identity where appropriate;
- tables that must remain unchanged;
- exact original attachment identity and exactly-once queueing;
- exact fake-HA entity/end state and unrelated-entity invariance;
- private fixture IDs/content/media/path must not appear in tool results,
  replies, or outbound attachments;
- clarification and privacy-refusal outcomes;
- compound requests that require **all** named capabilities;
- no invented non-zero goal baseline;
- English-only output.

This closes the demonstrated false-pass cases from the first review: wrong
amount/currency, wrong receipt/image, no picture, private media leak, invented
RM500/month goal baseline, wrong HA entity, and discovery-only reminder paths.

### 2. Live certification now enters through production ingress

Live cases call `ingress.process(payload)`, not `brain.respond()` directly.
Therefore the internal path includes:

- inbound idempotency claim;
- typed/voice turn normalization;
- image/PDF media processing;
- quoted/swipe-reply context resolution;
- attachment pairing rules;
- normal brain/tool orchestration;
- exact `outbound_messages` rows that the WhatsApp egress worker would send.

The outbox worker itself is **not started**, so certification never sends a real
WhatsApp message.

Voice cases use a synthetic audio payload and a patched local transcription so
post-transcription production behaviour is exercised without external STT cost.
Real WhatsApp audio transport remains a manual gate.

Image/PDF contracts now exercise ingress/media persistence with deterministic
local OCR/text fixtures, including exact event-media linkage. Actual phone-side
rendering remains manual.

### 3. Core fixture is valid

The synthetic dentist event moved to **17:00 on 1 October 2026**, outside the
synthetic morning shift. The seed fails closed if that event is rejected and CI
contains a seed-validity self-test.

### 4. Discovery can never be the successful capability

`discover_alex_tools` is represented only as `routing.discovery`.
A discovery-dependent offline case becomes `LIVE_REQUIRED`; live certification
must subsequently execute the real semantic capability and satisfy its state
assertions.

### 5. Contracts are decoupled from today's MCP tool names

`behavior_capabilities.py` defines stable semantic capabilities such as
`finance.write`, `memory.get`, `goal.create`, `home.control`, etc.
The adapter maps them to the current MCP implementation.

This is intentional preparation for the planned v0.5.0 facade: the behavioural
catalog should survive a tool-surface redesign while one adapter changes.

New MCP tools still trigger catalog drift unless they are classified, and every
implemented owner capability must have behavioural coverage.

### 6. One shared certification clock

`runtime_clock.py` is the single Python-level runtime clock for user-visible
date/time decisions. Production uses the real clock. Certification sets
`ALEX_CERT_NOW`.

The shared clock is now used by ingress timing, idempotency age, selection
expiry, Phase-2 relative dates, finance/work defaults, model-local date context,
and outbox retry calculations. SQLite `CURRENT_TIMESTAMP` fields that remain
are audit metadata and are not used as behavioural truth.

A midnight chain tests 23:55 -> 00:05 Malaysia rollover.

### 7. English output is an owner policy

Input may be English, Tamil, Tanglish, Malay terms, typo-heavy text, or a natural
mix. **Alex must reply in English.** Another language is allowed only when the
user explicitly requests translation/quoted-language output.

Tamil reminder and Tamil memory contracts are separate, so one cannot pass via
the other's capability.

### 8. Adversarial and held-out phrasing

The zero-token offline tier adds deterministic transformations such as:

- lowercase/no punctuation;
- casual WhatsApp filler;
- common typo/abbreviation forms.

Paid live mode samples a bounded subset.

An optional local-only held-out corpus is supported through
`--heldout-corpus` / `ALEX_CERT_HELDOUT_CORPUS`. It can contain the owner's
real historical phrasing. It is never committed by the rig, and report output
contains only a hash marker such as `[heldout:...]`, not the original phrase.

### 9. Multi-turn depth

Current chains include:

- receipt discovery -> exact receipt -> quoted "send that again";
- saved-picture browse -> numbered selection -> exact original image;
- diary -> relative reminder -> agenda;
- draft plan -> refinement -> readback;
- goal creation -> owner-agency readback;
- expense -> natural correction -> supersession/history;
- replay of the identical inbound message ID -> exactly-once state;
- saved memory -> service/schema reinitialization -> retrieval;
- owner DM private read -> same request in family group -> no private leak;
- relative reminder across local midnight rollover.

### 10. Task lifecycle is explicit

Tasks are not allowed to degrade into plan notes. The required lifecycle is:

- create;
- list/read;
- edit;
- complete;
- reopen;
- cancel.

Task fields include required title/status/visibility plus optional assignee,
due date/time, plan link, notes, and a separately-linked reminder. A due date is
optional. A reminder is created only when explicitly requested.

The current v0.4.4 MCP surface does not implement this lifecycle, so task
certification intentionally fails until the product is repaired.

### 11. Goal owner agency remains a structural requirement

A user may create a goal without deciding a monthly contribution. The current
`planning_create_goal` schema still requires `baseline_monthly`. The rig
correctly treats that as a product defect rather than inventing a value.

### 12. The tester is mutation-tested

Rig self-tests deliberately feed the judge broken outcomes and require failure,
including:

- discovery without the real reminder action;
- "here it is" with no image;
- wrong file identity;
- wrong finance state;
- invented goal baseline;
- wrong HA entity;
- private object/file leakage;
- non-English output;
- incomplete compound execution;
- privacy refusal semantics.

The core fixture and shared clock are also directly self-tested.

### 13. Sandbox hardening

Live certification:

- binds `ALEX_DATA_DIR` and `ALEX_OPTIONS_PATH` before application imports;
- uses a disposable DB per prompt/conversation;
- verifies every case DB path is inside the temporary sandbox;
- replaces `ha._request` with a fresh in-memory fake;
- also points HA URL at a dead local port and removes `SUPERVISOR_TOKEN`;
- never starts the WhatsApp outbox worker;
- reads provider credentials only from the explicitly selected source options/env;
- writes the temporary options file with restrictive permissions where supported;
- recursively redacts common credential/token patterns from reports.

`phase_certify.py` also now gives every deterministic subprocess an explicit
temporary data/options path. It no longer removes those variables and risks
falling back to production `/data`.

### 14. Latency evidence

Internal live reports now record:

- total ingress/brain time;
- provider latency;
- tool latency;
- model-call count;
- tool rounds;
- p50/p95/max by domain.

Production diagnostics separately compute:

- inbound -> outbound queue latency;
- outbound queue -> bridge delivery latency;
- inbound -> bridge delivery latency;
- maximum delivery attempts.

This can distinguish Alex/model/tool delay from durable outbox/bridge retry
delay. It still cannot prove when the WhatsApp phone UI rendered a message; that
remains manual.

The outbox's exponential retry currently tops out at **256 seconds** because the
exponent is capped at 8.

## Execution modes

### Catalog — zero provider cost

```bash
python alex-mcp/app/behavior_cert.py --mode catalog
```

Validates contract/catalog consistency, capability classification, owner
coverage and explicit manual gates.

### Offline — zero provider cost

```bash
python alex-mcp/app/behavior_cert.py --mode offline --phase all \
  --report /tmp/alex-behavior-offline.json
```

Exercises the actual provider-facing tool surface with catalog, adversarial, and
optional held-out phrasings. It distinguishes:

- `PASS` — direct required capability route is available;
- `LIVE_REQUIRED` — discovery must be proven by the provider tier;
- `FAIL` — a hard missing/routing/structural requirement.

It emits stable failure signatures. `behavior_known_failures.json` records the
current v0.4.4 repair debt. CI fails when a **new** signature appears; a
disappearing signature is treated as an improvement. The baseline is explicitly
not a PASS list and must not be expanded merely to silence a regression.

### Live — opt-in, spend-capped, sandboxed

```bash
python alex-mcp/app/behavior_cert.py --mode live --phase phase2 --provider auto \
  --max-live-cost-usd 0.25 --report /tmp/alex-behavior-live.json
```

This uses a real configured provider against synthetic state only. The
one-command phase gate deliberately refuses to spend provider tokens while a
cheaper deterministic/offline hard failure already exists.

**A full paid live run has not been claimed or performed for the currently
known-broken v0.4.4 product.** That is intentional: v0.4.4 still has offline
hard failures (including task lifecycle and goal owner-agency) that should be
repaired first.

## Manual gates that remain intentionally external

Internal certification does not claim to prove:

- genuine WhatsApp voice upload/download transport;
- phone-side media rendering / exactly-once display;
- genuine family-group @mention wake metadata;
- genuine group swipe-reply wake metadata;
- typing indicator and phone-visible latency/pull-refresh behaviour;
- actual scheduled reminder delivery to WhatsApp;
- physical Home Assistant effects;
- QR/session persistence across a real add-on restart.

These remain explicit `MANUAL_GATES`.

## Future phase workflow

For each new Alex phase/capability:

1. implement the deterministic service and Tier-A invariants;
2. add stable semantic capability mapping;
3. add owner-visible behavioural contracts/state assertions;
4. run unit/self-test/Tier-A/catalog/offline;
5. repair all new hard failures;
6. run spend-capped live behaviour;
7. only then return to the owner for the remaining manual gates;
8. finish with one short whole-system production acceptance run.

A new owner-facing capability cannot silently appear without classification and
behavioural coverage.

## Completion policy

The owner chose not to repeat architecture review after every repair pass. Claude's
first independent review already identified the material weaknesses; those
findings were used as the repair specification above.

The test rig itself is considered ready to merge when:

- the first-review material findings are addressed in code;
- tester mutation probes reject the demonstrated false-pass cases;
- the core seed/self-tests pass;
- normal unit, Tier-A, catalog, offline-runner, architecture and container CI are green;
- the PR remains sandbox-safe and no manual-only gate is falsely claimed as internal.

After that, development proceeds. We review the **completed Alex product/release**
rather than repeatedly reviewing the testing idea itself.

This does not weaken the certification standard. Alex still cannot receive
`PASS_INTERNAL` while product-level behavioural failures remain. The known
v0.4.4 failure baseline is repair debt, not acceptance.

## Owner language policy

Input may be English, Tamil, Tanglish, Malay terms, typo-heavy text, incomplete
sentences, or a natural mix. Alex's normal response must **always be English**.
Another language is allowed only when the user explicitly asks for translation
or quoted content in that language.

## Next use

Once this rig is merged, product work should consume it in this order:

1. repair the currently exposed v0.4.4 behavioural failures;
2. rerun the deterministic and offline gates after each capability repair;
3. run spend-capped live certification only when cheaper gates are clean enough
   to justify provider cost;
4. return to the owner only for explicit manual gates;
5. perform one short whole-system acceptance run before declaring the completed
   Alex release certified.
