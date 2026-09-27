#!/usr/bin/env python3
"""Offline Phase-3-style stress harness for Alex MCP.

No provider, WhatsApp or Home Assistant network call is made. The harness
attacks the deterministic trust boundary: duplicate delivery, concurrent writes,
privacy, repeated receipts, reminder durability, numbered retrieval and the
advanced Phase-2 finance/work engines.
"""
from __future__ import annotations

import asyncio
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
import phase2_library
import phase2_delegation
import phase2_monitor
import phase2_presence
import phase2_reports
import profile_config
import services
import brain
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
    phase2_library.ensure_schema()
    phase2_delegation.ensure_schema()

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

    # 9. Advanced work rules: absence eligibility blocks default OT while
    # observed OT_WORKED remains an immutable historical fact.
    phase2_work.record_work_event(
        "ANNUAL_LEAVE", "2026-10-02", "+60111111111", units_days=1
    )
    blocked = phase2_work.effective_ot_for_date("2026-10-03", "+60111111111")
    assert_true(not blocked["ot"], "Friday absence did not block weekend OT")
    assert_true(
        blocked["eligibility"]["reason"] == "FRIDAY_ABSENCE_BLOCKS_WEEKEND_OT",
        "wrong OT absence eligibility reason",
    )
    phase2_work.record_work_event(
        "OT_WORKED", "2026-10-03", "+60111111111",
        start_time="06:00", end_time="10:00", hours=4,
        note="Observed actual work",
    )
    observed = phase2_work.effective_ot_for_date("2026-10-03", "+60111111111")
    assert_true(observed["ot"] and observed["kind"] == "OT_WORKED",
                "observed OT was overwritten by policy")
    checks.append("advanced roster/OT eligibility preserved observed facts")

    # 10. One-period goal exceptions and stash allocation never rewrite the
    # recurring plan.
    period_goal = phase2_finance.create_goal(
        "Period goal", 6000, 200, "+60111111111", visibility="private"
    )
    phase2_finance.set_goal_period_target(
        period_goal["goal_id"], "2026-09", 100, "+60111111111",
        reason="stress one-month exception",
    )
    dev = phase2_finance.evaluate_goal_deviation(
        period_goal["goal_id"], 100, "2026-09", "+60111111111"
    )
    assert_true(dev["status"] == "ON_PLAN", "period target override ignored")
    assert_true(
        phase2_finance.goal_progress(period_goal["goal_id"], "+60111111111")["baseline_monthly"] == 200,
        "period override rewrote recurring baseline",
    )
    bonus = phase2_finance.record_cash_event(
        "BONUS", 500, "2026-09-27", "+60111111111", visibility="private"
    )
    stash = phase2_finance.create_cash_pool(
        "Stress stash", "+60111111111", visibility="private"
    )
    phase2_finance.allocate_cash_to_pool(
        bonus["cash_event_id"], stash["pool_id"], 200, "+60111111111"
    )
    assert_true(
        phase2_finance.cash_pool_balance(stash["pool_id"], "+60111111111")["balance"] == 200,
        "stash allocation incorrect",
    )
    assert_true(
        phase2_finance.cash_event_status(bonus["cash_event_id"], "+60111111111")["unallocated"] == 300,
        "unallocated extra cash was not preserved",
    )
    checks.append("period goal exception and stash allocation stayed owner-controlled")

    # 11. Diary overlap tickets, linked-reminder choices, and family-plan
    # publishing all preserve explicit-choice/privacy contracts.
    claim("plan-private", "+60111111111", "plan")
    pa = with_action_key(actor("plan-private", "+60111111111"), "plan-private-action")
    private_plan = phase2.create_plan(
        pa, "Private holiday", "2026-12-20T09:00:00+08:00",
        notes="private budget RM9999"
    )
    shared_plan = phase2.share_plan(
        with_action_key(pa, "plan-share-action"), private_plan["plan_id"]
    )
    conn = db.connect()
    try:
        copied = conn.execute(
            "SELECT space_id,notes FROM plans WHERE plan_id=?",
            (shared_plan["plan_id"],),
        ).fetchone()
        assert_true(copied["space_id"] == "FAMILY_SHARED", "plan copy not shared")
        assert_true(copied["notes"] is None, "private plan notes leaked to family")
    finally:
        conn.close()

    claim("diary-base", "+60111111111", "event")
    ba = with_action_key(actor("diary-base", "+60111111111"), "diary-base-action")
    base_event = phase2.add_diary_event(
        ba, "Wedding", "2026-12-21T18:00:00+08:00",
        "2026-12-21T21:00:00+08:00", reminder_minutes_before=60,
    )
    assert_true(base_event["status"] == "created", "base diary event failed")
    claim("diary-overlap", "+60111111111", "party")
    oa = with_action_key(actor("diary-overlap", "+60111111111"), "diary-overlap-action")
    overlap = phase2.add_diary_event(
        oa, "Party", "2026-12-21T19:00:00+08:00",
        "2026-12-21T20:00:00+08:00",
    )
    assert_true(overlap["status"] == "needs_choice", "diary overlap was not gated")
    assert_true(overlap["conflict_kind"] == "DIARY", "wrong diary conflict kind")
    assert_true(bool(overlap.get("expires_at_utc")), "conflict ticket lacks expiry")

    ask_move = phase2.update_diary_event(
        ba, base_event["diary_id"], start_local="2026-12-21T17:00:00+08:00"
    )
    assert_true(ask_move["status"] == "needs_reminder_choice",
                "linked reminder moved without explicit choice")
    checks.append("diary conflicts, linked reminders and family-plan privacy held")

    # 12. Household library keeps private warranty/manual metadata private.
    asset = phase2_library.create_asset(
        "Private appliance", "+60111111111", visibility="private",
        warranty_end="2026-10-10",
    )
    phase2_library.link_document(
        asset["asset_id"], "MANUAL", "media:stress-manual", "+60111111111"
    )
    assert_true(
        len(phase2_library.list_assets("+60111111111", include_documents=True)) == 1,
        "owner cannot retrieve private asset",
    )
    assert_true(
        phase2_library.list_assets("+60222222222") == [],
        "private asset leaked to spouse",
    )
    checks.append("asset/manual/warranty privacy boundary held")

    # 13. Proactive monitoring remains completely silent until explicitly delegated.
    assert_true(
        phase2_monitor.ot_allocation_candidates("+60111111111") == [],
        "monitor produced candidate without delegation",
    )
    phase2_delegation.create_delegation(
        "OT_GOAL_TRACK", "Stress goal", "+60111111111", "stress-delegation",
        visibility="private", explicit_user_instruction=True,
    )
    delegated = phase2_monitor.ot_allocation_candidates("+60111111111")
    assert_true(bool(delegated), "explicit delegation did not enable monitor candidate")
    assert_true(
        all(not x.get("automatic_allocation") for x in delegated),
        "monitor auto-allocated variable cash",
    )
    checks.append("proactive monitors stayed silent until explicit delegation")

    # 14. Reminder state history is durable and delivery policy is conservative.
    claim("rem-history", "+60111111111", "remind")
    rh_actor = with_action_key(actor("rem-history", "+60111111111"), "rem-history-action")
    rh = services.create_reminder(
        rh_actor, "Stress history task", "2026-12-30T09:00:00+08:00",
        follow_up_after_hours=24,
    )
    services.update_reminder(rh_actor, rh["reminder_id"], "ack")
    history = services.reminder_history(rh_actor, rh["reminder_id"])["history"]
    assert_true(
        [x["event_type"] for x in history][:2] == ["CREATED", "ACKNOWLEDGED"],
        "reminder transition history missing",
    )
    routine = phase2_presence.delivery_decision(
        {"quiet_start": "22:00", "quiet_end": "06:00", "presence_aware": True},
        "away",
        __import__("datetime").datetime(2026, 9, 27, 23, 0,
                                         tzinfo=__import__("datetime").timezone.utc),
        "routine",
    )
    critical = phase2_presence.delivery_decision(
        {"quiet_start": "22:00", "quiet_end": "06:00", "presence_aware": True},
        "away",
        __import__("datetime").datetime(2026, 9, 27, 23, 0,
                                         tzinfo=__import__("datetime").timezone.utc),
        "time_critical",
    )
    assert_true(routine["decision"] == "DEFER", "routine quiet-hours policy failed")
    assert_true(critical["decision"] == "DELIVER", "time-critical reminder was deferred")
    checks.append("reminder history and quiet/time-critical policy held")

    # 15. Tool exposure stays tiny while typo-heavy language retains an AI
    # discovery escape hatch.
    scenarios = [
        "how much did I spend this weekend",
        "remind me tomorrow 9 pay electricity",
        "turn off living room light",
        "allocate extra cash to Europe goal",
        "show receipt ref 1234",
        "what shift next week",
        "move dentist appointment Friday",
        "save this photo for me",
        "monitor my Stress goal",
        "export my finance report as pdf",
        "நாளைக்கு 9 மணிக்கு பில் கட்ட நினைவூட்டு",
    ]
    for phrase in scenarios:
        selected = brain._select_tool_names(
            phrase, ["attachment"] if "photo" in phrase else None
        )
        assert_true(
            len(selected) <= brain.TOOL_EXPOSURE_MAX,
            f"tool exposure exceeded cap for {phrase}: {selected}",
        )
    explicit_save = brain._select_tool_names(
        "Save this as my payment receipt", ["OCR receipt MYR 10"]
    )
    assert_true("save_item" in explicit_save and "log_expense" not in explicit_save,
                "explicit save still exposed finance write")
    typo_specs = asyncio.run(
        brain._tool_specs("alx plz remidn me tmrw 9 pay elctrcity")
    )
    typo_names = [x["function"]["name"] for x in typo_specs]
    assert_true(
        brain.DISCOVERY_TOOL_NAME in typo_names and len(typo_names) <= brain.TOOL_EXPOSURE_MAX,
        "typo discovery valve missing or too large",
    )
    checks.append("six-tool token cap and typo discovery valve held")

    # 16. Latest-record semantics and privacy-safe report generation survive
    # the accumulated workload.
    for idx, amount in enumerate((4.0, 8.5), 1):
        mid = f"latest-stress-{idx}"
        claim(mid, "+60111111111", "special coffee")
        la = with_action_key(actor(mid, "+60111111111"), f"latest-stress-action-{idx}")
        services.log_expense(
            la, "Latest special coffee", amount, "food", currency="MYR",
            event_date_local=(
                "2026-09-25T08:00:00+08:00" if idx == 1
                else "2026-09-27T08:00:00+08:00"
            ),
        )
    claim("latest-stress-query", "+60111111111", "coffee just now")
    latest = services.query_finances(
        actor("latest-stress-query", "+60111111111"),
        search="Latest special coffee",
    )
    assert_true(latest["latest_record"]["amount"] == 8.5,
                "latest transaction drifted to aggregate/older record")
    snapshot = phase2_reports.build_snapshot(
        "+60111111111", "DIRECT_DM", "all", "2026-09",
        include_raw_income=False,
    )
    assert_true(
        snapshot["baseline_plan"]["fixed_income_monthly"] is None,
        "privacy-safe report exposed raw private salary",
    )
    assert_true(
        phase2_reports.minimal_pdf(snapshot).startswith(b"%PDF-1.4"),
        "local report PDF failed",
    )
    checks.append("latest-record semantics and privacy-safe reports held")

    # 17. Database integrity after the workload.
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
    checks.append("SQLite quick_check and foreign keys clean after extended stress")

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
