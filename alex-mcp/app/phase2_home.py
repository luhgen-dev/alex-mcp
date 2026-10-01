"""Project Jarvis Phase 2 Home Assistant context helpers.

No Home Assistant API calls are made here. Live integration will inject current
entity snapshots and traffic/travel estimates. Departure and alarm suggestions
are calculations only; they never create an alarm automatically.
"""
from __future__ import annotations

import struct
import zlib
from datetime import date, datetime, timedelta

import phase2_schedule
import phase2_work
import profile_config


def _parse_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _clock_dt(d, value):
    if not value:
        return None
    hour, minute = (int(x) for x in str(value).split(":"))
    return datetime(d.year, d.month, d.day, hour, minute)


def _active_roster(sender_phone, conversation_type):
    rows = profile_config.authorized_records(
        sender_phone, conversation_type,
        kind="roster", requested_scope="private")
    active = [row for row in rows if row["payload"].get("active", True)]
    if len(active) != 1:
        raise ValueError("Exactly one active private roster is required")
    return active[0]["payload"]


def shift_departure_plan(on_date, sender_phone,
                         conversation_type="DIRECT_DM",
                         travel_minutes=None, prep_minutes=30,
                         arrival_buffer_minutes=10):
    """Calculate a work departure/alarm candidate from roster + supplied travel.

    travel_minutes is intentionally injected: future live integration can use
    the user's preferred traffic source. No route duration is invented here.
    """
    d = _parse_date(on_date)
    if travel_minutes is None:
        return {
            "date": d.isoformat(), "status": "NEEDS_TRAVEL_ESTIMATE",
            "action_taken": False,
        }
    travel = int(travel_minutes)
    prep = int(prep_minutes)
    buffer = int(arrival_buffer_minutes)
    if min(travel, prep, buffer) < 0:
        raise ValueError("Departure timing values cannot be negative")

    leave = phase2_schedule.leave_plans_for_date(
        d, sender_phone, conversation_type)
    if leave and leave[0]["status"] in ("PLANNED","CONFIRMED","TAKEN"):
        return {
            "date": d.isoformat(), "status": "LEAVE_DAY",
            "work_required": False, "leave": leave[0],
            "action_taken": False,
        }

    roster = _active_roster(sender_phone, conversation_type)
    shift = phase2_work.effective_shift(
        d, sender_phone, conversation_type)["shift"]
    confidence = "ROSTER"
    if d.weekday() >= 5:
        ot = phase2_work.effective_ot_for_date(
            d, sender_phone, conversation_type)
        if not ot.get("ot"):
            return {
                "date": d.isoformat(), "status": "NO_WORK_PATTERN",
                "work_required": False, "shift": shift,
                "action_taken": False,
            }
        start_clock = ot.get("start")
        confidence = (
            "RECORDED_OT" if ot.get("source_event_type") == "OT_WORKED"
            else "PLANNED_OT" if ot.get("source_event_type") == "OT_PLANNED"
            else "DEFAULT_OT_PATTERN"
        )
    else:
        start_clock = (
            roster.get("day_start")
            if shift == "morning" else roster.get("evening_start")
            if shift == "evening" else None
        )
    if not start_clock:
        return {
            "date": d.isoformat(), "status": "NO_START_TIME",
            "work_required": shift != "off", "shift": shift,
            "action_taken": False,
        }

    work_start = _clock_dt(d, start_clock)
    leave_home = work_start - timedelta(minutes=travel + buffer)
    alarm = leave_home - timedelta(minutes=prep)
    return {
        "date": d.isoformat(),
        "status": "READY",
        "work_required": True,
        "shift": shift,
        "work_start": work_start.strftime("%H:%M"),
        "travel_minutes": travel,
        "arrival_buffer_minutes": buffer,
        "prep_minutes": prep,
        "leave_home": leave_home.strftime("%H:%M"),
        "suggested_alarm": alarm.strftime("%H:%M"),
        "basis": confidence,
        "action_taken": False,
    }


def summarize_home(entity_snapshots):
    """Create a deterministic household status summary from HA snapshots."""
    summary = {
        "total_entities": 0,
        "unavailable": [],
        "lights_on": [],
        "open_entries": [],
        "unlocked": [],
        "climate_active": [],
        "media_playing": [],
        "people_home": [],
        "alarms": [],
    }
    for raw in entity_snapshots or []:
        entity = dict(raw)
        entity_id = str(entity.get("entity_id") or "")
        domain = entity_id.split(".", 1)[0] if "." in entity_id else str(entity.get("domain") or "")
        state = str(entity.get("state") or "").lower()
        name = str(
            entity.get("name") or entity.get("friendly_name")
            or entity_id or "Unknown entity")
        device_class = str(entity.get("device_class") or "").lower()
        summary["total_entities"] += 1

        if state in ("unavailable", "unknown"):
            summary["unavailable"].append(name)
            continue
        if domain == "light" and state == "on":
            summary["lights_on"].append(name)
        if (
            domain == "binary_sensor"
            and device_class in ("door","window","garage_door","opening")
            and state in ("on","open")
        ):
            summary["open_entries"].append(name)
        if domain == "lock" and state in ("unlocked","open"):
            summary["unlocked"].append(name)
        if domain == "climate" and state not in ("off","idle",""):
            summary["climate_active"].append(name)
        if domain == "media_player" and state == "playing":
            summary["media_playing"].append(name)
        if domain == "person" and state in ("home","present"):
            summary["people_home"].append(name)
        if domain == "alarm_control_panel":
            summary["alarms"].append({"name": name, "state": state})

    summary["attention_count"] = (
        len(summary["unavailable"])
        + len(summary["open_entries"])
        + len(summary["unlocked"])
    )
    summary["status"] = (
        "ATTENTION" if summary["attention_count"] else "OK"
    )
    return summary


# Minimal 5x7 bitmap glyphs. Keeping the renderer in stdlib avoids adding a
# heavyweight image dependency to the HA add-on solely for a local status card.
_FONT = {
" ":["00000"]*7,
"A":["01110","10001","10001","11111","10001","10001","10001"],
"B":["11110","10001","10001","11110","10001","10001","11110"],
"C":["01111","10000","10000","10000","10000","10000","01111"],
"D":["11110","10001","10001","10001","10001","10001","11110"],
"E":["11111","10000","10000","11110","10000","10000","11111"],
"F":["11111","10000","10000","11110","10000","10000","10000"],
"G":["01111","10000","10000","10111","10001","10001","01111"],
"H":["10001","10001","10001","11111","10001","10001","10001"],
"I":["11111","00100","00100","00100","00100","00100","11111"],
"J":["00111","00010","00010","00010","10010","10010","01100"],
"K":["10001","10010","10100","11000","10100","10010","10001"],
"L":["10000","10000","10000","10000","10000","10000","11111"],
"M":["10001","11011","10101","10101","10001","10001","10001"],
"N":["10001","11001","10101","10011","10001","10001","10001"],
"O":["01110","10001","10001","10001","10001","10001","01110"],
"P":["11110","10001","10001","11110","10000","10000","10000"],
"Q":["01110","10001","10001","10001","10101","10010","01101"],
"R":["11110","10001","10001","11110","10100","10010","10001"],
"S":["01111","10000","10000","01110","00001","00001","11110"],
"T":["11111","00100","00100","00100","00100","00100","00100"],
"U":["10001","10001","10001","10001","10001","10001","01110"],
"V":["10001","10001","10001","10001","10001","01010","00100"],
"W":["10001","10001","10001","10101","10101","10101","01010"],
"X":["10001","10001","01010","00100","01010","10001","10001"],
"Y":["10001","10001","01010","00100","00100","00100","00100"],
"Z":["11111","00001","00010","00100","01000","10000","11111"],
"0":["01110","10001","10011","10101","11001","10001","01110"],
"1":["00100","01100","00100","00100","00100","00100","01110"],
"2":["01110","10001","00001","00010","00100","01000","11111"],
"3":["11110","00001","00001","01110","00001","00001","11110"],
"4":["00010","00110","01010","10010","11111","00010","00010"],
"5":["11111","10000","10000","11110","00001","00001","11110"],
"6":["01110","10000","10000","11110","10001","10001","01110"],
"7":["11111","00001","00010","00100","01000","01000","01000"],
"8":["01110","10001","10001","01110","10001","10001","01110"],
"9":["01110","10001","10001","01111","00001","00001","01110"],
":":["00000","00100","00100","00000","00100","00100","00000"],
"-":["00000","00000","00000","11111","00000","00000","00000"],
"/":["00001","00010","00010","00100","01000","01000","10000"],
".":["00000","00000","00000","00000","00000","00100","00100"],
}


def _canvas(width, height, rgb=(247,247,247)):
    return [bytearray(rgb * width) for _ in range(height)]


def _rect(canvas, x, y, w, h, rgb):
    height = len(canvas)
    width = len(canvas[0]) // 3
    for yy in range(max(0,y), min(height,y+h)):
        row = canvas[yy]
        for xx in range(max(0,x), min(width,x+w)):
            i = xx * 3
            row[i:i+3] = bytes(rgb)


def _text(canvas, x, y, value, scale=2, rgb=(25,25,25), max_chars=50):
    cursor = x
    for ch in str(value).upper()[:max_chars]:
        glyph = _FONT.get(ch, _FONT[" "])
        for gy, bits in enumerate(glyph):
            for gx, bit in enumerate(bits):
                if bit == "1":
                    _rect(
                        canvas, cursor + gx*scale, y + gy*scale,
                        scale, scale, rgb)
        cursor += 6 * scale


def _png_bytes(canvas):
    height = len(canvas)
    width = len(canvas[0]) // 3
    raw = b"".join(b"\x00" + bytes(row) for row in canvas)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render_home_report_png(summary, width=1200, height=720):
    """Render the locked premium deterministic local Home Status card."""
    import home_card
    return home_card.render(summary, width=width, height=height)
