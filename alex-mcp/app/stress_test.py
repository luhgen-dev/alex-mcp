#!/usr/bin/env python3
"""Offline Phase-3-style stress harness for Alex MCP.

No provider, WhatsApp or Home Assistant network call is made. The harness
attacks the deterministic trust boundary: duplicate delivery, concurrent writes,
privacy, repeated receipts, reminder durability, numbered retrieval and the
advanced Phase-2 finance/work engines.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

sandbox = tempfile.TemporaryDirectory(prefix="alex-mcp-stress-")
os.environ["ALEX_DATA_DIR"] = sandbox.name
os.environ["ALEX_OPTIONS_PATH"] = str(Path(sandbox.name) / "options.json")
Path(os.environ["ALEX_OPTIONS_PATH"]).write_text(json.dumps({
    "ai_provider": "grok",
    "xai_api_key": "",
    "husband_phone": "+60111111111",
    "wife_phone": "+60222222222",
    "timezone": "Asia/Kuala_Lumpur",
    "ocr_enabled": False,
    "context_turns": 8,
    "income_profiles": [{
        "id": "salary", "owner": "husband", "visibility": "private",
        "name": "Base salary", "amount": 5000, "currency": "MYR",
        "income_class": "fixed", "frequency": "monthly",
        "payday_day": 25, "active": True
    }],
    "roster_profiles": [{
        "id": "roster", "owner": "husband", "visibility": "private",
        "name": "Alternating", "cycle_start": "2026-09-21",
        "pattern": "evening,morning", "day_start": "07:45", "day_end": "16:15",
        "evening_start": "16:30", "evening_end": "01:00", "active": True
    }],
    "overtime_profiles": [{
        "id": "ot", "owner": "husband", "visibility": "private",
        "name": "OT", "currency": "MYR", "morning_pre_hours": 2,
        "morning_pre_start": "05:45", "morning_weekend_standard_start": "04:45",
        "morning_weekend_standard_end": "16:45", "morning_weekend_low_start": "05:45",
        "morning_weekend_low_end": "15:45", "evening_post_hours": 2.75,
        "evening_post_start": "01:00", "evening_post_end": "04:15",
        "evening_weekend_days": "saturday", "evening_weekend_standard_start": "16:15",
        "evening_weekend_standard_end": "04:15", "evening_weekend_low_start": "16:15",
        "evening_weekend_low_end": "23:59", "sunday_override_allowed": True,
        "payout_days": "7,12", "rate_formula": "", "active": True
    }],
    "leave_balances": [],
    "recurring_payments": [{
        "id": "power", "owner": "family", "visibility": "family",
        "name": "TNB electricity bill", "amount_type": "fixed", "amount": 120,
        "currency": "MYR", "due_day": 5, "frequency": "monthly", "active": True
    }],
    "account_aliases": [],
    "reminder_preferences": [],
    "presence_mappings": []
}), encoding="utf-8")

import db
import media
import phase2
import phase2_finance
import phase2_work
import profile_config
import services
from context import with_action_key


def claim(mid: str, phone: str, text: str = "", conversation_type: str = "DIRECT_DM",
          conversation_id: str | None = None):
    db.claim_inbound({
        "message_id": mid,
        "provider": "WHATSAPP",
        "conversation_id": conversation_id or (
            "family@g.us" if conversation_type == "GROUP"
            else phone.replace("+", "") + "@s.whatsapp.net"
        ),
        "conversation_type": conversation_type,
        "sender_phone": phone,
        "text": text,
    })


def actor(mid: str, phone: str, media_ids=None, conversation_type="DIRECT_DM",
          conversation_id=None):
    return db.resolve_actor(
        phone,
        conversation_id or (
            "family@g.us" if conversation_type == "GROUP"
            else phone.replace("+", "") + "@s.whatsapp.net"
        ),
        conversation_type, mid, media_ids or [],
    )


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main() -> dict:
    db.initialize()
    profile_config.ensure_schema()
    profile_config.sync_from_ha()
    phase2_finance.ensure_schema()
    phase2_work.ensure_schema()

    checks = []

    # 1. Serial idempotency: same logical action is never written twice.
    for i in range(100):
        mid = f"idem-{i}"
        claim(mid, "+60111111111", f"coffee {i}")
        a = with_action_key(actor(mid, "+60111111111"), f"idem-action-{i}")
        first = services.log_expense(a, f"Coffee {i}", 1.23, "food", currency="MYR")
        second = services.log_expense(a, f"Coffee {i}", 1.23, "food", currency="MYR")
        assert_true(first["event_id"] == second["event_id"], "idempotent event mismatch")
    qmid = "idem-query"
    claim(qmid, "+60111111111", "total")
    assert_true(services.query_finances(actor(qmid, "+60111111111"))["count"] == 100,
                "idempotency produced duplicate expenses")
    checks.append("100 repeated writes idempotent")

    # 2. Concurrent independent writes under WAL/busy_timeout.
    def concurrent_write(i):
        mid = f"conc-{i}"
        claim(mid, "+60111111111", f"concurrent {i}")
        a = with_action_key(actor(mid, "+60111111111"), f"conc-action-{i}")
        return services.log_expense(a, f"Concurrent {i}", 2, "food", currency="MYR")["event_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = [f.result() for f in as_completed([pool.submit(concurrent_write, i) for i in range(40)])]
    assert_true(len(ids) == 40 and len(set(ids)) == 40, "concurrent writes lost or duplicated")
    checks.append("40 concurrent WAL writes unique")

    # 3. Privacy: wife's DM cannot read husband's private records.
    wmid = "wife-private-check"
    claim(wmid, "+60222222222", "spend")
    wife_total = services.query_finances(actor(wmid, "+60222222222"))
    assert_true(wife_total["count"] == 0, "private husband expenses leaked to wife")
    gmid = "group-shared"
    claim(gmid, "+60111111111", "TNB", "GROUP", "family@g.us")
    ga = with_action_key(actor(gmid, "+60111111111", conversation_type="GROUP",
                                  conversation_id="family@g.us"), "group-action")
    shared = services.log_expense(ga, "TNB electricity bill", 120, "utilities", currency="MYR")
    assert_true(shared["space"] == "FAMILY_SHARED", "group write not forced shared")
    wife_total = services.query_finances(actor(wmid, "+60222222222"), search="TNB")
    assert_true(wife_total["count"] == 1, "shared family record not visible to wife")
    checks.append("DM/group privacy boundary held")

    # 4. Same-looking recurring receipts remain separate and exact originals retrievable.
    receipt_ids = []
    raw = base64.b64encode(b"same recurring receipt template").decode("ascii")
    for i in range(40):
        mid = f"receipt-{i}"
        claim(mid, "+60111111111", "")
        media_id = media.save_media(mid, "IMAGE", "image/jpeg", raw)
        receipt_ids.append(media_id)
        a = with_action_key(actor(mid, "+60111111111", [media_id]), f"receipt-action-{i}")
        services.log_expense(
            a, "TNB electricity bill", 120, "utilities", currency="MYR",
            event_date_local=f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}T09:00:00+08:00",
            reference=f"REF-STRESS-{i:03d}",
        )
    assert_true(len(set(receipt_ids)) == 40, "identical receipts collapsed by content")
    rmid = "receipt-find"
    claim(rmid, "+60111111111", "find REF-STRESS-017")
    found = services.find_receipts(actor(rmid, "+60111111111"), query="REF-STRESS-017")
    assert_true(len(found["matches"]) == 1, "reference did not disambiguate receipt")
    exact = services.get_receipt(actor(rmid, "+60111111111"), found["matches"][0]["media_id"])
    assert_true(exact["status"] == "found" and exact.get("_attachments"), "original receipt unavailable")
    checks.append("40 recurring receipts remained independently retrievable")

    # 5. Explicit memory is separate and numbered follow-up resolves exact result.
    for i in range(20):
        mid = f"memory-{i}"
        claim(mid, "+60111111111", f"save key note {i}")
        a = with_action_key(actor(mid, "+60111111111"), f"memory-action-{i}")
        services.save_item(a, f"Keys {i}", f"Key location note {i}", tags="keys")
    mmid = "memory-find"
    claim(mmid, "+60111111111", "find saved keys")
    ma = actor(mmid, "+60111111111")
    matches = services.search_saved_items(ma, "keys", limit=20)
    assert_true(len(matches["matches"]) == 20, "saved-memory search lost records")
    selected = services.resolve_numbered_choice(ma, 7)
    assert_true(selected["item_id"] == matches["matches"][6]["item_id"],
                "numbered selection drifted from presented list")
    checks.append("stable numbered memory retrieval")

    # 6. Reminders survive large insert volume and spouse routing remains deterministic.
    for i in range(80):
        mid = f"rem-{i}"
        claim(mid, "+60111111111", "reminder")
        a = with_action_key(actor(mid, "+60111111111"), f"rem-action-{i}")
        services.create_reminder(
            a, f"Task {i}", f"2026-12-{(i % 27) + 1:02d}T09:00:00+08:00",
            recipient="wife" if i % 10 == 0 else "me",
        )
    conn = db.connect()
    try:
        assert_true(conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 80,
                    "reminder writes missing/duplicated")
        spouse = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE owner_id='USR_WIFE'"
        ).fetchone()[0]
        assert_true(spouse == 8, "spouse reminder routing count wrong")
    finally:
        conn.close()
    checks.append("80 durable reminders with spouse routing")

    # 7. Work conflict cannot silently create leave.
    rmid = "stress-roster"
    claim(rmid, "+60111111111", "work")
    ra = with_action_key(actor(rmid, "+60111111111"), "stress-roster-action")
    phase2.set_work_roster(
        ra, "2026-11-02", "Day",
        "2026-11-02T08:00:00+08:00", "2026-11-02T17:00:00+08:00",
    )
    dmid = "stress-diary"
    claim(dmid, "+60111111111", "appointment")
    da = with_action_key(actor(dmid, "+60111111111"), "stress-diary-action")
    pending = phase2.add_diary_event(
        da, "Appointment", "2026-11-02T10:00:00+08:00",
        "2026-11-02T11:00:00+08:00",
    )
    assert_true(pending["status"] == "needs_choice", "work clash was not gated")
    conn = db.connect()
    try:
        assert_true(conn.execute("SELECT COUNT(*) FROM leave_records").fetchone()[0] == 0,
                    "leave created before explicit conflict choice")
    finally:
        conn.close()
    resolved = phase2.resolve_diary_conflict(da, pending["conflict_id"], 1)
    assert_true(resolved["leave"]["state"] == "PLANNED", "choice 1 did not create PLANNED leave")
    checks.append("work conflict required explicit choice before planned leave")

    # 8. Mature finance engine: variable cash remains unallocated and bills are not
    # labelled unpaid merely for lack of receipt.
    goal = phase2_finance.create_goal(
        "Stress goal", 5000, 200, "+60111111111", visibility="private"
    )
    cash = phase2_finance.record_cash_event(
        "OT", 400, "2026-09-27", "+60111111111", visibility="private"
    )
    status = phase2_finance.cash_event_status(cash["cash_event_id"], "+60111111111")
    assert_true(status["allocation_state"] == "UNALLOCATED", "OT auto-allocated")
    assert_true(
        phase2_finance.goal_progress(goal["goal_id"], "+60111111111")["baseline_monthly"] == 200,
        "goal baseline mutated",
    )
    phase2_finance.ensure_obligation_instances("2026-09", "+60111111111")
    phase2_finance.refresh_obligation_states("2026-09-27", "+60111111111")
    obligations = phase2_finance.list_obligations("+60111111111", period="2026-09")
    assert_true(obligations and obligations[0]["state"] == "UNCONFIRMED_DUE",
                "missing receipt was treated as confirmed unpaid")
    checks.append("variable cash and obligation states stayed conservative")

    # 9. Database integrity after the workload.
    conn = db.connect()
    try:
        assert_true(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "quick_check failed")
        assert_true(not conn.execute("PRAGMA foreign_key_check").fetchall(), "foreign-key violation")
        totals = {
            "financial_events": conn.execute("SELECT COUNT(*) FROM financial_events").fetchone()[0],
            "media_objects": conn.execute("SELECT COUNT(*) FROM media_objects").fetchone()[0],
            "reminders": conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
            "saved_items": conn.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0],
        }
    finally:
        conn.close()
    checks.append("SQLite quick_check and foreign keys clean")

    report = {
        "status": "PASS",
        "checks": checks,
        "totals": totals,
        "note": "Offline deterministic Phase-3-style stress only; provider/WhatsApp/HA live behavior is MCP Check.",
    }
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    try:
        main()
    finally:
        sandbox.cleanup()
