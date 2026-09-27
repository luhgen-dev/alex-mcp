#!/usr/bin/env python3
import db
import profile_config
import phase2_finance
import phase2_work
import phase2_library
import phase2_delegation
import phase2_schedule

if __name__ == "__main__":
    db.initialize()
    # Reuse the already-proven Phase-2 deterministic engines, but make their
    # stable household facts editable from Home Assistant options.
    profile_config.ensure_schema()
    profile_config.sync_from_ha()
    phase2_finance.ensure_schema()
    phase2_work.ensure_schema()
    phase2_library.ensure_schema()
    phase2_delegation.ensure_schema()
    phase2_schedule.ensure_schema()
    print("[Alex MCP] database and household profiles ready")
