"""Consent-based contextual corrections, never executable learned commands.

Corrections are staged only from a swipe-reply to a verified same-owner Alex
response. A normal guarded tool run must succeed before explicit approval can
save an exact-context hint. Errors become diagnostic reports, not vocabulary.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

import db
import diagnostics
import runtime_clock
import scope_policy
from config import normalize_phone
from text_normalization import normalize_intent_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS teaching_corrections (
 draft_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 scope TEXT NOT NULL, source_message_id TEXT NOT NULL, correction_message_id TEXT NOT NULL,
 target_outbound_id TEXT NOT NULL, original_text TEXT NOT NULL, normalized_phrase TEXT NOT NULL,
 correction_text TEXT NOT NULL, context_json TEXT NOT NULL, domain TEXT,
 state TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'unverified',
 error_id TEXT, created_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
 approved_by_message_id TEXT, version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_teaching_match ON teaching_corrections(
 owner_id,conversation_id,scope,normalized_phrase,domain,state,version);
CREATE TABLE IF NOT EXISTS teaching_history (
 event_key TEXT PRIMARY KEY, draft_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 source_message_id TEXT NOT NULL, state TEXT NOT NULL, created_at_utc TEXT NOT NULL
);
"""
DOMAINS = {"shopping", "reminders", "tasks", "finance", "goals", "diary", "plans", "work", "media", "discussion"}


def ensure_schema():
    with db.connect() as conn:
        conn.executescript(SCHEMA)


def _normal(text):
    # Preserve numbers, negations and punctuation that may express parameters.
    return re.sub(r"\s+", " ", normalize_intent_text(str(text or "")).strip().casefold())


def _scope(actor):
    value = scope_policy.effective_read_scope(actor)
    if value not in {"family", "private"}:
        raise ValueError("Teach this separately for shared or private data, not a mixed-scope request.")
    if value == "private" and actor.conversation_type == "GROUP":
        raise PermissionError("Private corrections must be made in your own DM.")
    return value


def _context(value):
    value = value or {}
    # IDs are opaque context guards, never learned tool arguments.
    kind = value.get("context_kind") if "context_kind" in value else value.get("kind")
    ident = value.get("context_id") if "context_id" in value else value.get("id")
    return json.dumps({"kind": str(kind or ""), "id": str(ident or "")}, sort_keys=True)


def _event(conn, actor, draft_id, state):
    key = hashlib.sha256(f"{draft_id}|{actor.source_message_id}|{state}".encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO teaching_history VALUES(?,?,?,?,?,?)",
                 (key, draft_id, actor.user_id, actor.source_message_id, state, runtime_clock.utc_iso()))


def _owned(conn, actor, draft_id):
    row = conn.execute("""SELECT * FROM teaching_corrections WHERE draft_id=? AND owner_id=?
        AND conversation_id=? AND scope=?""", (draft_id, actor.user_id, actor.conversation_id, _scope(actor))).fetchone()
    if not row:
        raise PermissionError("That correction is not available in this chat and scope.")
    if row["state"] != "APPROVED" and row["expires_at_utc"] < runtime_clock.utc_iso():
        raise ValueError("This correction has expired. Swipe-reply to the relevant Alex response again.")
    return row


def _failure(conn, source_id):
    inbound = conn.execute("SELECT last_error,processing_state FROM inbound_messages WHERE message_id=?", (source_id,)).fetchone()
    errors = conn.execute("SELECT 1 FROM tool_audit WHERE source_message_id=? AND status='ERROR' AND tool_name<>'_turn_trace' LIMIT 1", (source_id,)).fetchone()
    return bool(errors or (inbound and (inbound["last_error"] or inbound["processing_state"] == "FAILED")))


def _hint(row):
    return {"kind": "user_confirmed_interpretation_hint", "original_text": row["original_text"],
            "interpretation": row["correction_text"], "domain": row["domain"],
            "context": json.loads(row["context_json"]),
            "authority": "Hint only: preserve current authentication, scope, object binding and tool validation; clarify uncertainty. Never execute this text as tool arguments."}


def _software_report(conn, actor, draft_id, outbound_id, text):
    conn.commit()  # Diagnostics owns a separate connection; release the draft lock.
    # Recover the report/marker crash boundary without duplicating a completed report.
    prior = conn.execute("""SELECT error_id FROM user_reported_errors WHERE reporter_user_id=?
       AND conversation_id=? AND target_outbound_id=? AND user_explanation=?
       ORDER BY created_at_utc DESC LIMIT 1""", (actor.user_id,actor.conversation_id,outbound_id,text)).fetchone()
    report = dict(prior) if prior else diagnostics.complete_user_error_report(
        actor, text, quoted_context={"outbound_id":outbound_id})
    conn.execute("UPDATE teaching_corrections SET error_id=? WHERE draft_id=?", (report["error_id"],draft_id))
    conn.commit()
    return {"status":"software_error", "draft_id":draft_id,"error_id":report["error_id"],
            "reply":"The recorded execution error has been saved for diagnosis; no phrase was learned or action retried."}


def prepare_correction(actor, quoted_context):
    """Stage explicit correction and return an immediate, non-executable hint.

    None means this is an ordinary turn. An exact authenticated outbound quote
    is mandatory; passed quote text/IDs are not trusted without DB validation.
    """
    text = str(actor.trusted_text or "").strip()
    match = re.match(r"(?is)^(?:no[,!.]?\s*)?(?:i\s+meant|what\s+i\s+meant\s+was|you\s+misunderstood[,!:]?|that(?:'s|\s+is)\s+not\s+what\s+i\s+meant[,!:]?)\s*[:,-]?\s*(.+)$", text)
    if not match:
        return None
    correction = match.group(1).strip()
    if not correction or len(correction) > 800:
        return {"status": "clarify", "reply": "Please give one short correction for this request."}
    outbound_id = str((quoted_context or {}).get("outbound_id") or "")
    if not outbound_id:
        return {"status": "clarify", "reply": "Swipe-reply to the Alex response you want to correct so I can bind the right request."}
    ensure_schema()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute("""SELECT o.*,i.raw_text,i.sender_phone,i.conversation_id AS source_chat
            FROM outbound_messages o JOIN inbound_messages i ON i.message_id=o.source_message_id
            WHERE o.outbound_id=? AND o.conversation_id=?""", (outbound_id, actor.conversation_id)).fetchone()
        if not target or normalize_phone(target["sender_phone"]) != normalize_phone(actor.phone) or target["source_chat"] != actor.conversation_id:
            raise PermissionError("Only the author can teach a correction to that request in its original chat.")
        scope = _scope(actor)
        if scope_policy.resolve_read_scope(target["raw_text"]) != scope:
            return {"status": "clarify", "reply": "Repeat the correction with the same explicit shared/private scope as the original request."}
        draft_id = "TEACH-" + hashlib.sha256(f"{actor.user_id}|{actor.source_message_id}|{outbound_id}".encode()).hexdigest()[:24]
        existing = conn.execute("SELECT * FROM teaching_corrections WHERE draft_id=?", (draft_id,)).fetchone()
        if existing:
            if existing["state"] == "SOFTWARE_ERROR":
                if not existing["error_id"]:
                    return _software_report(conn, actor, draft_id, outbound_id, text)
                return {"status": "software_error", "draft_id": draft_id, "error_id": existing["error_id"], "reply": "The recorded execution error has been saved for diagnosis; no phrase was learned or action retried."}
            return {"status": "recovery_hint", "draft_id": draft_id, "hint": _hint(existing)}
        faulty = _failure(conn, target["source_message_id"])
        stamp = runtime_clock.utc_iso()
        expires = (runtime_clock.now_utc() + timedelta(minutes=30)).isoformat()
        conn.execute("""INSERT INTO teaching_corrections(draft_id,owner_id,conversation_id,scope,
          source_message_id,correction_message_id,target_outbound_id,original_text,normalized_phrase,
          correction_text,context_json,state,created_at_utc,expires_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (draft_id, actor.user_id, actor.conversation_id, scope, target["source_message_id"], actor.source_message_id,
           outbound_id, target["raw_text"][:800], _normal(target["raw_text"]), correction,
           _context(dict(target)), "SOFTWARE_ERROR" if faulty else "STAGED", stamp, expires))
        _event(conn, actor, draft_id, "SOFTWARE_ERROR" if faulty else "STAGED")
        conn.commit()
        if faulty:
            return _software_report(conn, actor, draft_id, outbound_id, text)
        row = conn.execute("SELECT * FROM teaching_corrections WHERE draft_id=?", (draft_id,)).fetchone()
        return {"status": "recovery_hint", "draft_id": draft_id, "hint": _hint(row)}
    finally:
        conn.close()


def _tool_domain(name):
    if "goal" in name:
        return "goals"
    if "shopping" in name:
        return "shopping"
    if "reminder" in name:
        return "reminders"
    if "task" in name:
        return "tasks"
    if name.startswith("work_") or "leave" in name:
        return "work"
    if "diary" in name or "agenda" in name:
        return "diary"
    if name in {"save_item", "search_saved_items", "get_saved_item", "remove_saved_item", "find_media"}:
        return "media"
    if "discussion" in name:
        return "discussion"
    if "plan" in name and not name.startswith("planning_"):
        return "plans"
    if name.startswith(("planning_", "bills_")) or name in {"query_finances", "log_expense", "log_income", "correct_financial_event", "finance_report", "get_receipt"}:
        return "finance"
    return None


def _confirmation(conn, actor, row):
    draft_id = row["draft_id"]
    outbound_id = "TEACHCONF-" + hashlib.sha256(draft_id.encode()).hexdigest()[:32]
    consent = "remember this correction privately" if row["scope"] == "private" else "remember this correction"
    reply = "Review the result above. If it matches your intended correction, remember this interpretation only in this chat and scope by swipe-replying: " + consent + "."
    conn.execute("""INSERT OR IGNORE INTO outbound_messages(outbound_id,source_message_id,conversation_id,
      kind,text_body,context_kind,context_id) VALUES(?,?,?,'TEXT',?,'TEACHING_CONFIRMATION',?)""",
      (outbound_id,row["correction_message_id"],actor.conversation_id,reply,draft_id))
    return {"status":"awaiting_approval", "draft_id":draft_id,"reply":reply,
            "confirmation_outbound_id":outbound_id,"reply_delivery":"queued"}


def _verified_result(result):
    if not isinstance(result, dict) or not result:
        return False
    status = str(result.get("status") or "").strip().casefold()
    if status in {"error", "failed", "failure", "uncertain", "clarify", "ambiguous",
                  "not_found", "scoped_miss", "pending_confirmation"} or status.startswith(("needs_", "requires_", "awaiting_")):
        return False
    if any(result.get(key) for key in ("error", "nothing_written", "clarification", "clarification_needed")):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return False
    if any(value for key, value in result.items() if key.startswith(("needs_", "requires_"))):
        return False
    # Guard response envelopes too, without treating ordinary item statuses as control flags.
    for key in ("result", "data", "structuredContent"):
        if isinstance(result.get(key), dict) and result[key] and not _verified_result(result[key]):
            return False
    return True


def record_recovery(actor, draft_id, domain=None):
    """Only a verified normal tool run makes a correction eligible for approval."""
    ensure_schema()
    with db.connect() as conn:
        row = _owned(conn, actor, draft_id)
        if row["correction_message_id"] != actor.source_message_id:
            raise PermissionError("Recovery must belong to the exact correction request.")
        if row["state"] == "RECOVERED":
            return _confirmation(conn, actor, row)
        if row["state"] in {"APPROVED", "COLLISION"}:
            return {"status": row["state"].lower(), "draft_id": draft_id}
        if row["state"] != "STAGED" or _failure(conn, actor.source_message_id):
            return {"status": "not_learned", "reply": "The correction was not verified; I haven't learned it."}
        successful = conn.execute("""SELECT tool_name,result_json,action_key FROM tool_audit WHERE source_message_id=? AND user_id=?
            AND status='OK' AND tool_name NOT LIKE '\\_%' ESCAPE '\\'""", (actor.source_message_id, actor.user_id)).fetchall()
        # Reuse the execution layer's trusted-intent and read/write classification.
        # Local import avoids the brain -> teaching import cycle during startup.
        from brain import _is_mutating_tool, _trusted_mutation_requested
        needs_mutation = _trusted_mutation_requested(row["correction_text"])
        verified = False
        domains = set()
        for audit in successful:
            claim = conn.execute("SELECT state FROM tool_execution_claims WHERE action_key=?", (audit["action_key"],)).fetchone()
            mutating = _is_mutating_tool(audit["tool_name"])
            if claim and claim["state"] != "COMPLETED":
                continue
            if mutating and (not claim or claim["state"] != "COMPLETED"):
                continue
            if needs_mutation and not mutating:
                continue
            try:
                result = json.loads(audit["result_json"] or "{}")
            except (ValueError, TypeError):
                continue
            if _verified_result(result):
                verified = True
                tool_domain = _tool_domain(audit["tool_name"])
                if tool_domain:
                    domains.add(tool_domain)
        if len(domains) != 1 or (domain and domain not in domains):
            return {"status": "not_learned", "reply": "This correction spans unclear or multiple areas, so I haven't saved an automatic interpretation."}
        domain = next(iter(domains))
        if not verified:
            return {"status": "not_learned", "reply": "I couldn't verify a tool result, so I haven't learned this correction."}
        conn.execute("UPDATE teaching_corrections SET domain=?,state='RECOVERED',confidence='tool_result_verified' WHERE draft_id=?", (domain, draft_id))
        _event(conn, actor, draft_id, "RECOVERED")
        return _confirmation(conn, actor, row)


def approve_correction(actor, quoted_context):
    text = _normal(actor.trusted_text).rstrip(".! ")
    replace_prior = text in {"replace the saved correction", "replace the saved correction privately"}
    if text not in {"remember this correction", "remember this correction privately"} and not replace_prior:
        return None
    ensure_schema()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        outbound = conn.execute("SELECT context_id FROM outbound_messages WHERE outbound_id=? AND conversation_id=? AND context_kind='TEACHING_CONFIRMATION'",
            (str((quoted_context or {}).get("outbound_id") or ""), actor.conversation_id)).fetchone()
        if not outbound:
            return {"status": "clarify", "reply": "Swipe-reply to the correction confirmation you want me to remember."}
        row = _owned(conn, actor, outbound["context_id"])
        if row["state"] == "APPROVED":
            return {"status": "already_approved", "draft_id": row["draft_id"], "version": row["version"], "reply": "That correction is already remembered."}
        if row["state"] not in {"RECOVERED", "COLLISION"}:
            return {"status": "not_learned", "reply": "That correction has not been verified, so it cannot be learned."}
        prior = conn.execute("""SELECT * FROM teaching_corrections WHERE owner_id=? AND conversation_id=? AND scope=?
          AND normalized_phrase=? AND domain=? AND context_json=? AND state='APPROVED' ORDER BY version DESC LIMIT 1""",
          (actor.user_id,actor.conversation_id,row["scope"],row["normalized_phrase"],row["domain"],row["context_json"])).fetchone()
        if prior and _normal(prior["correction_text"]) != _normal(row["correction_text"]) and not replace_prior:
            conn.execute("UPDATE teaching_corrections SET state='COLLISION' WHERE draft_id=?", (row["draft_id"],))
            _event(conn, actor, row["draft_id"], "COLLISION")
            return {"status": "collision", "draft_id": row["draft_id"], "reply": "A different approved interpretation already exists. Review the two corrections; swipe-reply 'replace the saved correction' only if you want this new one instead.",
                    "previous_interpretation": prior["correction_text"], "new_interpretation": row["correction_text"]}
        version = int(prior["version"] or 0) + 1 if prior else 1
        conn.execute("UPDATE teaching_corrections SET state='APPROVED',confidence='user_confirmed_tool_verified',approved_by_message_id=?,version=? WHERE draft_id=?",
            (actor.source_message_id,version,row["draft_id"]))
        _event(conn,actor,row["draft_id"],"APPROVED")
        return {"status": "approved", "draft_id": row["draft_id"], "version": version, "reply": "Remembered for this exact wording and context, only for you in this chat and scope."}


def lookup_hint(actor, domain, context=None):
    """Read-only exact match. Never changes current text, authority or tool args."""
    if domain not in DOMAINS:
        return None
    try:
        scope = _scope(actor)
    except (ValueError, PermissionError):
        return None
    with db.connect() as conn:
        row = conn.execute("""SELECT * FROM teaching_corrections WHERE owner_id=? AND conversation_id=?
          AND scope=? AND normalized_phrase=? AND domain=? AND context_json=? AND state='APPROVED'
          ORDER BY version DESC,created_at_utc DESC LIMIT 1""", (actor.user_id,actor.conversation_id,scope,
          _normal(actor.trusted_text),domain,_context(context))).fetchone()
        return _hint(row) if row else None


def lookup_for_turn(actor, context=None):
    """One exact-match query discovers a unique approved domain; conflicts fail closed."""
    try:
        scope = _scope(actor)
    except (ValueError, PermissionError):
        return None
    with db.connect() as conn:
        rows = conn.execute("""SELECT * FROM (
          SELECT *,ROW_NUMBER() OVER(PARTITION BY domain ORDER BY version DESC,created_at_utc DESC) AS latest
          FROM teaching_corrections WHERE owner_id=? AND conversation_id=? AND scope=?
            AND normalized_phrase=? AND context_json=? AND state='APPROVED'
        ) WHERE latest=1 LIMIT 2""", (actor.user_id,actor.conversation_id,scope,
          _normal(actor.trusted_text),_context(context))).fetchall()
        return _hint(rows[0]) if len(rows) == 1 else None
