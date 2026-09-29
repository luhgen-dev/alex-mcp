#!/usr/bin/env python3
"""External ChatGPT-oracle certification for Alex v0.5.

These cases are an intentionally separate, model-authored held-out suite.  The
oracle decisions were authored by GPT-5.6 Sol in the interactive engineering
session, not derived from Alex's regex router.  CI replays the decisions
without any provider/API call and verifies that the production provider facade
can actually expose and translate the intended capability.

This complements (not replaces) behavior_cert.py, stress_test.py and the live
MCP/WhatsApp gates.  It is deliberately deterministic/replayable so a later
code change cannot silently reinterpret what the external reasoner expected.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import brain
import facade


HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "chatgpt_oracle_cases.json"

REQUIRED_DOMAINS = {
    "finance", "receipts", "memory", "reminders", "shopping",
    "tasks", "diary", "plans", "planning", "bills", "work",
    "assets", "monitoring", "home", "reports", "diagnostics", "utility",
}


def _names(specs: list[dict]) -> set[str]:
    return {
        str(spec.get("function", {}).get("name") or "")
        for spec in specs
        if isinstance(spec, dict)
    } - {""}


def _schema_compatibility(tool_name: str, args: dict) -> list[str]:
    """Verify translated arguments against the real MCP schema."""
    specs = asyncio.run(brain._tool_specs_for_names({tool_name}))
    if len(specs) != 1:
        return [f"underlying MCP tool {tool_name!r} is missing"]
    schema = specs[0].get("function", {}).get("parameters", {}) or {}
    properties = set((schema.get("properties") or {}).keys())
    required = set(schema.get("required") or ())
    supplied = set(args)
    errors: list[str] = []
    extra = sorted(supplied - properties)
    missing = sorted(required - supplied)
    if extra:
        errors.append(f"{tool_name!r} translation has unknown arguments: {extra}")
    if missing:
        errors.append(f"{tool_name!r} translation is missing required arguments: {missing}")
    return errors


def _surface_integrity() -> list[str]:
    """Ensure every facade mapping/pack still points at a real MCP tool."""
    referenced = set(facade.UNDERLYING_TO_FACADE)
    for tools in facade.PACK_TOOLS.values():
        referenced |= set(tools)
    specs = asyncio.run(brain._tool_specs_for_names(referenced))
    existing = _names(specs)
    missing = sorted(referenced - existing)
    return [f"facade references missing MCP tools: {missing}"] if missing else []


def _check_case(case: dict) -> list[str]:
    errors: list[str] = []
    prompt = str(case.get("prompt") or "")
    decision = dict(case.get("decision") or {})
    tool = str(decision.get("tool") or "")
    args = dict(decision.get("arguments") or {})
    expected = str(case.get("expected_underlying") or "")

    specs = asyncio.run(brain._provider_tool_specs(prompt))
    exposed = _names(specs)
    if len(exposed) > brain.TOOL_EXPOSURE_MAX:
        errors.append(f"surface has {len(exposed)} tools > {brain.TOOL_EXPOSURE_MAX}")
    if tool not in exposed:
        errors.append(f"oracle tool {tool!r} not exposed; got {sorted(exposed)}")

    forbidden = set(case.get("forbidden_facades") or ())
    leaked = sorted(exposed & forbidden)
    if leaked:
        errors.append(f"forbidden facade(s) exposed: {leaked}")

    if tool == "load_pack":
        pack = str(args.get("pack") or "")
        try:
            available = facade.pack_tools(pack)
        except Exception as exc:
            errors.append(f"invalid pack {pack!r}: {exc}")
            return errors
        if expected and expected not in available:
            errors.append(
                f"pack {pack!r} cannot reach {expected!r}; has {sorted(available)}"
            )
        ranked = brain._cap_tool_names(available, prompt, None)
        if expected and expected not in ranked:
            errors.append(
                f"pack {pack!r} would evict required {expected!r} under six-tool cap; "
                f"ranked={sorted(ranked)}"
            )
    else:
        translated = facade.simple_translation(tool, args)
        if translated is None:
            # shopping_change can deliberately perform a deterministic
            # name->stable-id read before the mutation.
            if not (
                tool == "shopping_change"
                and str(args.get("operation") or "").casefold() == "update"
                and args.get("item")
                and expected == "update_shopping_item"
            ):
                errors.append(f"oracle decision {tool!r} is not translatable")
        elif expected and translated[0] != expected:
            errors.append(
                f"oracle expected {expected!r} but facade translates to {translated[0]!r}"
            )
        if translated is not None:
            errors.extend(_schema_compatibility(translated[0], translated[1]))

    # Name-resolved shopping updates are intentionally two-step; verify the
    # eventual mutator exists even though its stable item_id comes from step 1.
    if (
        tool == "shopping_change"
        and str(args.get("operation") or "").casefold() == "update"
        and args.get("item")
        and expected == "update_shopping_item"
    ):
        if not asyncio.run(brain._tool_specs_for_names({"update_shopping_item"})):
            errors.append("shopping name resolver cannot reach update_shopping_item")

    pair = case.get("typed_equivalent")
    if pair:
        paired = _names(asyncio.run(brain._provider_tool_specs(str(pair))))
        if exposed != paired:
            errors.append(
                f"voice/text parity drift: voice={sorted(exposed)} typed={sorted(paired)}"
            )
    return errors


def run() -> dict:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = list(payload.get("cases") or [])
    failures: list[dict] = []
    integrity = _surface_integrity()
    if integrity:
        failures.append({"id": "_surface_integrity", "errors": integrity})
    domains = {str(case.get("domain") or "") for case in cases}
    missing_domains = sorted(REQUIRED_DOMAINS - domains)
    if missing_domains:
        failures.append({
            "id": "_coverage",
            "errors": [f"missing product domains: {missing_domains}"],
        })

    voice_cases = 0
    mixed_cases = 0
    for case in cases:
        if str(case.get("source") or "text") == "voice":
            voice_cases += 1
        if str(case.get("language") or "en") != "en":
            mixed_cases += 1
        errors = _check_case(case)
        if errors:
            failures.append({"id": case.get("id"), "errors": errors})

    result = {
        "status": "PASS" if not failures else "FAIL",
        "oracle": payload.get("oracle"),
        "suite_version": payload.get("suite_version"),
        "cases": len(cases),
        "domains": len(domains & REQUIRED_DOMAINS),
        "voice_cases": voice_cases,
        "mixed_language_cases": mixed_cases,
        "surface_integrity": "PASS" if not integrity else "FAIL",
        "failures": failures,
        "notes": (
            "External model-authored reasoning decisions replayed against the "
            "real provider-facing facade; no paid provider call is made."
        ),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    args = parser.parse_args()
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.report:
        Path(args.report).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
