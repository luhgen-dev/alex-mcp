# External Alex Lab

## Why it exists

The owner should not have to install a Home Assistant build and manually use
WhatsApp just to discover ordinary routing, state, privacy or conversation bugs.

The External Alex Lab runs Alex's real Python code outside Home Assistant against
disposable test state. It is the engineering loop used before the final real
WhatsApp/Home Assistant acceptance test.

The intended release path is now:

1. External Lab: deterministic tests + Tier A + Tier B offline routing.
2. Repair failures and rerun externally until clean.
3. Optional real-provider Tier B benchmark against the same contracts.
4. Install the candidate Home Assistant add-on.
5. Run only the genuinely external checks: WhatsApp mention/reply metadata,
   phone-side delivery/rendering, real audio transport, scheduler delivery,
   QR/session persistence and physical Home Assistant devices.

## Safety

`alex_lab.py` always creates explicit temporary data/options paths. It points
Home Assistant at a dead loopback port and removes `SUPERVISOR_TOKEN`. It does
not start the WhatsApp outbox worker and it makes no provider calls.

The lab therefore cannot mutate the live `/data` database, send certification
messages to WhatsApp, spend API credit, or control physical Home Assistant
devices.

## Evidence available to the engineering loop

Every run writes:

- `alex-lab-report.json` — compact gate/check summary.
- `alex-lab-failures.jsonl` — one diagnosis packet per offline failure or
  discovery-dependent case.

A prior live-provider JSON report may be supplied with `--live-report`. The lab
does not rerun or spend against that report; it only converts failed prompt and
conversation observations into compact packets containing the prompt, reply,
problems, tool calls, state diff and latency.

This is specifically designed so the engineering assistant can inspect failures
from GitHub Actions, patch the repository, push the branch, and inspect the next
run without requiring the owner to interact with Home Assistant between each
repair.

## Commands

Zero-cost external diagnosis:

```bash
python alex-mcp/app/alex_lab.py --phase all \
  --report /tmp/alex-lab-report.json \
  --packets /tmp/alex-lab-failures.jsonl
```

Use a prior live-provider benchmark as additional evidence:

```bash
python alex-mcp/app/alex_lab.py --phase all \
  --live-report /path/to/behavior-live.json \
  --report /tmp/alex-lab-report.json \
  --packets /tmp/alex-lab-failures.jsonl
```

Release-gate mode:

```bash
python alex-mcp/app/alex_lab.py --phase all --gate
```

`--gate` exits non-zero until internal behaviour is clean. Normal diagnosis
mode still reports `PRODUCT_FAIL` but exits successfully so the evidence
artifact is preserved while repairs are in progress.

## GitHub Actions

`.github/workflows/alex-lab.yml` runs on pull requests and can also be started
manually. It uploads both evidence files as the `alex-lab-evidence` artifact
for 14 days.

The workflow intentionally does not contain provider secrets. This keeps the default
external repair loop zero-cost and deterministic.

For a targeted provider-dependent repair, the same external lab can opt in to
the real Tier-B provider path without Home Assistant:

```bash
python alex-mcp/app/alex_lab.py --phase phase2 \
  --contract p2.plan.update --contract conv.plan.refine \
  --run-live --provider auto \
  --source-options /path/to/options.json \
  --max-live-cost-usd 0.20 \
  --report /tmp/alex-lab-report.json \
  --packets /tmp/alex-lab-failures.jsonl
```

The provider child still uses the certification rig's disposable database, fake
Home Assistant and spend cap. Targeted contract selection is specifically meant
for the repair loop: after a code change, re-run only the affected behaviour
instead of paying to repeat the whole benchmark.

## Important boundary

A green External Lab means Alex's internally simulatable behaviour is clean. It
does **not** claim that WhatsApp transport, linked-device metadata, phone
rendering, physical device state or QR/session persistence have been tested.
Those remain the short final production acceptance.
