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

    version = int(snapshot.get("snapshot_version") or 0)
    decisions: list[dict[str, Any]] = [{
        "_meta": {
            "corpus_fingerprint": fingerprint,
            "reviewer": snapshot.get("reviewer"),
            "review_date": snapshot.get("review_date"),
            "method": snapshot.get("method"),
            "snapshot_version": version,
        }
    }]

    if version >= 3:
        by_id = snapshot.get("decisions_by_packet_id")
        if not isinstance(by_id, dict):
            raise ValueError("v3 real-AI snapshot is missing decisions_by_packet_id")
        expected_ids = {str(packet["packet_id"]) for packet in packets}
        actual_ids = {str(pid) for pid in by_id}
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        if missing or extra:
            raise ValueError(
                "v3 real-AI packet-id mismatch "
                f"(missing={missing[:5]}, extra={extra[:5]})"
            )
        if int(snapshot.get("decision_count") or -1) != len(expected_ids):
            raise ValueError("v3 real-AI decision_count does not match packet set")

        delta = snapshot.get("delta_review") or {}
        parent = snapshot.get("parent_review") or {}
        reviewed_delta = {
            str(pid) for pid in (delta.get("reviewed_packet_ids") or [])
        }
        if len(reviewed_delta) != int(delta.get("changed_existing") or 0) + int(
            delta.get("newly_added") or 0
        ):
            raise ValueError("v3 real-AI delta provenance count is inconsistent")
        carried = sum(
            1 for row in by_id.values()
            if isinstance(row, dict)
            and row.get("provenance") == "parent_independent_model"
        )
        refreshed = sum(
            1 for row in by_id.values()
            if isinstance(row, dict)
            and row.get("provenance") == "delta_engineering_review"
        )
        if carried != int(parent.get("carried_forward_unchanged") or -1):
            raise ValueError("v3 carried-forward provenance count is inconsistent")
        if refreshed != len(reviewed_delta):
            raise ValueError("v3 delta-review provenance count is inconsistent")
        if not reviewed_delta <= expected_ids:
            raise ValueError("v3 delta-review contains unknown packet ids")

        for packet in packets:
            pid = str(packet["packet_id"])
            row = by_id.get(pid)
            if not isinstance(row, dict):
                raise ValueError(f"invalid v3 real-AI decision for {pid}")
            decisions.append({
                "packet_id": pid,
                "decision": str(row.get("decision") or "").strip().lower(),
                "tools": [
                    str(name) for name in (row.get("tools") or [])
                    if str(name).strip()
                ],
                "reply_language": str(row.get("reply_language") or "en"),
            })
        return decisions

    # Backward-compatible v2 ordered snapshots.
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

    for packet, code in zip(packets, sequence):
        template = toolsets.get(str(code))
        if not isinstance(template, dict):
            raise ValueError(f"unknown real-AI decision code: {code}")
        decisions.append({
            "packet_id": packet["packet_id"],
            "decision": str(template.get("decision") or "").strip().lower(),
            "tools": [
                str(name) for name in (template.get("tools") or [])
                if str(name).strip()
            ],
            "reply_language": str(template.get("reply_language") or "en"),
        })
    return decisions


async def run(snapshot_path: Path) -> dict[str, Any]:
    packets = await human_ai_lab.build_packets("all")
    snapshot = _load_snapshot(snapshot_path)
    decisions = _decisions_from_snapshot(packets, snapshot)
    report = human_ai_lab.score_packets(packets, decisions, "all")
    parent = snapshot.get("parent_review") or {}
    delta = snapshot.get("delta_review") or {}
    report["external_model_review"] = {
        "reviewer": snapshot.get("reviewer"),
        "review_date": snapshot.get("review_date"),
        "method": snapshot.get("method"),
        "snapshot_version": snapshot.get("snapshot_version"),
        "static_snapshot_not_live_api": True,
        "parent_independent_model_packets": parent.get("carried_forward_unchanged"),
        "delta_engineering_review_packets": len(delta.get("reviewed_packet_ids") or []),
        "note": (
            "For v3, unchanged public packets retain the prior independent-model "
            "judgment only after packet comparison; changed/new packets are explicitly "
            "labelled engineering delta review rather than a blind first pass. "
            "The score does not prove unseen wording or real WhatsApp/audio transport."
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
