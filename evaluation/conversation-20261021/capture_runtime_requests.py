"""Temporary private worker: capture existing fixture's actual first brain request."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--fixture", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
repo = args.repo.resolve()
sys.path.insert(0, str(repo / "alex-mcp" / "app"))
sys.path.insert(0, str(repo / "tests"))
spec = importlib.util.spec_from_file_location("runtime_packet_fixture", args.fixture.resolve())
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ConversationFixture.setUpClass()
rows = []
for number, phrase in enumerate(module.FOLLOWUP_VARIATIONS, 1):
    case = module.ConversationFixture()
    case.setUp()
    try:
        _, pending, _ = case.draft()
        current = case.actor("cr-varied-answer", phrase)
        fixed = os.environ.get("ALEX_CERT_NOW")
        sqlite_wall_clock = None
        if fixed:
            # Only align the disposable rows seeded above. SQLite's built-in
            # CURRENT_TIMESTAMP is separate from the Python certification clock.
            # No runtime/fixture source or post-action result is changed.
            conn = module.db.connect()
            try:
                sqlite_wall_clock = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                for table, column in (("pending_items", "created_at_utc"),
                                      ("conversation_turns", "created_at_utc"),
                                      ("outbound_messages", "created_at_utc"),
                                      ("inbound_messages", "received_at_utc")):
                    conn.execute(f"UPDATE {table} SET {column}=?", (fixed,))
                conn.commit()
                pending = dict(conn.execute("SELECT * FROM pending_items WHERE item_id=?",
                                            (pending["item_id"],)).fetchone())
            finally:
                conn.close()
        capture_clock = module.core.runtime_clock.utc_iso()
        request = case.capture_request(current, phrase)
        rows.append({"variation": number, "phrase": phrase, "request": request,
                     "fixture_pending_record": pending,
                     "actor_received_at_utc": current.received_at_utc,
                     "seeded_fixture_timestamps_aligned": bool(fixed),
                     "sqlite_wall_clock_at_seed": sqlite_wall_clock,
                     "runtime_clock_before_capture": capture_clock,
                     "runtime_clock_after_capture": module.core.runtime_clock.utc_iso()})
    finally:
        case.tearDown()
digest = hashlib.sha256()
for path in sorted((repo / "alex-mcp" / "app").rglob("*.py")):
    digest.update(str(path.relative_to(repo)).encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
args.output.write_text(json.dumps({
    "repo": str(repo), "revision": revision,
    "runtime_source_sha256": digest.hexdigest(),
    "clock_env": {"ALEX_CERT_NOW": os.environ.get("ALEX_CERT_NOW")}, "rows": rows,
}, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"captured_packets": len(rows), "output": str(args.output)}))
