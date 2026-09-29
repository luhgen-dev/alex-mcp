#!/usr/bin/env python3
from __future__ import annotations

"""One-command internal certification gate for an Alex development phase.

This orchestrates the existing deterministic tests plus Tier-B behaviour checks.
It never treats the external WhatsApp/physical-HA manual gates as internally
certified. Live provider testing is explicit and spend-capped.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from behavior_contracts import (
    CONVERSATION_CONTRACTS,
    MANUAL_GATES,
    PROMPT_CONTRACTS,
)


APP = Path(__file__).resolve().parent
REPO = APP.parents[1]


def _known_phases() -> list[str]:
    return sorted(
        {c.phase for c in PROMPT_CONTRACTS}
        | {c.phase for c in CONVERSATION_CONTRACTS}
        | {g.phase for g in MANUAL_GATES}
    )


def _run(label: str, cmd: list[str], env: dict[str, str],
         report_path: Path | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    report = None
    if report_path and report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    return {
        "label": label,
        "returncode": proc.returncode,
        "status": (
            report.get("status")
            if isinstance(report, dict)
            else ("PASS" if proc.returncode == 0 else "FAIL")
        ),
        "report": report,
        # Keep enough evidence for diagnosis without flooding the gate summary.
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-30:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-30:]),
    }


def _write(path: Path | None, payload: dict) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    print(text)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def main() -> dict:
    p = argparse.ArgumentParser(description="Run Alex internal phase certification")
    p.add_argument("--phase", choices=_known_phases(), required=True)
    p.add_argument(
        "--live", action="store_true",
        help="after deterministic/offline gates are clean, run real provider behaviour in the sandbox",
    )
    p.add_argument(
        "--provider", choices=("auto", "gemini", "grok", "openai"), default="auto",
    )
    p.add_argument(
        "--source-options", default=os.environ.get("ALEX_CERT_SOURCE_OPTIONS", "/data/options.json"),
    )
    p.add_argument("--hard-latency-ms", type=int, default=20000)
    p.add_argument("--max-live-cost-usd", type=float, default=0.25)
    p.add_argument(
        "--heldout-corpus", default=os.environ.get("ALEX_CERT_HELDOUT_CORPUS"),
        help="optional local-only JSON of owner-written held-out phrasings",
    )
    p.add_argument("--live-adversarial-per-contract", type=int, default=1)
    p.add_argument("--report", default=None, help="write the combined JSON gate report here")
    p.add_argument("--no-fail-exit", action="store_true")
    args = p.parse_args()

    py = sys.executable
    source_env = os.environ.copy()

    with tempfile.TemporaryDirectory(prefix="alex-phase-cert-") as tmp:
        tmpdir = Path(tmp)
        reports = tmpdir / "reports"
        reports.mkdir()

        env_root = tmpdir / "environments"
        env_root.mkdir()

        def isolated_env(label: str) -> dict[str, str]:
            data_dir = env_root / label / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            options_path = env_root / label / "options.json"
            options_path.write_text(json.dumps({
                "ai_provider": "grok",
                "xai_api_key": "",
                "husband_phone": "+60111111111",
                "wife_phone": "+60222222222",
                "timezone": "Asia/Kuala_Lumpur",
                "ocr_enabled": False,
                "context_turns": 8,
            }), encoding="utf-8")
            child = dict(source_env)
            # Explicit paths are safer than popping: config defaults to /data
            # when the variables are absent.
            child["ALEX_DATA_DIR"] = str(data_dir)
            child["ALEX_OPTIONS_PATH"] = str(options_path)
            child["ALEX_HA_API_URL"] = "http://127.0.0.1:9/certification-no-ha"
            child.pop("SUPERVISOR_TOKEN", None)
            return child

        checks: list[dict[str, Any]] = []
        checks.append(_run(
            "unit_tests",
            [py, "-m", "unittest", "discover", "-s", "tests", "-v"],
            isolated_env("unit-tests"),
        ))
        checks.append(_run(
            "structural_selftest",
            [py, "alex-mcp/app/selftest.py"],
            isolated_env("structural-selftest"),
        ))
        checks.append(_run(
            "phase3_tier_a_stress",
            [py, "alex-mcp/app/stress_test.py"],
            isolated_env("tier-a-stress"),
        ))
        checks.append(_run(
            "final_architecture_audit",
            [py, "alex-mcp/app/final_audit.py"],
            isolated_env("final-audit"),
        ))

        catalog_path = reports / "catalog.json"
        checks.append(_run(
            "tier_b_catalog",
            [py, "alex-mcp/app/behavior_cert.py", "--mode", "catalog",
             "--report", str(catalog_path)],
            isolated_env("tier-b-catalog"),
            catalog_path,
        ))

        offline_path = reports / f"{args.phase}-offline.json"
        offline_cmd = [
            py, "alex-mcp/app/behavior_cert.py", "--mode", "offline",
            "--phase", args.phase, "--no-fail-exit",
            "--report", str(offline_path),
        ]
        if args.heldout_corpus:
            offline_cmd += ["--heldout-corpus", args.heldout_corpus]
        offline = _run(
            "tier_b_offline",
            offline_cmd,
            isolated_env("tier-b-offline"),
            offline_path,
        )
        checks.append(offline)

        deterministic_fail = any(
            item["status"] != "PASS"
            for item in checks
            if item["label"] not in {"tier_b_offline"}
        )
        offline_fail = offline["status"] == "FAIL"

        live = None
        live_skipped_reason = None
        if args.live and not deterministic_fail and not offline_fail:
            live_path = reports / f"{args.phase}-live.json"
            live_cmd = [
                py, "alex-mcp/app/behavior_cert.py",
                "--mode", "live", "--phase", args.phase,
                "--provider", args.provider,
                "--source-options", args.source_options,
                "--hard-latency-ms", str(max(1000, args.hard_latency_ms)),
                "--max-live-cost-usd", str(max(0.0, args.max_live_cost_usd)),
                "--live-adversarial-per-contract",
                str(max(0, args.live_adversarial_per_contract)),
                "--no-fail-exit",
                "--report", str(live_path),
            ]
            if args.heldout_corpus:
                live_cmd += ["--heldout-corpus", args.heldout_corpus]
            live = _run(
                "tier_b_live",
                live_cmd,
                isolated_env("tier-b-live"),
                live_path,
            )
            checks.append(live)
        elif args.live:
            live_skipped_reason = (
                "Live provider certification was skipped to avoid spending tokens "
                "while deterministic/catalog/offline hard failures remain."
            )

        if deterministic_fail or offline_fail:
            status = "FAIL"
        elif args.live:
            status = "PASS_INTERNAL" if live and live["status"] == "PASS" else (
                live["status"] if live else "FAIL"
            )
        else:
            status = "READY_FOR_LIVE"

        phase_manual = [
            {
                "id": gate.id,
                "domain": gate.domain,
                "description": gate.description,
                "reason": gate.reason,
            }
            for gate in MANUAL_GATES
            if gate.phase == args.phase
        ]

        summary = {
            "mode": "phase_gate",
            "phase": args.phase,
            "status": status,
            "live_requested": args.live,
            "live_skipped_reason": live_skipped_reason,
            "checks": [
                {
                    "label": item["label"],
                    "status": item["status"],
                    "returncode": item["returncode"],
                    "summary": (
                        item["report"].get("summary")
                        if isinstance(item.get("report"), dict)
                        else None
                    ),
                }
                for item in checks
            ],
            "manual_gates_still_required": phase_manual,
            "meaning": {
                "PASS_INTERNAL": (
                    "All internal deterministic, architecture, routing and live-provider "
                    "behaviour gates passed. External WhatsApp/physical-HA gates remain."
                ),
                "READY_FOR_LIVE": (
                    "Deterministic/catalog/offline gates passed; run again with --live "
                    "before asking the owner to perform external smoke tests."
                ),
                "FAIL": (
                    "At least one internal gate failed. Repair Alex before returning "
                    "to the owner for manual smoke testing."
                ),
            }.get(status, "Internal certification did not complete successfully."),
        }

        out = Path(args.report) if args.report else None
        _write(out, summary)
        if status == "FAIL" and not args.no_fail_exit:
            raise SystemExit(1)
        if args.live and status != "PASS_INTERNAL" and not args.no_fail_exit:
            raise SystemExit(1)
        return summary


if __name__ == "__main__":
    main()
