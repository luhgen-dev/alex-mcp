"""Draft MCP 2.0 Events transport. No listener, credentials or subscriptions at import.

The caller MUST supply an authenticated principal and a current authorization
check. This module is not authentication middleware. A webhook ACK means receipt,
never completion of a household action. Secrets must be kept in protected storage.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from urllib.parse import urlsplit

EVENT_NAME = "alex.request.created"
MAX_BODY = 262144
MAX_ATTEMPTS = 4


class EventError(ValueError):
    def __init__(self, reason: str, code: int = -32602):
        super().__init__(reason)
        self.reason, self.code = reason, code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def event_definition():
    return {
        "name": EVENT_NAME,
        "description": "An authenticated household request is awaiting interpretation. Read it using its request_id.",
        "delivery": ["webhook"],
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "payloadSchema": {"type": "object", "properties": {"request_id": {"type": "string"}},
                          "required": ["request_id"], "additionalProperties": False},
    }


def _key(secret):
    try:
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            raise ValueError()
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise EventError("invalid_signing_secret") from None


def validate_url(url):
    """Syntax/literal validation; sender also validates ALL resolved IPs on connect."""
    try:
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        if p.port not in (None, 443):
            raise ValueError()
        if any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
            raise ValueError()
        if len(url) > 4096 or p.hostname.endswith("."):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(p.hostname)
        except ValueError:
            ip = None
        if ip is not None and not _public(ip):
            raise ValueError()
        if p.hostname.lower() == "localhost" or p.hostname.lower().endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        return p
    except (ValueError, TypeError, AttributeError):
        raise EventError("unsafe_callback_url") from None


def _public(ip):
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Numeric validated address: no second DNS lookup of the callback hostname.
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def safe_https_sender(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    """HTTPS POST, DNS-pinned public destinations only; never follows redirects."""
    p = validate_url(url)
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(entry[4][0] for entry in addresses))
    if not ips or any(not _public(ipaddress.ip_address(ip)) for ip in ips):
        raise EventError("unsafe_callback_address", -32015)
    conn = _PinnedHTTPS(p.hostname, ips[0], 5)
    try:
        target = (p.path or "/") + ("?" + p.query if p.query else "")
        conn.request("POST", target, body=body, headers=headers)
        response = conn.getresponse()
        result = response.read(16385)
        if len(result) > 16384:
            raise EventError("callback_response_too_large", -32015)
        return response.status, result
    finally:
        conn.close()


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _headers(secret, event_id, now, body, subscription_id, old_secret=None):
    signed_at = str(int(now))
    material = event_id.encode() + b"." + signed_at.encode() + b"." + body
    keys = [secret] + ([old_secret] if old_secret else [])
    signatures = ["v1," + base64.b64encode(hmac.new(_key(k), material, hashlib.sha256).digest()).decode() for k in keys]
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": signed_at, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id}


class EventSubscriptions:
    def __init__(self, db_path, *, authorized, sender=safe_https_sender, clock=time.time):
        self.db_path, self.authorized, self.sender, self.clock = str(db_path), authorized, sender, clock
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dot_event_subscriptions (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, url TEXT NOT NULL,
                    secret TEXT NOT NULL, old_secret TEXT, rotate_until REAL NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS dot_event_deliveries (
                    subscription_id TEXT NOT NULL, event_id TEXT NOT NULL, body TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending', status INTEGER, deadline REAL NOT NULL,
                    PRIMARY KEY(subscription_id,event_id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(dot_event_deliveries)")}
            if "deadline" not in columns:
                # Existing prototype deliveries expire rather than gain a new lease.
                db.execute("ALTER TABLE dot_event_deliveries ADD COLUMN deadline REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _auth(self, principal):
        if not isinstance(principal, str) or not principal:
            raise EventError("not_authorized", -32001)
        if not self.authorized(principal):
            # Revocation is terminal for existing subscriptions, not a pause.
            with self._db() as db:
                db.execute("UPDATE dot_event_deliveries SET state='stopped' WHERE subscription_id IN (SELECT id FROM dot_event_subscriptions WHERE principal=?)", (principal,))
                db.execute("DELETE FROM dot_event_subscriptions WHERE principal=?", (principal,))
            raise EventError("not_authorized", -32001)

    def _identity(self, principal, params, *, secret_required):
        self._auth(principal)
        if not isinstance(params, dict) or params.get("name") != EVENT_NAME or params.get("arguments", {}) != {}:
            raise EventError("invalid_event_or_arguments")
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise EventError("unsupported_delivery")
        url = delivery.get("url")
        validate_url(url)
        if secret_required:
            _key(delivery.get("secret"))
        identity = canonical([principal, url, EVENT_NAME, {}])
        return "sub_" + hashlib.sha256(identity.encode()).hexdigest(), delivery

    def subscribe(self, principal, params):
        sid, delivery = self._identity(principal, params, secret_required=True)
        ttl = params.get("ttlMs", 3600000)
        if ttl is None:
            ttl = 3600000  # This implementation never grants permanent access.
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1000 <= ttl <= 86400000:
            raise EventError("invalid_ttl")
        if params.get("cursor") is not None:
            raise EventError("replay_not_supported")
        now = self.clock()
        if any(other != sid for other in self.active_ids(principal)):
            raise EventError("only_one_dot_subscription_allowed")
        challenge = secrets.token_urlsafe(32)
        body = canonical({"type": "verification", "challenge": challenge}).encode()
        try:
            status, response = self.sender(delivery["url"], body, _headers(
                delivery["secret"], "verify_" + secrets.token_hex(16), now, body, sid))
            echoed = json.loads(response).get("challenge")
            valid = 200 <= status < 300 and isinstance(echoed, str) and hmac.compare_digest(echoed, challenge)
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            valid = False
        if not valid or self.clock() - now > 10:
            raise EventError("challenge_failed", -32015)
        self._auth(principal)  # Recheck after external verification.
        expires = self.clock() + ttl / 1000
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM dot_event_subscriptions WHERE principal=? AND id!=? AND expires>?",
                          (principal, sid, self.clock())).fetchone():
                raise EventError("only_one_dot_subscription_allowed")
            prior = db.execute("SELECT secret FROM dot_event_subscriptions WHERE id=?", (sid,)).fetchone()
            old = prior["secret"] if prior and prior["secret"] != delivery["secret"] else None
            db.execute("INSERT OR REPLACE INTO dot_event_subscriptions VALUES (?,?,?,?,?,?,?)",
                       (sid, principal, delivery["url"], delivery["secret"], old, self.clock() + 60 if old else 0, expires))
        return {"id": sid, "refreshBefore": _iso(expires), "cursor": None, "truncated": False}

    def unsubscribe(self, principal, params):
        sid, _ = self._identity(principal, params, secret_required=False)
        with self._db() as db:
            db.execute("DELETE FROM dot_event_subscriptions WHERE id=? AND principal=?", (sid, principal))
            db.execute("UPDATE dot_event_deliveries SET state='stopped' WHERE subscription_id=?", (sid,))
        return {}

    def active_ids(self, principal):
        self._auth(principal)
        with self._db() as db:
            return [r["id"] for r in db.execute(
                "SELECT id FROM dot_event_subscriptions WHERE principal=? AND expires>? ORDER BY id",
                (principal, self.clock()))]

    def ready(self, principal):
        self._auth(principal)
        with self._db() as db:
            return db.execute("SELECT 1 FROM dot_event_subscriptions WHERE principal=? AND expires>? LIMIT 1",
                              (principal, self.clock())).fetchone() is not None

    def publish(self, principal, *, request_id, event_id, occurred_at, _subscription_id=None):
        """Send/ retry a stable event once per active subscription, without sleeping.

        Caller schedules retries using next_try. A process crash while sending leaves
        a delivery 'sending'; it is not retried automatically (receipt is uncertain).
        The request ledger remains authoritative for execution status.
        """
        self._auth(principal)
        if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (request_id, event_id, occurred_at)):
            raise EventError("invalid_event")
        try:
            if datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise EventError("timestamp_requires_timezone") from None
        body = canonical({"eventId": event_id, "name": EVENT_NAME, "timestamp": occurred_at,
                          "data": {"request_id": request_id}, "cursor": None}).encode()
        if len(body) > MAX_BODY:
            raise EventError("event_too_large")
        with self._db() as db:
            subscriptions = db.execute("SELECT * FROM dot_event_subscriptions WHERE principal=? AND expires>?",
                                       (principal, self.clock())).fetchall()
        results = []
        for sub in subscriptions:
            if _subscription_id is not None and sub["id"] != _subscription_id:
                continue
            self._auth(principal)
            now = self.clock()
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT OR IGNORE INTO dot_event_deliveries(subscription_id,event_id,body,deadline) VALUES(?,?,?,?)",
                           (sub["id"], event_id, body.decode(), now + 300))
                row = db.execute("SELECT * FROM dot_event_deliveries WHERE subscription_id=? AND event_id=?",
                                 (sub["id"], event_id)).fetchone()
                if row["body"] != body.decode():
                    raise EventError("event_id_conflict")
                if row["state"] == "pending" and row["deadline"] <= now:
                    db.execute("UPDATE dot_event_deliveries SET state='expired' WHERE subscription_id=? AND event_id=?", (sub["id"], event_id))
                    results.append({"subscription_id": sub["id"], "event_id": event_id, "state": "expired"})
                    continue
                if row["state"] != "pending" or row["next_try"] > now:
                    results.append(dict(row))
                    continue
                # Recheck expiry/unsubscribe under the same transaction as the claim.
                live = db.execute("SELECT * FROM dot_event_subscriptions WHERE id=? AND expires>?", (sub["id"], now)).fetchone()
                if live is None:
                    continue
                sub = live
                attempt = row["attempts"] + 1
                db.execute("UPDATE dot_event_deliveries SET state='sending',attempts=? WHERE subscription_id=? AND event_id=?",
                           (attempt, sub["id"], event_id))
            old = sub["old_secret"] if sub["rotate_until"] > now else None
            try:
                status, _ = self.sender(sub["url"], body, _headers(sub["secret"], event_id, now, body, sub["id"], old))
            except (OSError, EventError, http.client.HTTPException):
                status = 0
            transient = status == 0 or status in (408, 429) or 500 <= status < 600
            state = "accepted" if 200 <= status < 300 else ("pending" if transient and attempt < MAX_ATTEMPTS else "failed")
            next_try = self.clock() + 2 ** attempt if state == "pending" else 0
            with self._db() as db:
                db.execute("UPDATE dot_event_deliveries SET state=?,status=?,next_try=? WHERE subscription_id=? AND event_id=? AND state='sending'",
                           (state, status, next_try, sub["id"], event_id))
            results.append({"subscription_id": sub["id"], "event_id": event_id, "state": state,
                            "status": status, "attempts": attempt, "next_try": next_try})
        return results

    def drain_pending(self, principal):
        """Retry due deliveries only for their original subscription; no event replay.

        Delivers at most 32 due records per drain. Five-minute delivery lifetime
        and four total attempts apply. Caller schedules this, no worker is started.
        """
        self._auth(principal)
        now = self.clock()
        with self._db() as db:
            rows = db.execute("""SELECT d.* FROM dot_event_deliveries d
                JOIN dot_event_subscriptions s ON s.id=d.subscription_id
                WHERE s.principal=? AND s.expires>? AND d.state='pending' AND d.next_try<=?
                ORDER BY d.next_try LIMIT 32""", (principal, now, now)).fetchall()
        results = []
        for row in rows:
            event = json.loads(row["body"])
            results.extend(self.publish(principal, request_id=event["data"]["request_id"],
                event_id=event["eventId"], occurred_at=event["timestamp"],
                _subscription_id=row["subscription_id"]))
        return results
