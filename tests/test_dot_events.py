import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))
from dot_events import EventSubscriptions, EventError, validate_url, safe_https_sender

SECRET = "whsec_" + base64.b64encode(b"x" * 32).decode()


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events.db")
        self.now = 1000000
        self.allowed = True
        self.calls = []
        self.status = 200
        self.bad_challenge = False
        self.events = EventSubscriptions(self.path, authorized=lambda p: self.allowed and p == "owner", sender=self.send, clock=lambda: self.now)
        self.params = {"name": "alex.request.created", "arguments": {}, "delivery": {"mode": "webhook", "url": "https://receiver.example/events", "secret": SECRET}}

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, url, body, headers):
        self.calls.append((url, body, headers))
        obj = json.loads(body)
        if obj.get("type") == "verification":
            return 200, json.dumps({"challenge": "wrong" if self.bad_challenge else obj["challenge"]}).encode()
        return self.status, b"{}"

    def publish(self, **kwargs):
        return self.events.publish("owner", request_id=kwargs.get("request_id", "req1"), event_id="evt1", occurred_at="2026-10-20T00:00:00Z")

    def test_verified_persistent_subscription_and_signature(self):
        sub = self.events.subscribe("owner", self.params)
        self.assertTrue(self.events.ready("owner"))
        reopened = EventSubscriptions(self.path, authorized=lambda p: True, sender=self.send, clock=lambda: self.now)
        self.assertTrue(reopened.ready("owner"))
        result = self.publish()
        self.assertEqual(result[0]["state"], "accepted")
        _, body, h = self.calls[-1]
        signed = h["webhook-id"].encode() + b"." + h["webhook-timestamp"].encode() + b"." + body
        expected = "v1," + base64.b64encode(hmac.new(b"x" * 32, signed, hashlib.sha256).digest()).decode()
        self.assertEqual(h["webhook-signature"], expected)
        self.assertEqual(h["X-MCP-Subscription-Id"], sub["id"])
        n = len(self.calls)
        self.publish()
        self.assertEqual(len(self.calls), n)
        with self.assertRaises(EventError):
            self.publish(request_id="different")

    def test_challenge_failure_and_secret_validation(self):
        self.bad_challenge = True
        with self.assertRaises(EventError): self.events.subscribe("owner", self.params)
        self.assertFalse(self.events.ready("owner"))
        for secret in ("bad", "whsec_@@", "whsec_" + base64.b64encode(b"short").decode()):
            self.params["delivery"]["secret"] = secret
            with self.assertRaises(EventError): self.events.subscribe("owner", self.params)

    def test_expiry_revocation_and_unsubscribe(self):
        self.params["ttlMs"] = 1000
        self.events.subscribe("owner", self.params)
        self.now += 2
        self.assertFalse(self.events.ready("owner"))
        self.assertEqual(self.publish(), [])
        self.events.subscribe("owner", self.params)
        self.allowed = False
        with self.assertRaises(EventError): self.publish()
        self.allowed = True
        self.events.unsubscribe("owner", self.params)
        self.events.unsubscribe("owner", self.params)
        self.assertFalse(self.events.ready("owner"))

    def test_refresh_identity_rotation(self):
        first = self.events.subscribe("owner", self.params)
        self.params["delivery"]["secret"] = "whsec_" + base64.b64encode(b"y" * 32).decode()
        second = self.events.subscribe("owner", self.params)
        self.assertEqual(first["id"], second["id"])
        self.publish()
        self.assertEqual(len(self.calls[-1][2]["webhook-signature"].split()), 2)

    def test_bounded_retries_same_event_fresh_signature(self):
        self.events.subscribe("owner", self.params)
        self.status = 503
        self.assertEqual(self.publish()[0]["state"], "pending")
        first = self.calls[-1]
        n = len(self.calls)
        self.publish()
        self.assertEqual(len(self.calls), n)
        for i in range(3):
            self.now += 20
            result = self.publish()
        self.assertEqual(result[0]["state"], "failed")
        self.assertEqual(first[1], self.calls[-1][1])
        self.assertNotEqual(first[2]["webhook-signature"], self.calls[-1][2]["webhook-signature"])
        n = len(self.calls)
        self.now += 100
        self.publish()
        self.assertEqual(len(self.calls), n)

    def test_410_413_and_redirect_never_retry(self):
        for code in (410, 413, 302):
            with self.subTest(code=code):
                e = EventSubscriptions(os.path.join(self.tmp.name, str(code)), authorized=lambda p: True, sender=self.send, clock=lambda: self.now)
                e.subscribe("owner", self.params)
                self.status = code
                out = e.publish("owner", request_id="req", event_id="evt", occurred_at="2026-10-20T00:00:00Z")
                self.assertEqual(out[0]["state"], "failed")

    def test_unsafe_urls_and_dns_rebinding_guard(self):
        for url in ("http://example.com", "https://127.0.0.1", "https://[::1]", "https://localhost", "https://a.local", "https://user:pass@example.com", "https://example.com:8443", "https://example.com/#frag"):
            with self.subTest(url=url), self.assertRaises(EventError): validate_url(url)
        with patch("dot_events.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(EventError): safe_https_sender("https://example.com", b"{}", {})

    def test_dns_pinning_preserves_hostname_and_does_not_follow_redirect(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status = 302
        response.read.return_value = b"{}"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("dot_events.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), patch("dot_events._PinnedHTTPS", return_value=connection) as ctor:
            self.assertEqual(safe_https_sender("https://example.com/events?q=1", b"{}", {}), (302, b"{}"))
            ctor.assert_called_once_with("example.com", "93.184.216.34", 5)
            connection.request.assert_called_once_with("POST", "/events?q=1", body=b"{}", headers={})
            connection.close.assert_called_once()
        with patch("dot_events.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]), patch("dot_events._PinnedHTTPS") as ctor:
            with self.assertRaises(EventError): safe_https_sender("https://example.com", b"{}", {})
            ctor.assert_not_called()

    def test_process_crash_does_not_repeat_uncertain_delivery(self):
        self.events.subscribe("owner", self.params)
        def crash(*args):
            raise KeyboardInterrupt()
        self.events.sender = crash
        with self.assertRaises(KeyboardInterrupt): self.publish()
        self.events.sender = self.send
        n = len(self.calls)
        self.assertEqual(self.publish()[0]["state"], "sending")
        self.assertEqual(len(self.calls), n)

    def test_bad_challenge_status_and_expired_challenge_are_rejected(self):
        def slow(url, body, headers):
            self.now += 11
            return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()
        self.events.sender = slow
        with self.assertRaises(EventError): self.events.subscribe("owner", self.params)
        self.assertFalse(self.events.ready("owner"))

    def test_drain_retries_and_expires_without_replay_to_new_subscription(self):
        self.events.subscribe("owner", self.params)
        self.status = 503
        self.publish()
        self.assertEqual(self.events.drain_pending("owner"), [])
        self.now += 3
        self.status = 200
        result = self.events.drain_pending("owner")
        self.assertEqual(result[0]["state"], "accepted")
        self.assertEqual(self.events.drain_pending("owner"), [])
        self.status = 503
        self.events.publish("owner", request_id="req2", event_id="evt2", occurred_at="2026-10-20T00:00:00Z")
        self.now += 301
        n = len(self.calls)
        result = self.events.drain_pending("owner")
        self.assertEqual(result[0]["state"], "expired")
        self.assertEqual(len(self.calls), n)

    def test_inputs_reject_foreign_event_filters_and_no_replay(self):
        for changed in ({"name": "other"}, {"arguments": {"phone": "someone"}}, {"cursor": "old"}, {"ttlMs": True}):
            with self.subTest(changed=changed), self.assertRaises(EventError):
                self.events.subscribe("owner", {**self.params, **changed})
        with self.assertRaises(EventError): self.events.subscribe("other", self.params)


if __name__ == "__main__": unittest.main()
