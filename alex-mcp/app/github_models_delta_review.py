#!/usr/bin/env python3
"""Refresh Alex's independent reasoning snapshot using GitHub Models.

This script deliberately preserves the existing certification boundary:
- the reviewer receives public reasoning packets only;
- expected contracts/scorer output/earlier decisions are never sent;
- unchanged packets are carried only from the embedded verified v5 parent;
- every changed/new packet receives a fresh raw model decision;
- the repo's existing v6 importer reconstructs and verifies the snapshot.

Authentication: GitHub Actions GITHUB_TOKEN with models: read.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import human_ai_lab
import real_ai_snapshot_review


MODELS_URL = "https://models.github.ai/inference/chat/completions"


def _canonical(packet: dict[str, Any]) -> str:
    return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _public_delta(
    packets: list[dict[str, Any]], parent_bundle: dict[str, Any]
) -> list[dict[str, Any]]:
    previous = human_ai_lab.expand_review_bundle(parent_bundle)
    previous_by_id = {str(row["packet_id"]): row for row in previous}
    current = [human_ai_lab._public_packet(row) for row in packets]
    return [
        row for row in current
        if str(row["packet_id"]) not in previous_by_id
        or _canonical(row) != _canonical(previous_by_id[str(row["packet_id"])])
    ]


def _extract_parent(snapshot: dict[str, Any]) -> tuple[str, str]:
    version = int(snapshot.get("snapshot_version") or 0)
    if version == 6:
        evidence = (snapshot.get("parent_review") or {}).get("evidence") or {}
        snapshot_source = evidence.get("snapshot") or {}
        bundle_source = evidence.get("public_bundle") or {}
        parent_raw = snapshot_source.get("raw_json")
        bundle_raw = bundle_source.get("raw_json")
        if isinstance(parent_raw, str) and isinstance(bundle_raw, str):
            return parent_raw, bundle_raw
        raise ValueError("v6 snapshot is missing embedded verified v5 parent evidence")
    raise ValueError(
        "Automated delta refresh currently requires the repository's verified v6 snapshot "
        "with embedded v5 parent evidence"
    )


def _compact_delta(delta: list[dict[str, Any]]) -> dict[str, Any]:
    if not delta:
        return {"shared_packet_fields": {}, "tools_by_ref": {}, "packets": []}
    shared = {}
    for key in (
        "packet_version", "reasoning_instruction", "runtime_policy",
        "evaluation_scope", "reply_language_policy",
    ):
        if key in delta[0] and all(row.get(key) == delta[0][key] for row in delta):
            shared[key] = delta[0][key]
    tools_by_ref = {}
    packets = []
    import hashlib
    for row in delta:
        refs = []
        for tool in row.get("available_tools", []):
            canonical = json.dumps(
                tool, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            ref = "tool-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            tools_by_ref[ref] = tool
            refs.append(ref)
        packets.append({
            **{
                key: value for key, value in row.items()
                if key not in shared and key != "available_tools"
            },
            "tool_refs": refs,
        })
    return {
        "shared_packet_fields": shared,
        "tools_by_ref": tools_by_ref,
        "packets": packets,
    }


async def prepare(
    snapshot_path: Path, prompt_path: Path, metadata_path: Path
) -> dict[str, Any]:
    packets = await human_ai_lab.build_packets("all")
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    _parent_raw, bundle_raw = _extract_parent(snapshot)
    parent_bundle = json.loads(bundle_raw)
    delta = _public_delta(packets, parent_bundle)
    metadata = {
        "corpus_fingerprint": fingerprint,
        "delta_packets": len(delta),
        "packet_ids": [str(row["packet_id"]) for row in delta],
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not delta:
        prompt_path.write_text(
            "No independent delta review is required.\n", encoding="utf-8"
        )
        return metadata

    compact = _compact_delta(delta)
    reviewer = "GitHub Copilot auto-selected isolated public-packet reviewer"
    prompt = f"""You are an isolated independent reviewer for Alex, a household assistant.

You are given ONLY public reasoning packets. Hidden certification contracts,
expected answers, previous decisions and scorer output are not available to you.

For EACH packet:
1. Expand tool_refs using tools_by_ref.
2. Follow reasoning_instruction and runtime_policy exactly.
3. Choose exactly one decision: tools, clarify, refuse, or answer.
4. If decision=tools, select only supplied tool names and include all capabilities
   required for the whole turn, but no unrelated capability.
5. If decision is clarify/refuse/answer, tools MUST be [].
6. Never invent record IDs, facts, amounts, currency, dates or permissions.
7. reply_language must follow the packet's language policy.

Return ONLY valid JSON with this exact top-level shape:
{{
  "format": "alex-independent-review-v1",
  "corpus_fingerprint": "{fingerprint}",
  "reviewer": "{reviewer}",
  "review_date": "{date.today().isoformat()}",
  "review_scope": "public_packets_only",
  "expected_answers_visible": false,
  "decisions": [
    {{
      "packet_id": "exact packet id",
      "decision": "tools|clarify|refuse|answer",
      "tools": ["exact supplied tool name"],
      "reply_language": "en",
      "rationale": "brief reason based only on public evidence"
    }}
  ]
}}

Every packet below must have exactly one decision. Do not add unknown packet IDs.
Do not use Markdown or code fences.

PUBLIC DELTA PACKETS:
{json.dumps(compact, ensure_ascii=False, separators=(",", ":"))}
"""
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    return metadata


def _raw_json_response(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if raw.startswith(chr(96) * 3):
        raise ValueError(
            "independent reviewer returned Markdown fences; raw JSON is required"
        )
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("independent reviewer response must be a JSON object")
    return raw


async def import_review(
    snapshot_path: Path, response_path: Path, report_path: Path | None
) -> dict[str, Any]:
    packets = await human_ai_lab.build_packets("all")
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
    parent_raw, bundle_raw = _extract_parent(existing)
    parent_bundle = json.loads(bundle_raw)
    delta = _public_delta(packets, parent_bundle)
    if not delta:
        return {
            "status": "UNCHANGED",
            "corpus_fingerprint": fingerprint,
            "delta_packets": 0,
        }

    raw = _raw_json_response(response_path)
    document = json.loads(raw)
    expected_ids = [str(row["packet_id"]) for row in delta]
    decisions = document.get("decisions") or []
    actual_ids = [
        str(row.get("packet_id") or "")
        for row in decisions
        if isinstance(row, dict)
    ]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != set(expected_ids):
        raise ValueError(
            "independent reviewer did not cover the exact delta packet set"
        )
    if document.get("corpus_fingerprint") != fingerprint:
        raise ValueError("independent reviewer corpus fingerprint mismatch")
    if document.get("review_scope") != "public_packets_only":
        raise ValueError("independent reviewer did not attest public-only review")
    if document.get("expected_answers_visible") is not False:
        raise ValueError(
            "independent reviewer did not attest hidden expected answers"
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent_path = root / "parent-v5.json"
        bundle_path = root / "parent-public.json"
        review_path = root / "copilot-delta.json"
        parent_path.write_text(parent_raw, encoding="utf-8")
        bundle_path.write_text(bundle_raw, encoding="utf-8")
        review_path.write_text(raw + "\n", encoding="utf-8")
        refreshed = real_ai_snapshot_review.import_delta_review(
            packets, [review_path], parent_path, bundle_path
        )
        validated = real_ai_snapshot_review._decisions_from_snapshot(
            packets, refreshed
        )
        score = human_ai_lab.score_packets(packets, validated, "all")
        result = {
            "status": score.get("status"),
            "corpus_fingerprint": fingerprint,
            "delta_packets": len(delta),
            "reviewer": document.get("reviewer"),
            "score_summary": score.get("summary"),
        }
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "result": result,
                        "raw_review": document,
                        "score": score,
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        if score.get("status") != "PASS":
            raise RuntimeError(
                "Fresh independent delta review disagrees with certification "
                "contracts. The shipped snapshot was left unchanged."
            )
        snapshot_path.write_text(
            json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--snapshot", required=True)
    prepare_parser.add_argument("--prompt-out", required=True)
    prepare_parser.add_argument("--metadata-out", required=True)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("--snapshot", required=True)
    import_parser.add_argument("--response", required=True)
    import_parser.add_argument("--report")

    args = parser.parse_args()
    if args.command == "prepare":
        result = asyncio.run(
            prepare(
                Path(args.snapshot),
                Path(args.prompt_out),
                Path(args.metadata_out),
            )
        )
    else:
        result = asyncio.run(
            import_review(
                Path(args.snapshot),
                Path(args.response),
                Path(args.report) if args.report else None,
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
