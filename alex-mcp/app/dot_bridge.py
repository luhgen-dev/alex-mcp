"""Experimental actual-dot bridge core. No listener, OAuth server, or model client.

An authenticated deployment adapter must inject the verified OAuth principal.
Never populate it from JSON-RPC arguments or an unverified request header.
Only shared shopping is exposed in this first test; existing providers are intact.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Callable

MAX_TEXT = 4000
MAX_ACTIONS = 12
REQUEST_TTL = 180
EVENT_NAME = "alex.request.created"


class BridgeError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _id(*parts):
    return hashlib.sha256(canonical(parts).encode()).hexdigest()


class DotBridge:
    """Durable request/action ledger, with injected trusted application adapters.

    Uses Alex's database so completion and its outbound reply commit together.
    A claim is committed BEFORE invoking a tool. A crash or exception between
    effect and result leaves UNCERTAIN/RUNNING, never an automatic retry.
    """

    def __init__(self, path, *, principal: str, enabled: Callable[[], bool],
                 resolve_actor, execute, events, clock=time.time):
        self.path, self.principal = path, principal
        self.enabled, self.resolve_actor = enabled, resolve_actor
        self.execute, self.events, self.clock = execute, events, clock
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS dot_requests(
                    request_id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL,
                    conversation TEXT NOT NULL, owner TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    actor_json TEXT NOT NULL, text TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING', reply TEXT,
                    outbound_id TEXT);
                CREATE TABLE IF NOT EXISTS dot_actions(
                    request_id TEXT NOT NULL, action_id TEXT NOT NULL,
                    tool TEXT NOT NULL, arguments TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(request_id,action_id));
            ''')

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def authorize(self, principal):
        if not self.enabled() or not self.principal or principal != self.principal:
            raise PermissionError("dot bridge is disabled or connection is not authorized")

    def ready(self):
        return bool(self.enabled() and self.principal and self.events.ready(self.principal))

    def enqueue(self, actor, text):
        """Called only below ingress authentication, never as a remote tool."""
        self.authorize(self.principal)
        if not self.ready():
            raise BridgeError("dot is not connected; no command was executed")
        if actor.conversation_type != "DIRECT_DM" or actor.source != "text":
            raise BridgeError("dot test currently accepts direct text messages only")
        if actor.read_scope not in (None, "family") or actor.private_handoff:
            raise BridgeError("dot test currently supports the shared shopping list only")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise BridgeError("dot test requires 1–4000 characters of text")
        # Revalidate the phone mapping at submission; immutable binding then
        # gets revalidated again on every read/action/completion.
        fresh = self.resolve_actor(actor.phone, actor.conversation_id,
                                   actor.conversation_type, actor.source_message_id)
        if fresh.user_id != actor.user_id:
            raise PermissionError("household identity changed")
        request_id = _id("dot", actor.source_message_id)
        now = self.clock()
        subscription_ids = self.events.active_ids(self.principal)
        if len(subscription_ids) != 1:
            raise BridgeError("exactly one verified dot subscription is required")
        subscription_id = subscription_ids[0]
        self.expire_pending()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM dot_requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["actor_json"] != canonical(asdict(actor)) or old["text"] != text:
                    raise BridgeError("source message conflict")
                return {"request_id": request_id, "state": old["state"]}
            # A newer command supersedes only untouched older turns. An older
            # command with any execution claim must first be reconciled.
            active = conn.execute("""SELECT request_id FROM dot_requests
                WHERE conversation=? AND state IN ('PENDING','REVIEW')""", (actor.conversation_id,)).fetchall()
            for row in active:
                if conn.execute("SELECT 1 FROM dot_actions WHERE request_id=? LIMIT 1", (row[0],)).fetchone():
                    raise BridgeError("Previous dot command needs completion or reconciliation; no new action was taken")
            conn.execute("UPDATE dot_requests SET state='SUPERSEDED' WHERE conversation=? AND state='PENDING'",
                         (actor.conversation_id,))
            conn.execute("""INSERT INTO dot_requests(request_id,source_id,conversation,owner,
                subscription_id,actor_json,text,created,expires) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (request_id, actor.source_message_id, actor.conversation_id, actor.user_id, subscription_id,
                          canonical(asdict(actor)), text, now, now + REQUEST_TTL))
        # publish() must persist/retry delivery. A delivery ACK is not completion.
        try:
            self.publish_pending()
        except Exception:
            # Durable request itself is the publication intent. The runtime
            # drain retries it; transport failure must not invoke another brain.
            pass
        return {"request_id": request_id, "state": "PENDING"}

    def expire_pending(self):
        """Close expired turns; uncertain effects remain quarantined for review."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM dot_requests WHERE state='PENDING' AND expires<=?",
                                (self.clock(),)).fetchall()
            for row in rows:
                try:
                    self._request(conn, row["request_id"], active=False)
                except PermissionError:
                    conn.execute("UPDATE dot_requests SET state='REVOKED' WHERE request_id=?", (row["request_id"],))
                    continue
                actions = conn.execute("SELECT tool,state,result FROM dot_actions WHERE request_id=?",
                                       (row["request_id"],)).fetchall()
                uncertain = any(a["state"] != "DONE" for a in actions)
                state = "REVIEW" if uncertain else "EXPIRED"
                if uncertain:
                    reply = "The dot request timed out with an uncertain tool result. An action may have happened. I won't retry it; operator review is needed."
                elif actions:
                    summary = [{"tool": a["tool"], "result": json.loads(a["result"])} for a in actions]
                    reply = "The dot reply timed out. These tool results were recorded (no further action was taken): " + canonical(summary)[:3000]
                else:
                    reply = "The dot request timed out before any tool executed. No action was taken."
                oid = "dot-timeout:" + row["request_id"]
                conn.execute("""INSERT INTO outbound_messages(outbound_id,source_message_id,
                    conversation_id,kind,text_body) VALUES(?,?,?,'TEXT',?)""",
                             (oid,row["source_id"],row["conversation"],reply))
                conn.execute("UPDATE dot_requests SET state=?,reply=?,outbound_id=? WHERE request_id=?",
                             (state,reply,oid,row["request_id"]))

    def publish_pending(self):
        """Drain durable publication intent, including a crash after enqueue."""
        self.authorize(self.principal)
        self.expire_pending()
        with self.connect() as conn:
            rows = conn.execute("""SELECT request_id,created,subscription_id FROM dot_requests
                WHERE state='PENDING' AND expires>? ORDER BY created LIMIT 32""",
                (self.clock(),)).fetchall()
        for row in rows:
            self.events.publish(self.principal, request_id=row["request_id"],
                event_id=row["request_id"],
                occurred_at=datetime.fromtimestamp(row["created"], timezone.utc).isoformat(),
                _subscription_id=row["subscription_id"])
        return self.events.drain_pending(self.principal)

    def _request(self, conn, request_id, *, active=True):
        row = conn.execute("SELECT * FROM dot_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise BridgeError("unknown request")
        saved = json.loads(row["actor_json"])
        actor = self.resolve_actor(saved["phone"], saved["conversation_id"],
                                   saved["conversation_type"], saved["source_message_id"])
        if actor.user_id != row["owner"] or "FAMILY_SHARED" not in actor.allowed_spaces:
            raise PermissionError("sender access revoked")
        # Restrict below the model even for DM actors with private membership.
        actor = replace(actor, allowed_spaces=("FAMILY_SHARED",), read_scope="family",
                        trusted_text=row["text"], source="text")
        if active and row["subscription_id"] not in self.events.active_ids(self.principal):
            raise BridgeError("original dot subscription expired or was revoked")
        if active and (row["state"] != "PENDING" or row["expires"] <= self.clock()):
            raise BridgeError("request is completed, superseded, or expired; no new execution allowed")
        return row, actor

    def read(self, principal, request_id):
        self.authorize(principal)
        with self.connect() as conn:
            row, _ = self._request(conn, request_id, active=False)
            actions = [dict(r) for r in conn.execute("""SELECT action_id,tool,state,result
                FROM dot_actions WHERE request_id=? ORDER BY rowid""", (request_id,))]
        for action in actions:
            action["result"] = json.loads(action["result"]) if action["result"] else None
        return {"request_id": request_id, "text": row["text"],
                "state": "EXPIRED" if row["state"] == "PENDING" and row["expires"] <= self.clock() else row["state"],
                "expires_at": row["expires"], "actions": actions, "reply": row["reply"]}

    def call(self, principal, request_id, action_id, tool, arguments):
        self.authorize(principal)
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 80:
            raise BridgeError("action_id must be a stable 1–80 character identifier")
        validate_action(tool, arguments)
        encoded = canonical(arguments)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # ACL checked even on a cached result. Completed/expired requests
            # may retrieve old results but cannot initiate another effect.
            row, actor = self._request(conn, request_id, active=False)
            prior = conn.execute("SELECT * FROM dot_actions WHERE request_id=? AND action_id=?",
                                 (request_id, action_id)).fetchone()
            if prior and (prior["tool"] != tool or prior["arguments"] != encoded):
                raise BridgeError("action_id was already used with different arguments")
            # Replay protection is server-owned, not merely a model instruction.
            # Same mutation/args in one source request cannot get a fresh effect
            # by inventing a different action_id. Distinct item operations remain allowed.
            if prior is None and tool != "list_shopping_items":
                prior = conn.execute("SELECT * FROM dot_actions WHERE request_id=? AND tool=? AND arguments=?",
                                     (request_id, tool, encoded)).fetchone()
            if prior:
                if prior["tool"] != tool or prior["arguments"] != encoded:
                    raise BridgeError("action_id was already used with different arguments")
                if prior["state"] == "DONE":
                    if prior["action_id"] != action_id:
                        if conn.execute("SELECT count(*) FROM dot_actions WHERE request_id=?", (request_id,)).fetchone()[0] >= MAX_ACTIONS:
                            raise BridgeError("action limit reached")
                        conn.execute("INSERT INTO dot_actions VALUES(?,?,?,?,?,?)",
                                     (request_id,action_id,tool,encoded,"DONE",prior["result"]))
                    return json.loads(prior["result"])
                raise BridgeError("execution uncertain; inspect status, never retry with a new action_id")
            self._request(conn, request_id)
            if conn.execute("SELECT 1 FROM dot_actions WHERE request_id=? AND state!='DONE'", (request_id,)).fetchone():
                raise BridgeError("previous execution uncertain; no further actions allowed")
            if conn.execute("SELECT count(*) FROM dot_actions WHERE request_id=?", (request_id,)).fetchone()[0] >= MAX_ACTIONS:
                raise BridgeError("action limit reached")
            conn.execute("INSERT INTO dot_actions VALUES(?,?,?,?,?,NULL)",
                         (request_id, action_id, tool, encoded, "RUNNING"))
        actor = replace(actor, action_key="dot:" + _id(request_id, action_id))
        try:
            result = self.execute(actor, tool, arguments)
            encoded_result = canonical(result)
        except Exception:
            with self.connect() as conn:
                conn.execute("UPDATE dot_actions SET state='UNCERTAIN' WHERE request_id=? AND action_id=?",
                             (request_id, action_id))
            raise BridgeError("Tool did not return a durable result; execution may have occurred. Reconcile before proceeding.") from None
        with self.connect() as conn:
            conn.execute("UPDATE dot_actions SET state='DONE',result=? WHERE request_id=? AND action_id=?",
                         (encoded_result, request_id, action_id))
        return result

    def complete(self, principal, request_id, reply):
        self.authorize(principal)
        if not isinstance(reply, str) or not reply.strip() or len(reply) > MAX_TEXT:
            raise BridgeError("reply must contain 1–4000 characters")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row, actor = self._request(conn, request_id, active=False)
            if row["state"] == "COMPLETED":
                if row["reply"] != reply:
                    raise BridgeError("request already completed with a different reply")
                return {"status": "queued", "outbound_id": row["outbound_id"]}
            self._request(conn, request_id)
            if conn.execute("SELECT 1 FROM dot_actions WHERE request_id=? AND state!='DONE'", (request_id,)).fetchone():
                raise BridgeError("uncertain execution cannot be reported as complete")
            outbound_id = "dot:" + request_id
            conn.execute("""INSERT INTO outbound_messages(outbound_id,source_message_id,
                conversation_id,kind,text_body) VALUES(?,?,?,'TEXT',?)""",
                         (outbound_id, actor.source_message_id, actor.conversation_id, reply))
            conn.execute("UPDATE dot_requests SET state='COMPLETED',reply=?,outbound_id=? WHERE request_id=?",
                         (reply, outbound_id, request_id))
        return {"status": "queued", "outbound_id": outbound_id}


def validate_action(tool, args):
    permitted = {
        "add_shopping_item": {"item", "quantity", "notes"},
        "list_shopping_items": {"include_purchased", "limit"},
        "update_shopping_item": {"item_id", "status", "quantity", "notes", "item"},
    }
    if tool not in permitted or not isinstance(args, dict) or set(args) - permitted[tool]:
        raise BridgeError("Only the shared-shopping test tools and declared arguments are allowed")
    for key, value in args.items():
        if key == "include_purchased":
            if type(value) is not bool:
                raise BridgeError("include_purchased must be boolean")
        elif key == "limit":
            if type(value) is not int or not 1 <= value <= 50:
                raise BridgeError("limit must be an integer from 1 to 50")
        elif not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise BridgeError("text arguments must be 1–500 characters")
    required = {"add_shopping_item": "item", "update_shopping_item": "item_id"}.get(tool)
    if required and required not in args:
        raise BridgeError(required + " is required")
    if "status" in args and args["status"] not in ("OPEN", "PURCHASED", "REMOVED"):
        raise BridgeError("status must be OPEN, PURCHASED, or REMOVED")


def execute_shopping(actor, tool, arguments):
    """Existing deterministic executors; no old semantic brain or provider."""
    import services
    validate_action(tool, arguments)
    if tool == "add_shopping_item":
        return services.add_shopping_item(actor, **arguments, shared=True)
    if tool == "list_shopping_items":
        return services.list_shopping_items(actor, **arguments, scope="family")
    return services.update_shopping_item(actor, **arguments)
