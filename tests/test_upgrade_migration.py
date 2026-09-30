import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import db


LEGACY_LEAVE_TABLE = """
CREATE TABLE leave_records (
    leave_id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    leave_date TEXT NOT NULL,
    end_date TEXT,
    leave_type TEXT NOT NULL DEFAULT 'ANNUAL_LEAVE',
    status TEXT NOT NULL DEFAULT 'PLANNED',
    portion TEXT NOT NULL DEFAULT 'FULL',
    note TEXT,
    source_diary_id TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id,leave_date,portion)
);
"""


class UpgradeMigrationTests(unittest.TestCase):
    def test_pre_052_leave_table_upgrades_before_scope_index_creation(self):
        """A real pre-0.5.2 DB must boot without requiring a fresh database."""
        with tempfile.TemporaryDirectory(prefix="alex-upgrade-") as tmp:
            path = os.path.join(tmp, "alex_mcp.db")
            conn = sqlite3.connect(path)
            try:
                conn.executescript(LEGACY_LEAVE_TABLE)
                conn.execute(
                    """INSERT INTO leave_records(
                           leave_id,action_key,owner_id,leave_date,status,portion
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        "legacy-leave",
                        "legacy-action",
                        "USR_HUSBAND",
                        "2026-09-30",
                        "PLANNED",
                        "FULL",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(db, "DATA_DIR", tmp), patch.object(db, "DB_PATH", path):
                db.initialize()

                conn = db.connect()
                try:
                    columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(leave_records)").fetchall()
                    }
                    self.assertIn("space_id", columns)

                    row = conn.execute(
                        "SELECT space_id FROM leave_records WHERE leave_id='legacy-leave'"
                    ).fetchone()
                    self.assertEqual(row["space_id"], "HUSBAND_PVT")

                    indexes = {
                        row["name"]
                        for row in conn.execute("PRAGMA index_list(leave_records)").fetchall()
                    }
                    self.assertIn("idx_leave_owner_space_date", indexes)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
