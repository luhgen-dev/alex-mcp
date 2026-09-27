from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

from db import connect

EGRESS_URL = "http://127.0.0.1:5002/send"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _send(payload: dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        EGRESS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, r.read().decode("utf-8")
    except Exception as exc:
        return False, str(exc)


def _payload(row) -> dict:
    kind = row["kind"]
    if kind == "TEXT":
        return {"to": row["conversation_id"], "kind": "text", "text": row["text_body"] or ""}
    path = row["local_path"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path or "missing attachment path")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return {
        "to": row["conversation_id"],
        "kind": "image" if kind == "IMAGE" else "document",
        "file_b64": data,
        "mimetype": row["mime_type"] or ("image/jpeg" if kind == "IMAGE" else "application/octet-stream"),
        "filename": os.path.basename(path),
        "caption": row["text_body"] or "",
    }


def sweep():
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT * FROM outbound_messages WHERE delivery_status='PENDING'
               ORDER BY created_at_utc,rowid LIMIT 10"""
        ).fetchall()
        for row in rows:
            try:
                payload = _payload(row)
                ok, detail = _send(payload)
            except Exception as exc:
                ok, detail = False, str(exc)
            attempts = int(row["attempt_count"] or 0) + 1
            if ok:
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status='SENT',attempt_count=?,
                       delivered_at_utc=?,last_error=NULL WHERE outbound_id=?""",
                    (attempts, _now(), row["outbound_id"]),
                )
            else:
                status = "FAILED" if attempts >= 5 else "PENDING"
                conn.execute(
                    """UPDATE outbound_messages SET delivery_status=?,attempt_count=?,last_error=?
                       WHERE outbound_id=?""",
                    (status, attempts, detail[:1000], row["outbound_id"]),
                )
        conn.commit()
    finally:
        conn.close()


def main():
    while True:
        try:
            sweep()
        except Exception as exc:
            print(f"[Alex MCP outbox] {exc}", flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
