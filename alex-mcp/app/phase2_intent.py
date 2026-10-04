"""Phase-2 deterministic intent and audience gate.

This module is deliberately conservative. Explicit user wording wins. Clear
natural meaning may resolve one intent. Genuine multi-meaning input produces
one clarification and performs no write.
"""
from __future__ import annotations

import re


INTENTS = {
    "REMINDER", "DIARY", "EXPENSE", "OBLIGATION", "SAVED_MEMORY", "PLAN"
}


def routing_contract(conversation_type, *, explicit_recipient=None,
                     explicit_family=False, explicit_private=False):
    """Resolve Phase-2 diary/plan/reminder audience without model discretion.

    DIRECT_DM defaults personal. GROUP defaults family/shared.
    An explicitly named recipient (spouse/both/group) controls delivery while
    unrelated source-chat context remains outside that recipient's payload.
    """
    ctype = str(conversation_type or "").upper()
    recipient = str(explicit_recipient or "").lower() or None
    if recipient not in (None, "self", "spouse", "both", "group"):
        raise ValueError("Recipient must be self, spouse, both or group")

    if ctype == "GROUP":
        if explicit_private:
            raise ValueError("Private diary/reminder data must be created in DM")
        visibility = "family"
        default_recipient = "group"
    else:
        if explicit_family and explicit_private:
            raise ValueError("Cannot request family and private simultaneously")
        visibility = "family" if explicit_family else "private"
        default_recipient = "self"

    recipient = recipient or default_recipient
    if recipient in ("spouse", "both", "group"):
        delivery_visibility = "family"
    else:
        delivery_visibility = visibility

    return {
        "visibility": visibility,
        "recipient": recipient,
        "delivery_visibility": delivery_visibility,
        "source_context_may_be_forwarded": False,
    }


def phase1_reminder_bridge_policy(conversation_type, *,
                                  explicit_recipient=None):
    """Translate Phase-2 reminder audience rules to Phase-1 service arguments.

    This does not call Phase 1. It exists so post-Smoke-Test integration is a
    wiring step rather than a new privacy decision.
    """
    route = routing_contract(
        conversation_type, explicit_recipient=explicit_recipient)
    recipient = route["recipient"]
    # Phase-1 create_reminder(private_requested=...) only uses this flag for
    # sender/self reminders. Explicit spouse/both/group destinations are
    # FAMILY_SHARED delivery records by design.
    private_requested = (
        str(conversation_type or "").upper() != "GROUP"
        and recipient == "self"
        and route["visibility"] == "private"
    )
    return {
        "recipient": recipient,
        "private_requested": private_requested,
        "source_context_may_be_forwarded": False,
        "reminder_content_only": True,
    }


def _date_signal(text):
    return bool(re.search(
        r"\b(?:today|tomorrow|tonight|next\s+(?:week|month|"
        r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)|"
        r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
        r"\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?))\b",
        text, re.IGNORECASE))


def _money_signal(text):
    return bool(re.search(
        r"\b(?:RM|MYR|SGD)\s*\d+(?:[.,]\d+)?\b|"
        r"\b\d+(?:[.,]\d+)?\s*(?:RM|MYR|SGD)\b",
        text, re.IGNORECASE))


def reminder_write_signal(text) -> bool:
    """Recognize trusted user wording that clearly asks Alex to prompt them later.

    This is intentionally semantic rather than a single magic keyword. It is
    used only to authorize reminder-domain routing/pending state; the actual
    reminder write still goes through the normal deterministic tool and time
    validation path.
    """
    low = str(text or "").strip().casefold()
    if not low:
        return False

    explicit = bool(re.search(
        r"\b(?:remind\s+(?:me|us|my\s+wife|my\s+husband|priya)|"
        r"set\s+(?:a\s+)?reminder|add\s+(?:a\s+)?reminder)\b",
        low,
    ))
    if explicit:
        return True

    natural_patterns = (
        r"\b(?:nudge|ping|buzz|alert|notify)\s+(?:me|us)\b",
        r"\b(?:give|send)\s+(?:me|us)\s+(?:a\s+)?"
        r"(?:nudge|ping|buzz|alert|heads?[ -]?up|shout)\b",
        r"\bdon'?t\s+let\s+(?:me|us)\s+forget\b",
        r"\b(?:make|be)\s+sure\s+(?:i|we)\s+"
        r"(?:remember|don'?t\s+forget|do\s+not\s+forget)\b",
        r"\bhelp\s+(?:me|us)\s+remember\b",
        r"\b(?:wake|message)\s+(?:me|us)\b.{0,40}"
        r"\b(?:at|around|by|before|after|tomorrow|tonight|today|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    )
    return any(re.search(pattern, low) for pattern in natural_patterns)


def classify_read_intent(text):
    """Resolve casual schedule language to Agenda unless work is explicit."""
    raw = str(text or "").strip()
    low = raw.casefold()
    if not raw:
        return {"status": "unknown", "intent": None}
    if re.search(
        r"\b(?:roster|shift|work\s+schedule|working\s+schedule|"
        r"what\s+shift|which\s+shift|am\s+i\s+working|"
        r"did\s+i\s+work)\b", low
    ):
        return {"status": "resolved", "intent": "ROSTER"}
    if re.search(
        r"\b(?:agenda|what\s+(?:do\s+i|have\s+i)\s+have|"
        r"what'?s\s+(?:my|our)\s+schedule|"
        r"what\s+is\s+(?:my|our)\s+schedule|"
        r"what'?s\s+(?:my|our)\s+plan|"
        r"what\s+is\s+(?:my|our)\s+plan)\b", low
    ):
        return {"status": "resolved", "intent": "AGENDA"}
    return {"status": "unknown", "intent": None}


def classify_write_intent(text, *, has_media=False):
    """Return resolved/compound/clarification/no_write without mutating data."""
    raw = str(text or "").strip()
    low = raw.casefold()
    if not raw:
        return {"status": "no_write", "intents": []}

    # Read questions that mention a date and money must not be mistaken for a
    # new diary/reminder/expense write before the AI sees them. Keep explicit
    # write verbs authoritative so requests such as "Can you remind me..." are
    # still allowed through the mutation classifier.
    reminder_request = reminder_write_signal(raw)
    interrogative = bool(
        re.match(r"^(?:what|how|which|when|did|do|show|list)\b", low)
        or raw.endswith("?")
    )
    explicit_write_verb = bool(re.search(
        r"\b(?:remind|schedule|set|add|log|record|save|remember|create|"
        r"cancel|delete|remove|update|change|correct|mark)\b",
        low,
    )) or reminder_request
    if interrogative and not explicit_write_verb:
        return {
            "status": "no_write", "intents": [],
            "requires_clarification": False,
            "basis": "INTERROGATIVE_READ",
        }

    explicit = []
    if reminder_request:
        explicit.append("REMINDER")
    if re.search(r"\b(?:add|put|save|record)\b.{0,25}\b(?:my|our|the)?\s*diary\b|"
                 r"\b(?:diary|calendar)\s+(?:entry|event)\b", low):
        explicit.append("DIARY")

    cash_write = bool(re.search(
        r"\b(?:record|log)\b.{0,30}\b(?:ot|overtime|bonus|salary|refund|extra cash)\b",
        low,
    ))
    if cash_write:
        explicit.append("CASH")

    # A generic "record RM..." is an expense only when it is not clearly a
    # variable-income/cash instruction. This prevents one cash message from
    # advertising both ledger-expense and planning-cash writers.
    if (
        not cash_write
        and re.search(
            r"\b(?:log|record|add)\b.{0,25}\bexpense\b|"
            r"\b(?:log|record|add)\b.{0,25}\b(?:rm|myr|sgd)\s*\d|"
            r"\b(?:i\s+)?(?:spent|paid)\s+(?:RM|MYR|SGD|\d)",
            low, re.I,
        )
    ):
        explicit.append("EXPENSE")
    if re.search(r"\b(?:bill|payment|instalment|installment)\b.{0,25}"
                 r"\b(?:due|payable|need\s+to\s+pay)\b", low):
        explicit.append("OBLIGATION")
    if has_media and re.search(r"\b(?:remember|save|keep)\s+(?:this|it)\b", low):
        explicit.append("SAVED_MEMORY")
    if re.search(r"\b(?:start|create|save)\b.{0,30}\b(?:trip|holiday|vacation)\s+plan\b|"
                 r"\b(?:let'?s|want\s+to)\s+plan\b", low):
        explicit.append("PLAN")

    # Explicit multi-action wording is allowed: e.g. "put this in my diary
    # and remind me at 4". This is not ambiguity because the user asked for
    # both writes.
    explicit = list(dict.fromkeys(explicit))
    if len(explicit) > 1:
        return {
            "status": "compound",
            "intents": explicit,
            "requires_clarification": False,
        }
    if len(explicit) == 1:
        return {
            "status": "resolved",
            "intent": explicit[0],
            "intents": explicit,
            "basis": "EXPLICIT",
            "requires_clarification": False,
        }

    has_date = _date_signal(raw)
    has_money = _money_signal(raw)

    cash_natural = bool(
        has_money
        and re.search(r"\b(?:got|received|credited|earned|paid\s+me|came\s+in)\b", low)
        and re.search(r"\b(?:ot|overtime|bonus|salary|refund|extra cash)\b", low)
    )
    if cash_natural:
        return {
            "status": "resolved", "intent": "CASH",
            "intents": ["CASH"], "basis": "CLEAR_CASH_INFLOW",
            "requires_clarification": False,
        }

    expense_natural = bool(re.search(
        r"\b(?:bought|purchase(?:d)?|cost\s+me|paid\s+for)\b", low)
        and has_money)
    diary_natural = bool(
        has_date
        and not re.search(r"\b(?:expenses?|transactions?|payments?|spending)\b", low)
        and re.search(
            r"\b(?:i|we)\s+(?:have|got|am\s+going\s+to|are\s+going\s+to|"
            r"will\s+attend|am\s+attending|are\s+attending)\b|"
            r"\b(?:wedding|birthday|party|appointment|service|meeting|flight|"
            r"holiday|vacation|trip)\b", low)
    )

    correction_natural = bool(
        has_money
        and re.search(r"\b(?:correct|correction|change|fix|amend|update|wrong|actually)\b", low)
        and re.search(
            r"\b(?:expense|transaction|payment|purchase|spent|paid|amount|parking|toll|receipt)\b",
            low,
        )
    )

    if correction_natural:
        return {
            "status": "resolved", "intent": "EXPENSE",
            "intents": ["EXPENSE"], "basis": "FINANCIAL_CORRECTION",
            "requires_clarification": False,
        }

    if expense_natural and not diary_natural:
        return {
            "status": "resolved", "intent": "EXPENSE",
            "intents": ["EXPENSE"], "basis": "CLEAR_NATURAL_MEANING",
            "requires_clarification": False,
        }

    if diary_natural and not has_money:
        return {
            "status": "resolved", "intent": "DIARY",
            "intents": ["DIARY"], "basis": "CLEAR_NATURAL_MEANING",
            "requires_clarification": False,
        }

    # "Car service Friday RM200" is intentionally not guessed. It could be an
    # appointment, a reminder, or money already paid/expected.
    if has_date and has_money:
        return {
            "status": "clarification",
            "intents": ["DIARY", "REMINDER", "EXPENSE"],
            "requires_clarification": True,
            "question": (
                "Do you mean this is an appointment for your diary, "
                "something you want me to remind you about, or an expense "
                "you already paid?"
            ),
        }

    if has_date and re.search(
            r"\b(?:service|appointment|party|wedding|meeting|collect|pickup|"
            r"pick\s+up|renewal)\b", low):
        return {
            "status": "clarification",
            "intents": ["DIARY", "REMINDER"],
            "requires_clarification": True,
            "question": (
                "Should I put that in your diary, set a reminder, or both?"
            ),
        }

    return {
        "status": "no_write",
        "intents": [],
        "requires_clarification": False,
    }
