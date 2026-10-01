from __future__ import annotations

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf"


def _font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(BOLD if bold else FONT, size)
    except Exception:
        return ImageFont.load_default()


def render(summary: dict, width: int = 1200, height: int = 720) -> bytes:
    """Deterministic premium landscape card populated only from HA state."""
    width, height = max(960, width), max(600, height)
    bg = (12, 18, 27)
    panel = (27, 36, 48)
    text = (244, 246, 248)
    muted = (157, 168, 181)
    amber = (220, 163, 70)
    calm = (104, 177, 143)
    warn = (211, 116, 83)

    im = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(im)

    def rr(box, radius=22, fill=panel):
        d.rounded_rectangle(box, radius=radius, fill=fill)

    def tx(x, y, value, size=16, fill=text, bold=False):
        d.text((x, y), str(value), font=_font(size, bold), fill=fill)

    def names(values, limit=2):
        rows = [str(v) for v in (values or []) if str(v).strip()]
        if not rows:
            return "None"
        suffix = f"  +{len(rows)-limit} more" if len(rows) > limit else ""
        return " • ".join(rows[:limit]) + suffix

    tx(54, 38, "ALEX", 26, text, True)
    tx(54, 76, "HOME STATUS", 16, amber, True)
    status = str(summary.get("status") or "OK").upper()
    attention = int(summary.get("attention_count") or 0)
    pill = "HOME OK" if status == "OK" else "ATTENTION"
    pill_color = calm if status == "OK" else amber
    rr((width-278, 42, width-54, 98), 28, (31, 42, 54))
    d.ellipse((width-252, 63, width-238, 77), fill=pill_color)
    tx(width-222, 57, pill, 17, text, True)
    d.line((54, 122, width-54, 122), fill=(48, 59, 72), width=1)

    left_x = 54
    left_w = int(width * 0.55)
    right_x = left_x + left_w + 28
    right_w = width - right_x - 54
    tx(left_x, 148, "HOME AT A GLANCE", 14, muted, True)
    tx(right_x, 148, "WHAT MATTERS NOW", 14, muted, True)

    metrics = [
        ("People home", len(summary.get("people_home", []))),
        ("Lights on", len(summary.get("lights_on", []))),
        ("Open entries", len(summary.get("open_entries", []))),
        ("Unlocked", len(summary.get("unlocked", []))),
        ("Climate active", len(summary.get("climate_active", []))),
        ("Media playing", len(summary.get("media_playing", []))),
    ]
    gap, card_h = 16, 112
    card_w = (left_w-gap)//2
    for idx, (name, value) in enumerate(metrics):
        col, row = idx % 2, idx // 2
        x = left_x + col*(card_w+gap)
        y = 184 + row*(card_h+gap)
        rr((x, y, x+card_w, y+card_h))
        tx(x+22, y+19, name.upper(), 12, muted, True)
        is_warning = name in {"Open entries", "Unlocked"} and value
        tx(x+22, y+48, value, 32, warn if is_warning else text, True)
        if is_warning:
            tx(x+75, y+62, "needs attention", 11, warn)

    rr((right_x, 184, right_x+right_w, 365))
    if attention:
        tx(right_x+22, 204, f"{attention} item{'s' if attention != 1 else ''} need attention", 18, amber, True)
        rows = []
        if summary.get("open_entries"):
            rows.append("Open: " + names(summary["open_entries"]))
        if summary.get("unlocked"):
            rows.append("Unlocked: " + names(summary["unlocked"]))
        if summary.get("unavailable"):
            rows.append("Unavailable: " + names(summary["unavailable"]))
        for idx, row in enumerate(rows[:4]):
            tx(right_x+22, 248+idx*31, row, 13)
    else:
        tx(right_x+22, 204, "Everything looks calm", 19, calm, True)
        tx(right_x+22, 246, "No open entries or unlocked locks detected.", 13, muted)

    rr((right_x, 383, right_x+right_w, 624))
    tx(right_x+22, 404, "ACTIVE NOW", 12, muted, True)
    active = [
        ("Climate", names(summary.get("climate_active"))),
        ("Lights", names(summary.get("lights_on"))),
        ("Media", names(summary.get("media_playing"))),
        ("People", names(summary.get("people_home"))),
    ]
    for idx, (name, value) in enumerate(active):
        y = 438 + idx*42
        tx(right_x+22, y, name, 12, amber if value != "None" else muted, True)
        tx(right_x+116, y, value, 12, text if value != "None" else muted)

    tx(54, height-54, f"{int(summary.get('total_entities') or 0)} Home Assistant entities checked", 11, muted)
    if summary.get("unavailable"):
        tx(width-250, height-54, f"{len(summary['unavailable'])} unavailable", 11, amber, True)
    else:
        tx(width-235, height-54, "All reporting normally", 11, calm, True)

    out = BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue()
