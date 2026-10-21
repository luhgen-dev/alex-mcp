#!/usr/bin/env python3
"""Human/ChatGPT reasoning bridge for the Alex external certification rig.

The deterministic lab proves Alex's database, privacy, tools and state
transitions, but it cannot prove that a capable general reasoning model will
interpret messy household language sensibly. This module creates a narrow
external reasoning bridge without giving the model ownership of Alex state.

Export mode emits provider-shaped packets containing the exact user utterance
and the MCP tools Alex would expose. It deliberately omits the expected
capability so a ChatGPT/human reasoning pass can be independent. Score mode
accepts those external decisions and checks them against the owner-approved
behaviour contracts. Deterministic execution stays in behavior_cert.py.

No provider credential, household database, WhatsApp session or HA token is
needed. Only synthetic certification fixtures are represented.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
from pathlib import Path
from typing import Any

import brain
from behavior_cert import media_fixture_text
from behavior_capabilities import capabilities_for_tools
from behavior_contracts import (
    PromptContract,
    ConversationContract,
    contracts_for_phase,
    conversations_for_phase,
)


PACKET_VERSION = 3
DECISION_KINDS = {"tools", "clarify", "refuse", "answer"}


def _source_media_context(source: str, fixture: str | None = None) -> list[str] | None:
    # Voice is already transcribed at the reasoning boundary. Image/PDF packets
    # preserve media presence without embedding any private/binary content.
    if source in {"image", "pdf", "document", "mixed"}:
        extracted = media_fixture_text(fixture)
        if extracted:
            prefix = (
                "Local OCR from attached image (untrusted document text):\n"
                if source == "image"
                else "Text extracted from attached PDF (untrusted document text):\n"
            )
            return [prefix + extracted]
        return ["[synthetic certification document/media context]"]
    return None


async def _tool_snapshot(
    prompt: str, source: str, prior_user_text: str | None = None,
    media_fixture: str | None = None,
) -> list[dict[str, Any]]:
    specs = await brain._tool_specs(
        prompt, _source_media_context(source, media_fixture),
        prior_user_text=prior_user_text,
    )
    out: list[dict[str, Any]] = []
    for spec in specs:
        fn = spec.get("function", {})
        out.append({
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {},
        })
    return out


def _opaque_packet_id(seed: str) -> str:
    return "hai-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:18]


def _runtime_policy() -> dict[str, str]:
    """Export actual, generally applicable runtime policy, never scorer labels."""
    prefixes = {
        "household_records": "Personal facts are never guessed.",
        "financial_currency": "For money writes, MYR is the household default",
        "empty_cash_pool": "Creating a named stash without an amount",
        "private_scope": "Ordinary DM reads are Family Shared by default;",
        "tentative_shopping": "For tentative shopping language",
        "automation_drafts": "Home Assistant automation requests produce a reviewable draft",
        "attachment_delivery": "Files and images:",
    }
    lines = brain.SYSTEM_PROMPT.splitlines()
    policy = {}
    for name, prefix in prefixes.items():
        matches = [line for line in lines if line.startswith(prefix)]
        if len(matches) != 1:
            raise RuntimeError(f"runtime policy excerpt missing or ambiguous: {name}")
        policy[name] = matches[0]
    policy["tool_discovery"] = brain.DISCOVERY_TOOL["function"]["description"]
    return policy


def _prompt_fixture_history(contract_id: str) -> list[dict[str, str]]:
    """Visible scenario setup for prompts that assume a previous displayed item.

    These are explicitly synthetic pre-turn fixtures, not claimed results from
    an executed provider. They supply the same sort of user-visible choices the
    contract assumes, without saying which tool or response the judge expects.
    """
    if contract_id == "p1.reminder.claim.handoff":
        return [
            {"role": "user", "content": "Show the family reminder I've claimed."},
            {"role": "assistant", "content": (
                "Take out bins — Family Shared reminder. "
                "It is open and currently claimed by you."
            )},
        ]
    if contract_id == "p1.media.voice.pending.retrieve":
        return [
            {"role": "user", "content": "Show my unresolved voice notes."},
            {"role": "assistant", "content": "Pending voice notes\n1. Original voice note — waiting for typed clarification."},
        ]
    if contract_id == "p2.diary.conflict.resolve":
        return [
            {"role": "user", "content": "Add a dentist appointment during my work shift."},
            {"role": "assistant", "content": (
                "The dentist appointment overlaps your work shift.\n"
                "1. Add the appointment and create PLANNED leave.\n"
                "2. Add the appointment and keep the clash.\n"
                "3. Cancel this request."
            )},
        ]
    return []


def _conversation_result_fixture(contract_id: str, step_index: int) -> str:
    if contract_id == "conv.memory.numbered" and step_index == 0:
        return "Saved pictures\n1. Vinyl — original saved picture."
    if contract_id == "conv.diary.reminder" and step_index == 0:
        return "1 October 2026 agenda\n1. Dentist appointment — 4:00 PM."
    return "[previous synthetic Alex result; use only user-turn continuity]"


def _packet_base(packet_id: str, contract_id: str, phase: str, domain: str,
                 source: str, prompt: str, conversation_type: str,
                 actor: str) -> dict[str, Any]:
    # Contract metadata is retained only in memory for scoring and is stripped
    # from the exported reasoning packet. The external model therefore cannot
    # cheat by reading an id such as "shopping.update".
    return {
        "packet_version": PACKET_VERSION,
        "packet_id": packet_id,
        "_contract_id": contract_id,
        "_phase": phase,
        "_domain": domain,
        "source": source,
        "conversation_type": conversation_type,
        "actor": actor,
        "prompt": prompt,
        "reasoning_instruction": (
            "Act as Alex's reasoning layer only. Select the supplied MCP capabilities "
            "needed to complete the whole turn, or choose clarify/refuse/answer. "
            "A whole-turn capability plan may include a lookup followed by the "
            "requested action, conditional on a unique authorized lookup result. "
            "Selecting that later capability does not claim its arguments are already "
            "known or that it has executed. Never invent household facts, record ids, "
            "amounts, currencies or timing. If a required user decision is missing, "
            "clarify. Never assume a capability that is not supplied."
        ),
        "runtime_policy": _runtime_policy(),
        "evaluation_scope": (
            "Capability selection for synthetic interpreted input. Source labels "
            "describe fixture provenance; they do not certify raw audio authorization, "
            "transcription, actual attachment rendering or live delivery. Any prior "
            "assistant fixture text is scenario setup, not observed provider execution."
        ),
        "reply_language_policy": (
            "English unless the user explicitly requests another language."
        ),
    }


async def build_packets(phase: str = "all") -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []

    for contract in contracts_for_phase(phase):
        for source in contract.sources:
            for index, prompt in enumerate(contract.variants):
                packet_id = _opaque_packet_id(
                    f"{contract.id}|{source}|v{index + 1}|{prompt}"
                )
                packet = _packet_base(
                    packet_id, contract.id, contract.phase, contract.domain,
                    source, prompt, contract.conversation_type, contract.actor,
                )
                packet["available_tools"] = await _tool_snapshot(
                    prompt, source, media_fixture=contract.media_fixture
                )
                packet["conversation_history"] = _prompt_fixture_history(contract.id)
                media_context = _source_media_context(source, contract.media_fixture)
                if media_context:
                    packet["media_context"] = media_context
                packets.append(packet)

    for contract in conversations_for_phase(phase):
        for source in contract.sources:
            history: list[dict[str, str]] = []
            for index, step in enumerate(contract.steps):
                packet_id = _opaque_packet_id(
                    f"{contract.id}|{source}|s{index + 1}|{step.prompt}"
                )
                packet = _packet_base(
                    packet_id, contract.id, contract.phase, contract.domain,
                    source, step.prompt, step.conversation_type, step.actor,
                )
                prior_user_text = next(
                    (
                        item["content"]
                        for item in reversed(history)
                        if item.get("role") == "user"
                    ),
                    None,
                )
                packet["available_tools"] = await _tool_snapshot(
                    step.prompt, source, prior_user_text,
                    media_fixture=step.media_fixture,
                )
                packet["conversation_history"] = list(history)
                packet["conversation_step"] = index + 1
                media_context = _source_media_context(source, step.media_fixture)
                if media_context:
                    packet["media_context"] = media_context
                packets.append(packet)
                history.append({"role": "user", "content": step.prompt})
                history.append({
                    "role": "assistant",
                    "content": _conversation_result_fixture(contract.id, index),
                })
    return packets


def _decision_index(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in decisions:
        pid = str(row.get("packet_id") or "")
        if pid:
            out[pid] = row
    return out


def _decision_metadata(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    for row in decisions:
        meta = row.get("_meta")
        if isinstance(meta, dict):
            return meta
    return {}


def _contract_maps(phase: str):
    prompts = {c.id: c for c in contracts_for_phase(phase)}
    conversations = {c.id: c for c in conversations_for_phase(phase)}
    return prompts, conversations


def _expectation_for_packet(packet: dict[str, Any], prompts, conversations):
    cid = packet["_contract_id"]
    if cid in prompts:
        return prompts[cid]
    contract: ConversationContract = conversations[cid]
    step_index = int(packet.get("conversation_step") or 1) - 1
    return contract.steps[step_index]


def score_packets(packets: list[dict[str, Any]], decisions: list[dict[str, Any]],
                  phase: str = "all") -> dict[str, Any]:
    decision_by_id = _decision_index(decisions)
    decision_meta = _decision_metadata(decisions)
    current_fingerprint = _corpus_fingerprint(packets)
    reviewed_fingerprint = str(
        decision_meta.get("corpus_fingerprint") or ""
    ).strip()
    corpus_match = reviewed_fingerprint == current_fingerprint
    prompts, conversations = _contract_maps(phase)
    results: list[dict[str, Any]] = []

    for packet in packets:
        expected = _expectation_for_packet(packet, prompts, conversations)
        decision = decision_by_id.get(packet["packet_id"])
        problems: list[str] = []
        if decision is None:
            problems.append("missing external AI decision")
            kind = "missing"
            selected_tools: set[str] = set()
        else:
            kind = str(decision.get("decision") or "").strip().lower()
            if kind not in DECISION_KINDS:
                problems.append(f"invalid decision kind: {kind or '<empty>'}")
            selected_tools = {
                str(name) for name in (decision.get("tools") or [])
                if str(name).strip()
            }

        available = {
            str(tool.get("name"))
            for tool in packet.get("available_tools", [])
            if tool.get("name")
        }
        unavailable = sorted(selected_tools - available)
        if unavailable:
            problems.append(
                "selected tools were not actually exposed: " + ", ".join(unavailable)
            )

        caps = set(capabilities_for_tools(selected_tools))
        required_any = set(expected.required_any) - {"routing.discovery"}
        required_all = set(getattr(expected, "required_all", frozenset())) - {
            "routing.discovery"
        }
        forbidden = set(getattr(expected, "forbidden", frozenset())) - {
            "routing.discovery"
        }

        expect_clarification = bool(
            getattr(expected, "expect_clarification", False)
        )
        expect_refusal = bool(getattr(expected, "expect_refusal", False))
        allow_answer = bool(getattr(expected, "allow_answer", False))

        if expect_clarification:
            if kind != "clarify":
                problems.append("expected a clarification decision")
        elif expect_refusal:
            # A blind reasoner does not receive hidden ownership/private fixture
            # facts. It may either refuse from the visible request context OR
            # select the relevant protected capability and let Alex's
            # deterministic ACL/tool layer return the refusal. Requiring a
            # pre-tool refusal would reward guessing private state.
            if kind != "refuse" and not (caps & required_any):
                problems.append(
                    "expected a refusal or an authorized attempt through the protected capability"
                )
        else:
            if not (allow_answer and kind == "answer"):
                if required_any and not (caps & required_any):
                    problems.append(
                        "required capability not selected: "
                        + " / ".join(sorted(required_any))
                    )
                missing_all = sorted(required_all - caps)
                if missing_all:
                    problems.append(
                        "required-all capability not selected: "
                        + ", ".join(missing_all)
                    )

        bad = sorted(caps & forbidden)
        if bad:
            problems.append(
                "forbidden capability selected: " + ", ".join(bad)
            )

        # A reasoning model does not pass merely because the correct tool was
        # somewhere in a shotgun blast. Extra READ tools are tolerated because
        # a model may need to resolve an id/focus first, but every MUTATING tool
        # it chooses must be justified by the private contract for this turn.
        # This closes the false-positive where selecting every exposed tool
        # could otherwise pass a read-only question and mutate household state.
        expected_caps = required_any | required_all
        unjustified_mutators: list[str] = []
        for tool_name in sorted(selected_tools):
            if not brain._is_mutating_tool(tool_name):
                continue
            tool_caps = set(capabilities_for_tools({tool_name})) - {
                "routing.discovery"
            }
            if not (tool_caps & expected_caps):
                unjustified_mutators.append(tool_name)
        if unjustified_mutators:
            problems.append(
                "unrequested mutating tools selected: "
                + ", ".join(unjustified_mutators)
            )

        if "discover_alex_tools" in selected_tools and required_any:
            problems.append(
                "discovery selected despite the required capability already being exposed"
            )

        reply_language = str(
            (decision or {}).get("reply_language") or "en"
        ).lower()
        expected_language = str(
            getattr(expected, "reply_language", "en") or "en"
        ).lower()
        if expected_language == "en" and reply_language not in {"en", "english"}:
            problems.append(f"reply language drifted to {reply_language}")

        results.append({
            "packet_id": packet["packet_id"],
            "contract_id": packet["_contract_id"],
            "domain": packet["_domain"],
            "source": packet["source"],
            "status": "PASS" if not problems else "FAIL",
            "decision": kind,
            "tools": sorted(selected_tools),
            "problems": problems,
        })

    failed = [row for row in results if row["status"] == "FAIL"]
    meta_failures = []
    if not reviewed_fingerprint:
        meta_failures.append(
            "external AI decision snapshot has no corpus_fingerprint metadata"
        )
    elif not corpus_match:
        meta_failures.append(
            "external AI decision snapshot was reviewed against a different packet corpus"
        )
    return {
        "mode": "human_ai",
        "status": "PASS" if not failed and not meta_failures else "FAIL",
        "summary": {
            "packets": len(packets),
            "corpus_fingerprint": current_fingerprint,
            "reviewed_corpus_fingerprint": reviewed_fingerprint or None,
            "corpus_match": corpus_match,
            "decisions_received": len(decision_by_id),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "domains": sorted({row["domain"] for row in results}),
            "sources": sorted({row["source"] for row in results}),
        },
        "meta_failures": meta_failures,
        "failures": failed,
        "results": results,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _public_packet(row: dict[str, Any]) -> dict[str, Any]:
    """Strip certification answers/labels before handing a packet to an AI."""
    return {
        key: value for key, value in row.items()
        if not key.startswith("_")
    }


def _corpus_fingerprint(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        public = _public_packet(row)
        digest.update(
            json.dumps(
                public, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(_public_packet(row), ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def public_review_bundle(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Losslessly deduplicate public policy/schemas for an isolated reviewer.

    The full packet fingerprint remains authoritative. No private contracts,
    expected decisions, previous decisions or scorer output enter this bundle.
    """
    public = [_public_packet(row) for row in rows]
    shared = {}
    for key in (
        "packet_version", "reasoning_instruction", "runtime_policy",
        "evaluation_scope", "reply_language_policy",
    ):
        if public and key in public[0] and all(row.get(key) == public[0][key] for row in public):
            shared[key] = public[0][key]
    catalogue = {}
    packets = []
    for row in public:
        refs = []
        for tool in row.get("available_tools", []):
            canonical = json.dumps(tool, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ref = "tool-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            if ref in catalogue and catalogue[ref] != tool:
                raise ValueError("tool schema hash collision")
            catalogue[ref] = tool
            refs.append(ref)
        packets.append({
            **{key: value for key, value in row.items()
               if key not in shared and key != "available_tools"},
            "tool_refs": refs,
        })
    return {
        "format": "alex-public-reasoning-bundle-v1",
        "corpus_fingerprint": _corpus_fingerprint(rows),
        "packet_count": len(public),
        "review_boundary": (
            "Shared fields apply to every packet. Each tool_refs entry expands "
            "to the exact tool schema in tools_by_ref, preserving order. Review "
            "only supplied public evidence; no expected answers are included."
        ),
        "shared_packet_fields": shared,
        "tools_by_ref": catalogue,
        "packets": packets,
    }


def expand_review_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify that compact public review input reconstructs the original corpus."""
    if bundle.get("format") != "alex-public-reasoning-bundle-v1":
        raise ValueError("unsupported public review bundle")
    rows = [{
        **bundle["shared_packet_fields"],
        **{key: value for key, value in row.items() if key != "tool_refs"},
        "available_tools": [bundle["tools_by_ref"][ref] for ref in row["tool_refs"]],
    } for row in bundle["packets"]]
    if len(rows) != bundle.get("packet_count"):
        raise ValueError("public review packet count mismatch")
    if _corpus_fingerprint(rows) != bundle.get("corpus_fingerprint"):
        raise ValueError("public review corpus fingerprint mismatch")
    return rows


def packet_audit(packets: list[dict[str, Any]], phase: str = "all") -> dict[str, Any]:
    ids = [row["packet_id"] for row in packets]
    domains = {row["_domain"] for row in packets}
    sources = {row["source"] for row in packets}
    duplicate_ids = sorted({pid for pid in ids if ids.count(pid) > 1})
    missing_tools = [
        row["packet_id"] for row in packets
        if not row.get("available_tools") and row["_domain"] not in {"language"}
    ]

    # Packet generation is itself part of certification. Before handing a blind
    # packet to ChatGPT/human QC, prove that the packet actually contains the
    # owner-required capability and excludes forbidden ones. Otherwise a clean
    # external reasoner could be asked to solve an impossible turn.
    prompts, conversations = _contract_maps(phase)
    routing_gaps: list[dict[str, Any]] = []
    forbidden_exposure: list[dict[str, Any]] = []
    for row in packets:
        expected = _expectation_for_packet(row, prompts, conversations)
        names = {
            str(tool.get("name"))
            for tool in row.get("available_tools", [])
            if tool.get("name")
        }
        caps = set(capabilities_for_tools(names))
        required_any = set(expected.required_any) - {"routing.discovery"}
        required_all = set(getattr(expected, "required_all", frozenset())) - {
            "routing.discovery"
        }
        forbidden = set(getattr(expected, "forbidden", frozenset())) - {
            "routing.discovery"
        }

        missing_any = bool(required_any) and not bool(caps & required_any)
        missing_all = sorted(required_all - caps)
        if missing_any or missing_all:
            routing_gaps.append({
                "packet_id": row["packet_id"],
                "contract_id": row["_contract_id"],
                "source": row["source"],
                "conversation_step": row.get("conversation_step"),
                "missing_any_of": sorted(required_any) if missing_any else [],
                "missing_required_all": missing_all,
                "available_capabilities": sorted(caps),
            })

        bad = sorted(caps & forbidden)
        if bad:
            forbidden_exposure.append({
                "packet_id": row["packet_id"],
                "contract_id": row["_contract_id"],
                "source": row["source"],
                "conversation_step": row.get("conversation_step"),
                "forbidden_capabilities": bad,
            })

    status = (
        "PASS"
        if packets and not duplicate_ids and not missing_tools
        and not routing_gaps and not forbidden_exposure
        else "FAIL"
    )
    return {
        "mode": "human_ai_packet_audit",
        "status": status,
        "summary": {
            "packets": len(packets),
            "corpus_fingerprint": _corpus_fingerprint(packets),
            "domains": sorted(domains),
            "sources": sorted(sources),
            "duplicate_packet_ids": duplicate_ids,
            "packets_without_tools": missing_tools,
            "routing_gaps": len(routing_gaps),
            "forbidden_exposure": len(forbidden_exposure),
        },
        "routing_gaps": routing_gaps,
        "forbidden_exposure": forbidden_exposure,
    }


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Alex external human/ChatGPT reasoning bridge"
    )
    parser.add_argument(
        "--mode", choices=("export", "score", "selftest"), default="selftest"
    )
    parser.add_argument(
        "--phase", choices=("all", "phase1", "phase2", "phase3"), default="all"
    )
    parser.add_argument("--packets", default="alex-human-ai-packets.jsonl")
    parser.add_argument("--decisions", default="alex-human-ai-decisions.jsonl")
    parser.add_argument("--report", default="alex-human-ai-report.json")
    parser.add_argument("--review-bundle", default=None,
                        help="optional lossless public-only policy/schema deduplication for blind review")
    args = parser.parse_args()

    packets = asyncio.run(build_packets(args.phase))
    if args.review_bundle:
        bundle = public_review_bundle(packets)
        expand_review_bundle(bundle)
        target = Path(args.review_bundle)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    packet_path = Path(args.packets)
    if args.mode in {"export", "selftest"}:
        _write_jsonl(packet_path, packets)

    if args.mode == "score":
        if not packet_path.exists():
            _write_jsonl(packet_path, packets)
        decisions = _read_jsonl(Path(args.decisions))
        report = score_packets(packets, decisions, args.phase)
    else:
        report = packet_audit(packets, args.phase)

    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("status") != "PASS":
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
