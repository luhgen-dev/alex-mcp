#!/usr/bin/env python3
"""Export blind interpretation packets and score recorded holdout observations.

This does not call a provider or execute Alex. Export defaults to the first turn
of each scenario. A later turn requires actual prior responses and current state
from a trace, never an oracle prefix constructed from expected answers.

Examples:
  python tools/conversation_holdout.py export --output /tmp/holdout-public.json
  python tools/conversation_holdout.py score --observations /tmp/observed.json \
      --output /tmp/holdout-review.json

Trace schema for a later turn:
  {scenario_id, source:"observed_run", oracle_context_used:false, state,
   completed_turns:[{input, assistant_messages:[{role,actor_id,conversation_id,
                    message_id,text,trusted_binding?}]}]}

Observations schema:
  {evaluation_kind, provenance:{provider,model,prompt_configuration,
    candidate_revision}, turns:[{scenario_id,turn,packet_sha256,observed:{
    decision,exact_target_ids,spaces,destinations,resolved_pending_ids?,
    state_must_equal?,expected_active_record_count?},
    manual_review?:{reviewer,checks:{<expected field>:true|false}}}]}

Use stable fixture aliases for observed IDs, bound by the execution adapter.
The scorer checks structural fields and explicitly leaves language, actual
backend effects and forbidden actions for independent review. A model's own
claimed success is not a backend observation. No fresh-model result is inferred
from an export, a scripted provider, or an unreviewed observation file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/conversation_holdout_review.json"
INPUT_FIELDS = {"actor_id", "conversation_id", "message_id", "received_at_utc", "source",
                "text", "quoted_message_id", "attachments"}
RECORD_FIELDS = {"amount_minor", "awaiting", "claimable", "claimed_by_user_id", "content",
                 "currency", "description", "destination", "due_at_utc", "entity_type",
                 "id", "item_name", "kind", "media_id", "owner_id", "recipient",
                 "source_message_id", "space_id", "start_local", "status", "target_id",
                 "task_text", "title"}
SELECTION_FIELDS = {"conversation_id", "created_at_utc", "expires_at_utc", "id", "items",
                    "kind", "owner_id", "space_id"}
ATTACHMENT_FIELDS = {"filename", "kind", "media_id", "untrusted_extracted_text", "untrusted_transcript"}
HISTORY_FIELDS = {"actor_id", "conversation_id", "message_id", "role", "text"}
BINDING_FIELDS = {"pending_item_id", "financial_event_id", "selection_id", "reminder_id",
                  "saved_item_id", "media_id", "item_id", "task_id", "plan_id"}
ACTOR_FIELDS = {"private_space", "allowed_spaces", "direct_chat"}
CHAT_FIELDS = {"type", "display_name", "actor_id"}
STRUCTURAL_FIELDS = {"decision", "reference", "exact_target_ids", "spaces", "destinations",
                     "resolved_pending_ids", "state_must_equal", "expected_active_record_count"}
SET_FIELDS = {"exact_target_ids", "spaces", "destinations", "resolved_pending_ids"}
MANUAL_FIELDS = {"backend_outcomes", "unchanged_ids", "forbidden", "reply_must_include",
                 "group_reply_policy", "clarification_slots", "carried_slots"}


def pick(value, fields):
    return {key: value[key] for key in sorted(fields) if key in value}


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def visible_context(public, state, history, current):
    actor_id, chat_id = current["actor_id"], current["conversation_id"]
    actor = public["actors"][actor_id]
    group = public["conversations"][chat_id]["type"] == "GROUP"
    spaces = {"FAMILY_SHARED"} if group else set(actor["allowed_spaces"])
    records = []
    for record in state.get("records", []):
        if record.get("space_id") not in spaces:
            continue
        if record.get("entity_type") in {"REMINDER_DRAFT", "PENDING_ITEM"}:
            if record.get("owner_id") != actor_id or record.get("destination") != chat_id:
                continue
        records.append(pick(record, RECORD_FIELDS))
    ids = {r["id"] for r in records}
    media_ids = {r["media_id"] for r in records if r.get("media_id")}
    media_ids.update(current.get("attachments", []))
    selections = []
    for selection in state.get("selection_sets", []):
        if (selection.get("owner_id") != actor_id or selection.get("conversation_id") != chat_id
                or selection.get("space_id") not in spaces):
            continue
        clean = pick(selection, SELECTION_FIELDS)
        clean["items"] = {n: identity for n, identity in clean.get("items", {}).items()
                          if identity in ids | media_ids}
        selections.append(clean)
    ids.update(s["id"] for s in selections)
    ids.update(media_ids)
    turns = []
    for turn in history:
        if turn.get("conversation_id") != chat_id:
            continue
        if not group and turn.get("actor_id") not in {actor_id, "ALEX"}:
            continue
        if set(turn.get("trusted_binding", {})) - BINDING_FIELDS:
            continue
        binding = pick(turn.get("trusted_binding", {}), BINDING_FIELDS)
        # Do not include the text of a message bound to inaccessible content.
        if any(identity not in ids for identity in binding.values()):
            continue
        clean = pick(turn, HISTORY_FIELDS)
        if binding:
            clean["trusted_binding"] = binding
        turns.append(clean)
    attachments = [pick(a, ATTACHMENT_FIELDS) for a in state.get("attachments", [])
                   if a.get("media_id") in media_ids]
    return {"records": records, "selection_sets": selections, "attachments": attachments}, turns


def export_packet(fixture, scenario, turn=1, trace=None):
    if turn < 1 or turn > len(scenario["steps"]):
        raise ValueError("turn is outside this scenario")
    current = pick(scenario["steps"][turn - 1]["input"], INPUT_FIELDS)
    history = list(scenario["history"])
    state = scenario["initial_state"]
    if turn > 1:
        if not trace or trace.get("scenario_id") != scenario["id"]:
            raise ValueError("later turns require the matching observed trace")
        if trace.get("source") != "observed_run" or trace.get("oracle_context_used") is not False:
            raise ValueError("trace must identify actual observations without an oracle prefix")
        completed = trace.get("completed_turns", [])
        if len(completed) != turn - 1 or not isinstance(trace.get("state"), dict):
            raise ValueError("trace needs every prior turn and its actual current state")
        for index, prior in enumerate(completed):
            expected_input = pick(scenario["steps"][index]["input"], INPUT_FIELDS)
            if pick(prior["input"], INPUT_FIELDS) != expected_input:
                raise ValueError("trace input differs from the corresponding scenario turn")
            if not prior.get("assistant_messages"):
                raise ValueError("trace must include actual prior assistant messages")
            history.append({**prior["input"], "role": "user"})
            history.extend(prior["assistant_messages"])
        state = trace["state"]
    public = fixture["public_context"]
    visible, history = visible_context(public, state, history, current)
    actor_id = current["actor_id"]
    packet = {
        "schema_version": 1, "scenario_id": scenario["id"], "turn": turn,
        "evaluation_mode": "blind interpretation packet; not an Alex runtime transcript",
        "public_context": {
            "timezone": public["timezone"], "clock_utc": current["received_at_utc"],
            "authenticated_actor": {"id": actor_id, **pick(public["actors"][actor_id], ACTOR_FIELDS)},
            "conversations": {key: pick(value, CHAT_FIELDS) for key, value in public["conversations"].items()
                              if value.get("type") == "GROUP" or value.get("actor_id") == actor_id},
        },
        "visible_state": visible, "history": history, "input": current,
    }
    packet["packet_sha256"] = fingerprint(packet)
    return packet


def score(fixture, observations):
    rows = {}
    for row in observations.get("turns", []):
        key = (row["scenario_id"], row["turn"])
        if key in rows:
            raise ValueError(f"duplicate observation {key}")
        rows[key] = row
    known = {(s["id"], n) for s in fixture["scenarios"] for n in range(1, len(s["steps"]) + 1)}
    if set(rows) - known:
        raise ValueError("observations contain an unknown scenario/turn")
    results = []
    for scenario in fixture["scenarios"]:
        for number, step in enumerate(scenario["steps"], 1):
            row = rows.get((scenario["id"], number))
            result = {"scenario_id": scenario["id"], "turn": number}
            if row is None:
                results.append({**result, "status": "not_executed"})
                continue
            expected, observed = step["expected"], row.get("observed", {})
            failures, unreviewed = [], []
            for field in sorted(STRUCTURAL_FIELDS & expected.keys()):
                if field not in observed:
                    unreviewed.append(field)
                elif (set(observed[field]) != set(expected[field]) if field in SET_FIELDS
                      else observed[field] != expected[field]):
                    failures.append(field)
            review = row.get("manual_review", {})
            checks = review.get("checks", {}) if review.get("reviewer") else {}
            for field in sorted(MANUAL_FIELDS & expected.keys()):
                if field != "backend_outcomes" and expected[field] in ([], {}):
                    continue
                if checks.get(field) is False:
                    failures.append(field)
                elif checks.get(field) is not True:
                    unreviewed.append(field)
            if not row.get("packet_sha256"):
                unreviewed.append("prompt_packet_fingerprint")
            elif number == 1 and row["packet_sha256"] != export_packet(fixture, scenario)["packet_sha256"]:
                failures.append("prompt_packet_fingerprint")
            status = "failed" if failures else "review_required" if unreviewed else "reviewed_pass"
            results.append({**result, "status": status, "failed_fields": failures,
                            "unreviewed_fields": unreviewed})
    statuses = {name: sum(r["status"] == name for r in results)
                for name in ("not_executed", "review_required", "failed", "reviewed_pass")}
    return {
        "evaluation_kind": observations.get("evaluation_kind", "unspecified"),
        "provenance": observations.get("provenance", {}),
        "fresh_model_accuracy_established_by_this_tool": False,
        "summary": {"total_turns": len(results), **statuses}, "turns": results,
        "limitations": [
            "This command compares supplied observations; it neither calls a provider nor verifies their provenance.",
            "Actual backend changes, unchanged records, forbidden actions and reply semantics need independent review.",
            "Interpretation-only packet review is separate from Alex integration and end-to-end execution.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "score"])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario")
    parser.add_argument("--turn", type=int, default=1)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    if args.command == "export":
        scenarios = [s for s in fixture["scenarios"] if not args.scenario or s["id"] == args.scenario]
        if not scenarios or (args.turn > 1 and len(scenarios) != 1):
            parser.error("select one valid scenario for a later turn")
        trace = json.loads(args.trace.read_text()) if args.trace else None
        packets = [export_packet(fixture, s, args.turn, trace) for s in scenarios]
        output = {"status": "EXPORTED_NOT_EXECUTED", "packet_count": len(packets), "packets": packets}
    else:
        if not args.observations:
            parser.error("score requires --observations")
        output = score(fixture, json.loads(args.observations.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), **{k: v for k, v in output.items()
                                                  if k in {"status", "packet_count", "summary"}}}))


if __name__ == "__main__":
    main()
