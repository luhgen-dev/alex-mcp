# External Alex Lab

The External Alex Lab is the development loop that runs Alex outside Home Assistant and WhatsApp.
It exists so almost all behaviour can be repaired and re-certified before the owner performs the
small final set of real transport/device acceptance checks.

## What it uses

- The same Alex Python modules, MCP surface, router, brain, database services and behaviour contracts.
- Disposable data directories for every certification run.
- The existing Tier A stress harness and Tier B behaviour certification rather than a second fake implementation.
- Optional real provider calls, but only when explicitly requested and with a hard spend cap.
- Targeted contract reruns, so a repair can re-test only the failed capability instead of paying for the whole suite.
- A compact triage report containing the exact failing contract, prompt, tools called, state diff and judge problems.

## What it deliberately does not claim

Real WhatsApp mention/reply metadata, phone rendering, real voice transport, QR/session persistence,
scheduled delivery through the production outbox, and physical Home Assistant effects remain manual gates.

## Normal repair loop

1. Run the free external lab (catalog + offline routing + deterministic tests).
2. Repair any regression and rerun automatically in GitHub CI.
3. For ambiguous/provider-dependent failures, run only the affected contracts in paid live mode.
4. Inspect triage.json, patch Alex, and repeat until the internal product status is clean.
5. Build/install the add-on only after that, then run the short real WhatsApp/HA acceptance set.

Example free run:

    python alex-mcp/app/external_lab.py certify --phase all --report-dir /tmp/alex-lab

Example targeted live run:

    python alex-mcp/app/external_lab.py certify --phase phase2 \
      --contract p2.plan.update --contract conv.plan.refine \
      --live --provider auto --source-options /path/to/options.json \
      --max-live-cost-usd 0.20 --report-dir /tmp/alex-lab

Existing live report triage (zero provider spend):

    python alex-mcp/app/external_lab.py triage \
      --input behavior-cert-report.json --report-dir /tmp/alex-triage

## Safety

The free lab explicitly points data, options and HA URLs at disposable/dead local paths and removes
SUPERVISOR_TOKEN from child processes. The provider-live child then creates its own disposable certification
database and only reads configured provider credentials from the source-options file. No WhatsApp worker is
started by the lab.

GitHub CI runs only the free lab. Provider-live runs are never silently triggered by a pull request.
