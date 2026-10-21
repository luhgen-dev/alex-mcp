"""Bounded, source-checked reminder time meanings.

This module never supplies an actor, record ID, recipient, destination or clock.
The model may normalize a time expression; the caller supplies authenticated
user text and an independently revalidated pending record. Numeric facts and
explicit ambiguities in that text constrain the proposed meaning.
"""
from __future__ import annotations

import re

from text_normalization import normalize_intent_text


FIELDS = frozenset({
    "source_text", "kind", "reference", "relative_minutes", "date_text",
    "time_text", "needs_clarification",
})
REQUIRED = frozenset({"source_text", "kind", "reference", "needs_clarification"})
_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_UNITS = r"minutes?|mins?|minits?|minit|mints?|mnts?|mns|m|hours?|hrs?|hr|h"
_WEEKDAYS = {
    "mon": "monday", "monday": "monday", "tue": "tuesday",
    "tues": "tuesday", "tuesday": "tuesday", "wed": "wednesday",
    "wednesday": "wednesday", "thu": "thursday", "thur": "thursday",
    "thurs": "thursday", "thursday": "thursday", "fri": "friday",
    "friday": "friday", "sat": "saturday", "saturday": "saturday",
    "sun": "sunday", "sunday": "sunday",
}
_MONTHS = {
    name: index for index, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"),
        ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
        ("aug", "august"), ("sep", "sept", "september"),
        ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ), 1) for name in names
}
_MONTH_FULL = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)


def normalized(text: str) -> str:
    return " ".join(normalize_intent_text(text).casefold().split()).strip()


def _fail(code: str = "REMINDER_NEEDS_TIME", message: str = "ask for an unambiguous time"):
    raise ValueError(f"{code}: {message}")


def number_text(text: str) -> str:
    """Normalize the bounded English number grammar, not whole command phrases."""
    value = normalized(text)
    value = re.sub(r"\bhalf\s+(?:an?\s+)?hour\b", "0.5 hour", value)
    value = re.sub(r"\ba\s+quarter\s+of\s+an?\s+hour\b", "0.25 hour", value)
    tens = "twenty|thirty|forty|fourty|fifty|sixty|seventy|eighty|ninety"
    units = "one|two|three|four|five|six|seven|eight|nine"
    value = re.sub(
        rf"\b({tens})[\s-]+({units})\b",
        lambda m: str(_NUMBERS[m[1]] + _NUMBERS[m[2]]), value,
    )
    value = re.sub(
        r"\b(" + "|".join(_NUMBERS) + r")\b",
        lambda m: str(_NUMBERS[m[1]]), value,
    )
    value = re.sub(r"\ban?\s+(hours?|hrs?|minutes?|mins?)\b", r"1 \1", value)
    value = re.sub(r"\b(\d{1,2})[- ]?ish\b", r"\1", value)
    value = re.sub(r"\ba\.?m\.?\b", "am", value)
    value = re.sub(r"\bp\.?m\.?\b", "pm", value)
    return value


def _latest_correction(text: str) -> str:
    # A current explicit correction outranks the earlier wording in that turn.
    parts = re.split(r"\b(?:actually|instead|make\s+it)\b", text)
    return parts[-1] if len(parts) > 1 else text


def relative_minutes_in(text: str, *, bare: bool = False) -> int | None:
    value = _latest_correction(number_text(text))
    if re.search(rf"\b(?:{_UNITS})\b\s*(?:(?:and|or|to)\s+)?\d+(?:\.\d+)?\s*(?:{_UNITS})\b", value):
        _fail(message="state a combined duration as one number of minutes")
    if re.search(rf"\b\d+(?:\.\d+)?\s*(?:-|to|or|and)\s*\d+(?:\.\d+)?\s*(?:{_UNITS})\b", value):
        _fail(message="a duration range needs one exact quantity")
    found = []
    for match in re.finditer(rf"\b(\d{{1,4}}(?:\.\d+)?)\s*({_UNITS})\b", value):
        prefix, suffix = value[:match.start()], value[match.end():]
        lead = bool(re.search(r"\b(?:in|after|within)\s+(?:(?:about|abt|around|ard|roughly|approximately)\s+)?$", prefix))
        tail = bool(re.match(
            r"\s+(?:later\b|from\s+(?:(?:the|this|my)\s+)?(?:current\s+)?(?:now|time|moment|message)\b)",
            suffix,
        ))
        only_duration = bare and not re.sub(
            r"\b(?:about|abt|around|ard|roughly|approximately|please|pls)\b|[\s.!?,]",
            "", prefix + suffix,
        )
        if not (lead or tail or only_duration):
            continue
        amount = float(match[1])
        is_hours = match[2].startswith("h")
        minutes = amount * 60 if is_hours else amount
        limit = 4320 if is_hours else 1440
        if minutes <= 0 or minutes > limit or not minutes.is_integer():
            _fail(message="duration is outside the supported bounded range")
        found.append(int(minutes))
    if len(set(found)) > 1:
        _fail(message="more than one duration was supplied")
    return found[-1] if found else None


def _daypart(text: str) -> str | None:
    value = normalized(text)
    value = re.sub(r"\bafter\s+dinner\b", "evening", value)
    value = re.sub(r"\bafter\s+lunch\b", "afternoon", value)
    found = re.findall(r"\b(morning|afternoon|evening|night|am|pm)\b", value)
    families = {"am" if part in {"morning", "am"} else "pm" for part in found}
    if len(families) > 1:
        _fail(message="the stated AM/PM or daypart is contradictory")
    return found[-1] if found else None


def _latest_daypart(text: str) -> str | None:
    for line in reversed(str(text or "").splitlines()):
        part = _daypart(_latest_correction(line))
        if part:
            return part
    return None


def _apply_daypart(hour: int, minute: int, part: str | None) -> tuple[int, int] | None:
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    if part in {"morning", "am"}:
        return (hour % 12, minute)
    if part in {"afternoon", "evening", "pm"}:
        return (hour % 12 + 12, minute)
    if part == "night" and hour == 12:
        return (0, minute)
    if part == "night" and 6 <= hour <= 11:
        return (hour + 12, minute)
    return None


def _open_clock_range(text: str) -> bool:
    value = number_text(text)
    # Hyphens inside an explicit calendar date are not a clock range.
    value = re.sub(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", "", value)
    return bool(re.search(
        r"\b(?:sometime|some\s+time|whenever|between)\b"
        r"|\b(?:after|before|later\s+than|earlier\s+than)\s+(?:about\s+)?\d"
        r"|\b\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\s*(?:-|to|or)\s*\d{1,2}\b"
        r"|\b(?:noon|midnight)\s+(?:or|to)\s+(?:noon|midnight|\d)",
        value,
    ))


def clock_in(text: str, *, inherited_daypart: str | None = None) -> tuple[int, int] | None:
    value = _latest_correction(number_text(text))
    if _open_clock_range(value):
        _fail(message="a range or open-ended time needs an exact point")
    part = _daypart(value) or inherited_daypart
    clocks = []
    occupied = []
    for token, clock in (("noon", (12, 0)), ("midnight", (0, 0))):
        if re.search(r"\b" + token + r"\b", value):
            clocks.append(clock)
    explicit = list(re.finditer(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", value))
    for m in explicit:
        clock = _apply_daypart(int(m[1]), int(m[2] or 0), m[3])
        if clock is None:
            _fail(message="the stated clock is invalid or contradictory")
        clocks.append(clock)
        occupied.append(m.span())
    colon = list(re.finditer(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)", value))
    for m in colon:
        if any(start <= m.start() < end for start, end in occupied):
            continue
        if int(m[1]) > 23:
            _fail(message="the stated clock is invalid or contradictory")
        clocks.append((int(m[1]), int(m[2])))
        occupied.append(m.span())
    candidates = list(re.finditer(r"\b(\d{1,2})(?:\.([0-5]\d))?\b", value))
    for m in candidates:
        if any(start <= m.start() < end for start, end in occupied):
            continue
        prefix, suffix = value[:m.start()], value[m.end():]
        if re.match(rf"\s*(?:{_UNITS})\b", suffix):
            continue
        anchored = (
            bool(re.search(r"\b(?:at|around|about|abt|ard|roughly|approximately)\s*$", prefix))
            or bool(re.match(r"\s*(?:o[' ]?clock|tomorrow|today|tonight|in\s+the|this\s+)?\s*(?:morning|afternoon|evening|night)\b", suffix))
            or (part is not None and bool(re.match(r"\s*(?:o[' ]?clock\s+)?(?:tomorrow|today|tonight)\b", suffix)))
            or (bool(clocks) and bool(re.search(r"\b(?:or|and)\s*$", prefix)))
            or not re.sub(r"\b(?:please|pls)\b|[\s.!?,]", "", prefix + suffix)
        )
        if anchored:
            clock = _apply_daypart(int(m[1]), int(m[2] or 0), part)
            if clock is None:
                _fail(message="ask for AM/PM or an unambiguous clock")
            clocks.append(clock)
    if len(set(clocks)) > 1:
        _fail(message="more than one clock was supplied")
    if clocks and part:
        hour = clocks[-1][0]
        incompatible = (
            (part in {"am", "morning"} and hour >= 12)
            or (part in {"pm", "afternoon", "evening"} and hour < 12)
            or (part == "night" and hour not in {0, 18, 19, 20, 21, 22, 23})
        )
        if incompatible:
            _fail(message="the stated clock and daypart disagree")
    return clocks[-1] if clocks else None


def dates_in(text: str) -> list[str]:
    value = normalized(text)
    value = re.sub(r"\b(?:tmr|tmrw|tomoro|tomorow)\b", "tomorrow", value)
    if re.search(r"\bday\s+after\s+tomorrow\b", value):
        _fail("REMINDER_NEEDS_DATE", "state the exact date for this relative day")
    result = []
    for m in re.finditer(r"\b(today|tomorrow|tonight|later|this\s+evening)\b", value):
        result.append("tomorrow" if m[1] == "tomorrow" else "today")
    for m in re.finditer(r"\b(?:(next|this|coming)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", value):
        result.append(("next " if m[1] == "next" else "") + _WEEKDAYS[m[2]])
    result.extend(re.findall(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", value))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")(?:\s*,?\s*((?:19|20)\d{2}))?\b", value):
        result.append(f"{int(m[1])} {_MONTH_FULL[_MONTHS[m[2]]-1]}" + (" " + m[3] if m[3] else ""))
    return list(dict.fromkeys(result))


def canonical_date(text: str) -> str:
    value = normalized(text)
    if len(value) > 80:
        _fail("REMINDER_NEEDS_DATE", "date interpretation is too long")
    tokens = dates_in(value)
    if len(tokens) != 1:
        _fail("REMINDER_NEEDS_DATE", "provide one canonical date or weekday")
    if not re.fullmatch(
        r"today|tomorrow|tonight|later|this evening|"
        r"(?:(?:next|this|coming) )?(?:" + "|".join(_WEEKDAYS) + r")|"
        r"(?:19|20)\d{2}-\d{2}-\d{2}|"
        r"\d{1,2}(?:st|nd|rd|th)? (?:" + "|".join(_MONTHS) + r")(?: (?:19|20)\d{2})?",
        value,
    ):
        _fail("REMINDER_NEEDS_DATE", "date text contains unsupported extra content")
    return tokens[0]


def _latest_dates(text: str) -> list[str]:
    for line in reversed(str(text or "").splitlines()):
        found = dates_in(_latest_correction(normalized(line)))
        if found:
            return found
    return []


def validate(spec: dict, *, current_text: str, pending_text: str = "", pending_bound: bool = False) -> dict:
    if not isinstance(spec, dict) or set(spec) - FIELDS or not REQUIRED <= set(spec):
        _fail(message="invalid time interpretation shape")
    if type(spec.get("needs_clarification")) is not bool or spec["needs_clarification"]:
        _fail(message="the interpreted time remains ambiguous")
    kind, reference = spec.get("kind"), spec.get("reference")
    if (
        not isinstance(kind, str) or not isinstance(reference, str)
        or kind not in {"RELATIVE", "LOCAL_CLOCK"}
        or reference not in {"CURRENT_MESSAGE", "ACTIVE_DRAFT"}
    ):
        _fail(message="invalid time interpretation kind/reference")
    if reference == "ACTIVE_DRAFT" and not pending_bound:
        _fail(message="the referenced pending reminder is unavailable")
    source = spec.get("source_text")
    if not isinstance(source, str) or not source.strip() or len(source) > 240:
        _fail(message="a bounded user-authored source span is required")
    current = str(current_text or "")
    pending = str(pending_text or "") if pending_bound and reference == "ACTIVE_DRAFT" else ""
    current_match = normalized(source) in normalized(current)
    pending_match = reference == "ACTIVE_DRAFT" and normalized(source) in normalized(pending)
    if not (current_match or pending_match):
        _fail(message="time evidence does not occur in the authenticated user text")
    current_relative = relative_minutes_in(current, bare=pending_bound)
    if kind == "RELATIVE":
        minutes = spec.get("relative_minutes")
        if type(minutes) is not int or not 1 <= minutes <= 4320:
            _fail(message="relative minutes must be a bounded integer")
        if spec.get("date_text") not in (None, "") or spec.get("time_text") not in (None, ""):
            _fail(message="relative and calendar fields cannot be mixed")
        if not current_match or current_relative is None:
            _fail(message="restate the relative duration in the current message")
        if current_relative != minutes:
            _fail(message="the proposed duration contradicts the user's quantity")
        remaining = re.sub(
            rf"\b\d+(?:\.\d+)?\s*(?:{_UNITS})\b", "",
            _latest_correction(number_text(current)),
        )
        remaining = re.sub(r"\blater\b", "", remaining)
        if dates_in(remaining) or clock_in(remaining):
            _fail(message="relative and calendar time evidence conflicts")
        return {"kind": kind, "canonical_text": f"in {minutes} minutes", "relative_minutes": minutes}

    if spec.get("relative_minutes") is not None:
        _fail(message="calendar and relative fields cannot be mixed")
    if current_relative is not None:
        _fail(message="the current relative duration must not become a model-selected clock")
    date_text, time_text = spec.get("date_text"), spec.get("time_text")
    if not isinstance(date_text, str) or not isinstance(time_text, str) or len(time_text) > 40:
        _fail(message="canonical date and clock fields are required")
    proposed_date = canonical_date(date_text)
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d|(?:0?[1-9]|1[0-2])(?::[0-5]\d)?\s*(?:am|pm)|noon|midnight", normalized(time_text)):
        _fail(message="time_text must contain one exact canonical clock")
    proposed_clock = clock_in(time_text)
    actual_clock = clock_in(current, inherited_daypart=_latest_daypart(pending))
    if actual_clock is None and pending:
        for line in reversed(pending.splitlines()):
            actual_clock = clock_in(line, inherited_daypart=_daypart(current))
            if actual_clock is not None:
                break
    if actual_clock is None or proposed_clock != actual_clock:
        _fail(message="the proposed clock is not supported by the user-authored time")
    source_dates = _latest_dates(current) or _latest_dates(pending)
    if proposed_date not in source_dates:
        _fail("REMINDER_NEEDS_DATE", "the proposed date is not supported by the user-authored date")
    if "today" in source_dates and "tomorrow" in source_dates:
        _fail("REMINDER_AMBIGUOUS_DATE", "more than one day was stated")
    explicit_dates = [x for x in source_dates if re.match(r"\d", x)]
    if len(explicit_dates) > 1:
        _fail("REMINDER_AMBIGUOUS_DATE", "more than one calendar date was stated")
    weekday_constraints = [x for x in source_dates if x.removeprefix("next ") in _WEEKDAYS.values()]
    if len(weekday_constraints) > 1:
        _fail("REMINDER_AMBIGUOUS_DATE", "more than one weekday was stated")
    # Keep original weekday constraints visible to the backend's date checks.
    canonical = " ".join(dict.fromkeys([proposed_date, *weekday_constraints]))
    canonical += f" {actual_clock[0]:02d}:{actual_clock[1]:02d}"
    return {"kind": kind, "canonical_text": canonical, "relative_minutes": None}
