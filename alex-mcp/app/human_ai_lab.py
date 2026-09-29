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
from behavior_capabilities import capabilities_for_tools
from behavior_contracts import (
    PromptContract,
    ConversationContract,
    contracts_for_phase,
    conversations_for_phase,
)


PACKET_VERSION = 1
DECISION_KINDS = {"tools", "clarify", "refuse", "answer"}


def _source_media_context(source: str) -> list[str] | None:
    # Voice is already transcribed at the reasoning boundary. Image/PDF packets
    # preserve media presence without embedding any private/binary content.
    if source in {"image", "pdf", "document", "mixed"}:
        return ["[synthetic certification document/media context]"]
    return None


async def _tool_snapshot(
    prompt: str, source: str, prior_user_text: str | None = None
) -> list[dict[str, Any]]:
    specs = await brain._tool_specs(
        prompt, _source_media_context(source),
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
            "Act as Alex's reasoning layer only. Choose the supplied MCP tool(s) "
            "needed for this turn, or choose clarify/refuse/answer. Do not invent "
            "household facts. Never assume a capability that is not supplied."
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
                packet_id = _opaque_packet_id(f"{contract.id}|{source}|v{index + 1}")
                packet = _packet_base(
                    packet_id, contract.id, contract.phase, contract.domain,
                    source, prompt, contract.conversation_type, contract.actor,
                )
                packet["available_tools"] = await _tool_snapshot(prompt, source)
                packet["conversation_history"] = []
                packets.append(packet)

    for contract in conversations_for_phase(phase):
        for source in contract.sources:
            history: list[dict[str, str]] = []
            for index, step in enumerate(contract.steps):
                packet_id = _opaque_packet_id(f"{contract.id}|{source}|s{index + 1}")
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
                    step.prompt, source, prior_user_text
                )
                packet["conversation_history"] = list(history)
                packet["conversation_step"] = index + 1
                packets.append(packet)
                history.append({"role": "user", "content": step.prompt})
                history.append({
                    "role": "assistant",
                    "content": (
                        "[previous synthetic Alex result; use only user-turn continuity]"
                    ),
                })
    return packets


def _decision_index(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in decisions:
        pid = str(row.get("packet_id") or "")
        if pid:
            out[pid] = row
    return out


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
    return {
        "mode": "human_ai",
        "status": "PASS" if not failed else "FAIL",
        "summary": {
            "packets": len(packets),
            "decisions_received": len(decision_by_id),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "domains": sorted({row["domain"] for row in results}),
            "sources": sorted({row["source"] for row in results}),
        },
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


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(_public_packet(row), ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


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
    args = parser.parse_args()

    packets = asyncio.run(build_packets(args.phase))
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
