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
import os
import re
import tempfile
import urllib.error
import urllib.request
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


def _json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^\s*```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```\s*$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("reviewer response must be one JSON object")
    return parsed


def _call_model(token: str, model: str, packet: dict[str, Any]) -> dict[str, Any]:
    available = [
        str(tool.get("name") or "")
        for tool in packet.get("available_tools", [])
        if str(tool.get("name") or "").strip()
    ]
    system = (
        "You are an isolated independent reviewer for a household-assistant reasoning packet. "
        "You can see ONLY public packet data and must not infer hidden expected answers. "
        "Follow the packet's reasoning_instruction and runtime_policy. "
        "Choose exactly one decision from: tools, clarify, refuse, answer. "
        "If decision=tools, choose only capability/tool names present in available_tools and "
        "include every tool needed for the whole turn, but no unrelated tools. "
        "If clarify/refuse/answer, tools must be an empty list. "
        "Return ONLY one JSON object with keys packet_id, decision, tools, reply_language, rationale. "
        "Do not use markdown."
    )
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
            },
        ],
    }
    request = urllib.request.Request(
        MODELS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"GitHub Models HTTP {exc.code}: {detail}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("GitHub Models returned no choices")
    content = ((choices[0].get("message") or {}).get("content") or "")
    decision = _json_object(content)
    pid = str(packet["packet_id"])
    if str(decision.get("packet_id") or "") != pid:
        raise ValueError(f"reviewer packet id mismatch for {pid}")
    kind = str(decision.get("decision") or "").strip().casefold()
    if kind not in human_ai_lab.DECISION_KINDS:
        raise ValueError(f"invalid reviewer decision {kind!r} for {pid}")
    tools = decision.get("tools")
    if not isinstance(tools, list) or any(not isinstance(name, str) for name in tools):
        raise ValueError(f"invalid tool list for {pid}")
    if kind != "tools" and tools:
        raise ValueError(f"non-tool decision returned tools for {pid}")
    unknown = sorted(set(tools) - set(available))
    if unknown:
        raise ValueError(f"reviewer selected unavailable tools for {pid}: {unknown}")
    language = str(decision.get("reply_language") or "").strip()
    if not language:
        raise ValueError(f"reviewer omitted reply language for {pid}")
    return {
        "packet_id": pid,
        "decision": kind,
        "tools": tools,
        "reply_language": language,
        "rationale": str(decision.get("rationale") or "").strip()[:2000],
    }


async def refresh(snapshot_path: Path, model: str, token: str, report_path: Path | None) -> dict[str, Any]:
    packets = await human_ai_lab.build_packets("all")
    current_fingerprint = human_ai_lab._corpus_fingerprint(packets)
    existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
    parent_raw, bundle_raw = _extract_parent(existing)
    parent_bundle = json.loads(bundle_raw)
    delta = _public_delta(packets, parent_bundle)
    if not delta:
        return {
            "status": "UNCHANGED",
            "corpus_fingerprint": current_fingerprint,
            "delta_packets": 0,
        }

    decisions = []
    for index, packet in enumerate(delta, 1):
        print(
            f"[independent-review] {index}/{len(delta)} {packet['packet_id']}",
            flush=True,
        )
        decisions.append(_call_model(token, model, packet))

    source_document = {
        "format": "alex-independent-review-v1",
        "corpus_fingerprint": current_fingerprint,
        "reviewer": f"GitHub Models {model} isolated public-packet reviewer",
        "review_date": date.today().isoformat(),
        "review_scope": "public_packets_only",
        "expected_answers_visible": False,
        "decisions": decisions,
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        parent_path = root / "parent-v5.json"
        bundle_path = root / "parent-public.json"
        review_path = root / "github-models-delta.json"
        parent_path.write_text(parent_raw, encoding="utf-8")
        bundle_path.write_text(bundle_raw, encoding="utf-8")
        raw_review = json.dumps(source_document, ensure_ascii=False, indent=2) + "\n"
        review_path.write_text(raw_review, encoding="utf-8")

        refreshed = real_ai_snapshot_review.import_delta_review(
            packets, [review_path], parent_path, bundle_path
        )
        # Reconstruct from the raw evidence before scoring. This is the same
        # fail-closed validation used by normal CI.
        model_decisions = real_ai_snapshot_review._decisions_from_snapshot(
            packets, refreshed
        )
        score = human_ai_lab.score_packets(packets, model_decisions, "all")
        result = {
            "status": score.get("status"),
            "corpus_fingerprint": current_fingerprint,
            "delta_packets": len(delta),
            "reviewer": source_document["reviewer"],
            "score_summary": score.get("summary"),
            "reviewed_packet_ids": [row["packet_id"] for row in decisions],
        }
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        **result,
                        "raw_review": source_document,
                        "score": score,
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        if score.get("status") != "PASS":
            raise RuntimeError(
                "Fresh independent delta review disagrees with certification contracts; "
                "snapshot was not updated. Inspect the preserved review report."
            )
        snapshot_path.write_text(
            json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        default=str(Path(__file__).resolve().parent / "real_ai_snapshot.json"),
    )
    parser.add_argument("--model", default=os.environ.get("ALEX_REVIEW_MODEL", "openai/gpt-4.1"))
    parser.add_argument("--report", default="/tmp/alex-github-models-review.json")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    result = asyncio.run(
        refresh(
            Path(args.snapshot),
            args.model,
            token,
            Path(args.report) if args.report else None,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
