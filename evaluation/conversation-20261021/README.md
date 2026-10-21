# Conversation candidate evidence — 21 October 2026

Read the [candidate report](../../CONVERSATION_CANDIDATE_REPORT.md) for the
decision and interpretation. This directory preserves synthetic evaluation
inputs, independent decisions, execution receipts and failed runs. It contains
no live household activity and does not certify the deployed provider.

## Final results and provenance

| Files | Meaning |
| --- | --- |
| `gates.json`, `manifest.json` | Final gate summary, source identities and SHA-256 file manifest. |
| `comparison.json` | Same 50 contracts on the baseline and final candidate: 15 versus 50 passed. Scripted providers; not language accuracy. |
| `baseline-unit.log`, `candidate-unit.log` | Baseline 520 tests and candidate 597 tests, both with two skips and no failures. |
| `behavior-final.json`, `packet-audit-final.json`, `architecture-final.json`, `deterministic-oracle-final.json` | Final offline certification results; deterministic engineering evidence. |
| `legacy-changed-public-packets.json`, `legacy-independent-decisions.json`, `legacy-model-review.json` | Blind review of 59 changed legacy packets: 49 agree, ten disagree. The other 329 decisions retain their previous provenance in the runtime snapshot. The combined scorer fails at 378/388. |
| `runtime-packets.json`, `runtime-review-decisions.json`, `runtime-review-provenance.json` | Clock-aligned actual first requests from each revision, untouched fresh decisions and the unblinding map. The map was withheld from the reviewer. |
| `runtime-replay-baseline.json`, `runtime-replay-candidate.json` | Real brain/MCP/backend replay of those decisions. All 20 first requests match reviewed messages and schemas exactly. Candidate: two reminders, six rejected creates and two reviewer clarifications. |
| `runtime-postfix-summary.json` | Final replay and lifecycle verification after the retention/trace fixes, retaining original capture and final runtime fingerprints separately. |
| `ingress-lifecycle-after-retention-fix.json` | Three final full-ingress scripted probes: two rejected follow-ups retain the exact original draft; an independent new reminder still creates. |
| `baseline-ingress-continuation-probe.json` | Baseline ingress already recovers context for eight of the ten phrases and has semantic rescue for the other two. Direct-brain missing context must not be reported as baseline WhatsApp failure. |
| `holdout-harness-checks.json`, `holdout-not-executed.json` | Harness checks only. All 48 turns in the separate 24-scenario holdout remain unexecuted. |

The summaries retain absolute paths from the original execution for provenance.
Use the filenames above to locate the archived copies. Snapshot re-scoring does
not constitute another fresh model evaluation. Review decisions are proposals;
the backend receipts and durable rows establish what actually happened.

## Failed and superseded runs

- `preliminary-runtime-*` preserves the first review with mismatched actor and
  processing clocks. Three relative proposed times were already past. The
  clock-aligned review supersedes it for time evaluation.
- `runtime-review-summary.json` preserves the original clock-aligned analysis
  before the retention fix, including the actual backend failures.
- `runtime-replay-candidate-before-retention-fix.json` and
  `ingress-lifecycle-before-retention-fix.json` preserve the observed candidate
  behavior before repair. The latter shows the original task being cancelled
  and replaced by a time-only draft after rejection.
- `comparison-before-retention-fix.json`,
  `candidate-unit-before-retention-fix.log` and `gates-before-retention-fix.json`
  retain the earlier 590-test stage. The final full suite has 597 tests.
- `stress.log` is the earlier local stress run. The final gate file explicitly
  identifies prior checks carried forward and those rerun after the final edit.

The fixed capture clock is `2026-10-21T08:00:00+00:00`. Actor receipt time and
seeded pending/history/message rows were aligned before direct-brain capture.
SQLite's default clock was not overridden. Full-ingress lifecycle probes instead
align Python and actor times to current wall time, translate the two relative
due times explicitly, and preserve that transformation in the evidence. They
are separate scripted probes, not exact replicas of a blind ingress review.

## Reproduce the final execution checks

Install `alex-mcp/app/requirements.txt` in a Python 3.12 environment. From the
repository root, the main report gives the unit and certification commands.
The following commands replay saved independent proposals in disposable
databases; they do not call a live provider.

```sh
ALEX_CERT_NOW=2026-10-21T08:00:00+00:00 python evaluation/conversation-20261021/replay_clocked_runtime.py \
  --repo . \
  --fixture tests/test_conversation_reliability.py \
  --label candidate \
  --mapping evaluation/conversation-20261021/runtime-review-provenance.json \
  --public evaluation/conversation-20261021/runtime-packets.json \
  --decisions evaluation/conversation-20261021/runtime-review-decisions.json \
  --allow-runtime-source-change \
  --output /tmp/alex-runtime-replay.json
```

The explicit source-change flag permits the recorded retention/trace repair
after capture; it does not relax equality of the actual first messages and tool
schemas with those the reviewer saw. Both source fingerprints are recorded. A
future change to those packets must get a fresh review, not reuse this result.

```sh
python evaluation/conversation-20261021/probe_ingress_lifecycle.py \
  --repo . \
  --fixture tests/test_conversation_reliability.py \
  --mapping evaluation/conversation-20261021/runtime-review-provenance.json \
  --decisions evaluation/conversation-20261021/runtime-review-decisions.json \
  --allow-runtime-source-change \
  --output /tmp/alex-ingress-lifecycle.json
```

`capture_runtime_requests.py` and `assemble_clocked_packets.py` preserve the
extraction and blinding procedure. The two summary scripts preserve the original
analysis procedure and original temporary paths; they are archival workers,
not a portable benchmark CLI. New language accuracy evidence requires a fresh
isolated reviewer or the configured provider and explicit execution scoring.
