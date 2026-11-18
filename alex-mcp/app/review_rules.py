"""Small response repairs. Historical and live evaluation use the same pure checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from text_normalization import normalize_intent_text


def normal(value):
    return normalize_intent_text(str(value or "")).casefold().strip()


FEEDBACK = re.compile(
    r"^(?:hey\s+)?(?:alex[,\s]+)?(?:i think (?:this|that|you) should (?:be corrected|handle|respond|answer)|"
    r"(?:next time|from now on|in future)[,\s]+(?:(?:when|if) .{0,180}\b(?:respond|answer|return|give me both|send me the (?:available|other)|tell me which|show me the other)\b|please (?:show|include|answer)|"
    r"(?:show|include|answer|respond|give me both|send (?:the|me the) (?:available|other)))|"
    r"(?:i(?:'d| would) like you to|you should) (?:change how|handle this|correct this)|"
    r"(?:feature request|suggestion for alex)\b|(?:i prefer|please use|keep|make) (?:your |the )?(?:replies|answers|responses) |"
    r"(?:next time|from now on|in future)[,\s]+(?:keep|make|use) )"
)
REPAIR_NAMES = {
    "utility_coverage": "account for every requested utility",
    "receipt_delivery": "verify original receipt delivery",
    "empty_promise": "replace an unfinished lookup promise with an outcome",
}
# Limit automatic intervention to fresh lookups. Never turn a quotation,
# schedule, historical query or a change request into authority to act.
EXCLUDED = re.compile(
    r"\b(?:if|unless|when|next time|instead|do not|don't|never|stop|cancel|remember|"
    r"remind|schedule|every|yesterday|last month|history|historical|previous|"
    r"pay|paid|delete|remove|add|save|log|record|send to|forward|share|why|example|turn on|turn off|unlock|toggle|instead|except)\b"
)
RECEIPT = re.compile(r"\b(?:send|show|retrieve|fetch|get|attach|open)\b.{0,65}\b(?:receipt|resit)\b")
LOOKUP = re.compile(r"\b(?:check|find|look up|search|how much|price|retrieve|fetch|show)\b")
PROMISE = re.compile(
    r"^(?:sure[,!. ]*|ok(?:ay)?[,!. ]*)?(?:(?:i(?:'m| am) (?:working on|checking|looking into)|"
    r"i(?:'ll| will) (?:get (?:back|it)|check|look (?:it up|into)|send|retrieve)).{0,135})$",
    re.I,
)
LIMITATION = re.compile(
    r"\b(?:couldn't|could not|can't|cannot|unable|unavailable|no match|not found|which|choose)\b"
)
CLAIM = re.compile(r"\b(?:attached|here (?:is|it is|you go)|i(?:'ve| have)? sent|i(?:'ll| will) send)\b")


@dataclass(frozen=True)
class Repair:
    rule: str
    instruction: str
    fallback: str


def applicable(text):
    text = normal(text)
    if EXCLUDED.search(text) or FEEDBACK.search(text):
        return set()
    rules = set()
    if (
        set(re.findall(r"\b(?:tnb|saj)\b", text)) == {"tnb", "saj"}
        and re.search(r"\bbills?\b|\bbalances?\b", text)
        and (LOOKUP.search(text) or text.endswith("?"))
    ):
        rules.add("utility_coverage")
    if RECEIPT.search(text) and not re.search(r"\b(?:list|details|amount|total|summary|number)\b", text):
        rules.add("receipt_delivery")
    if LOOKUP.search(text):
        rules.add("empty_promise")
    return rules


def evaluate(text, reply, providers=(), attachment=False):
    """Return a bounded repair only when the original read request supports it."""
    rules = applicable(text)
    answer = normal(reply)
    if "utility_coverage" in rules:
        missing = [
            p for p in ("tnb", "saj") if p not in providers or not re.search(r"\b" + p + r"\b", answer)
        ]
        if missing:
            labels = " and ".join(p.upper() for p in missing)
            if all(
                re.search(r"\b" + p + r"\b.{0,55}(?:unconfirmed|unavailable|couldn.t|could not)", answer)
                for p in missing
            ):
                return None
            return Repair(
                "utility_coverage",
                "Account for both TNB and SAJ. Check only the missing utility and keep the verified result for the other.",
                f"{labels} — current balance unconfirmed.",
            )
    original_requested = re.search(r"\b(?:original|image|photo|file)\b", normal(text))
    claims_receipt = CLAIM.search(answer) or (original_requested and answer and "?" not in answer)
    if "receipt_delivery" in rules and not attachment and not LIMITATION.search(answer) and claims_receipt:
        return Repair(
            "receipt_delivery",
            "The original receipt is missing. Use the authorized retrieval tools or ask which receipt if ambiguous.",
            "I couldn't retrieve the original receipt for this request.",
        )
    if "empty_promise" in rules and PROMISE.fullmatch(answer) and not attachment:
        return Repair(
            "empty_promise",
            "Finish the requested lookup using authorized tools, or state what is unavailable.",
            "I couldn't verify the requested result.",
        )
    return None


def replay_verified(problem, text, reply, providers, attachment, verified_bill_reply=""):
    """Check the failing example, the candidate wording and near-miss controls without I/O."""
    old = evaluate(text, reply, providers, attachment)
    if not old or old.rule != problem.rule:
        return False
    candidate = (
        (verified_bill_reply + "\n" + problem.fallback).strip()
        if problem.rule == "utility_coverage"
        else problem.fallback
    )
    if evaluate(text, candidate, providers, attachment):
        return False
    return (
        evaluate("Show my TNB and SAJ bills", "TNB RM10. SAJ RM5.", ("tnb", "saj")) is None
        and evaluate("Send my receipt", "Here it is.", attachment=True) is None
        and not applicable("Don't check my TNB and SAJ bills")
    )


# These tools read existing records or public catalogues. Hints may only
# prioritize a tool already offered by the normal router on the current turn.
LOOKUP_TOOLS = {
    "find_receipts",
    "search_manjaku_prices",
    "search_mydin_prices",
    "bills_list",
    "get_agenda",
    "get_agenda_range",
    "list_tasks",
    "list_reminders",
    "work_schedule",
    "work_day",
    "list_shopping_items",
    "search_saved_items",
}
STOP = set(
    "a an the my our me i can could would you we in at on to of for is are how much what whats please "
    "look up get show check find search fetch price prices cost current again tell alex".split()
)


def lookup_words(text):
    value = normal(text)
    if not LOOKUP.search(value) or EXCLUDED.search(value):
        return frozenset()
    return frozenset(re.findall(r"[^\W_]+", value)) - STOP


def similar_lookup(left, right):
    # Explicit changes of retailer, utility or size must supersede older context.
    key_terms = {"manjaku", "mydin", "tnb", "saj", "digi", "xl", "xxl", "xs", "small", "large"}
    if (left & key_terms) != (right & key_terms):
        return False
    return len(left & right) >= 3 and len(left & right) / len(left | right) >= 0.70


def feedback_repair(text):
    """A narrowly stated request to return available bills can be tested on synthetic evidence."""
    value = normal(text)
    if not value.startswith(("next time", "from now on", "alex, next time", "in future")):
        return None
    if not re.search(r"\b(?:tnb|saj|bills?)\b", value) or not re.search(
        r"\b(?:fails?|unavailable|missing)\b", value
    ):
        return None
    if not re.search(
        r"\b(?:send|return|show|give me) (?:me )?(?:the )?(?:other|available|successful|one that worked)\b",
        value,
    ):
        return None
    if re.search(r"\b(?:never|don't|do not|pay|private|group|forward|share|instead|only if)\b", value):
        return None
    case = ("Check my TNB and SAJ bills", "TNB RM10.", {"tnb"}, False)
    problem = evaluate(*case)
    return problem.rule if problem and replay_verified(problem, *case, "TNB checked.") else None
