#!/usr/bin/env python3
"""Sharded independent Copilot review for Alex reasoning certification."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path

import human_ai_lab
import real_ai_snapshot_review
from github_models_delta_review import _compact_delta, _extract_parent, _public_delta


def _prompt(shard, fingerprint: str, index: int, total: int) -> str:
    reviewer = (
        f"GitHub Copilot auto-selected isolated public-packet reviewer "
        f"shard {index}/{total}"
    )
    compact = _compact_delta(shard)
    return f"""You are an isolated independent reviewer for Alex, a household assistant.

You are given ONLY public reasoning packets. Hidden certification contracts,
expected answers, previous decisions and scorer output are not available.

For EACH packet:
1. Expand tool_refs using tools_by_ref.
2. Follow reasoning_instruction and runtime_policy exactly.
3. Choose exactly one decision: tools, clarify, refuse, or answer.
4. For decision=tools, use ONLY exact tool names present in THAT packet's
   expanded available_tools. Never borrow a tool name from another packet.
5. Include all supplied tools needed for the whole turn, but no unrelated tool.
   If a supplied tool accepts a natural name/reference and resolves it internally,
   use it directly instead of inventing a separate lookup.
6. For clarify/refuse/answer, tools MUST be [].
7. Never invent record IDs, facts, amounts, currency, dates or permissions.
8. reply_language must follow the packet policy.

Return ONLY valid JSON:
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
      "rationale": "brief public-evidence reason"
    }}
  ]
}}

Review every packet below exactly once. Do not add unknown packet IDs.
Do not use Markdown or code fences.

PUBLIC DELTA PACKETS:
{json.dumps(compact, ensure_ascii=False, separators=(",", ":"))}
"""


async def prepare(snapshot: Path, prompt_dir: Path, metadata: Path, shard_size: int):
    packets = await human_ai_lab.build_packets("all")
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    existing = json.loads(snapshot.read_text(encoding="utf-8"))
    if str(existing.get("corpus_fingerprint") or "") == fingerprint:
        delta = []
    else:
        _parent, bundle_raw = _extract_parent(existing)
        delta = _public_delta(packets, json.loads(bundle_raw))
    size = max(12, min(80, int(shard_size)))
    shards = [delta[i:i + size] for i in range(0, len(delta), size)]
    prompt_dir.mkdir(parents=True, exist_ok=True)
    meta_shards = []
    for index, shard in enumerate(shards, 1):
        name = f"shard-{index:03d}.txt"
        (prompt_dir / name).write_text(
            _prompt(shard, fingerprint, index, len(shards)), encoding="utf-8"
        )
        meta_shards.append({
            "file": name,
            "packet_count": len(shard),
            "packet_ids": [str(row["packet_id"]) for row in shard],
        })
    result = {
        "corpus_fingerprint": fingerprint,
        "delta_packets": len(delta),
        "shard_count": len(shards),
        "shard_size": size,
        "shards": meta_shards,
    }
    metadata.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _read_response(path: Path):
    raw = path.read_text(encoding="utf-8").strip()
    if raw.startswith(chr(96) * 3):
        raise ValueError(f"{path.name}: Markdown fences are not accepted")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError(f"{path.name}: response must be a JSON object")
    return raw, doc


async def import_shards(snapshot: Path, response_dir: Path, report: Path | None):
    packets = await human_ai_lab.build_packets("all")
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    existing = json.loads(snapshot.read_text(encoding="utf-8"))
    if str(existing.get("corpus_fingerprint") or "") == fingerprint:
        return {"status": "UNCHANGED", "delta_packets": 0,
                "corpus_fingerprint": fingerprint}
    parent_raw, bundle_raw = _extract_parent(existing)
    delta = _public_delta(packets, json.loads(bundle_raw))
    expected = [str(row["packet_id"]) for row in delta]
    expected_set = set(expected)
    if not delta:
        return {"status": "UNCHANGED", "delta_packets": 0,
                "corpus_fingerprint": fingerprint}

    sources = []
    seen = set()
    response_paths = sorted(
        p for p in response_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".json", ".txt"}
    )
    if not response_paths:
        raise ValueError("no independent review shard responses found")
    for path in response_paths:
        raw, doc = _read_response(path)
        if doc.get("corpus_fingerprint") != fingerprint:
            raise ValueError(f"{path.name}: corpus fingerprint mismatch")
        if doc.get("review_scope") != "public_packets_only":
            raise ValueError(f"{path.name}: public-only attestation missing")
        if doc.get("expected_answers_visible") is not False:
            raise ValueError(f"{path.name}: expected-answer attestation invalid")
        rows = doc.get("decisions")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{path.name}: no decisions")
        local = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}: invalid decision row")
            pid = str(row.get("packet_id") or "")
            if pid not in expected_set:
                raise ValueError(f"{path.name}: unknown packet id {pid!r}")
            if pid in local or pid in seen:
                raise ValueError(f"duplicate reviewed packet id {pid}")
            local.add(pid)
            seen.add(pid)
        sources.append((raw, doc))

    missing = [pid for pid in expected if pid not in seen]
    if missing:
        raise ValueError(
            f"independent shards missed {len(missing)} packets: "
            + ", ".join(missing[:10])
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = root / "parent-v5.json"
        bundle = root / "parent-public.json"
        parent.write_text(parent_raw, encoding="utf-8")
        bundle.write_text(bundle_raw, encoding="utf-8")
        review_paths = []
        for idx, (raw, _doc) in enumerate(sources, 1):
            path = root / f"review-{idx:03d}.json"
            path.write_text(raw + "\n", encoding="utf-8")
            review_paths.append(path)
        refreshed = real_ai_snapshot_review.import_delta_review(
            packets, review_paths, parent, bundle
        )
        decisions = real_ai_snapshot_review._decisions_from_snapshot(
            packets, refreshed
        )
        score = human_ai_lab.score_packets(packets, decisions, "all")
        result = {
            "status": score.get("status"),
            "corpus_fingerprint": fingerprint,
            "delta_packets": len(delta),
            "review_shards": len(sources),
            "score_summary": score.get("summary"),
        }
        if report:
            report.write_text(
                json.dumps({
                    "result": result,
                    "raw_reviews": [doc for _raw, doc in sources],
                    "score": score,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if score.get("status") != "PASS":
            raise RuntimeError(
                "Fresh sharded independent review disagrees with certification contracts"
            )
        snapshot.write_text(
            json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--prompt-dir", required=True)
    p.add_argument("--metadata-out", required=True)
    p.add_argument("--shard-size", type=int, default=48)
    i = sub.add_parser("import")
    i.add_argument("--snapshot", required=True)
    i.add_argument("--response-dir", required=True)
    i.add_argument("--report")
    args = parser.parse_args()
    if args.command == "prepare":
        result = asyncio.run(prepare(
            Path(args.snapshot), Path(args.prompt_dir),
            Path(args.metadata_out), args.shard_size
        ))
    else:
        result = asyncio.run(import_shards(
            Path(args.snapshot), Path(args.response_dir),
            Path(args.report) if args.report else None
        ))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
