from __future__ import annotations

"""Single runtime clock for Alex.

Production uses the real wall clock. Certification may set ALEX_CERT_NOW to an
offset-aware ISO-8601 timestamp so *all* Python-level date/time decisions share
one deterministic instant without monkey-patching individual modules.
"""

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


_ENV = "ALEX_CERT_NOW"


def _fixed_utc() -> datetime | None:
    raw = str(os.environ.get(_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{_ENV} must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        raise RuntimeError(f"{_ENV} must include a timezone offset")
    return value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return _fixed_utc() or datetime.now(timezone.utc)


def utc_iso() -> str:
    return now_utc().isoformat()


def now_in(tz: str | ZoneInfo) -> datetime:
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(str(tz))
    return now_utc().astimezone(zone)


def today(tz: str | ZoneInfo | None = None) -> date:
    if tz is None:
        return now_utc().date()
    return now_in(tz).date()
