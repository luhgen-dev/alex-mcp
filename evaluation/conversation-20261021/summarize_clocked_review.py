"""Unblind fixed-clock packet review and report actual replay/backend outcomes."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


root = Path("/tmp/alex-conversation-evidence")
mapping_path = root / "clocked-runtime-blind-mapping-private.json"
public_path = root / "clocked-runtime-blind-packets.json"
decisions_path = root / "clocked-runtime-independent-decisions.json"
mapping = json.loads(mapping_path.read_text())
public_bytes = public_path.read_bytes()
raw_decisions = decisions_path.read_bytes()
assert hashlib.sha256(public_bytes).hexdigest() == mapping["public_file_sha256"]
packets = {p["packet_id"]: p for p in json.loads(public_bytes)["packets"]}
decisions = {p["packet_id"]: p for p in json.loads(raw_decisions)}
assert len(packets) == len(decisions) == 20 and set(packets) == set(decisions)
replays = {label: json.loads((root / f"clocked-runtime-replay-{label}.json").read_text())
           for label in ("baseline", "candidate")}
replayed = {p["packet_id"]: p for value in replays.values() for p in value["rows"]}
for label, replay in replays.items():
    assert replay["counts"]["rows"] == replay["counts"]["exact_replay_packets"] == 10
    assert replay["counts"]["worker_errors"] == 0
    assert replay["runtime_source_sha256"] == mapping["provenance"][label]["runtime_source_sha256"]
    assert replay["decisions_file_sha256"] == hashlib.sha256(raw_decisions).hexdigest()
    assert replay["public_file_sha256"] == mapping["public_file_sha256"]


def state_for(packet):
    for message in packet["messages"]:
        value = message.get("content", "")
        if isinstance(value, str) and '{"conversation_state"' in value:
            return json.loads(value[value.index('{"conversation_state"'):])["conversation_state"]
    return {}


def packet_facts(packet):
    state = state_for(packet)
    contents = [m.get("content") for m in packet["messages"] if m.get("role") in {"user", "assistant"}]
    return {
        "prior_task_present": "Remind me to water the plants" in contents,
        "prior_question_present": "What time should I remind you to water the plants?" in contents,
        "active": state.get("active"), "current_time": state.get("current_time"),
        "offered_tools": [t["function"]["name"] for t in packet["tools"]],
    }


absolute = {1: "2026-10-22T19:00:00", 3: "2026-10-22T20:00:00",
            4: "2026-10-22T19:00:00", 8: "2026-10-21T23:30:00", 9: "2026-10-22T19:00:00"}
offsets = {2: 40, 5: 30, 10: 40}
ambiguities = {6: "Specific time after 20:00 tonight is missing.",
               7: "AM/PM for approximately seven tomorrow is missing."}
grouped = {}
for info in mapping["packets"]:
    grouped.setdefault(info["variation_index"], {})[info["revision_label"]] = info
counts = {label: Counter() for label in replays}
pairs = []
for index, group in sorted(grouped.items()):
    assert set(group) == {"baseline", "candidate"}
    assert group["baseline"]["input_phrase"] == group["candidate"]["input_phrase"]
    pair = {"variation_index": index, "input_phrase": group["candidate"]["input_phrase"]}
    for label, info in group.items():
        packet_id = info["packet_id"]
        decision = decisions[packet_id]
        replay = replayed[packet_id]
        facts = packet_facts(packets[packet_id])
        actual = replay["after"]["rows"]["reminders"]
        receipts = [json.loads(m["content"]) for m in replay["tool_receipts"]]
        pending = replay["pending_after"]
        assert len(pending) == 1 and pending[0] == info["seeded_pending_record"]
        assert replay["actor_before"] == replay["actor_after"]
        assert all(not replay["actor_before"].get(k) for k in (
            "reminder_context_text", "reminder_semantic_text", "reminder_routing_json"))
        counts[label]["packets"] += 1
        counts[label]["clarification_decisions"] += decision["decision"] == "clarify"
        counts[label]["scheduling_proposals"] += bool(decision["tool_calls"])
        counts[label]["actual_reminders_created"] += len(actual)
        counts[label]["backend_time_clarification_receipts"] += sum(r.get("status") == "needs_clarification" for r in receipts)
        counts[label]["exact_review_packet_replays"] += replay["actual_replay_first_request_matches_reviewed_packet"]
        counts[label]["pending_rows_unchanged"] += pending[0] == info["seeded_pending_record"]
        other_delta = {t: n for t, n in replay["domain_record_count_delta"].items() if t != "reminders" and n}
        assert not other_delta
        for reminder in actual:
            assert reminder["task_text"] == "water the plants"
            assert reminder["owner_id"] == "USR_HUSBAND"
            assert reminder["conversation_id"] == "60111111111@s.whatsapp.net"
            assert reminder["space_id"] == "FAMILY_SHARED"
            assert reminder["source_message_id"] == "cr-varied-answer"
        pair[label] = {
            "packet_id": packet_id, "packet_facts": facts,
            "reviewer_decision_verbatim": decision,
            "actual_replay": {
                "actual_first_messages_and_schemas_equal_reviewed_packet": True,
                "tool_receipts": receipts, "reminders_created": actual,
                "created_reminder_count": len(actual),
                "domain_record_count_delta": replay["domain_record_count_delta"],
                "pending_row_after": pending[0],
                "actor_reminder_fields_were_not_enriched": True,
                "python_clock": replay["after"]["runtime_clock"],
                "sqlite_wall_clock": replay["after"]["sqlite_wall_clock"],
            },
        }
    assert not pair["baseline"]["packet_facts"]["prior_task_present"]
    assert pair["candidate"]["packet_facts"]["prior_task_present"]
    state = state_for(packets[group["candidate"]["packet_id"]])
    assert state["active"]["id"] == group["candidate"]["seeded_pending_record"]["item_id"]
    tz = ZoneInfo(state["current_time"]["timezone"])
    received = datetime.fromisoformat(state["current_time"]["received_at_utc"]).astimezone(tz)
    processing = datetime.fromisoformat(state["current_time"]["local_datetime"])
    assert received == processing
    candidate_decision = pair["candidate"]["reviewer_decision_verbatim"]
    if index in ambiguities:
        assert candidate_decision["decision"] == "clarify" and not candidate_decision["tool_calls"]
        assert "plant" in candidate_decision["reply"]
        pair["time_assessment"] = {"kind": "material_missing_time_detail", "detail": ambiguities[index],
                                   "focused_task_preserving_clarification": True}
    else:
        assert len(candidate_decision["tool_calls"]) == 1
        call = candidate_decision["tool_calls"][0]
        assert call["name"] == "create_reminder" and call["arguments"]["task"] == "water the plants"
        assert call["arguments"]["recipient"] == "me"
        proposed = datetime.fromisoformat(call["arguments"]["due_local"]).replace(tzinfo=tz)
        expected = (received + timedelta(minutes=offsets[index]) if index in offsets
                    else datetime.fromisoformat(absolute[index]).replace(tzinfo=tz))
        assert proposed == expected and proposed > processing
        for reminder in pair["candidate"]["actual_replay"]["reminders_created"]:
            assert datetime.fromisoformat(reminder["due_at_utc"]) == expected
        pair["time_assessment"] = {
            "kind": "relative" if index in offsets else "absolute_local_time",
            "proposed_due_local": proposed.isoformat(),
            "consistent_with_supplied_date_clock_and_wording": True,
            "received_at_local": received.isoformat(), "processing_local_datetime": processing.isoformat(),
            "offset_minutes": offsets.get(index),
            "note": "Proposal correctness and backend acceptance are separate; see actual_replay receipts/rows.",
        }
    pair["pending_identity_assessment"] = {
        "candidate_active_id_equals_exact_seeded_pending_id": True,
        "create_reminder_schema_takes_no_pending_id": True,
        "exact_draft_consumption_not_completed_by_direct_brain": True,
        "explanation": "The actual seeded draft remains unchanged after this direct-brain replay. In normal message handling, ingress owns continuation append and post-create draft resolution. No cleanup was injected here.",
    }
    pair["interpretation"] = (
        "Baseline clarification is sensible for its missing task context. Candidate keeps the task but asks for a material time detail."
        if index in ambiguities else
        "Baseline clarification is sensible for its missing task context. Candidate reviewer proposes a grounded action; the real backend accepted it."
        if pair["candidate"]["actual_replay"]["created_reminder_count"] else
        "Baseline clarification is sensible for its missing task context. Candidate reviewer proposes a grounded action, but the real backend rejects the raw time wording and requests a time again."
    )
    pairs.append(pair)

assert counts["baseline"]["clarification_decisions"] == 10
assert counts["candidate"]["scheduling_proposals"] == 8
assert counts["candidate"]["actual_reminders_created"] == 2
assert counts["candidate"]["backend_time_clarification_receipts"] == 6
ingress_path = root / "clocked-ingress-lifecycle-probe.json"
ingress = json.loads(ingress_path.read_text())
assert ingress["counts"] == {"rows": 3, "worker_errors": 0, "reminders_created": 1}
ingress_summary = []
for row in ingress["rows"]:
    old = row["old_pending_after"]
    new_pending = [p for p in row["after"]["rows"]["pending_items"] if p["item_id"] != old["item_id"]]
    is_continuation = row["case"].startswith("continuation_")
    if is_continuation:
        assert row["created_reminder_count"] == 0 and old["status"] == "CANCELLED"
        assert len(new_pending) == 1 and new_pending[0]["status"] == "PENDING"
        assert new_pending[0]["accumulated_text"] == row["input_phrase"]
        assessment = "Observed task-loss lifecycle gap: backend asks for a time; ingress creates a new time-only draft and cancels the original plant task draft. No reminder was created."
    else:
        assert row["created_reminder_count"] == 1 and old["status"] == "PENDING" and not new_pending
        reminder = row["after"]["rows"]["reminders"][0]
        assert reminder["task_text"] == "call the dentist"
        assert reminder["source_message_id"] == row["payload"]["message_id"]
        assert datetime.fromisoformat(reminder["due_at_utc"]) - datetime.fromisoformat(ingress["clock_setup"]["python_clock_fixed_before_imports"]) == timedelta(minutes=40)
        assessment = "Independent new request creates the dentist reminder without consuming the old plant draft."
    ingress_summary.append({
        "case": row["case"], "input_phrase": row["input_phrase"],
        "semantic_interpreter_calls_forced_unclear": len(row["semantic_interpreter_calls"]),
        "original_reviewed_call": row["original_reviewed_call"], "translated_call": row["translated_call"],
        "relative_delay_seconds": row["relative_delay_seconds"],
        "created_reminder_count": row["created_reminder_count"],
        "actual_reminders": row["after"]["rows"]["reminders"],
        "old_pending_after": old, "new_pending_rows": new_pending,
        "tool_receipts": row["tool_receipts"], "assessment": assessment,
    })

comparison = json.loads((root / "conversation-comparison-pre-retention.json").read_text())
assert comparison["baseline"]["summary"]["passed"] == 15
assert comparison["candidate"]["summary"]["passed"] == comparison["candidate"]["summary"]["tests"] == 50
unit_text = (root / "candidate-unit-pre-retention.log").read_text()
assert re.search(r"Ran 590 tests", unit_text) and "OK (skipped=2)" in unit_text
baseline_ingress = json.loads((root / "baseline-ingress-continuation-probe.json").read_text())
assert baseline_ingress["recovered_with_unclear"] == 8 and baseline_ingress["semantic_rescue_required"] == 2

paths = [mapping_path, public_path, decisions_path, ingress_path,
         root / "clocked-runtime-replay-baseline.json", root / "clocked-runtime-replay-candidate.json",
         root / "baseline-ingress-continuation-probe.json"]
report = {
    "schema_version": 1,
    "evaluation_kind": "Fresh blind reasoning over actual first runtime packets, then exact-packet scripted replay through real direct brain/MCP/backend, plus three separate scripted full-ingress lifecycle probes",
    "deployed_model_execution": False, "general_accuracy_score": None,
    "scope": "Ten known phrasings of one synthetic pending reminder, baseline and candidate, one fresh independent reviewer. No live WhatsApp, real provider, or household delivery. All actual writes are disposable test rows.",
    "source_files": {p.name: {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths},
    "runtime_provenance": mapping["provenance"], "clock_setup": mapping["clock_setup"],
    "counts": {label: dict(value) for label, value in counts.items()},
    "interpretation": [
        "The candidate supplies the exact pending task/question/reference and the reviewer preserves that task in all ten cases: eight create proposals and two appropriate focused clarifications.",
        "Actual backend replay accepted only two of the eight proposals. Six raw time phrasings triggered REMINDER_NEEDS_TIME/needs_clarification; grounded understanding did not translate into successful scheduling for those cases.",
        "The two accepted reminders have the verified plant task, intended UTC due, current source message, USR_HUSBAND owner, configured self DM, and FAMILY_SHARED space. Domain-record deltas show no other domain writes.",
        "All twenty replays matched the reviewed messages and tool schemas exactly before the scripted proposal was released; the raw decisions were not changed.",
        "The baseline's ten clarifications reflect missing direct-brain task context. This is not evidence of ten ordinary WhatsApp failures: separate unchanged-baseline ingress probing recovers eight directly and has semantic rescue for the other two.",
        "Direct-brain replay leaves all pending drafts unchanged; exact-ID prompt availability is verified, while normal draft completion belongs to ingress.",
        "The separate full-ingress UNCLEAR probes expose a real task-loss lifecycle gap for the two unusual relative phrasings: failed creation prompts a new time-only draft that cancels the original task draft.",
        "The separate new-reminder probe correctly creates the dentist reminder and leaves the old plant draft pending.",
    ],
    "time_anchor_findings": {
        "received_at_utc": "2026-10-21T08:00:00+00:00", "local_datetime": "2026-10-21T16:00:00+08:00",
        "receipt_and_processing_clocks_same_instant": True,
        "relative_proposals": {str(k): {"minutes": v, "due_local": (datetime.fromisoformat("2026-10-21T16:00:00+08:00") + timedelta(minutes=v)).isoformat()} for k, v in offsets.items()},
        "cannot_distinguish_receipt_vs_processing_policy_when_clocks_equal": True,
        "sqlite_boundary": "Python was frozen at08:00UTC; SQLite action-created timestamps retain actual wall time. In particular the relative reminder can precede its SQL created_at in this artificial fixture. No post-action timestamp was rewritten; this is not evidence of a production past-due scheduling bug.",
        "initial_unclocked_review_preserved_separately": "actual-runtime-review-summary.json; it had a receipt/processing-clock mismatch and is not used for acceptance counts here.",
    },
    "genuine_ambiguities": ambiguities,
    "separate_full_ingress_probe": {
        "clock_setup": ingress["clock_setup"], "rows": ingress_summary,
        "not_an_independent_model_or_byte_identical_ingress_evaluation": True,
        "original_and_clock_translated_args_recorded": True,
        "no_actor_field_injection_or_pending_cleanup": True,
    },
    "baseline_ingress_boundary": {
        "source": str(root / "baseline-ingress-continuation-probe.json"),
        "same_id_question_recovered_with_unclear_interpreter": 8,
        "requires_semantic_rescue": [2, 5],
        "both_bind_with_stubbed_positive_semantics": True,
        "source_locations": {
            "baseline_ingress.py": [952, 1058, 1657, 1753, 2225],
            "meaning": "Continuation/recovery; context applied before brain; pending append and actor enrichment; post-brain resolution on successful current-message reminder.",
        },
    },
    "separate_deterministic_test_gates": {
        "comparison_snapshot": str(root / "conversation-comparison-pre-retention.json"),
        "unit_snapshot": str(root / "candidate-unit-pre-retention.log"),
        "as_of": "Runtime captured in this pre-retention replay; newer tests/fixes must be reported separately.",
        "baseline_contracts": comparison["baseline"]["summary"],
        "candidate_contracts": comparison["candidate"]["summary"],
        "candidate_full_unit_tests": 590, "candidate_unit_skipped": 2,
        "candidate_unit_passed": 588, "unit_failures": 0, "unit_errors": 0,
        "qualification": "Passing existing scripted contracts does not cover or erase the newly observed backend/lifecycle gaps.",
    },
    "reproducibility_workers": ["export_runtime_packets.py", "assemble_clocked_packets.py",
                               "replay_clocked_runtime.py", "probe_ingress_lifecycle.py", "summarize_clocked_review.py"],
    "limitations": [
        "One reviewer, one task, ten known phrasings; not held-out general accuracy or a deployed Alex model benchmark.",
        "The live semantic router was disabled in the packet capture and direct replay.",
        "Backend replay uses the reviewer's first decision as a scripted provider response; after actual tool receipts, fixed neutral terminal text ends the loop. This does not evaluate fresh model reactions to errors.",
        "The three lifecycle probes force the semantic interpreter to UNCLEAR. Their outcomes establish the conditional behavior, not the frequency of that condition with a real provider.",
        "The task-loss probe was executed on the candidate only; no claim is made that the underlying draft-supersession behavior was newly introduced in this branch.",
        "No throughput, latency, tokens, spend, cross-provider robustness, production traffic, or outbound delivery was measured.",
    ],
    "pairs": pairs,
    "raw_decisions_verbatim": raw_decisions.decode(),
}
output = root / "clocked-runtime-review-summary.json"
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
assert decisions_path.read_bytes() == raw_decisions
print(json.dumps({"summary": str(output), "counts": report["counts"],
                  "ingress_cases": len(ingress_summary), "ingress_task_loss_cases": 2}, indent=2))
