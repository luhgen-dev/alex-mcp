from __future__ import annotations

import re
import unicodedata


def normalize_intent_text(text: str | None) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace(chr(0x2019), "'").replace(chr(0x2018), "'")
    value = value.replace(chr(0x02BC), "'").replace(chr(0x2032), "'")
    value = value.replace(chr(0x201C), '"').replace(chr(0x201D), '"')
    value = value.replace(chr(0x2033), '"').replace(chr(0x2026), "...")
    value = re.sub(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+", " ", value)
    return value
