#!/usr/bin/env python3
"""Capture real ingress requests, then replay independent assistant decisions.

This worker uses the ten existing FOLLOWUP_VARIATIONS regression phrases. It
does not evaluate unseen holdouts or call Alex's configured model. The initial
missing-time question is scripted; the narrow interpreter returns UNCLEAR and
the live router is disabled. Everything else in ingress/MCP/backend runs against
the existing disposable test database. No outbox delivery worker is started.

Capture:
  python tools/reminder_handoff_review.py capture --output-dir /tmp/runtime-review

Give ONLY public_packets.json to an independent reviewer. Each shuffled entry
contains exactly opaque_id, messages and tools. The reviewer returns:
  {"responses": [{"opaque_id": "...", "assistant": {
    "role": "assistant", "content": "", "tool_calls": [{
      "id": "call_1", "type": "function", "function": {
        "name": "create_reminder", "arguments": "{raw JSON argument string}"
      }}]}}]}
For clarification, use the assistant's actual question and an empty tool_calls.
Arguments are passed through unchanged, including malformed or rejected values.

Replay:
  python tools/reminder_handoff_review.py replay --capture-dir /tmp/runtime-review \
      --responses /tmp/reviewer-responses.json --output /tmp/runtime-observed.json

Replay restores each pre-followup SQLite backup, exact inbound payload and
Python clock. It compares the serialized first messages+tools byte for byte
before returning any proposed decision to the brain. A tool decision receives
one fixed neutral terminal response; no additional independent reasoning is
performed. Actual receipts, writes and pending/outbound bindings are exported.

SQLite's own expiry clock is not mocked. Replay promptly: if passage of wall
time changes ingress context, the exact-request check rejects that replay.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "tests"))

from test_conversation_reliability import FOLLOWUP_VARIATIONS  # noqa: E402
from test_reminder_handoff_integration import (  # noqa: E402
    ReminderHandoffIntegrationTests, brain, core, db,
)


NEUTRAL_RESPONSE = "I have reviewed the result."
UNCLEAR = {"intent": "UNCLEAR", "confidence": 0.0,
           "normalized_reply": "", "status": "ok"}
OBSERVED_TABLES = (
    "reminders", "pending_items", "reminder_events", "outbound_messages",
    "tool_audit", "tool_execution_claims", "inbound_messages",
)


def json_bytes(value):
    """Preserve dict/list ordering while giving capture and replay exact bytes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def runtime_fingerprint():
    paths = sorted((REPOSITORY / "alex-mcp" / "app").glob("*.py"))
    paths += [REPOSITORY / "alex-mcp" / "app" / "schema.sql"]
    return {str(path.relative_to(REPOSITORY)): sha256(path.read_bytes()) for path in paths}


def model_visible(request):
    return {"messages": request["messages"], "tools": request.get("tools", [])}


def snapshot_database(path):
    destination = sqlite3.connect(str(path))
    source = db.connect()
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()


def restore_database(path):
    source = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    destination = db.connect()
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()


def tables(fixture):
    return {name: fixture._rows(name) for name in OBSERVED_TABLES}


def validate_assistant(value):
    if not isinstance(value, dict) or value.get("role") != "assistant":
        raise ValueError("Each response needs an assistant object with role=assistant")
    if value.get("content") is not None and not isinstance(value["content"], str):
        raise ValueError("Assistant content must be a string or null")
    calls = value.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError("tool_calls must be a list")
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError("Each tool call must have type=function")
        if not isinstance(call.get("id"), str) or not call["id"] or call["id"] in seen:
            raise ValueError("Each tool call needs its own nonempty string id")
        seen.add(call["id"])
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("A tool call needs function.name")
        if not isinstance(function.get("arguments"), str):
            raise ValueError("function.arguments must be the reviewer's raw JSON string")
    return copy.deepcopy(value)


class ProviderMessage:
    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.content = data.get("content")
        self.tool_calls = [
            SimpleNamespace(id=call["id"], function=SimpleNamespace(
                name=call["function"]["name"], arguments=call["function"]["arguments"]))
            for call in data.get("tool_calls") or []
        ]

    def model_dump(self, exclude_none=True):
        return {key: copy.deepcopy(value) for key, value in self.data.items()
                if not exclude_none or value is not None}


def run_followup(payload, first_assistant, *, expected_request_bytes=None):
    """Only provider choices are substituted; the real runtime handles them."""
    first_assistant = validate_assistant(first_assistant)
    requests = []
    errors = []
    first_matches = None
    allowed_requests = 2 if first_assistant.get("tool_calls") else 1

    class Completions:
        def create(self, **kwargs):
            nonlocal first_matches
            requests.append(copy.deepcopy(kwargs))
            number = len(requests)
            if number == 1 and expected_request_bytes is not None:
                first_matches = json_bytes(model_visible(kwargs)) == expected_request_bytes
                if not first_matches:
                    errors.append("First main-brain messages/tools differ from the captured request")
                    raise AssertionError(errors[-1])
            if errors:
                raise AssertionError(errors[0])
            if number > allowed_requests:
                errors.append("Runtime requested an additional, unsupplied provider decision")
                raise AssertionError(errors[-1])
            data = first_assistant if number == 1 else {
                "role": "assistant", "content": NEUTRAL_RESPONSE, "tool_calls": [],
            }
            return SimpleNamespace(choices=[SimpleNamespace(message=ProviderMessage(data))], usage=None)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    route = {"provider": "grok", "model": "offline-fixture",
             "reasoning_effort": "low", "role": "manual"}
    with patch.object(brain, "_provider_routes", return_value=[route]), \
         patch.object(brain, "_client_for", return_value=fake), \
         patch.object(brain.shadow_router, "live_enabled", return_value=False), \
         patch.object(brain, "interpret_control_intent", return_value=UNCLEAR) as interpreter:
        result = core.ingress.process(copy.deepcopy(payload))
    if not requests:
        errors.append("Ingress did not reach the main brain")
    return {
        "ingress_result": result,
        "requests": requests,
        "main_brain_request_count": len(requests),
        "interpreter_call_count": interpreter.call_count,
        "first_request_matches_capture": first_matches,
        "worker_errors": errors,
    }


def fixture_case(name):
    fixture = ReminderHandoffIntegrationTests(methodName="runTest")
    fixture.setUp()
    fixture._testMethodName = name
    return fixture


def close_fixture(fixture):
    try:
        fixture.tearDown()
    finally:
        fixture.doCleanups()


def capture(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    if (output_dir / "private_manifest.json").exists():
        raise ValueError("Capture directory already contains a manifest; use a new directory")
    (output_dir / "snapshots").mkdir(exist_ok=True)
    (output_dir / "requests").mkdir(exist_ok=True)
    ReminderHandoffIntegrationTests.setUpClass()
    public, cases = [], []
    fingerprint = runtime_fingerprint()
    for index, phrase in enumerate(FOLLOWUP_VARIATIONS, 1):
        opaque_id = secrets.token_hex(12)
        fixture_name = "review-" + secrets.token_hex(10)
        fixture = fixture_case(fixture_name)
        try:
            draft, _ = fixture._seed_conversation()
            payload = fixture._payload(fixture_name + "-followup", phrase)
            snapshot_relative = f"snapshots/{opaque_id}.sqlite3"
            snapshot_path = output_dir / snapshot_relative
            snapshot_database(snapshot_path)
            before = tables(fixture)
            result = run_followup(payload, {
                "role": "assistant", "content": NEUTRAL_RESPONSE, "tool_calls": [],
            })
            if result["worker_errors"] or result["main_brain_request_count"] != 1:
                raise AssertionError(f"Capture failed for private case {index}: {result['worker_errors']}")
            if fixture._rows("reminders"):
                raise AssertionError("Neutral capture unexpectedly created a reminder")
            request = result["requests"][0]
            visible = model_visible(request)
            visible_bytes = json_bytes(visible)
            request_relative = f"requests/{opaque_id}.json"
            exact_relative = f"requests/{opaque_id}.messages-tools.json"
            write_json(output_dir / request_relative, request)
            (output_dir / exact_relative).write_bytes(visible_bytes)
            public.append({"opaque_id": opaque_id, **visible})
            cases.append({
                "opaque_id": opaque_id, "regression_phrase_number": index,
                "regression_phrase": phrase, "fixture_name": fixture_name,
                "clock_utc": fixture.now.isoformat(), "payload": payload,
                "draft_id": draft["item_id"], "draft": draft,
                "pre_followup_tables": before,
                "database_snapshot": snapshot_relative,
                "database_snapshot_sha256": sha256(snapshot_path.read_bytes()),
                "full_first_request": request_relative,
                "exact_messages_tools": exact_relative,
                "messages_tools_sha256": sha256(visible_bytes),
                "capture_main_brain_request_count": result["main_brain_request_count"],
                "capture_interpreter_call_count": result["interpreter_call_count"],
                "capture_ingress_result": result["ingress_result"],
            })
        finally:
            close_fixture(fixture)
    secrets.SystemRandom().shuffle(public)
    write_json(output_dir / "public_packets.json", public)
    manifest = {
        "schema_version": 1,
        "evaluation_kind": "full_ingress_regression_capture_for_independent_decision_replay",
        "configured_provider_called": False,
        "unseen_holdouts": False,
        "runtime_fingerprint": fingerprint,
        "synthetic_options": json.loads(Path(core.OPTIONS).read_text()),
        "neutral_terminal_response": NEUTRAL_RESPONSE,
        "cases": cases,
        "limitations": [
            "These are ten existing regression phrases, not unseen holdouts.",
            "Only the first main-brain decision is supplied by the independent reviewer.",
            "The initial missing-time question and narrow UNCLEAR interpreter are controlled fixtures.",
            "SQLite expiry uses wall time; an expired or changed context will fail exact replay.",
            "No configured language provider or household delivery path is exercised.",
        ],
    }
    if runtime_fingerprint() != fingerprint:
        raise AssertionError("Runtime source changed during capture")
    write_json(output_dir / "private_manifest.json", manifest)
    return {"status": "CAPTURED_NOT_REVIEWED", "packet_count": len(public),
            "public_packets": str(output_dir / "public_packets.json"),
            "private_manifest": str(output_dir / "private_manifest.json"),
            "interpreter_calls": sum(case["capture_interpreter_call_count"] for case in cases),
            "main_brain_requests": len(cases)}


def effects(before, after, payload):
    old_pending = {row["item_id"]: row for row in before["pending_items"]}
    new_pending = {row["item_id"]: row for row in after["pending_items"]}
    old_reminders = {row["reminder_id"] for row in before["reminders"]}
    old_outbound = {row["outbound_id"] for row in before["outbound_messages"]}
    return {
        "created_reminders": [row for row in after["reminders"]
                              if row["reminder_id"] not in old_reminders],
        "pending_changes": [
            {"item_id": identity, "before": old_pending.get(identity), "after": new_pending.get(identity)}
            for identity in sorted(set(old_pending) | set(new_pending))
            if old_pending.get(identity) != new_pending.get(identity)
        ],
        "new_outbound_messages": [row for row in after["outbound_messages"]
                                  if row["outbound_id"] not in old_outbound],
        "current_message_id": payload["message_id"],
    }


def replay(capture_dir, responses_path):
    manifest = json.loads((capture_dir / "private_manifest.json").read_text())
    if runtime_fingerprint() != manifest["runtime_fingerprint"]:
        raise ValueError("Runtime source differs from capture; create a fresh capture and review")
    supplied = json.loads(responses_path.read_text())
    response_rows = supplied.get("responses") if isinstance(supplied, dict) else None
    if not isinstance(response_rows, list):
        raise ValueError("Responses file must contain a responses list")
    responses = {}
    for response in response_rows:
        opaque_id = response["opaque_id"]
        if opaque_id in responses:
            raise ValueError("Duplicate response for opaque_id")
        responses[opaque_id] = validate_assistant(response["assistant"])
    if set(responses) != {case["opaque_id"] for case in manifest["cases"]}:
        raise ValueError("Supply exactly one response for every captured opaque_id")
    ReminderHandoffIntegrationTests.setUpClass()
    observed = []
    for case in manifest["cases"]:
        snapshot_path = capture_dir / case["database_snapshot"]
        if sha256(snapshot_path.read_bytes()) != case["database_snapshot_sha256"]:
            raise ValueError("Pre-followup database snapshot checksum mismatch")
        expected_bytes = (capture_dir / case["exact_messages_tools"]).read_bytes()
        if sha256(expected_bytes) != case["messages_tools_sha256"]:
            raise ValueError("Captured first-request checksum mismatch")
        fixture = fixture_case(case["fixture_name"])
        try:
            restore_database(snapshot_path)
            before = tables(fixture)
            if before != case["pre_followup_tables"]:
                raise AssertionError("Restored pre-followup rows differ from the captured state")
            with patch.dict(os.environ, {"ALEX_CERT_NOW": case["clock_utc"]}):
                result = run_followup(case["payload"], responses[case["opaque_id"]],
                                      expected_request_bytes=expected_bytes)
            after = tables(fixture)
            receipts = []
            if len(result["requests"]) >= 2:
                receipts = [message for message in result["requests"][1]["messages"]
                            if message.get("role") == "tool"]
            observed.append({
                "opaque_id": case["opaque_id"],
                "private_regression_phrase_number": case["regression_phrase_number"],
                "current_text": case["payload"]["text"], "clock_utc": case["clock_utc"],
                "proposed_assistant": responses[case["opaque_id"]],
                "draft_id": case["draft_id"],
                "capture_messages_tools_sha256": case["messages_tools_sha256"],
                "replay_messages_tools_sha256": (
                    sha256(json_bytes(model_visible(result["requests"][0])))
                    if result["requests"] else None),
                **result,
                "tool_receipts": receipts,
                "before": before, "after": after,
                "effects": effects(before, after, case["payload"]),
            })
        finally:
            close_fixture(fixture)
    matched = sum(row["first_request_matches_capture"] is True for row in observed)
    return {
        "schema_version": 1,
        "evaluation_kind": "independent_assistant_decisions_replayed_through_actual_full_ingress",
        "configured_provider_called": False, "unseen_holdouts": False,
        "reviewer_metadata": supplied.get("reviewer", {}),
        "responses_sha256": sha256(responses_path.read_bytes()),
        "capture_manifest_sha256": sha256((capture_dir / "private_manifest.json").read_bytes()),
        "summary": {
            "cases": len(observed), "exact_first_request_matches": matched,
            "worker_error_cases": sum(bool(row["worker_errors"]) for row in observed),
            "main_brain_requests": sum(row["main_brain_request_count"] for row in observed),
            "interpreter_calls": sum(row["interpreter_call_count"] for row in observed),
            "actual_created_reminders": sum(len(row["effects"]["created_reminders"]) for row in observed),
            "original_drafts_resolved": sum(any(
                item["item_id"] == row["draft_id"] and item["status"] == "RESOLVED"
                and item["resolution_message_id"] == row["effects"]["current_message_id"]
                for item in row["after"]["pending_items"]) for row in observed),
        },
        "cases": observed,
        "limitations": manifest["limitations"] + [
            "Counts report observed effects; no model statement is treated as proof of a completed action.",
            "Clock, task, destination and clarification appropriateness require evaluation of the actual rows and user text.",
            "After tool execution the terminal provider response is fixed neutral text, not a second independent judgment.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--output-dir", type=Path, required=True)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--capture-dir", type=Path, required=True)
    replay_parser.add_argument("--responses", type=Path, required=True)
    replay_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        output = capture(args.output_dir.resolve())
    else:
        output = replay(args.capture_dir.resolve(), args.responses.resolve())
        write_json(args.output, output)
        output = {"output": str(args.output), "summary": output["summary"]}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
