"""Three isolated full-ingress lifecycle probes; scripted model, real MCP/backend.

These use a current aligned clock, not the frozen direct-brain review packets.
Only the due_local clock is translated for the two reviewed relative proposals.
"""
import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback
from unittest.mock import patch
from zoneinfo import ZoneInfo


parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--fixture", type=Path, required=True)
parser.add_argument("--mapping", type=Path, required=True)
parser.add_argument("--decisions", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--allow-runtime-source-change", action="store_true",
                    help="Explicit post-fix regression run; preserve capture and current hashes separately")
args = parser.parse_args()
now = datetime.now(timezone.utc).replace(microsecond=0)
os.environ["ALEX_CERT_NOW"] = now.isoformat()
mapping = json.loads(args.mapping.read_text())
decisions_bytes = args.decisions.read_bytes()
decisions = {d["packet_id"]: d for d in json.loads(decisions_bytes)}
repo = args.repo.resolve()
sys.path.insert(0, str(repo / "alex-mcp" / "app"))
sys.path.insert(0, str(repo / "tests"))
spec = importlib.util.spec_from_file_location("lifecycle_probe_fixture", args.fixture.resolve())
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ConversationFixture.setUpClass()
digest = hashlib.sha256()
for path in sorted((repo / "alex-mcp" / "app").rglob("*.py")):
    digest.update(str(path.relative_to(repo)).encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
source_hash = digest.hexdigest()
captured_hash = mapping["provenance"]["candidate"]["runtime_source_sha256"]
assert args.allow_runtime_source_change or source_hash == captured_hash

cases = []
for number in (2, 5):
    entry = next(p for p in mapping["packets"] if p["revision_label"] == "candidate"
                 and p["variation_index"] == number)
    proposal = decisions[entry["packet_id"]]
    assert len(proposal["tool_calls"]) == 1
    original = proposal["tool_calls"][0]
    assert original["name"] == "create_reminder"
    translated = copy.deepcopy(original)
    due = datetime.fromisoformat(original["arguments"]["due_local"])
    if due.tzinfo is None:
        due = due.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    delay = due.astimezone(timezone.utc) - datetime.fromisoformat(entry["actor_received_at_utc"])
    translated["arguments"]["due_local"] = (
        now + delay).astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(tzinfo=None).isoformat()
    cases.append({"case": f"continuation_unclear_variation_{number}",
                  "input_phrase": entry["input_phrase"], "review_packet_id": entry["packet_id"],
                  "original_reviewed_call": original, "translated_call": translated,
                  "relative_delay_seconds": delay.total_seconds()})
cases.append({"case": "independent_new_reminder_with_old_draft",
              "input_phrase": "New reminder: remind me to call the dentist in forty minutes.",
              "review_packet_id": None, "original_reviewed_call": None,
              "translated_call": {"name": "create_reminder", "arguments": {
                  "task": "call the dentist", "due_local": (
                      now + timedelta(minutes=40)).astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(tzinfo=None).isoformat(),
                  "recipient": "me"}},
              "relative_delay_seconds": 2400})


def snapshot():
    conn = module.db.connect()
    try:
        tables = ("pending_items", "reminders", "reminder_events", "tool_audit",
                  "tool_execution_claims", "inbound_messages", "outbound_messages",
                  "conversation_turns", "tasks", "saved_items", "shopping_items", "financial_events")
        return {"python_clock": module.core.runtime_clock.utc_iso(),
                "sqlite_wall_clock": conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0],
                "rows": {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}}
    finally:
        conn.close()


rows = []
for setup in cases:
    case = module.ConversationFixture()
    case.setUp()
    row = copy.deepcopy(setup)
    try:
        _, pending, _ = case.draft()
        row["old_pending_id"] = pending["item_id"]
        conn = module.db.connect()
        try:
            for table, column in (("pending_items", "created_at_utc"),
                                  ("conversation_turns", "created_at_utc"),
                                  ("outbound_messages", "created_at_utc"),
                                  ("inbound_messages", "received_at_utc")):
                conn.execute(f"UPDATE {table} SET {column}=?", (now.isoformat(),))
            conn.commit()
        finally:
            conn.close()
        row["before"] = snapshot()
        row["semantic_interpreter_calls"] = []
        row["brain_entry"] = []
        real_respond = module.brain.respond

        def unclear(actor, **kwargs):
            row["semantic_interpreter_calls"].append({
                "actor": asdict(actor),
                **{k: sorted(v) if isinstance(v, set) else v for k, v in kwargs.items()},
            })
            return {"intent": "UNCLEAR", "confidence": 0.0, "normalized_reply": ""}

        async def record_brain(actor, *positional, **kwargs):
            row["brain_entry"].append({"actor": asdict(actor), "args": positional, "kwargs": kwargs})
            return await real_respond(actor, *positional, **kwargs)

        call = setup["translated_call"]
        payload = case.payload("lifecycle-" + setup["case"], setup["input_phrase"],
                               sent_at_ms=int(now.timestamp() * 1000))
        row["payload"] = payload
        steps = [{"tool": call["name"], "args": call["arguments"]},
                 {"content": "I have checked the result."}]
        with patch.object(module.brain, "interpret_control_intent", side_effect=unclear), \
             patch.object(module.brain, "respond", side_effect=record_brain):
            result, requests = case.scripted(steps, payload=payload, allow_unexposed=True)
        row["ingress_result"] = result
        row["requests"] = requests
        row["tool_receipts"] = [m for m in requests[-1]["messages"] if m.get("role") == "tool"]
        row["metrics"] = case.offline_metrics
        row["worker_error"] = None
    except Exception:
        row["worker_error"] = traceback.format_exc()
    finally:
        row["after"] = snapshot()
        row["old_pending_after"] = next((p for p in row["after"]["rows"]["pending_items"]
                                         if p["item_id"] == row.get("old_pending_id")), None)
        row["created_reminder_count"] = len(row["after"]["rows"]["reminders"])
        rows.append(row)
        case.tearDown()
report = {
    "schema_version": 1,
    "evidence_kind": "Three scripted full-ingress lifecycle probes with real MCP/backend in a disposable DB; no live model, external delivery or independent ingress-reviewer evaluation",
    "repo": str(repo), "runtime_source_sha256": source_hash,
    "review_capture_runtime_source_sha256": captured_hash,
    "runtime_changed_since_review_capture": source_hash != captured_hash,
    "runtime_change_explicitly_allowed_for_postfix_regression": args.allow_runtime_source_change,
    "decisions_file_sha256": hashlib.sha256(decisions_bytes).hexdigest(),
    "clock_setup": {
        "python_clock_fixed_before_imports": now.isoformat(),
        "aligned_to_current_sqlite_wall_clock": True,
        "payload_sent_at_matches": True,
        "seeded_rows_timestamped_before_execution": True,
        "relative_proposal_due_shifted_by_clock_offset_only": True,
        "raw_and_translated_arguments_preserved": True,
        "actor_fields_changed_outside_normal_ingress": False,
        "pending_cleanup_or_action_result_rewrite": False,
    },
    "provider_setup": "Continuation interpreter deliberately returns UNCLEAR; create proposals are scripted, then one neutral terminal text. Tool receipts and DB rows determine outcomes.",
    "rows": rows,
    "counts": {"rows": len(rows), "worker_errors": sum(bool(r["worker_error"]) for r in rows),
               "reminders_created": sum(r["created_reminder_count"] for r in rows)},
}
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"output": str(args.output), **report["counts"]}))
