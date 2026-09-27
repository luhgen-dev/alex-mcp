from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from dateutil.rrule import rrulestr

from db import connect, utc_now


def fire_due():
    now = datetime.now(timezone.utc)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status='OPEN' AND due_at_utc<=?
               ORDER BY due_at_utc LIMIT 20""",
            (now.isoformat(),),
        ).fetchall()

        for row in rows:
            oid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO outbound_messages(
                    outbound_id,conversation_id,kind,text_body
                   ) VALUES(?,?, 'TEXT', ?)""",
                (oid, row["conversation_id"], f"⏰ Reminder: {row['task_text']}"),
            )
            recurrence = row["recurrence_rule"]
            if recurrence:
                try:
                    start = datetime.fromisoformat(row["due_at_utc"])
                    rule = rrulestr(recurrence, dtstart=start)
                    next_dt = rule.after(now, inc=False)
                except Exception:
                    next_dt = None
                if next_dt:
                    conn.execute(
                        """UPDATE reminders SET due_at_utc=?,status='OPEN',last_fired_at_utc=?
                           WHERE reminder_id=?""",
                        (next_dt.astimezone(timezone.utc).isoformat(), utc_now(), row["reminder_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE reminders SET status='DUE',last_fired_at_utc=? WHERE reminder_id=?",
                        (utc_now(), row["reminder_id"]),
                    )
            else:
                conn.execute(
                    "UPDATE reminders SET status='DUE',last_fired_at_utc=? WHERE reminder_id=?",
                    (utc_now(), row["reminder_id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    while True:
        try:
            fire_due()
        except Exception as exc:
            print(f"[Alex MCP scheduler] {exc}", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
