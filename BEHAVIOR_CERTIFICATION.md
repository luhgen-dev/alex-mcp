# Alex Behaviour Certification Rig — Claude Handover

## Why this was built

Alex already had a strong **Phase 3 deterministic stress harness** in
`alex-mcp/app/stress_test.py`. That harness is valuable and remains untouched.
It attacks backend invariants directly: idempotency, WAL concurrency, privacy,
repeated receipts, reminder durability, numbered retrieval, roster/OT rules,
planning ownership, diary conflicts, monitoring, reports and database integrity.

The gap exposed by the v0.4.4 live smoke test is above that layer.

A backend capability can be correct while Alex still fails depending on how the
user phrases the request. Examples observed live include:

- one wording can read a goal while another claims no goal-reading tool exists;
- a goal can be routed as a plan;
- a requested task can become a plan note;
- "what time is my dentist appointment?" can loop even though full agenda works;
- a natural receipt follow-up can send the wrong saved image;
- a correctly transcribed English voice note can receive a Malay reply;
- direct work/HA reads can fall into the four-model-call safety stop;
- ordinary relative-date wording can be interpreted inconsistently.

Those are **behaviour-boundary failures**, not failures that the existing
`stress_test.py` was designed to detect.

This change therefore adds **Phase 3 Tier B: Behaviour Certification** rather
than replacing Phase 3 Tier A.

## Architecture after this change

### Tier A — deterministic stress/invariants (existing)

`alex-mcp/app/stress_test.py`

Purpose:
- prove deterministic service correctness;
- prove privacy and state invariants;
- attack concurrency/idempotency/durability;
- stay provider-free and network-free.

### Tier B — language + conversation certification (new)

`alex-mcp/app/behavior_contracts.py`
`alex-mcp/app/behavior_cert.py`

Purpose:
- express owner-visible capabilities as contracts;
- hit the same capability through multiple natural phrasings;
- certify both text and post-transcription voice paths where appropriate;
- test multi-turn continuity, not only isolated commands;
- verify which tools were actually exposed/called;
- detect forbidden mutations;
- detect max-step/tool-loop failures;
- verify expected attachment queuing;
- flag unsolicited Malay replies to English voice transcripts;
- collect per-turn latency;
- keep all writes inside a disposable certification database.

### Final external gate — targeted real WhatsApp/HA acceptance

Some things cannot honestly be certified from an internal process. They are
explicitly represented as `MANUAL_GATES`, rather than being silently claimed
as tested:

- real WhatsApp voice transport;
- actual phone-side media rendering / exactly-once delivery;
- genuine family-group @mention metadata;
- genuine swipe-reply metadata;
- typing indicator and phone-visible latency / pull-to-refresh behaviour;
- real scheduled reminder delivery;
- physical Home Assistant effect;
- QR/session persistence across restart.

That keeps the rig honest: it certifies what it can observe and names what still
needs a human/device check.

## What is in the contract catalog

The catalog spans all three historical Alex phases.

### Phase 1

- finance latest/list/write;
- semantic receipt lookup;
- reminders read/write;
- shopping read/write;
- explicit saved-memory browse and saved-picture retrieval;
- multi-turn receipt follow-up;
- WhatsApp-only transport/delivery/group/manual gates.

### Phase 2

- agenda;
- diary detail;
- plan read/update;
- **task create/read semantics**;
- goals list/create/owner-agency;
- cash recording/allocation;
- reserves;
- roster/leave/OT;
- recurring bills;
- assets/warranty;
- delegated monitoring;
- diagnostics;
- Home Assistant read safety;
- reports;
- cross-domain diary -> reminder conversation;
- multi-turn plan refinement;
- goal owner-agency conversation;
- simulated **post-wake** family-group and spouse-DM privacy checks. These
  deliberately bypass the WhatsApp wake gate so the ACL/brain behaviour can be
  certified internally, while genuine @mention/swipe-reply metadata remains a
  manual WhatsApp gate.

The task contracts are intentional even though the current MCP surface has no
dedicated task lifecycle. A certification rig must represent the owner's
required behaviour, not merely mirror whatever code currently exists.

### Phase 3

- HA negation/hypothetical safety;
- typo-heavy wording/discovery;
- Tamil routing;
- read-only fallback safety;
- explicit manual transport/session/latency gates;
- existing Tier-A deterministic stress remains the lower layer.

## Three execution modes

### 1. Catalog audit — zero cost, CI-safe

```bash
python alex-mcp/app/behavior_cert.py --mode catalog
```

This proves the rig itself is coherent:
- unique contract IDs;
- multiple natural variants per prompt contract;
- all declared phase/domain coverage;
- required manual gates represented.

This is the mode added to ordinary CI because it tests the **rig**, not whether
the currently known-broken v0.4.4 release has already passed certification.

### 2. Offline certification — zero provider cost

```bash
python alex-mcp/app/behavior_cert.py --mode offline --phase all \
  --report /tmp/alex-behavior-offline.json
```

This attacks every natural-language variant through Alex's deterministic router.

For every phrase it checks:
- at least one required capability is exposed;
- forbidden mutation tools are not exposed;
- the six-tool schema cap is respected;
- required capability exists on the MCP surface.

It also performs structural checks that routing alone cannot see. In particular:

1. **Goal owner-agency check**  
   If `planning_create_goal` requires `baseline_monthly`, the rig fails.
   That schema currently forces the model either to invent a monthly amount or
   fail when the user intentionally leaves it undecided.

2. **Task lifecycle check**  
   If there is no task create/list/update surface, the rig fails rather than
   accepting "saved as a note" as equivalent behaviour.

This means the new rig is expected to expose real v0.4.4 gaps. That is a feature,
not a broken test.

During active repair, `--no-fail-exit` can be used to collect the whole report
without stopping at the first failing contract.

### 3. Live provider certification — disposable sandbox

```bash
ALEX_CERT_SOURCE_OPTIONS=/data/options.json \
python alex-mcp/app/behavior_cert.py --mode live --phase all --provider auto \
  --max-live-cost-usd 0.25 \
  --report /tmp/alex-behavior-live.json
```

This is opt-in because it consumes provider tokens.

Safety properties:
- a new `TemporaryDirectory` becomes `ALEX_DATA_DIR`;
- a temporary options file becomes `ALEX_OPTIONS_PATH` and is permission-hardened where supported;
- each prompt variant and each multi-turn scenario receives its own fresh SQLite database inside that temporary directory, so one wording cannot contaminate another;
- synthetic fixture integrity is checked before each core-seeded case;
- only provider credentials are copied from the source options file;
- credentials are never printed into the report;
- the rig verifies the bound DB path is inside the temporary directory;
- Home Assistant is replaced with a freshly reset in-memory fake for every live case;
- HA read/control/summary/report/automation reasoning is therefore testable
  internally without touching a physical device; the real physical effect remains
  a manual gate;
- production `/data` records are not read/written by the certification turns.

Seed fixtures include:
- management fee + original receipt;
- explicit "cobalt" memory;
- saved vinyl picture;
- family shopping items;
- dentist diary event and linked-style reminder;
- Malacca draft plan;
- Family Holiday Savings goal;
- deterministic work/bill profile configuration.

Each live turn is driven through the real `brain.respond` orchestration. The
rig then reads Alex's own `tool_audit` / `_turn_trace` evidence and checks:

- required tool actually called, not merely advertised;
- forbidden tool not called;
- no `max_steps` outcome;
- expected answer terms where the fixture gives an objective answer;
- actual attachment queue when a file is required;
- English voice transcript does not drift into a clearly Malay response;
- hard latency threshold (default 20 seconds);
- the estimated cost of each turn;
- durable household state changed/not-changed by table (row counts + hashes only, never row contents);
- a mutating tool that claims success but produces no durable household-state change is flagged;
- relative dates are evaluated against a fixed 29 September 2026 certification clock.

The live runner defaults to a **US$0.25 cumulative estimated spend cap**. If the
cap is reached before all cases run, the result is `INCOMPLETE_BUDGET`, never a
false PASS. The cap can be changed explicitly with `--max-live-cost-usd`; zero
disables the runner-level cap.

Multi-turn contracts keep one conversation ID so pronouns/deictic references
must survive naturally.

## One-command phase gate

For day-to-day development, `phase_certify.py` is the handoff command. It runs
the existing unit suite, structural self-test, Tier-A stress harness, final
architecture audit, Tier-B catalog audit and the requested phase's offline
behaviour certification in one isolated workflow.

Zero-provider-cost preflight:

```bash
python alex-mcp/app/phase_certify.py --phase phase2
```

A clean preflight reports `READY_FOR_LIVE`. Then run the same gate with live
provider behaviour enabled:

```bash
python alex-mcp/app/phase_certify.py --phase phase2 --live \
  --provider auto --max-live-cost-usd 0.25
```

Live provider work is automatically skipped if a cheaper deterministic/offline
gate is already failing, so known structural defects do not consume tokens.
`PASS_INTERNAL` means every internal layer passed; it **does not** claim the
external WhatsApp/device manual gates passed.

The available phase names are derived from the contract catalog rather than
hard-coded, so a future phase becomes selectable when its contracts/manual
gates are added.

## Phase workflow going forward

For every future Alex phase or significant capability:

1. Add/modify the deterministic service and Tier-A invariant tests.
2. Add the owner-visible behaviour contract(s) to
   `behavior_contracts.py` **before declaring the phase complete**.
3. Run:
   - existing unit tests;
   - existing `stress_test.py`;
   - `behavior_cert.py --mode catalog`;
   - `behavior_cert.py --mode offline --phase <phase>`.
   Offline reports distinguish direct routes from cases that genuinely require
   the model's `discover_alex_tools` valve; those are marked `LIVE_REQUIRED`
   rather than being falsely passed.
4. When offline has no hard failures, run live provider certification for that phase.
5. Only after both internal layers pass should the work return to the owner for
   the reduced manual WhatsApp/HA gate list.
6. After targeted manual gates pass, perform one short whole-system acceptance
   run. That final run validates the certification claim in the production path;
   it is no longer the place where basic routing defects should first be found.

A phase is not "certified" because one wording worked. If an ordinary equivalent
phrasing fails, the contract fails.

## Review request for Claude

Please review this as a **test architecture**, not as an assertion that v0.4.4
already passes it.

Specifically check:

1. Does the Tier A / Tier B / manual-gate separation make sense?
2. Are any owner-visible Alex capabilities missing from the contract catalog?
3. Are any contract expectations too narrow or likely to create false passes?
4. Are any expectations too strict and likely to create false failures?
5. Is the live sandbox genuinely isolated from production `/data`?
6. Can any live certification path accidentally control real Home Assistant?
7. Are provider credentials kept out of reports/source?
8. Are the goal owner-agency and missing-task structural checks correct?
9. Does the report contain enough evidence to diagnose a failed phrase without
   guessing the root cause?
10. What additional adversarial paraphrase families or multi-turn chains should
    be added before we call the rig complete?

Please do **not** weaken a failing contract merely to make the current build
green. If a contract accurately expresses the intended Alex behaviour, the
product should be repaired to satisfy it.

## Completion criterion for the rig itself

The rig should be considered complete only after:

- its CI/catalog self-audit passes;
- GPT review finds no obvious architecture/safety gap;
- Claude independently reviews the PR and either approves it or identifies
  changes;
- any agreed review changes are incorporated;
- both reviewers agree the catalog and execution model are sufficient.

That is separate from Alex itself passing every behavioural contract.
