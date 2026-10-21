"""Summarize final replay separately while preserving the pre-fix evidence."""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--evidence-dir", type=Path, default=Path("/tmp/alex-conversation-evidence"))
args = parser.parse_args()
root = args.evidence_dir
mapping = json.loads((root / "clocked-runtime-blind-mapping-private.json").read_text())
decisions_path = root / "clocked-runtime-independent-decisions.json"
baseline_path = root / "clocked-runtime-replay-baseline.json"
candidate_path = root / "clocked-runtime-replay-candidate-postfix.json"
lifecycle_path = root / "clocked-ingress-lifecycle-probe-postfix.json"
baseline = json.loads(baseline_path.read_text())
candidate = json.loads(candidate_path.read_text())
lifecycle = json.loads(lifecycle_path.read_text())
decision_sha = hashlib.sha256(decisions_path.read_bytes()).hexdigest()
assert candidate["decisions_file_sha256"] == baseline["decisions_file_sha256"] == lifecycle["decisions_file_sha256"] == decision_sha
assert candidate["counts"] == {"rows": 10, "worker_errors": 0, "exact_replay_packets": 10, "reminders_created": 2}
assert baseline["counts"] == {"rows": 10, "worker_errors": 0, "exact_replay_packets": 10, "reminders_created": 0}
assert candidate["runtime_source_sha256"] == lifecycle["runtime_source_sha256"]
assert candidate["public_file_sha256"] == mapping["public_file_sha256"]
assert candidate["runtime_changed_since_review_capture"] and candidate["runtime_change_explicitly_allowed_for_postfix_replay"]
assert lifecycle["runtime_changed_since_review_capture"] and lifecycle["runtime_change_explicitly_allowed_for_postfix_regression"]

direct_rows = []
for row in candidate["rows"]:
    receipts = [json.loads(m["content"]) for m in row["tool_receipts"]]
    actual = row["after"]["rows"]["reminders"]
    pending = row["pending_after"]
    assert len(pending) == 1 and pending[0]["status"] == "PENDING"
    assert pending == row["before"]["rows"]["pending_items"]
    assert row["actual_replay_first_request_matches_reviewed_packet"]
    assert row["actor_before"] == row["actor_after"]
    for reminder in actual:
        assert reminder["task_text"] == "water the plants"
        assert reminder["owner_id"] == "USR_HUSBAND"
        assert reminder["conversation_id"] == "60111111111@s.whatsapp.net"
        assert reminder["source_message_id"] == "cr-varied-answer"
    direct_rows.append({"variation_index": row["variation_index"],
                        "input_phrase": row["input_phrase"], "packet_id": row["packet_id"],
                        "reviewer_proposal_unchanged": row["reviewer_proposal"],
                        "actual_request_equal_to_reviewed_packet": True,
                        "receipts": receipts, "actual_reminders": actual,
                        "created_reminder_count": row["created_reminder_count"],
                        "pending_after": pending,
                        "domain_record_count_delta": row["domain_record_count_delta"]})
rejected = [r["variation_index"] for r in direct_rows if any(x.get("status") == "needs_clarification" for x in r["receipts"])]
assert rejected == [1, 2, 3, 4, 5, 9]
assert [r["variation_index"] for r in direct_rows if r["created_reminder_count"]] == [8, 10]

assert lifecycle["counts"] == {"rows": 3, "worker_errors": 0, "reminders_created": 1}
lifecycle_rows = []
for row in lifecycle["rows"]:
    old = row["old_pending_after"]
    original = next(p for p in row["before"]["rows"]["pending_items"] if p["item_id"] == old["item_id"])
    assert old == original and old["status"] == "PENDING"
    assert row["after"]["rows"]["pending_items"] == [original]
    continuation = row["case"].startswith("continuation_")
    if continuation:
        assert row["created_reminder_count"] == 0
        assert len(row["semantic_interpreter_calls"]) == 1
        out = [r for r in row["after"]["rows"]["outbound_messages"] if r["source_message_id"] == row["payload"]["message_id"]]
        assert len(out) == 1 and out[0]["context_kind"] == "PENDING_ITEM" and out[0]["context_id"] == old["item_id"]
        assessment = "Time parser still asks for clarification; the original task draft is now retained and the clarification is bound to its exact ID."
    else:
        assert row["created_reminder_count"] == 1 and not row["semantic_interpreter_calls"]
        actual = row["after"]["rows"]["reminders"][0]
        assert actual["task_text"] == "call the dentist"
        assert actual["source_message_id"] == row["payload"]["message_id"]
        assert datetime.fromisoformat(actual["due_at_utc"]) - datetime.fromisoformat(lifecycle["clock_setup"]["python_clock_fixed_before_imports"]) == timedelta(minutes=40)
        assessment = "New dentist reminder creates independently; old plant draft is neither consumed nor cancelled."
    lifecycle_rows.append({"case": row["case"], "assessment": assessment,
                           "old_pending_unchanged": True, "old_pending_after": old,
                           "created_reminder_count": row["created_reminder_count"],
                           "actual_reminders": row["after"]["rows"]["reminders"],
                           "original_reviewed_call": row["original_reviewed_call"],
                           "translated_call": row["translated_call"],
                           "tool_receipts": row["tool_receipts"]})

paths = [decisions_path, baseline_path, candidate_path, lifecycle_path,
         root / "clocked-runtime-review-summary.json", root / "clocked-ingress-lifecycle-probe.json"]
report = {
    "schema_version": 1,
    "evidence_kind": "Post-retention-fix replay of the same blind proposals and the same three scripted lifecycle probes; no new model review or live provider",
    "runtime_source_sha256": candidate["runtime_source_sha256"],
    "original_review_capture_runtime_source_sha256": candidate["review_capture_runtime_source_sha256"],
    "baseline_reused_unchanged": True,
    "total_baseline_and_final_candidate_exact_reviewed_packets": 20,
    "raw_review_decisions_unchanged": True,
    "source_files": {p.name: {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths},
    "direct_brain_counts": {
        "baseline": {"clarifications": 10, "actual_reminders": 0},
        "candidate": {"proposals": 8, "appropriate_ambiguity_clarifications": 2,
                      "actual_reminders": 2, "backend_time_clarifications": 6},
    },
    "interpretation": [
        "The final runtime presents exactly the same reviewed prompts/schemas; the original independent proposals remain applicable without a new blind review.",
        "The retention fix prevents the observed task-loss path in both full-ingress UNCLEAR probes. It does not make the two time phrasings schedulable; backend clarification remains.",
        "The independent new reminder still creates correctly while the old draft remains pending.",
        "Six of eight direct-brain creation proposals still fail time validation. Context grounding has improved, but the broad conversational scheduling goal is not yet established.",
        "Baseline direct-brain missing context is not proof of baseline ingress failure: eight of the ten phrasings already recover through baseline ingress, with semantic rescue available for the other two.",
    ],
    "direct_brain_rows": direct_rows,
    "full_ingress_clock_setup": lifecycle["clock_setup"],
    "full_ingress_rows": lifecycle_rows,
    "limitations": [
        "No general NLU accuracy, live provider reliability, latency, cost, production traffic or real delivery is measured.",
        "The direct-brain tests bypass ingress; normal pending resolution belongs to ingress and is not forced in replay.",
        "The full-ingress probes deliberately force semantic UNCLEAR and translate only due_local by the recorded clock offset; they are conditional scripted lifecycle tests.",
        "The initial failure and raw decisions remain preserved separately; this report does not erase earlier observations.",
    ],
    "reproducibility_workers": ["replay_clocked_runtime.py", "probe_ingress_lifecycle.py", "summarize_postfix_replay.py"],
}
output = root / "clocked-runtime-postfix-summary.json"
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"summary": str(output), "final_runtime_source_sha256": candidate["runtime_source_sha256"],
                  "exact_reviewed_packets": 20, "actual_candidate_reminders": 2,
                  "remaining_backend_time_clarifications": 6, "retention_probes_fixed": 2,
                  "new_request_isolated": True}, indent=2))
