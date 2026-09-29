#!/usr/bin/env python3
"""Score a frozen *real model* reasoning snapshot against Alex's private contracts.

This file contains no prompt-routing rules. The actual decisions live in
real_ai_snapshot.json as an ordered snapshot produced by GPT-5.6 Sol after
reviewing ONLY the public human-AI packets: prompt, visible history and exposed
tool descriptions. Private contract ids/expected capabilities were not part of
that decision pass.

The corpus fingerprint binds those judgments to one exact packet set. Any
prompt, history or tool-surface change invalidates the snapshot and requires a
fresh external-model pass rather than silently reusing old answers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import human_ai_lab


HERE = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = HERE / "real_ai_snapshot.json"


def _load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("real-AI snapshot must be a JSON object")
    return value


def _decisions_from_snapshot(
    packets: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    reviewed = str(snapshot.get("corpus_fingerprint") or "")
    if reviewed != fingerprint:
        raise RuntimeError(
            "real-AI snapshot corpus mismatch; a fresh model review is required "
            f"(snapshot={reviewed or '<missing>'}, current={fingerprint})"
        )

    sequence = snapshot.get("sequence")
    toolsets = snapshot.get("toolsets")
    if not isinstance(sequence, list) or not isinstance(toolsets, dict):
        raise ValueError("real-AI snapshot is missing sequence/toolsets")
    if len(sequence) != len(packets):
        raise ValueError(
            f"real-AI decision count mismatch: {len(sequence)} != {len(packets)}"
        )
    if int(snapshot.get("decision_count") or -1) != len(sequence):
        raise ValueError("real-AI snapshot decision_count does not match sequence")

    decisions: list[dict[str, Any]] = [{
        "_meta": {
            "corpus_fingerprint": fingerprint,
            "reviewer": snapshot.get("reviewer"),
            "review_date": snapshot.get("review_date"),
            "method": snapshot.get("method"),
            "snapshot_version": snapshot.get("snapshot_version"),
        }
    }]

    for packet, code in zip(packets, sequence):
        template = toolsets.get(str(code))
        if not isinstance(template, dict):
            raise ValueError(f"unknown real-AI decision code: {code}")
        kind = str(template.get("decision") or "").strip().lower()
        tools = [
            str(name) for name in (template.get("tools") or [])
            if str(name).strip()
        ]
        decisions.append({
            "packet_id": packet["packet_id"],
            "decision": kind,
            "tools": tools,
            "reply_language": "en",
        })
    return decisions


async def run(snapshot_path: Path) -> dict[str, Any]:
    packets = await human_ai_lab.build_packets("all")
    snapshot = _load_snapshot(snapshot_path)
    decisions = _decisions_from_snapshot(packets, snapshot)
    report = human_ai_lab.score_packets(packets, decisions, "all")
    report["external_model_review"] = {
        "reviewer": snapshot.get("reviewer"),
        "review_date": snapshot.get("review_date"),
        "method": snapshot.get("method"),
        "snapshot_version": snapshot.get("snapshot_version"),
        "static_snapshot_not_live_api": True,
        "note": (
            "This proves one fresh model pass over this exact public packet corpus. "
            "It does not prove unseen wording or real WhatsApp/audio transport."
        ),
    }
    return report


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Score frozen real-AI reasoning snapshot")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    report = asyncio.run(run(Path(args.snapshot)))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if report.get("status") != "PASS":
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
