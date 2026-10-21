"""Replay blind reviewer proposals through real Alex/MCP in disposable fixtures.

Only the provider response is scripted. This is not a deployed-model test or
an ingress test. The first request must equal the packet the reviewer saw.
"""
import argparse
import asyncio
import copy
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import patch


parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--fixture", type=Path, required=True)
parser.add_argument("--label", choices=("baseline", "candidate"), required=True)
parser.add_argument("--mapping", type=Path, required=True)
parser.add_argument("--public", type=Path, required=True)
parser.add_argument("--decisions", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--allow-runtime-source-change", action="store_true",
                    help="Post-fix replay; actual first messages/schemas must still match the reviewed packet exactly")
args = parser.parse_args()
fixed = "2026-10-21T08:00:00+00:00"
assert os.environ.get("ALEX_CERT_NOW") == fixed, "Set fixed clock before process/app imports"
mapping = json.loads(args.mapping.read_text())
public_bytes = args.public.read_bytes()
assert hashlib.sha256(public_bytes).hexdigest() == mapping["public_file_sha256"]
packets = {p["packet_id"]: p for p in json.loads(public_bytes)["packets"]}
decision_bytes = args.decisions.read_bytes()
decision_value = json.loads(decision_bytes)
decision_rows = decision_value if isinstance(decision_value, list) else decision_value["decisions"]
decisions = {p["packet_id"]: p for p in decision_rows}
assert len(decisions) == len(decision_rows) == len(packets) == 20
assert set(decisions) == set(packets)
repo = args.repo.resolve()
digest = hashlib.sha256()
for path in sorted((repo / "alex-mcp" / "app").rglob("*.py")):
    digest.update(str(path.relative_to(repo)).encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
source_hash = digest.hexdigest()
captured_hash = mapping["provenance"][args.label]["runtime_source_sha256"]
assert args.allow_runtime_source_change or source_hash == captured_hash, "Runtime changed since capture"
sys.path.insert(0, str(repo / "alex-mcp" / "app"))
sys.path.insert(0, str(repo / "tests"))
spec = importlib.util.spec_from_file_location("clocked_replay_fixture", args.fixture.resolve())
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ConversationFixture.setUpClass()

domain_tables = (
    "reminders", "tasks", "saved_items", "shopping_items", "financial_events",
    "diary_events", "plans", "leave_records", "work_roster", "savings_goals",
    "money_buckets", "alex_phase2_work_events", "alex_phase2_delegations",
    "alex_phase2_cash_events", "alex_phase2_assets", "alex_phase2_goals",
)
evidence_tables = (
    "pending_items", "reminder_events", "tool_audit", "tool_execution_claims",
    "outbound_messages", "conversation_turns", "inbound_messages",
)


def snapshot():
    conn = module.db.connect()
    try:
        rows = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
                for table in domain_tables + evidence_tables}
        return {"sqlite_wall_clock": conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0],
                "runtime_clock": module.core.runtime_clock.utc_iso(), "rows": rows}
    finally:
        conn.close()


def replay(case, actor, text, proposal, public, row):
    """Compare the actual request before returning any provider proposal."""
    requests = []
    row["requests"] = requests
    first_calls = proposal.get("tool_calls", [])

    class Message:
        def __init__(self, content, calls):
            self.content = content
            self.tool_calls = [SimpleNamespace(
                id=f"blind-review-replay-{index}",
                function=SimpleNamespace(name=call["name"],
                                         arguments=json.dumps(call.get("arguments", {}))))
                for index, call in enumerate(calls)]

        def model_dump(self, exclude_none=True):
            value = {"role": "assistant", "content": self.content}
            if self.tool_calls:
                value["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                    "name": call.function.name, "arguments": call.function.arguments}}
                    for call in self.tool_calls]
            return value

    class Completions:
        def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                row["actual_replay_first_request_matches_reviewed_packet"] = (
                    kwargs["messages"] == public["messages"]
                    and kwargs.get("tools", []) == public["tools"])
                assert row["actual_replay_first_request_matches_reviewed_packet"], "Actual request differs from blind packet"
                message = Message(proposal.get("reply", ""), first_calls)
            elif len(requests) == 2 and first_calls:
                message = Message("I have checked the result.", [])
            else:
                raise AssertionError("Unexpected additional provider request")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    route = {"provider": "grok", "model": "offline-fixture",
             "reasoning_effort": "low", "role": "manual"}
    with patch.object(module.brain, "_provider_routes", return_value=[route]), \
         patch.object(module.brain, "_client_for", return_value=fake), \
         patch.object(module.brain.shadow_router, "live_enabled", return_value=False):
        result = asyncio.run(module.brain.respond(actor, text))
    return result, requests


results = []
selected = sorted((p for p in mapping["packets"] if p["revision_label"] == args.label),
                  key=lambda p: p["variation_index"])
for entry in selected:
    packet_id = entry["packet_id"]
    public = packets[packet_id]
    proposal = decisions[packet_id]
    case = module.ConversationFixture()
    case.setUp()
    row = {"packet_id": packet_id, "revision_label": args.label,
           "variation_index": entry["variation_index"], "input_phrase": entry["input_phrase"],
           "reviewer_proposal": proposal}
    try:
        _, pending, _ = case.draft()
        actor = case.actor("cr-varied-answer", entry["input_phrase"])
        old_id = pending["item_id"]
        captured_id = entry["seeded_pending_record"]["item_id"]
        conn = module.db.connect()
        try:
            # Reconstruct the exact captured fixture identity before execution.
            # No proposal argument, runtime actor field or action result is changed.
            conn.execute("UPDATE pending_items SET item_id=? WHERE item_id=?", (captured_id, old_id))
            conn.execute("UPDATE outbound_messages SET context_id=? WHERE context_kind='PENDING_ITEM' AND context_id=?",
                         (captured_id, old_id))
            for table, column in (("pending_items", "created_at_utc"),
                                  ("conversation_turns", "created_at_utc"),
                                  ("outbound_messages", "created_at_utc"),
                                  ("inbound_messages", "received_at_utc")):
                conn.execute(f"UPDATE {table} SET {column}=?", (fixed,))
            conn.commit()
        finally:
            conn.close()
        row["actor_before"] = asdict(actor)
        row["before"] = snapshot()
        assert row["before"]["rows"]["pending_items"] == [entry["seeded_pending_record"]]
        assert actor.received_at_utc == fixed
        assert not getattr(actor, "reminder_context_text", "")
        assert not getattr(actor, "reminder_semantic_text", "")
        assert not getattr(actor, "reminder_routing_json", "")
        tool_calls = proposal.get("tool_calls", [])
        result, requests = replay(case, actor, entry["input_phrase"], proposal, public, row)
        row["brain_result"] = result
        row["requests"] = requests
        row["actual_replay_first_request_matches_reviewed_packet"] = (
            requests[0]["messages"] == public["messages"]
            and requests[0].get("tools", []) == public["tools"])
        row["metrics"] = {"scripted_model_calls": len(requests),
                          "initial_message_characters": len(json.dumps(requests[0]["messages"], ensure_ascii=False)),
                          "initial_offered_tool_count": len(requests[0].get("tools", []))}
        row["actor_after"] = asdict(actor)
        row["synthetic_terminal_reply"] = "I have checked the result." if tool_calls else None
        row["tool_receipts"] = [message for message in requests[-1]["messages"]
                                if message.get("role") == "tool"]
        row["worker_error"] = None
    except Exception:
        row["worker_error"] = traceback.format_exc()
    finally:
        # Snapshot actual action results before normal fixture disposal.
        row["after"] = snapshot()
        row["domain_record_count_delta"] = {
            table: len(row["after"]["rows"][table]) - len(row.get("before", {}).get("rows", {}).get(table, []))
            for table in domain_tables}
        row["created_reminder_count"] = row["domain_record_count_delta"]["reminders"]
        row["pending_after"] = row["after"]["rows"]["pending_items"]
        results.append(row)
        case.tearDown()

report = {
    "schema_version": 1,
    "evidence_kind": "Independent blind-reviewer proposals replayed as scripted provider responses through real direct brain and MCP/backend in disposable databases; ingress bypassed; no deployed provider call",
    "revision_label": args.label,
    "repo": str(repo),
    "revision": subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                               text=True, check=True).stdout.strip(),
    "runtime_source_sha256": source_hash,
    "review_capture_runtime_source_sha256": captured_hash,
    "runtime_changed_since_review_capture": source_hash != captured_hash,
    "runtime_change_explicitly_allowed_for_postfix_replay": args.allow_runtime_source_change,
    "public_file_sha256": mapping["public_file_sha256"],
    "decisions_file_sha256": hashlib.sha256(decision_bytes).hexdigest(),
    "clock_setup": mapping["clock_setup"],
    "replay_setup": {
        "captured_pending_uuid_restored_before_execution": True,
        "seeded_rows_timestamped_before_execution": True,
        "actor_reminder_context_semantic_and_routing_fields_left_empty": True,
        "action_results_or_created_timestamps_rewritten": False,
        "pending_resolution_or_cleanup_forced": False,
        "live_router_enabled": False,
        "subsequent_provider_response": "Fixed neutral terminal text after actual tool receipt; not fresh reviewer reasoning or an assertion of action success",
    },
    "counts": {"rows": len(results),
               "worker_errors": sum(bool(row["worker_error"]) for row in results),
               "exact_replay_packets": sum(row.get("actual_replay_first_request_matches_reviewed_packet", False) for row in results),
               "reminders_created": sum(row["created_reminder_count"] for row in results)},
    "rows": results,
}
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"output": str(args.output), **report["counts"]}))
