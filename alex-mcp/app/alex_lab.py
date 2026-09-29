#!/usr/bin/env python3
from __future__ import annotations

"""External Alex Lab.

This is the engineering loop that runs *outside* Home Assistant.  It exercises
Alex's real Python code against disposable state, produces compact failure
packets, and never starts the WhatsApp outbox or talks to a physical HA device.

It deliberately does not require provider keys.  Paid live-provider evidence
can be imported with --live-report, but zero-cost deterministic/offline testing
is always the first gate.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parent
REPO = APP.parents[1]
CERT_NOW = "2026-09-29T02:00:00+00:00"
KNOWN_FAILURES = APP / "behavior_known_failures.json"


def _isolated_env(root: Path, label: str, *, fixed_clock: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    data_dir = root / label / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    options_path = root / label / "options.json"
    options_path.write_text(json.dumps({
        "ai_provider": "grok",
        "xai_api_key": "",
        "gemini_api_key": "",
        "openai_api_key": "",
        "husband_phone": "+60111111111",
        "wife_phone": "+60222222222",
        "timezone": "Asia/Kuala_Lumpur",
        "context_turns": 8,
        "ocr_enabled": False,
    }), encoding="utf-8")
    env["ALEX_DATA_DIR"] = str(data_dir)
    env["ALEX_OPTIONS_PATH"] = str(options_path)
    env["ALEX_HA_API_URL"] = "http://127.0.0.1:9/alex-lab-no-ha"
    if fixed_clock:
        env["ALEX_CERT_NOW"] = CERT_NOW
    else:
        env.pop("ALEX_CERT_NOW", None)
    env.pop("SUPERVISOR_TOKEN", None)
    return env


def _run(label: str, cmd: list[str], env: dict[str, str],
         report_path: Path | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        cmd, cwd=REPO, env=env, text=True, capture_output=True, check=False
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
            else ("PASS" if proc.returncode == 0 else "ERROR")
        ),
        "report": report,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-25:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-25:]),
    }


def _contract_args(contract_ids: list[str]) -> list[str]:
    args: list[str] = []
    for contract_id in contract_ids:
        args.extend(["--contract", contract_id])
    return args


def _load_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to read JSON report {path!r}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON report {path!r} must contain an object")
    return value


def _live_failure_packets(report: dict[str, Any]) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for row in report.get("prompt_results", []):
        if row.get("status") != "FAIL":
            continue
        packets.append({
            "kind": "live_prompt",
            "contract": row.get("contract"),
            "phase": row.get("phase"),
            "domain": row.get("domain"),
            "source": row.get("source"),
            "prompt": row.get("prompt"),
            "reply": row.get("reply"),
            "problems": row.get("problems", []),
            "tools": [
                {
                    "tool": call.get("tool"),
                    "status": call.get("status"),
                    "arguments": call.get("arguments"),
                    "result": call.get("result"),
                }
                for call in row.get("trace", {}).get("calls", [])
            ],
            "state_diff": row.get("durable_state_diff", {}),
            "latency": row.get("latency", {}),
        })
    for conv in report.get("conversation_results", []):
        if conv.get("status") != "FAIL":
            continue
        for step in conv.get("steps", []):
            if step.get("status") != "FAIL":
                continue
            packets.append({
                "kind": "live_conversation_step",
                "contract": conv.get("contract"),
                "phase": conv.get("phase"),
                "domain": conv.get("domain"),
                "source": conv.get("source"),
                "step": step.get("step"),
                "prompt": step.get("prompt"),
                "reply": step.get("reply"),
                "problems": step.get("problems", []),
                "tools": [
                    {
                        "tool": call.get("tool"),
                        "status": call.get("status"),
                        "arguments": call.get("arguments"),
                        "result": call.get("result"),
                    }
                    for call in step.get("trace", {}).get("calls", [])
                ],
                "state_diff": step.get("durable_state_diff", {}),
                "latency": step.get("latency", {}),
            })
    return packets


def _offline_failure_packets(report: dict[str, Any]) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for row in report.get("failures", []):
        packets.append({
            "kind": "offline",
            "contract": row.get("contract"),
            "phase": row.get("phase"),
            "domain": row.get("domain"),
            "source": row.get("source"),
            "prompt": row.get("prompt"),
            "variant_kind": row.get("variant_kind"),
            "failure_kind": row.get("kind"),
            "detail": row.get("detail"),
            "required_capabilities": row.get("required_capabilities"),
            "required_all_capabilities": row.get("required_all_capabilities"),
            "provider_facing_capabilities": row.get("provider_facing_capabilities"),
            "provider_facing_tools": row.get("provider_facing_tools"),
        })
    for row in report.get("needs_live", []):
        packets.append({
            "kind": "offline_live_required",
            "contract": row.get("contract"),
            "phase": row.get("phase"),
            "domain": row.get("domain"),
            "source": row.get("source"),
            "prompt": row.get("prompt"),
            "variant_kind": row.get("variant_kind"),
            "failure_kind": row.get("kind"),
            "detail": row.get("detail"),
            "required_capabilities": row.get("required_capabilities"),
            "provider_facing_capabilities": row.get("provider_facing_capabilities"),
            "provider_facing_tools": row.get("provider_facing_tools"),
        })
    return packets


def run_lab(phase: str, imported_live: dict[str, Any] | None, contract_ids: list[str] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    py = sys.executable
    contract_ids = list(contract_ids or [])
    with tempfile.TemporaryDirectory(prefix="alex-external-lab-") as tmp:
        root = Path(tmp)
        reports = root / "reports"
        reports.mkdir()

        checks: list[dict[str, Any]] = []
        checks.append(_run(
            "unit_tests",
            [py, "-m", "unittest", "discover", "-s", "tests", "-v"],
            _isolated_env(root, "unit-tests"),
        ))
        checks.append(_run(
            "structural_selftest",
            [py, "alex-mcp/app/selftest.py"],
            _isolated_env(root, "selftest"),
        ))
        checks.append(_run(
            "tier_a_stress",
            [py, "alex-mcp/app/stress_test.py"],
            _isolated_env(root, "stress"),
        ))
        checks.append(_run(
            "final_architecture_audit",
            [py, "alex-mcp/app/final_audit.py"],
            _isolated_env(root, "final-audit"),
        ))

        catalog_path = reports / "catalog.json"
        checks.append(_run(
            "tier_b_catalog",
            [py, "alex-mcp/app/behavior_cert.py", "--mode", "catalog",
             "--report", str(catalog_path)],
            _isolated_env(root, "catalog", fixed_clock=True),
            catalog_path,
        ))

        offline_path = reports / "offline.json"
        offline = _run(
            "tier_b_offline",
            [py, "alex-mcp/app/behavior_cert.py", "--mode", "offline",
             "--phase", phase, "--no-fail-exit", "--report", str(offline_path)]
            + _contract_args(contract_ids),
            _isolated_env(root, "offline", fixed_clock=True),
            offline_path,
        )
        checks.append(offline)

        infrastructure_errors = [
            c["label"] for c in checks
            if c["returncode"] != 0 and c["label"] != "tier_b_offline"
        ]
        if offline.get("report") is None:
            infrastructure_errors.append("tier_b_offline_report_missing")

        offline_report = offline.get("report") or {}
        offline_failures = int(offline_report.get("summary", {}).get("failures") or 0)
        offline_live_required = int(
            offline_report.get("summary", {}).get("discovery_dependent_checks") or 0
        )

        packets = _offline_failure_packets(offline_report)
        imported_live_summary = None
        if imported_live:
            packets.extend(_live_failure_packets(imported_live))
            imported_live_summary = imported_live.get("summary")

        if infrastructure_errors:
            status = "LAB_ERROR"
        elif offline_report.get("status") == "PASS":
            status = "PASS"
        else:
            status = "PRODUCT_FAIL"

        report = {
            "mode": "external_alex_lab",
            "phase": phase,
            "status": status,
            "sandboxed": True,
            "home_assistant_dependency": False,
            "whatsapp_transport_started": False,
            "provider_calls_made": False,
            "contracts": contract_ids,
            "certification_clock": CERT_NOW,
            "summary": {
                "infrastructure_errors": infrastructure_errors,
                "offline_failures": offline_failures,
                "offline_live_required": offline_live_required,
                "failure_packets": len(packets),
                "imported_live_summary": imported_live_summary,
            },
            "checks": [
                {
                    "label": c["label"],
                    "status": c["status"],
                    "returncode": c["returncode"],
                    "summary": (
                        c["report"].get("summary")
                        if isinstance(c.get("report"), dict) else None
                    ),
                    "stdout_tail": c["stdout_tail"] if c["returncode"] else "",
                    "stderr_tail": c["stderr_tail"] if c["returncode"] else "",
                }
                for c in checks
            ],
            "manual_edges_still_external": offline_report.get("manual_gates_not_claimed", []),
        }
        return report, packets



def export_human_ai_packets(
    phase: str,
    packets_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Export the full zero-secret reasoning corpus for a ChatGPT/human pass."""
    py = sys.executable
    with tempfile.TemporaryDirectory(prefix="alex-human-ai-export-") as tmp:
        root = Path(tmp)
        env = _isolated_env(root, "human-ai", fixed_clock=True)
        return _run(
            "human_ai_packet_audit",
            [
                py, "alex-mcp/app/human_ai_lab.py",
                "--mode", "selftest",
                "--phase", phase,
                "--packets", str(packets_path),
                "--report", str(report_path),
            ],
            env,
            report_path,
        )


def run_real_ai_snapshot_review(report_path: Path) -> dict[str, Any]:
    """Score the frozen fresh-model decisions against the current blind corpus."""
    py = sys.executable
    with tempfile.TemporaryDirectory(prefix="alex-real-ai-review-") as tmp:
        root = Path(tmp)
        env = _isolated_env(root, "real-ai-review", fixed_clock=True)
        return _run(
            "real_ai_snapshot_review",
            [
                py, "alex-mcp/app/real_ai_snapshot_review.py",
                "--report", str(report_path),
            ],
            env,
            report_path,
        )


def run_chatgpt_reasoning_review(report_path: Path) -> dict[str, Any]:
    """Run the deterministic language/routing regression oracle."""
    py = sys.executable
    with tempfile.TemporaryDirectory(prefix="alex-chatgpt-review-") as tmp:
        root = Path(tmp)
        env = _isolated_env(root, "chatgpt-review", fixed_clock=True)
        return _run(
            "reasoning_regression_oracle",
            [
                py, "alex-mcp/app/chatgpt_reasoning_review.py",
                "--report", str(report_path),
            ],
            env,
            report_path,
        )


def run_live_provider(
    phase: str,
    contract_ids: list[str],
    provider: str,
    source_options: str,
    max_cost_usd: float,
    report_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the real provider tier externally against the certification sandbox."""
    py = sys.executable
    with tempfile.TemporaryDirectory(prefix="alex-external-live-") as tmp:
        root = Path(tmp)
        env = _isolated_env(root, "live", fixed_clock=True)
        cmd = [
            py, "alex-mcp/app/behavior_cert.py",
            "--mode", "live",
            "--phase", phase,
            "--provider", provider,
            "--source-options", source_options,
            "--max-live-cost-usd", str(max(0.0, max_cost_usd)),
            "--report", str(report_path),
            "--no-fail-exit",
        ] + _contract_args(contract_ids)
        result = _run("tier_b_live_provider", cmd, env, report_path)
    report = result.get("report") or {
        "mode": "live",
        "status": "ERROR",
        "summary": {"failures": 0},
    }
    return report, _live_failure_packets(report)


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run Alex externally, outside Home Assistant")
    parser.add_argument("--phase", choices=("all", "phase1", "phase2", "phase3"), default="all")
    parser.add_argument(
        "--contract", action="append", default=[],
        help="target one behaviour contract; repeatable for cheap repair loops",
    )
    parser.add_argument(
        "--live-report",
        help="optional prior behavior_cert live JSON; imported only for triage, never re-executed",
    )
    parser.add_argument("--run-live", action="store_true", help="opt in to real provider certification outside HA")
    parser.add_argument("--provider", choices=("auto", "gemini", "grok", "openai"), default="auto")
    parser.add_argument("--source-options", default=os.environ.get("ALEX_CERT_SOURCE_OPTIONS", "/data/options.json"))
    parser.add_argument("--max-live-cost-usd", type=float, default=0.25)
    parser.add_argument("--report", default="alex-lab-report.json")
    parser.add_argument("--packets", default="alex-lab-failures.jsonl")
    parser.add_argument(
        "--human-ai-packets", default=None,
        help="optional path for the provider-shaped ChatGPT/human reasoning corpus",
    )
    parser.add_argument(
        "--human-ai-report", default=None,
        help="optional path for the human-AI packet integrity report",
    )
    parser.add_argument(
        "--real-ai-review-report", default=None,
        help="optional path for the frozen fresh-model reasoning snapshot report",
    )
    parser.add_argument(
        "--chatgpt-review-report", default=None,
        help="optional path for the deterministic reasoning regression report",
    )
    parser.add_argument(
        "--gate", action="store_true",
        help="exit non-zero while product behaviour is not clean",
    )
    args = parser.parse_args()

    imported_live = _load_json(args.live_report)
    report, packets = run_lab(args.phase, imported_live, args.contract)

    report_path = Path(args.report)
    human_packets_path = Path(
        args.human_ai_packets
        or report_path.with_name("alex-human-ai-packets.jsonl")
    )
    human_report_path = Path(
        args.human_ai_report
        or report_path.with_name("alex-human-ai-report.json")
    )
    human = export_human_ai_packets(
        args.phase, human_packets_path, human_report_path
    )
    report["human_ai_bridge"] = {
        "status": human.get("status"),
        "returncode": human.get("returncode"),
        "summary": (
            human.get("report", {}).get("summary")
            if isinstance(human.get("report"), dict) else None
        ),
        "packets": str(human_packets_path),
        "report": str(human_report_path),
    }
    if human.get("returncode") != 0:
        report["status"] = "LAB_ERROR"
        report["summary"]["infrastructure_errors"] = sorted(set(
            list(report["summary"].get("infrastructure_errors") or [])
            + ["human_ai_packet_audit"]
        ))

    real_ai_review_path = Path(
        args.real_ai_review_report
        or report_path.with_name("alex-real-ai-reasoning-review.json")
    )
    real_ai_review = run_real_ai_snapshot_review(real_ai_review_path)
    real_ai_report = (
        real_ai_review.get("report")
        if isinstance(real_ai_review.get("report"), dict)
        else None
    )
    report["real_ai_reasoning_review"] = {
        "status": (
            real_ai_report.get("status")
            if isinstance(real_ai_report, dict)
            else real_ai_review.get("status")
        ),
        "returncode": real_ai_review.get("returncode"),
        "summary": (
            real_ai_report.get("summary")
            if isinstance(real_ai_report, dict) else None
        ),
        "external_model_review": (
            real_ai_report.get("external_model_review")
            if isinstance(real_ai_report, dict) else None
        ),
        "report": str(real_ai_review_path),
    }
    if real_ai_report is None:
        report["status"] = "LAB_ERROR"
        report["summary"]["infrastructure_errors"] = sorted(set(
            list(report["summary"].get("infrastructure_errors") or [])
            + ["real_ai_snapshot_review"]
        ))
    elif real_ai_report.get("status") != "PASS" and report.get("status") == "PASS":
        report["status"] = "PRODUCT_FAIL"

    chatgpt_review_path = Path(
        args.chatgpt_review_report
        or report_path.with_name("alex-chatgpt-reasoning-review.json")
    )
    chatgpt_review = run_chatgpt_reasoning_review(chatgpt_review_path)
    review_report = (
        chatgpt_review.get("report")
        if isinstance(chatgpt_review.get("report"), dict)
        else None
    )
    report["chatgpt_reasoning_review"] = {
        "status": (
            review_report.get("status")
            if isinstance(review_report, dict)
            else chatgpt_review.get("status")
        ),
        "returncode": chatgpt_review.get("returncode"),
        "summary": (
            review_report.get("summary")
            if isinstance(review_report, dict) else None
        ),
        "review": (
            review_report.get("review")
            if isinstance(review_report, dict) else None
        ),
        "report": str(chatgpt_review_path),
    }
    if review_report is None:
        report["status"] = "LAB_ERROR"
        report["summary"]["infrastructure_errors"] = sorted(set(
            list(report["summary"].get("infrastructure_errors") or [])
            + ["chatgpt_reasoning_review"]
        ))
    elif review_report.get("status") != "PASS" and report.get("status") == "PASS":
        report["status"] = "PRODUCT_FAIL"

    if args.run_live:
        live_path = Path(args.report).with_name("alex-lab-live.json")
        live_report, live_packets = run_live_provider(
            args.phase, args.contract, args.provider, args.source_options,
            args.max_live_cost_usd, live_path,
        )
        packets.extend(live_packets)
        report["provider_calls_made"] = True
        report["live_provider"] = {
            "status": live_report.get("status"),
            "summary": live_report.get("summary", {}),
            "report": str(live_path),
        }
        if live_report.get("status") != "PASS" and report.get("status") == "PASS":
            report["status"] = "PRODUCT_FAIL"

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    packets_path = Path(args.packets)
    packets_path.parent.mkdir(parents=True, exist_ok=True)
    with packets_path.open("w", encoding="utf-8") as fh:
        for packet in packets:
            fh.write(json.dumps(packet, ensure_ascii=False, default=str) + "\n")

    print(json.dumps({
        "status": report["status"],
        **report["summary"],
        "report": str(report_path),
        "packets": str(packets_path),
        "human_ai_packets": str(human_packets_path),
        "human_ai_report": str(human_report_path),
        "real_ai_review_report": str(real_ai_review_path),
        "reasoning_regression_report": str(chatgpt_review_path),
    }, indent=2, ensure_ascii=False))

    if report["status"] == "LAB_ERROR":
        raise SystemExit(2)
    if args.gate and report["status"] != "PASS":
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
