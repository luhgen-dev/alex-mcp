#!/usr/bin/env python3
"""Score a frozen *real model* reasoning snapshot against Alex's private contracts.

This file contains no prompt-routing rules. The actual decisions live in a
versioned snapshot. Legacy snapshots retain their original provenance; v5
imports a complete fresh review and preserves every raw reviewer source. v6
retains only exactly unchanged public packets from a verified v5 parent and
requires fresh raw independent evidence for every changed or added packet.
Independent reviewers receive only public packets: prompts, visible history,
input evidence, runtime policy excerpts and exposed tool descriptions.

The corpus fingerprint binds those judgments to one exact packet set. Any
prompt, history or tool-surface change invalidates the snapshot and requires a
fresh external-model pass rather than silently reusing old answers.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import human_ai_lab


HERE = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = HERE / "real_ai_snapshot.json"


def _unique_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in independent review: {key}")
        result[key] = value
    return result


def _review_source_decisions(
    fingerprint: str, packet_ids: list[str], sources: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validate exact raw-source coverage without consulting private answers."""
    expected_ids = set(packet_ids)
    if len(expected_ids) != len(packet_ids):
        raise ValueError("duplicate packet ids in review corpus")
    by_id: dict[str, Any] = {}
    reviewers: list[str] = []
    dates: list[str] = []
    source_ids: set[str] = set()
    if not sources:
        raise ValueError("v5 full review requires raw review sources")
    for source in sources:
        raw = source.get("raw_json")
        if not isinstance(raw, str):
            raise ValueError("v5 review source is missing raw_json")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if source.get("sha256") != digest or source.get("source_id") != digest:
            raise ValueError("v5 raw review source hash mismatch")
        if digest in source_ids:
            raise ValueError("duplicate v5 raw review source")
        source_ids.add(digest)
        document = json.loads(raw, object_pairs_hook=_unique_json_keys)
        if not isinstance(document, dict) or document.get("format") != "alex-independent-review-v1":
            raise ValueError("unsupported independent review source format")
        if document.get("corpus_fingerprint") != fingerprint:
            raise ValueError("independent review source corpus mismatch")
        if (document.get("review_scope") != "public_packets_only"
                or document.get("expected_answers_visible") is not False):
            raise ValueError("independent review source lacks public-only attestation")
        reviewer = document.get("reviewer")
        review_date = document.get("review_date")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("independent review source is missing reviewer")
        if not isinstance(review_date, str) or date.fromisoformat(review_date).isoformat() != review_date:
            raise ValueError("independent review source requires ISO review_date")
        if reviewer not in reviewers:
            reviewers.append(reviewer)
        dates.append(review_date)
        rows = document.get("decisions")
        if not isinstance(rows, list) or not rows:
            raise ValueError("independent review source requires decisions")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid independent review decision")
            pid = row.get("packet_id")
            if not isinstance(pid, str) or pid not in expected_ids:
                raise ValueError(f"unknown independent review packet id: {pid}")
            if pid in by_id:
                raise ValueError(f"duplicate independent review decision: {pid}")
            if "provenance" in row or "review_source_id" in row:
                raise ValueError("raw independent decision uses reserved provenance fields")
            # Validate representation, not agreement with any private contract.
            if row.get("decision") not in human_ai_lab.DECISION_KINDS:
                raise ValueError(f"invalid independent decision kind for {pid}")
            if not isinstance(row.get("tools"), list) or any(
                not isinstance(name, str) or not name.strip() for name in row["tools"]
            ):
                raise ValueError(f"invalid independent tool list for {pid}")
            if not isinstance(row.get("reply_language"), str) or not row["reply_language"].strip():
                raise ValueError(f"missing independent reply_language for {pid}")
            by_id[pid] = {
                **row, "provenance": "full_independent_model",
                "review_source_id": digest,
            }
    missing = sorted(expected_ids - set(by_id))
    if missing:
        raise ValueError(f"incomplete independent review coverage: {missing[:5]}")
    return by_id, reviewers, dates


def _full_review_snapshot(
    packets: list[dict[str, Any]], sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build v5 from preserved raw reviews; never infer or correct an answer.

    Public-only review is an explicit reviewer attestation, not something a
    hash can prove. Raw UTF-8 bytes and complete coverage are verifiable here.
    """
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    packet_ids = [str(packet["packet_id"]) for packet in packets]
    by_id, reviewers, dates = _review_source_decisions(fingerprint, packet_ids, sources)
    return {
        "snapshot_version": 5,
        "corpus_fingerprint": fingerprint,
        "reviewer": "; ".join(reviewers),
        "review_date": max(dates),
        "method": (
            "Fresh full-corpus review from public-only reviewer attestations; "
            "raw source JSON and SHA-256 are preserved. No earlier decisions "
            "are carried forward and no decisions are corrected by the importer."
        ),
        "decision_count": len(packet_ids),
        "full_review": {
            "reviewed_packet_ids": packet_ids,
            "carried_forward_unchanged": 0,
            "expected_answers_visible": False,
            "public_only_review_attested": True,
            "source_count": len(sources),
            "sources": sources,
        },
        "decisions_by_packet_id": {pid: by_id[pid] for pid in packet_ids},
    }


def _read_sources(source_paths: list[Path]) -> list[dict[str, Any]]:
    sources = []
    for path in source_paths:
        raw = path.read_bytes().decode("utf-8")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        sources.append({
            "source_id": digest, "source_name": path.name,
            "sha256": digest, "raw_json": raw,
        })
    return sources


def import_full_review(
    packets: list[dict[str, Any]], source_paths: list[Path],
) -> dict[str, Any]:
    """Import disjoint reviewer shards, preserving exact raw UTF-8 content."""
    return _full_review_snapshot(packets, _read_sources(source_paths))


def _evidence_document(source: dict[str, Any], label: str) -> dict[str, Any]:
    raw = source.get("raw_json")
    if not isinstance(raw, str):
        raise ValueError(f"v6 {label} is missing raw_json")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if source.get("sha256") != digest or source.get("source_id") != digest:
        raise ValueError(f"v6 {label} hash mismatch")
    document = json.loads(raw, object_pairs_hook=_unique_json_keys)
    if not isinstance(document, dict):
        raise ValueError(f"v6 {label} must be a JSON object")
    return document


def _verified_delta_snapshot(
    packets: list[dict[str, Any]], parent_evidence: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reconstruct v6 from verified parent bytes and exact independent delta.

    Only public-content equality permits reuse. Parent scores and private
    contracts do not participate; a failed parent decision is never corrected.
    """
    parent = _evidence_document(parent_evidence.get("snapshot") or {}, "parent snapshot")
    bundle = _evidence_document(parent_evidence.get("public_bundle") or {}, "parent public bundle")
    if parent.get("snapshot_version") != 5:
        raise ValueError("v6 requires a fully verified v5 parent snapshot")
    previous = human_ai_lab.expand_review_bundle(bundle)
    _decisions_from_snapshot(previous, parent)
    public = [human_ai_lab._public_packet(packet) for packet in packets]
    current_ids = [str(packet["packet_id"]) for packet in public]
    if len(set(current_ids)) != len(current_ids):
        raise ValueError("duplicate packet ids in current delta corpus")
    previous_by_id = {str(packet["packet_id"]): packet for packet in previous}
    # Canonical serialized content distinguishes JSON number/boolean types and
    # retains tool/message/list order; dictionary key order is immaterial.
    def canonical(packet: dict[str, Any]) -> str:
        return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    carried_ids = [str(packet["packet_id"]) for packet in public
                   if str(packet["packet_id"]) in previous_by_id
                   and canonical(packet) == canonical(previous_by_id[str(packet["packet_id"])])]
    carried_set = set(carried_ids)
    delta_ids = [pid for pid in current_ids if pid not in carried_set]
    if not delta_ids:
        raise ValueError("v6 delta review requires changed or added public packets")
    fingerprint = human_ai_lab._corpus_fingerprint(packets)
    refreshed, reviewers, dates = _review_source_decisions(fingerprint, delta_ids, sources)
    by_id = {}
    for pid in current_ids:
        by_id[pid] = (dict(parent["decisions_by_packet_id"][pid]) if pid in carried_set
                      else {**refreshed[pid], "provenance": "delta_independent_model"})
    return {
        "snapshot_version": 6,
        "corpus_fingerprint": fingerprint,
        "reviewer": "; ".join([parent["reviewer"], *reviewers]),
        "review_date": max([parent["review_date"], *dates]),
        "method": (
            "Verified independent delta review: exact unchanged public packets "
            "retain their v5 parent's original decisions and source provenance; "
            "every changed or added packet has a fresh raw independent decision. "
            "Parent snapshot, public bundle and raw delta sources are preserved "
            "and revalidated. No answer is corrected by the importer."
        ),
        "decision_count": len(current_ids),
        "parent_review": {
            "snapshot_version": 5, "corpus_fingerprint": parent["corpus_fingerprint"],
            "carried_forward_unchanged": len(carried_ids),
            "carried_packet_ids": carried_ids,
            "removed_packet_ids": [str(packet["packet_id"]) for packet in previous
                                   if str(packet["packet_id"]) not in set(current_ids)],
            "evidence": parent_evidence,
        },
        "delta_review": {
            "reviewed_packet_ids": delta_ids,
            "changed_existing": sum(pid in previous_by_id for pid in delta_ids),
            "newly_added": sum(pid not in previous_by_id for pid in delta_ids),
            "expected_answers_visible": False, "public_only_review_attested": True,
            "source_count": len(sources), "sources": sources,
        },
        "decisions_by_packet_id": by_id,
    }


def import_delta_review(
    packets: list[dict[str, Any]], source_paths: list[Path],
    parent_snapshot: Path, parent_public_bundle: Path,
) -> dict[str, Any]:
    """Preserve parent bytes and import only the exact changed public subset."""
    snapshot_source, bundle_source = _read_sources([parent_snapshot, parent_public_bundle])
    return _verified_delta_snapshot(packets, {
        "snapshot": snapshot_source, "public_bundle": bundle_source,
    }, _read_sources(source_paths))


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
    if version > 6:
        raise ValueError(f"unsupported real-AI snapshot version: {version}")
    decisions: list[dict[str, Any]] = [{
        "_meta": {
            "corpus_fingerprint": fingerprint,
            "reviewer": snapshot.get("reviewer"),
            "review_date": snapshot.get("review_date"),
            "method": snapshot.get("method"),
            "snapshot_version": version,
        }
    }]

    if version == 6:
        parent_review = snapshot.get("parent_review") or {}
        delta_review = snapshot.get("delta_review") or {}
        reconstructed = _verified_delta_snapshot(
            packets, parent_review.get("evidence") or {}, delta_review.get("sources") or [],
        )
        if snapshot != reconstructed:
            raise ValueError("v6 snapshot differs from verified parent and raw delta evidence")
        for packet in packets:
            row = reconstructed["decisions_by_packet_id"][str(packet["packet_id"])]
            decisions.append({key: row[key] for key in (
                "packet_id", "decision", "tools", "reply_language",
            )})
        return decisions

    if version == 5:
        full_review = snapshot.get("full_review") or {}
        reconstructed = _full_review_snapshot(packets, full_review.get("sources") or [])
        for key in ("reviewer", "review_date", "decision_count", "full_review", "decisions_by_packet_id"):
            if snapshot.get(key) != reconstructed[key]:
                raise ValueError(f"v5 snapshot differs from raw independent review evidence: {key}")
        for packet in packets:
            pid = str(packet["packet_id"])
            row = reconstructed["decisions_by_packet_id"][pid]
            decisions.append({key: row[key] for key in (
                "packet_id", "decision", "tools", "reply_language",
            )})
        return decisions

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
        carried_label = "parent_snapshot_unchanged" if version >= 4 else "parent_independent_model"
        delta_label = "delta_independent_model" if version >= 4 else "delta_engineering_review"
        carried = sum(
            1 for row in by_id.values()
            if isinstance(row, dict)
            and row.get("provenance") == carried_label
        )
        refreshed = sum(
            1 for row in by_id.values()
            if isinstance(row, dict)
            and row.get("provenance") == delta_label
        )
        if carried != int(parent.get("carried_forward_unchanged") or -1):
            raise ValueError("v3 carried-forward provenance count is inconsistent")
        if refreshed != len(reviewed_delta):
            raise ValueError("v3 delta-review provenance count is inconsistent")
        if not reviewed_delta <= expected_ids:
            raise ValueError("v3 delta-review contains unknown packet ids")
        if version >= 4:
            if carried + refreshed != len(expected_ids):
                raise ValueError("v4 every decision needs explicit review provenance")
            for pid, row in by_id.items():
                if ((pid in reviewed_delta) != (row.get("provenance") == delta_label)):
                    raise ValueError("v4 delta provenance does not match reviewed packet ids")
                if row.get("provenance") == carried_label and not row.get("parent_provenance"):
                    raise ValueError("v4 carried decision is missing its original provenance")

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


async def run(
    snapshot_path: Path, review_sources: list[Path] | None = None,
    parent_snapshot: Path | None = None, parent_public_bundle: Path | None = None,
) -> dict[str, Any]:
    if bool(parent_snapshot) != bool(parent_public_bundle) or (parent_snapshot and not review_sources):
        raise ValueError("both parent paths and raw review sources are required for a delta import")
    packets = await human_ai_lab.build_packets("all")
    if parent_snapshot:
        snapshot = import_delta_review(packets, review_sources, parent_snapshot, parent_public_bundle)
    else:
        snapshot = import_full_review(packets, review_sources) if review_sources else _load_snapshot(snapshot_path)
    decisions = _decisions_from_snapshot(packets, snapshot)
    if review_sources:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        # A failed score is still evidence. Preserve it, without overwriting any
        # earlier snapshot or a reviewer's source file.
        with snapshot_path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
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
    if int(snapshot.get("snapshot_version") or 0) >= 4:
        report["external_model_review"] = {
            "reviewer": snapshot.get("reviewer"),
            "review_date": snapshot.get("review_date"),
            "method": snapshot.get("method"),
            "snapshot_version": snapshot.get("snapshot_version"),
            "static_snapshot_not_live_api": True,
            "fresh_full_independent_pass": False,
            "parent_snapshot_unchanged_packets": parent.get("carried_forward_unchanged"),
            "fresh_independent_delta_packets": len(delta.get("reviewed_packet_ids") or []),
            "note": (
                "Only changed public packets received a fresh blind model review. "
                "Unchanged decisions retain the parent snapshot's mixed provenance, "
                "stored per decision. This scores capability selection, not exact "
                "arguments, durable effects, unseen language or live provider latency."
            ),
        }
    if int(snapshot.get("snapshot_version") or 0) == 5:
        full_review = snapshot["full_review"]
        report["external_model_review"] = {
            "reviewer": snapshot.get("reviewer"),
            "review_date": snapshot.get("review_date"),
            "method": snapshot.get("method"),
            "snapshot_version": 5,
            "static_snapshot_not_live_api": True,
            "fresh_full_independent_pass": True,
            "fresh_independent_review_packets": len(full_review["reviewed_packet_ids"]),
            "parent_snapshot_unchanged_packets": 0,
            "public_only_review_attested": True,
            "raw_review_sources": [{key: source[key] for key in (
                "source_id", "source_name", "sha256",
            )} for source in full_review["sources"]],
            "note": (
                "Every public packet has a fresh decision bound to preserved raw "
                "reviewer output. Public-only review is attested by each reviewer; "
                "hashes verify integrity, not independence. This scores capability "
                "selection, not exact arguments, durable effects, unseen language "
                "or live provider latency."
            ),
        }
    if int(snapshot.get("snapshot_version") or 0) == 6:
        report["external_model_review"] = {
            "reviewer": snapshot["reviewer"], "review_date": snapshot["review_date"],
            "method": snapshot["method"], "snapshot_version": 6,
            "static_snapshot_not_live_api": True, "fresh_full_independent_pass": False,
            "parent_snapshot_unchanged_packets": parent["carried_forward_unchanged"],
            "fresh_independent_delta_packets": len(delta["reviewed_packet_ids"]),
            "removed_parent_packets": len(parent["removed_packet_ids"]),
            "public_only_review_attested": True,
            "parent_raw_evidence": {name: {key: source[key] for key in (
                "source_id", "source_name", "sha256",
            )} for name, source in parent["evidence"].items()},
            "raw_delta_sources": [{key: source[key] for key in (
                "source_id", "source_name", "sha256",
            )} for source in delta["sources"]],
            "note": (
                "Carried decisions match a verified v5 parent's exact unchanged "
                "public packets and original raw-source decisions. Every changed "
                "or added packet has a fresh independent decision. Raw source "
                "hashes verify integrity, not reviewer independence. This scores "
                "capability selection, not execution or live provider behavior."
            ),
        }
    return report


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Score frozen real-AI reasoning snapshot")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--report", default=None)
    parser.add_argument("--import-review", action="append", default=[], metavar="JSON",
                        help="Raw alex-independent-review-v1 source; repeat for disjoint shards")
    parser.add_argument("--output-snapshot", default=None,
                        help="New v5 full or v6 delta snapshot path; must not already exist")
    parser.add_argument("--parent-snapshot", default=None,
                        help="Verified v5 parent snapshot for a v6 delta import")
    parser.add_argument("--parent-public-bundle", default=None,
                        help="Exact full public bundle reviewed by the v5 parent")
    args = parser.parse_args()
    if bool(args.import_review) != bool(args.output_snapshot):
        parser.error("--import-review and --output-snapshot must be supplied together")
    if bool(args.parent_snapshot) != bool(args.parent_public_bundle) or (
        args.parent_snapshot and not args.import_review
    ):
        parser.error("both parent options require --import-review and --output-snapshot")
    report = asyncio.run(run(
        Path(args.output_snapshot or args.snapshot),
        [Path(path) for path in args.import_review] or None,
        Path(args.parent_snapshot) if args.parent_snapshot else None,
        Path(args.parent_public_bundle) if args.parent_public_bundle else None,
    ))
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
