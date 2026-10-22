"""Transactional upgrade of legacy SQLite media CHECK constraints.

CREATE TABLE IF NOT EXISTS does not change an installed table's CHECK clauses.
Keep the existing table definition (including historical additive columns),
replace only the known type enum, and retain all archive rows/references.
"""
from __future__ import annotations

import re
import sqlite3


_TYPES = {
    "media_objects": ("media_type", ("IMAGE", "AUDIO", "VIDEO", "PDF", "DOCUMENT")),
    "outbound_messages": ("kind", ("TEXT", "IMAGE", "AUDIO", "VIDEO", "DOCUMENT")),
}


def ensure_original_media_types(conn: sqlite3.Connection) -> list[str]:
    """Expand old media enums atomically, restoring the caller's FK settings.

    Startup must call this outside a transaction. Unknown constraints or failed
    copy/integrity checks abort the upgrade; no rows or original files are lost.
    """
    plans = []
    for table, (column, expected) in _TYPES.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not row:
            continue
        sql = row[0]
        pattern = rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)\s*\)"
        match = re.search(pattern, sql, re.I)
        if not match:
            raise RuntimeError(f"Cannot safely migrate {table}: its media type constraint is unrecognized")
        actual = set(re.findall(r"'([^']+)'", match.group(1)))
        if actual == set(expected):
            continue
        if not actual or not actual.issubset(expected):
            raise RuntimeError(f"Cannot safely migrate {table}: unexpected media types")
        replacement = f"CHECK({column} IN (" + ",".join("'" + value + "'" for value in expected) + "))"
        updated = sql[:match.start()] + replacement + sql[match.end():]
        plans.append((table, updated))
    if not plans:
        return []
    if conn.in_transaction:
        raise RuntimeError("Media constraint upgrade requires a startup transaction boundary")

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy_alter = conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, sql in plans:
            temporary = "_alex_media_upgrade_" + table
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (temporary,)).fetchone():
                raise RuntimeError("Media migration temporary table already exists; refusing to overwrite it")
            dependent_sql = [row[0] for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
                (table,),
            ).fetchall()]
            # Names here are fixed internal constants, never user input.
            definition, count = re.subn(
                rf'^(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)["`\[]?{table}["`\]]?',
                lambda match: match.group(1) + '"' + temporary + '"', sql, count=1, flags=re.I,
            )
            if count != 1:
                raise RuntimeError(f"Cannot safely rebuild {table}: unrecognized CREATE statement")
            conn.execute(definition)
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            conn.execute(f'INSERT INTO "{temporary}" ({names}) SELECT {names} FROM "{table}"')
            old_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            copied_count = conn.execute(f'SELECT COUNT(*) FROM "{temporary}"').fetchone()[0]
            if old_count != copied_count:
                raise RuntimeError("Media migration row-count verification failed")
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
            for statement in dependent_sql:
                conn.execute(statement)
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Media migration foreign-key verification failed; original schema restored")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA legacy_alter_table={int(legacy_alter)}")
        conn.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")
    return [table for table, _ in plans]
