# 0.5.44 repair evidence

See [the repair report](../../HANDOFF_REPAIR_REPORT.md) for conclusions, scope
and live acceptance. All databases and messages here are synthetic fixtures.
No configured provider, household WhatsApp session or HA device was used.

## Evidence stages

- Files ending in `-release` contain the final repaired-source checks. The
  release corpus contains 394 public packets, including six added ambiguity
  cases. Its deterministic oracle is engineering regression coverage, not an
  independent model result.
- `independent-review-*-first.json`, `independent-snapshot-v5-first.json` and
  `independent-score-first.json` preserve the first fresh 388-packet review's
  369/388 result. No original model answer or rationale was rewritten.
- `public-review-bundle-first.json` and `public-packet-audit-first.json` identify
  that earlier public corpus. Its fingerprint differs from the release corpus.
- The `*-second.json` review files preserve the next full 392/394 result and
  its original public corpus. The last three positive fixtures were then made
  explicit about the reminder date or commute duration; their original
  incomplete wording remains in focused clarification coverage.
- The earlier `*-final.*` files retain the initial handoff-repair checks before
  the broader disagreement audit. They are historical evidence; the
  `*-release.*` files and repair report identify the release results.
- `unit-initial.log` and `ingress-integration-first.log` preserve failures found
  while repairing time validation and compatibility. The later
  `unit-release-fixture-import-first.log` records a test import-order problem:
  the new fixture loaded application settings before disposable household
  configuration. `unit-release.log` is the passing complete rerun after fixing
  that test initialization order.
- The previous candidate, including its direct-brain execution failures, is
  retained separately in `../conversation-20261021/`. Do not treat different
  harnesses or changed public corpora as paired live-model accuracy results.

## Independent capability review

The release review inputs are `public-review-bundle-release.json`. Shared fields
apply to every packet; tool references losslessly expand to exact schemas. The
three isolated reviewers receive disjoint public shards, no private contracts,
expected decisions, old answers, source code or score feedback.

Review files contain the reviewers' original JSON. The v5 full importer
preserves each raw UTF-8 source and SHA-256 and requires exact coverage. The
release v6 snapshot retains that complete parent snapshot and public bundle,
carries 391 byte-identical public packets with their unchanged original
decisions, and imports 3 fresh raw independent decisions. It reconstructs all
current decisions from this evidence on every load, rejecting drift or edits.
This is explicitly a verified delta review, not another fresh full pass.
Hashes establish integrity; public-only independence is an explicit process
attestation. Capability selection from synthetic interpreted inputs is separate
from successful execution or live provider behavior.

## Actual reminder execution replay

`runtime-capture-first/`, `runtime-capture-second/` and `runtime-capture-release/` preserve separate actual
full-ingress request captures. Each includes:

- Exact first main-brain messages/tools, public model inputs and their hashes.
- A private manifest containing the source fingerprint, frozen clock,
  authenticated payload and pre-followup database rows.
- Ten pre-followup SQLite snapshots compressed as `.sqlite3.gz.b64` for lossless
  UTF-8 version control. Decompression restores the exact original bytes.

The runtime reviewer sees only the public first-request packets and supplies
raw assistant/tool-call messages. The replay worker checks source fingerprints,
snapshot checksums, pre-turn rows and byte-identical first messages/tools before
executing those unchanged decisions through ingress, MCP and backend services.
Observed reports contain real tool receipts, database effects, exact pending
completion and outbound bindings. A model's success statement is not proof of
an action.

Restore a capture into a disposable directory before replaying it:

```python
import base64
import gzip
import shutil
from pathlib import Path

source = Path("evaluation/handoff-20261021/runtime-capture-release")
target = Path("/tmp/alex-synthetic-capture-restored")
shutil.copytree(source, target)
for encoded in (target / "snapshots").glob("*.sqlite3.gz.b64"):
    restored = encoded.with_name(encoded.name.removesuffix(".gz.b64"))
    restored.write_bytes(gzip.decompress(base64.b64decode(encoded.read_bytes())))
```

The worker command is:

```bash
python tools/reminder_handoff_review.py replay \
  --capture-dir /tmp/alex-synthetic-capture-restored \
  --responses evaluation/handoff-20261021/runtime-independent-responses-release.json \
  --output /tmp/alex-synthetic-replay.json
```

SQLite's expiry clock is not frozen by this harness. A replay after expiry can
correctly fail its byte-match check; it must not silently regenerate or alter
the original inputs. For a later fresh evaluation, use the worker's `capture`
command and obtain new independent responses. The preserved receipts and row
hashes document the actual executions at the recorded capture time.

The ten phrases are known regressions, not unseen holdouts. The initial
missing-time reply and narrow interpreter are controlled; the terminal response
after a tool is fixed neutral text. Configured-provider reasoning, total model
tokens/cost, live latency, delivery and pins require installed testing.
