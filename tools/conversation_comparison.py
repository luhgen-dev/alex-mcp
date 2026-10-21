#!/usr/bin/env python3
"""Run identical offline conversation checks against two repository snapshots.

Example:
  python tools/conversation_comparison.py --baseline /tmp/alex-baseline \
      --candidate . --report /tmp/conversation-comparison.json

Every worker imports the selected snapshot in a separate process and uses the
existing test fixture's disposable database. Model responses are scripted; the
report measures state/orchestration contracts, model-call counts and serialized
message sizes, NOT live model accuracy, tokens, provider costs or user latency.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import unittest


TEST_FILE = Path(__file__).resolve().parents[1] / "tests" / "test_conversation_reliability.py"


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []
        self._started = 0.0
        self._outcome = "unknown"
        self._detail = ""

    def startTest(self, test):
        super().startTest(test)
        self._started = time.perf_counter()
        self._outcome = "unknown"
        self._detail = ""

    def addSuccess(self, test):
        super().addSuccess(test)
        self._outcome = "passed"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._outcome = "failed"
        self._detail = self._exc_info_to_string(err, test)

    def addError(self, test, err):
        super().addError(test, err)
        self._outcome = "error"
        self._detail = self._exc_info_to_string(err, test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._outcome = "skipped"
        self._detail = reason

    def stopTest(self, test):
        self.rows.append({
            "case": test.id().split(".", 1)[-1],
            "evidence_path": getattr(test, "evidence_path", "unspecified"),
            "status": self._outcome,
            "local_test_ms": round((time.perf_counter() - self._started) * 1000, 3),
            "detail": self._detail,
            "offline_provider_metrics": getattr(test, "offline_metrics", None),
        })
        super().stopTest(test)


def revision(repo: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def source_fingerprint(repo: Path) -> str:
    """Include new, uncommitted runtime modules in snapshot identity."""
    digest = hashlib.sha256()
    for path in sorted((repo / "alex-mcp" / "app").rglob("*.py")):
        digest.update(str(path.relative_to(repo)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def worker(repo: Path, output: Path) -> int:
    sys.path.insert(0, str(repo / "alex-mcp" / "app"))
    sys.path.insert(0, str(repo / "tests"))
    spec = importlib.util.spec_from_file_location("conversation_reliability_tests", TEST_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    cases = result.rows
    metrics = [x["offline_provider_metrics"] for x in cases if x["offline_provider_metrics"]]
    value = {
        "repository": str(repo), "revision": revision(repo),
        "runtime_source_sha256": source_fingerprint(repo),
        "shared_test_file_sha256": hashlib.sha256(TEST_FILE.read_bytes()).hexdigest(),
        "evidence_kind": "deterministic state and scripted-provider orchestration",
        "live_model_accuracy_measured": False,
        "live_latency_or_cost_measured": False,
        "summary": {
            "tests": result.testsRun,
            "passed": sum(x["status"] == "passed" for x in cases),
            "failed": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped),
            "local_suite_seconds": round(time.perf_counter() - started, 3),
            "provider_packet_cases": len(metrics),
            "scripted_model_calls": sum(x["model_calls"] for x in metrics),
            "median_message_characters": statistics.median(
                x["message_characters"] for x in metrics) if metrics else None,
        },
        "cases": cases,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    # A baseline may intentionally fail new behavior checks. The parent uses
    # report statuses; worker exit status denotes infrastructure completion.
    return 0


def compare(baseline: Path, candidate: Path, report: Path, timeout: int) -> int:
    report.parent.mkdir(parents=True, exist_ok=True)
    snapshots = {}
    for label, repo in (("baseline", baseline), ("candidate", candidate)):
        path = report.with_name(report.stem + f"-{label}.json")
        log = report.with_name(report.stem + f"-{label}.log")
        with log.open("w") as stream:
            child = subprocess.run([
                sys.executable, str(Path(__file__).resolve()), "--worker",
                "--repo", str(repo), "--report", str(path),
            ], cwd=repo, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        if child.returncode or not path.exists():
            raise RuntimeError(f"{label} comparison worker failed; see {log}")
        snapshots[label] = json.loads(path.read_text())
    before = {x["case"]: x for x in snapshots["baseline"]["cases"]}
    after = {x["case"]: x for x in snapshots["candidate"]["cases"]}
    improved = [key for key in after if before[key]["status"] != "passed"
                and after[key]["status"] == "passed"]
    regressed = [key for key in after if before[key]["status"] == "passed"
                 and after[key]["status"] != "passed"]
    unresolved = [key for key in after if after[key]["status"] != "passed"]
    paired = [key for key in after if before[key]["offline_provider_metrics"]
              and after[key]["offline_provider_metrics"]]
    paired_metrics = []
    for key in paired:
        old, new = before[key]["offline_provider_metrics"], after[key]["offline_provider_metrics"]
        paired_metrics.append({
            "case": key, "baseline_calls": old["model_calls"], "candidate_calls": new["model_calls"],
            "baseline_initial_message_characters": old["message_characters"],
            "candidate_initial_message_characters": new["message_characters"],
            "message_character_delta": new["message_characters"] - old["message_characters"],
        })
    result = {
        "evidence_kind": "offline contract comparison; not a language-model benchmark",
        "test_file": str(TEST_FILE),
        "baseline": snapshots["baseline"], "candidate": snapshots["candidate"],
        "comparison": {
            "improved_count": len(improved), "improved_cases": improved,
            "regression_count": len(regressed), "regressed_cases": regressed,
            "unresolved_count": len(unresolved), "unresolved_cases": unresolved,
        },
        "paired_offline_metrics": {
            "cases_with_both_packets": len(paired_metrics),
            "baseline_calls": sum(row["baseline_calls"] for row in paired_metrics),
            "candidate_calls": sum(row["candidate_calls"] for row in paired_metrics),
            "median_message_character_delta": statistics.median(
                row["message_character_delta"] for row in paired_metrics) if paired_metrics else None,
            "rows": paired_metrics,
        },
        "limitations": [
            "Provider responses are scripted; semantic interpretation is not scored.",
            "Local test timing excludes real model, network and WhatsApp latency.",
            "Serialized message characters are not token or cost measurements.",
            "An absent new state API is a failed contract, not evidence of a live incident.",
            "Direct brain tests bypass ingress; their baseline failures do not establish a WhatsApp/ingress failure.",
        ],
    }
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "baseline": result["baseline"]["summary"],
        "candidate": result["candidate"]["summary"],
        "comparison": result["comparison"], "report": str(report),
    }, indent=2))
    return int(bool(unresolved))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.repo.resolve(), args.report.resolve())
    if not args.baseline:
        parser.error("--baseline is required")
    return compare(args.baseline.resolve(), args.candidate.resolve(),
                   args.report.resolve(), args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
