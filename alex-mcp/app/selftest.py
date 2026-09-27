from __future__ import annotations

import json
import os
import sqlite3
import uuid

import db
import services
import profile_config
import phase2_finance
import phase2_work
import phase2_library
import phase2_delegation
import phase2_schedule
from config import DATA_DIR, get_settings

REQUIRED_TABLES = {
    "inbound_messages","users","user_phone_history","spaces","memberships","routing_rules",
    "financial_events","financial_event_corrections","media_objects","event_media_links",
    "saved_items","shopping_items","reminders","savings_goals","money_buckets","leave_state",
    "work_roster","leave_records","diary_events","diary_reminder_links","plans","schedule_conflicts",
    "cashflow_baselines","selection_sets","reminder_events","monitor_notifications",
    "alex_profile_config_versions","alex_phase2_goals","alex_phase2_goal_baseline_versions",
    "alex_phase2_goal_period_targets","alex_phase2_goal_contributions","alex_phase2_cash_events",
    "alex_phase2_cash_allocations","alex_phase2_cash_pools","alex_phase2_cash_pool_allocations",
    "alex_phase2_plan_reserves","alex_phase2_obligation_instances","alex_phase2_evidence_facts",
    "alex_phase2_work_events","alex_phase2_assets","alex_phase2_asset_documents",
    "alex_phase2_delegations","alex_phase2_leave_plans","alex_phase2_commitments",
    "conversation_turns","outbound_messages","tool_audit","ai_usage","diagnostic_runs",
}


def run() -> dict:
    checks = []
    db.initialize()
    profile_config.ensure_schema()
    phase2_finance.ensure_schema()
    phase2_work.ensure_schema()
    phase2_library.ensure_schema()
    phase2_delegation.ensure_schema()
    phase2_schedule.ensure_schema()
    conn = db.connect()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        checks.append({"name": "schema", "ok": not missing, "detail": missing or "all required tables present"})

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        checks.append({"name": "wal", "ok": str(mode).lower() == "wal", "detail": str(mode)})

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks.append({"name": "foreign_keys", "ok": not fk, "detail": len(fk)})

        calc = services.calculate("2 + 3 * 4")
        checks.append({"name": "calculator", "ok": calc.get("result") == 14, "detail": calc})

        settings = get_settings()
        checks.append({"name": "configuration_loader", "ok": bool(settings.timezone), "detail": settings.ai_provider})

        passed = sum(1 for c in checks if c["ok"])
        failed = len(checks) - passed
        report = {"passed": passed, "failed": failed, "checks": checks}
        conn.execute(
            "INSERT INTO diagnostic_runs(run_id,run_type,passed,failed,report_json) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), "STARTUP_CORE", passed, failed, json.dumps(report)),
        )
        conn.commit()
    finally:
        conn.close()

    path = os.path.join(DATA_DIR, "selftest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result))
    raise SystemExit(0 if result["failed"] == 0 else 1)
