"""v0.5.33: provider circuit breaker, ChatGPT plan provider, shadow AI router.

Everything added in v0.5.33 is off by default. These tests prove three things:
1. With the shipped defaults nothing about live routing or replies changes.
2. When switched on, each piece does what it claims (and fails safe).
3. The ChatGPT plan adapter speaks the documented plan-usage contract
   (Responses API, store=false, stream=true, no system items, PKCE sign-in,
   rotating refresh tokens written atomically and serialised).
"""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import stat
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import test_core as core


db = core.db
brain = core.brain
ingress = core.ingress
Settings = core.Settings

import chatgpt_plan  # noqa: E402  (path set up by test_core)
import shadow_router  # noqa: E402


DM = "60111111111@s.whatsapp.net"


def _b64(data: dict) -> str:
    raw = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def fake_jwt(claims: dict) -> str:
    return _b64({"alg": "RS256", "typ": "JWT"}) + "." + _b64(claims) + ".sig"


class FakeHTTPResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Scripted stand-in for ``requests`` (post/get)."""

    def __init__(self, posts=None, gets=None, post_delay=0.0):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_calls = []
        self.get_calls = []
        self.post_delay = post_delay
        self.lock = threading.Lock()

    def post(self, url, data=None, headers=None, timeout=None):
        if self.post_delay:
            time.sleep(self.post_delay)
        with self.lock:
            self.post_calls.append({"url": url, "data": dict(data or {})})
            item = self.posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": dict(headers or {})})
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def completed_event(output, usage=None, response_id="resp_1"):
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "created_at": 1760000000,
            "model": "gpt-test",
            "output": output,
            "usage": usage or {
                "input_tokens": 120, "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 8},
            },
        },
    }


def text_output(text):
    return [{"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}]}]


def call_output(call_id, name, arguments):
    return [{"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "function_call", "id": "fc_1", "call_id": call_id,
             "name": name, "arguments": json.dumps(arguments)}]


class FakeResponses:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.bodies = []
        self.tokens = []

    def create(self, **body):
        self.bodies.append(body)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item)


class FakeOpenAIFactory:
    def __init__(self, scripted):
        self.responses = FakeResponses(scripted)

    def __call__(self, token):
        self.responses.tokens.append(token)
        return SimpleNamespace(responses=self.responses)


class StatusError(Exception):
    def __init__(self, status_code, message="", code=None):
        super().__init__(message or f"status {status_code}")
        self.status_code = status_code
        self.code = code


PLAN_SETTINGS = Settings(
    ai_provider="auto", gemini_api_key="gemini-test", xai_api_key="xai-test",
    chatgpt_plan_mode="primary", chatgpt_plan_model="gpt-test",
)


class V0533Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.AlexCoreTests.setUpClass()

    def setUp(self):
        core.AlexCoreTests.setUp(self)
        self._reset_plan_state()

    def tearDown(self):
        self._reset_plan_state()
        core.AlexCoreTests.setUp(self)

    @staticmethod
    def _reset_plan_state():
        shutil.rmtree(chatgpt_plan._base_dir(), ignore_errors=True)
        with brain._PROVIDER_COOLDOWN_LOCK:
            brain._PROVIDER_COOLDOWNS.clear()
        chatgpt_plan._NO_REASONING_MODELS.clear()
        conn = db.connect()
        try:
            conn.execute("DROP TABLE IF EXISTS alex_shadow_router_log")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def sign_in_fixture(*, expires_in=3600, now=None, refresh="rt-1", access="at-1"):
        current = time.time() if now is None else now
        chatgpt_plan._write_private_json("registration.json", {
            "client_id": "oaiapp_test", "ext_agent_host_id": chatgpt_plan.host_id(),
        })
        chatgpt_plan._write_private_json("credentials.json", {
            "status": "active", "client_id": "oaiapp_test",
            "access_token": access, "refresh_token": refresh,
            "expires_at": current + expires_in, "refreshed_at": current - 3600,
            "email": "owner@example.com",
        })
        chatgpt_plan._write_private_json("models.json", {
            "fetched_at": current, "models": [{"slug": "gpt-test", "display_name": "GPT test"}],
        })

    def claim_actor(self, message_id, text):
        payload = {
            "message_id": message_id, "provider": "WHATSAPP", "conversation_id": DM,
            "conversation_type": "DIRECT_DM", "sender_phone": "+60111111111", "text": text,
        }
        db.claim_inbound(payload)
        actor = db.resolve_actor("+60111111111", DM, "DIRECT_DM", message_id, [])
        return replace(actor, trusted_text=text, intent_text=text)


# ---------------------------------------------------------------------------
# 1. Circuit breaker
# ---------------------------------------------------------------------------

class CircuitBreakerTests(V0533Base):
    ROUTES = [
        {"provider": "gemini", "model": "g", "reasoning_effort": "low", "role": "primary_saver"},
        {"provider": "grok", "model": "x", "reasoning_effort": "low", "role": "resilience_fallback"},
    ]

    def test_cooling_provider_is_skipped_but_alex_is_never_stranded(self):
        brain._trip_provider("gemini", "provider_rate_limit_or_quota", now=1000.0)
        kept = brain._apply_circuit_breaker(self.ROUTES, now=1001.0)
        self.assertEqual([r["provider"] for r in kept], ["grok"])

        brain._trip_provider("grok", "provider_authentication_failed", now=1000.0)
        everything = brain._apply_circuit_breaker(self.ROUTES, now=1001.0)
        self.assertEqual([r["provider"] for r in everything], ["gemini", "grok"])

    def test_only_provider_wide_failures_trip_and_cooldowns_expire(self):
        brain._trip_provider("gemini", "provider_request_rejected", now=1000.0)
        brain._trip_provider("gemini", "internal_processing_error", now=1000.0)
        self.assertFalse(brain._provider_cooling("gemini", now=1001.0))

        brain._trip_provider("gemini", "provider_connection_error", now=1000.0)
        self.assertTrue(brain._provider_cooling("gemini", now=1059.0))
        self.assertFalse(brain._provider_cooling("gemini", now=1061.0))
        self.assertEqual(brain.provider_cooldowns(now=1061.0), {})

    def test_next_message_skips_provider_that_just_hit_quota(self):
        calls = []

        def client_for(provider, settings=None):
            class Completions:
                def create(self, **kwargs):
                    calls.append(provider)
                    if provider == "gemini":
                        raise StatusError(429, "quota")
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(
                            content="You spent RM0 today.", tool_calls=None))],
                        usage=None,
                    )
            return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

        with patch.object(brain, "_provider_routes", return_value=self.ROUTES), \
             patch.object(brain, "_client_for", side_effect=client_for):
            for mid in ("v0533-cb-1", "v0533-cb-2"):
                actor = self.claim_actor(mid, "How much did I spend today?")
                reply, _ = asyncio.run(brain.respond(actor, "How much did I spend today?"))
                self.assertEqual(reply, "You spent RM0 today.")

        # Message 1 tried Gemini (429) then Grok; message 2 went straight to Grok.
        self.assertEqual(calls, ["gemini", "grok", "grok"])
        self.assertIn("gemini", brain.provider_cooldowns())

    def test_successful_manual_probe_reopens_a_cooling_provider(self):
        brain._trip_provider("grok", "provider_rate_limit_or_quota")
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
                usage=None,
            ))))
        route = [{"provider": "grok", "model": "x", "reasoning_effort": "low", "role": "manual"}]
        with patch.object(brain, "_provider_routes", return_value=route), \
             patch.object(brain, "_client_for", return_value=fake):
            self.assertEqual(brain.provider_probe()["status"], "ok")
        self.assertFalse(brain._provider_cooling("grok"))


# ---------------------------------------------------------------------------
# 2. Routing with the ChatGPT plan
# ---------------------------------------------------------------------------

class PlanRoutingTests(V0533Base):
    def test_shipped_defaults_leave_routes_exactly_as_before(self):
        self.sign_in_fixture()  # even when signed in, mode "off" changes nothing
        settings = Settings(ai_provider="auto", gemini_api_key="g", xai_api_key="x")
        self.assertEqual(settings.chatgpt_plan_mode, "off")
        self.assertEqual(
            brain._provider_routes(settings, user_text="hello", tools=[]),
            brain._api_provider_routes(settings, user_text="hello", tools=[]),
        )

    def test_primary_mode_needs_sign_in(self):
        routes = brain._provider_routes(PLAN_SETTINGS, user_text="hello", tools=[])
        self.assertNotIn("chatgpt", [r["provider"] for r in routes])

    def test_primary_mode_leads_text_and_follows_first_route_for_images(self):
        self.sign_in_fixture()
        text = brain._provider_routes(PLAN_SETTINGS, user_text="hello", tools=[])
        self.assertEqual(text[0]["provider"], "chatgpt")
        self.assertEqual(text[0]["model"], "gpt-test")
        self.assertEqual([r["provider"] for r in text[1:]], ["gemini", "gemini", "grok"])

        visual = brain._provider_routes(
            PLAN_SETTINGS, user_text="what is this", tools=[],
            vision_parts=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        )
        self.assertEqual([r["provider"] for r in visual][:2], ["gemini", "chatgpt"])

    def test_shadow_only_mode_never_adds_a_live_route(self):
        self.sign_in_fixture()
        settings = replace(PLAN_SETTINGS, chatgpt_plan_mode="shadow_only")
        routes = brain._provider_routes(settings, user_text="hello", tools=[])
        self.assertNotIn("chatgpt", [r["provider"] for r in routes])

    def test_plan_usage_never_counts_against_the_api_budget(self):
        self.assertEqual(brain._provider_reported_cost_usd("chatgpt", None), 0.0)

    def test_unknown_mode_values_fall_back_to_off(self):
        path = os.environ["ALEX_OPTIONS_PATH"]
        with open(path, encoding="utf-8") as f:
            original = f.read()
        try:
            data = json.loads(original)
            data["chatgpt_plan_mode"] = "always"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            self.assertEqual(core.brain.get_settings().chatgpt_plan_mode, "off")
        finally:
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)


# ---------------------------------------------------------------------------
# 3. Chat Completions <-> Responses translation
# ---------------------------------------------------------------------------

class TranslationTests(V0533Base):
    def test_request_follows_the_plan_usage_contract(self):
        body = chatgpt_plan.chat_to_responses({
            "model": "gpt-test",
            "messages": [
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "system", "content": "RUNTIME CONTEXT"},
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "system", "content": "The user's message may be Tamil."},
                {"role": "user", "content": [
                    {"type": "text", "text": "what is this receipt"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ]},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_9", "type": "function",
                    "function": {"name": "query_finances", "arguments": "{\"period\":\"today\"}"},
                }]},
                {"role": "tool", "tool_call_id": "call_9", "content": "{\"total\":12}"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "query_finances", "description": "Read spending.",
                "parameters": {"type": "object", "properties": {"period": {"type": "string"}}},
            }}],
            "tool_choice": "auto",
            "reasoning_effort": "low",
        })
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["instructions"], "SYSTEM PROMPT\n\nRUNTIME CONTEXT")
        roles = [item.get("role") for item in body["input"]]
        self.assertNotIn("system", roles)
        self.assertIn({"role": "developer", "content": "The user's message may be Tamil."}, body["input"])
        self.assertEqual(body["input"][3]["content"], [
            {"type": "input_text", "text": "what is this receipt"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
        ])
        self.assertIn({"type": "function_call", "call_id": "call_9", "name": "query_finances",
                       "arguments": "{\"period\":\"today\"}"}, body["input"])
        self.assertIn({"type": "function_call_output", "call_id": "call_9",
                       "output": "{\"total\":12}"}, body["input"])
        self.assertEqual(body["tools"][0]["name"], "query_finances")
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        for unsupported in ("previous_response_id", "temperature", "max_output_tokens",
                            "metadata", "user", "background", "truncation"):
            self.assertNotIn(unsupported, body)

    def test_final_answer_round_sends_no_tools(self):
        body = chatgpt_plan.chat_to_responses({
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_response_is_shaped_like_a_chat_completion_and_round_trips(self):
        completed = chatgpt_plan.collect_stream(iter([
            {"type": "response.output_text.delta", "delta": "ignored"},
            completed_event(call_output("call_1", "log_expense", {"amount": 75})),
        ]))
        completion = chatgpt_plan.responses_to_chat(completed, "gpt-test")
        message = completion.choices[0].message
        self.assertIsNone(message.content)
        self.assertEqual(message.tool_calls[0].id, "call_1")
        self.assertEqual(message.tool_calls[0].function.name, "log_expense")
        self.assertEqual(json.loads(message.tool_calls[0].function.arguments), {"amount": 75})
        self.assertEqual(brain._usage_breakdown(completion.usage), (120, 100, 30, 8))

        # The assistant turn brain appends must translate back to a function_call.
        again = chatgpt_plan.chat_to_responses({
            "model": "gpt-test",
            "messages": [message.model_dump(exclude_none=True)],
        })
        self.assertEqual(again["input"], [{
            "type": "function_call", "call_id": "call_1", "name": "log_expense",
            "arguments": json.dumps({"amount": 75}),
        }])

        text = chatgpt_plan.responses_to_chat(
            chatgpt_plan.collect_stream(iter([completed_event(text_output("Done."))])),
            "gpt-test",
        )
        self.assertEqual(text.choices[0].message.content, "Done.")
        self.assertFalse(text.choices[0].message.tool_calls)

    def test_stream_failures_become_provider_errors_alex_already_understands(self):
        limit = iter([{"type": "response.failed", "response": {
            "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}}])
        with self.assertRaises(chatgpt_plan.ChatGPTPlanError) as caught:
            chatgpt_plan.collect_stream(limit)
        self.assertEqual(
            brain.classify_runtime_error(caught.exception)["category"],
            "provider_rate_limit_or_quota",
        )
        with self.assertRaises(chatgpt_plan.ChatGPTPlanError) as unavailable:
            chatgpt_plan.collect_stream(iter([{"type": "response.failed", "response": {
                "error": {"code": "subscription_sharing_usage_unavailable"}}}]))
        self.assertEqual(unavailable.exception.status_code, 503)
        with self.assertRaises(chatgpt_plan.ChatGPTPlanError):
            chatgpt_plan.collect_stream(iter([{"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "max_output_tokens"}}}]))
        with self.assertRaises(chatgpt_plan.ChatGPTPlanError) as cut:
            chatgpt_plan.collect_stream(iter([{"type": "response.output_text.delta", "delta": "Hal"}]))
        self.assertEqual(cut.exception.code, "stream_incomplete")


# ---------------------------------------------------------------------------
# 4. Sign-in and tokens
# ---------------------------------------------------------------------------

class SignInTests(V0533Base):
    def _token_response(self, *, client_id="oaiapp_new", nonce, scope=None, refresh="rt-1",
                        access="at-1", exp_offset=600):
        return FakeHTTPResponse(200, {
            "access_token": access, "refresh_token": refresh, "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope if scope is not None else chatgpt_plan.SCOPES,
            "id_token": fake_jwt({
                "iss": "https://auth.openai.com", "aud": client_id, "nonce": nonce,
                "exp": time.time() + exp_offset, "email": "owner@example.com", "sub": "u1",
            }),
        })

    def _start(self):
        start = chatgpt_plan.start_sign_in()
        query = {k: v[0] for k, v in parse_qs(urlparse(start["authorize_url"]).query).items()}
        pending = chatgpt_plan._read_json("pending.json")
        return start, query, pending

    def test_first_sign_in_registers_and_stores_a_private_session(self):
        start, query, pending = self._start()
        self.assertTrue(start["authorize_url"].startswith(chatgpt_plan.AUTHORIZE_URL + "?"))
        self.assertEqual(query["client_id"], "dynamic_agent_client")
        self.assertEqual(query["agent_name_hint"], "Alex")
        self.assertEqual(query["redirect_uri"], "http://127.0.0.1:1455/auth/callback")
        self.assertEqual(query["resource"], "https://api.openai.com/v1")
        self.assertIn("chatgpt.tokens.use.direct", query["scope"].split())
        self.assertIn("offline_access", query["scope"].split())
        self.assertEqual(query["code_challenge_method"], "S256")
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(pending["code_verifier"].encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(query["code_challenge"], expected_challenge)
        self.assertTrue(query["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual(query["ext_agent_host_id"], chatgpt_plan.host_id())
        self.assertNotIn("@", query["ext_agent_host_id"])

        session = FakeSession(
            posts=[self._token_response(nonce=pending["nonce"])],
            gets=[FakeHTTPResponse(200, {"models": [
                {"slug": "gpt-hidden", "visibility": "hide"},
                {"slug": "gpt-test", "display_name": "GPT test", "visibility": "list"},
            ]})],
        )
        pasted = (f"http://127.0.0.1:1455/auth/callback?code=abc&state={pending['state']}"
                  "&client_id=oaiapp_new&scope=openid")
        result = chatgpt_plan.finish_sign_in(pasted, session=session)

        exchange = session.post_calls[0]["data"]
        self.assertEqual(exchange, {
            "grant_type": "authorization_code", "client_id": "oaiapp_new", "code": "abc",
            "code_verifier": pending["code_verifier"],
            "redirect_uri": "http://127.0.0.1:1455/auth/callback",
            "resource": "https://api.openai.com/v1",
        })
        self.assertTrue(result["signed_in"])
        self.assertEqual(result["account_email"], "owner@example.com")
        self.assertEqual(result["models"], ["gpt-test"])
        self.assertIsNone(chatgpt_plan._read_json("pending.json"))
        self.assertEqual(chatgpt_plan.load_registration()["client_id"], "oaiapp_new")
        creds_path = os.path.join(chatgpt_plan._base_dir(), "credentials.json")
        self.assertEqual(stat.S_IMODE(os.stat(creds_path).st_mode), 0o600)
        # Status never leaks tokens.
        blob = json.dumps(chatgpt_plan.status(PLAN_SETTINGS))
        self.assertNotIn("at-1", blob)
        self.assertNotIn("rt-1", blob)

        # A later sign-in reuses the issued client and stops registering.
        _, again, _ = self._start()
        self.assertEqual(again["client_id"], "oaiapp_new")
        self.assertNotIn("agent_name_hint", again)

    def test_sign_in_fails_closed(self):
        _, _, pending = self._start()
        good_state = pending["state"]
        with self.assertRaises(chatgpt_plan.SignInError):
            chatgpt_plan.finish_sign_in("http://127.0.0.1:1455/auth/callback?code=a&state=WRONG",
                                        session=FakeSession())
        with self.assertRaises(chatgpt_plan.SignInError):
            chatgpt_plan.finish_sign_in(
                f"http://127.0.0.1:1455/auth/callback?error=access_denied&state={good_state}",
                session=FakeSession())
        self.assertIsNone(chatgpt_plan.load_credentials())

        _, _, pending = self._start()
        no_plan_scope = FakeSession(posts=[self._token_response(
            nonce=pending["nonce"], scope="openid profile email offline_access")])
        with self.assertRaises(chatgpt_plan.SignInError):
            chatgpt_plan.finish_sign_in(
                f"?code=a&state={pending['state']}&client_id=oaiapp_new", session=no_plan_scope)
        self.assertIsNone(chatgpt_plan.load_credentials())

        _, _, pending = self._start()
        replayed = FakeSession(posts=[self._token_response(nonce="someone-else")])
        with self.assertRaises(chatgpt_plan.SignInError):
            chatgpt_plan.finish_sign_in(
                f"?code=a&state={pending['state']}&client_id=oaiapp_new", session=replayed)
        self.assertIsNone(chatgpt_plan.load_credentials())

        _, _, pending = self._start()
        with self.assertRaises(chatgpt_plan.SignInError):
            chatgpt_plan.finish_sign_in(
                f"?code=a&state={pending['state']}&client_id=oaiapp_new",
                now=time.time() + chatgpt_plan.PENDING_TTL_SECONDS + 5, session=FakeSession())
        self.assertIsNone(chatgpt_plan.load_credentials())

    def test_refresh_rotates_atomically_and_only_when_needed(self):
        now = time.time()
        self.sign_in_fixture(expires_in=60, now=now)  # inside the 5-minute margin
        session = FakeSession(posts=[FakeHTTPResponse(200, {
            "access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})])
        self.assertEqual(chatgpt_plan.access_token(now=now, session=session), "at-2")
        self.assertEqual(session.post_calls[0]["data"], {
            "grant_type": "refresh_token", "client_id": "oaiapp_test",
            "refresh_token": "rt-1", "resource": "https://api.openai.com/v1",
        })
        stored = chatgpt_plan.load_credentials()
        self.assertEqual((stored["access_token"], stored["refresh_token"]), ("at-2", "rt-2"))
        # Fresh token: no further refresh.
        self.assertEqual(chatgpt_plan.access_token(now=now + 10, session=session), "at-2")
        self.assertEqual(len(session.post_calls), 1)

    def test_concurrent_refreshes_spend_the_rotating_token_once(self):
        now = time.time()
        self.sign_in_fixture(expires_in=-10, now=now)
        session = FakeSession(post_delay=0.2, posts=[FakeHTTPResponse(200, {
            "access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})])
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            chatgpt_plan.access_token(session=session))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, ["at-2"] * 4)
        self.assertEqual(len(session.post_calls), 1)

    def test_terminal_refresh_error_asks_for_sign_in_but_network_errors_do_not(self):
        import requests
        now = time.time()
        self.sign_in_fixture(expires_in=-10, now=now)
        with self.assertRaises(chatgpt_plan.ChatGPTPlanError) as down:
            chatgpt_plan.access_token(session=FakeSession(posts=[requests.ConnectionError("down")]))
        self.assertNotIsInstance(down.exception, chatgpt_plan.NotSignedIn)
        self.assertEqual(chatgpt_plan.load_credentials()["refresh_token"], "rt-1")

        with self.assertRaises(chatgpt_plan.NotSignedIn):
            chatgpt_plan.access_token(session=FakeSession(posts=[
                FakeHTTPResponse(400, {"error": "refresh_token_reused"})]))
        creds = chatgpt_plan.load_credentials()
        self.assertEqual(creds["status"], "needs_sign_in")
        self.assertNotIn("refresh_token", creds)
        self.assertNotIn("access_token", creds)
        self.assertTrue(chatgpt_plan.status()["needs_sign_in"])
        self.assertEqual(chatgpt_plan.ready_model(PLAN_SETTINGS), "")
        # Registration survives so the next sign-in reuses Alex's client id.
        self.assertEqual(chatgpt_plan.load_registration()["client_id"], "oaiapp_test")

    def test_sign_out_forgets_the_session(self):
        self.sign_in_fixture()
        self.assertTrue(chatgpt_plan.status()["signed_in"])
        chatgpt_plan.sign_out()
        self.assertFalse(chatgpt_plan.status()["signed_in"])
        with self.assertRaises(chatgpt_plan.NotSignedIn):
            chatgpt_plan.client(PLAN_SETTINGS)


# ---------------------------------------------------------------------------
# 5. The shim inside Alex's real tool loop
# ---------------------------------------------------------------------------

class PlanClientTests(V0533Base):
    def test_unauthorised_once_forces_one_refresh_then_retries(self):
        self.sign_in_fixture()
        factory = FakeOpenAIFactory([
            StatusError(401, "expired"),
            [completed_event(text_output("OK"))],
        ])
        with patch.object(chatgpt_plan, "access_token",
                          side_effect=["at-1", "at-2"]) as token:
            client = chatgpt_plan.PlanClient(PLAN_SETTINGS, openai_factory=factory)
            reply = client.chat.completions.create(
                model="gpt-test", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(reply.choices[0].message.content, "OK")
        self.assertEqual(factory.responses.tokens, ["at-1", "at-2"])
        self.assertEqual(token.call_args_list[1].kwargs, {"force": True})

    def test_model_without_reasoning_support_is_retried_once_and_remembered(self):
        self.sign_in_fixture()
        factory = FakeOpenAIFactory([
            StatusError(400, "Unsupported parameter: 'reasoning.effort'"),
            [completed_event(text_output("OK"))],
            [completed_event(text_output("OK again"))],
        ])
        client = chatgpt_plan.PlanClient(PLAN_SETTINGS, openai_factory=factory)
        client.chat.completions.create(model="gpt-test", reasoning_effort="low",
                                       messages=[{"role": "user", "content": "hi"}])
        client.chat.completions.create(model="gpt-test", reasoning_effort="low",
                                       messages=[{"role": "user", "content": "hi"}])
        self.assertIn("reasoning", factory.responses.bodies[0])
        self.assertNotIn("reasoning", factory.responses.bodies[1])
        self.assertNotIn("reasoning", factory.responses.bodies[2])

    def test_revoked_access_marks_sign_in_needed(self):
        self.sign_in_fixture()
        factory = FakeOpenAIFactory([StatusError(401, "revoked",
                                                 code="subscription_sharing_invalid_user")])
        client = chatgpt_plan.PlanClient(PLAN_SETTINGS, openai_factory=factory)
        with self.assertRaises(chatgpt_plan.NotSignedIn):
            client.chat.completions.create(model="gpt-test",
                                           messages=[{"role": "user", "content": "hi"}])
        self.assertTrue(chatgpt_plan.status()["needs_sign_in"])

    def test_alex_tool_loop_runs_unchanged_on_the_plan(self):
        self.sign_in_fixture()
        factory = FakeOpenAIFactory([
            [completed_event(call_output("call_1", "query_finances", {"period": "today"}))],
            [completed_event(text_output("You spent RM0 today."))],
        ])
        executed = []

        async def fake_call_mcp(actor, name, args, action_key):
            executed.append((name, args))
            return ({"status": "ok", "total": 0}, [])

        real_client_for = brain._client_for

        def client_for(provider, settings=None):
            if provider == "chatgpt":
                return chatgpt_plan.PlanClient(settings, openai_factory=factory)
            return real_client_for(provider, settings)

        actor = self.claim_actor("v0533-plan-loop", "How much did I spend today?")
        with patch.object(brain, "get_settings", return_value=PLAN_SETTINGS), \
             patch.object(brain, "_client_for", side_effect=client_for), \
             patch.object(brain, "_call_mcp", new=fake_call_mcp):
            reply, attachments = asyncio.run(
                brain.respond(actor, "How much did I spend today?"))

        self.assertEqual(reply, "You spent RM0 today.")
        self.assertEqual(executed, [("query_finances", {"period": "today"})])
        second = factory.responses.bodies[1]
        self.assertIn({"type": "function_call_output", "call_id": "call_1",
                       "output": json.dumps({"status": "ok", "total": 0},
                                            separators=(",", ":"))}, second["input"])
        self.assertTrue(all(b["store"] is False and b["stream"] is True
                            for b in factory.responses.bodies))
        self.assertEqual(brain.recent_trace("v0533-plan-loop")["routes"][0], "chatgpt:gpt-test")
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT provider, estimated_cost_usd FROM ai_usage WHERE source_message_id=?",
                ("v0533-plan-loop",)).fetchone()
        finally:
            conn.close()
        self.assertEqual((row["provider"], row["estimated_cost_usd"]), ("chatgpt", 0.0))

    def test_plan_outage_falls_back_to_existing_providers(self):
        self.sign_in_fixture()
        factory = FakeOpenAIFactory([[{"type": "response.failed", "response": {
            "error": {"code": "subscription_sharing_usage_limit_exceeded"}}}]])
        fallback_calls = []

        def client_for(provider, settings=None):
            if provider == "chatgpt":
                return chatgpt_plan.PlanClient(settings, openai_factory=factory)

            class Completions:
                def create(self, **kwargs):
                    fallback_calls.append(provider)
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content="Answered by fallback.", tool_calls=None))], usage=None)
            return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

        actor = self.claim_actor("v0533-plan-out", "How much did I spend today?")
        with patch.object(brain, "get_settings", return_value=PLAN_SETTINGS), \
             patch.object(brain, "_client_for", side_effect=client_for):
            reply, _ = asyncio.run(brain.respond(actor, "How much did I spend today?"))
        self.assertEqual(reply, "Answered by fallback.")
        self.assertEqual(fallback_calls, ["gemini"])
        self.assertEqual(brain.provider_cooldowns()["chatgpt"]["category"],
                         "provider_rate_limit_or_quota")


# ---------------------------------------------------------------------------
# 6. Shadow router
# ---------------------------------------------------------------------------

CATALOGUE = [
    {"name": "query_finances", "summary": "Read spending."},
    {"name": "log_expense", "summary": "Log an expense."},
    {"name": "update_shopping_item", "summary": "Mark a shopping item bought."},
    {"name": "list_shopping_items", "summary": "Show the shopping list."},
]


class FakePlanClient:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=self.content))])


class ShadowRouterTests(V0533Base):
    def job(self, **overrides):
        job = {
            "source_message_id": "m1", "conversation_type": "DIRECT_DM",
            "text": "bought 1 and 3", "quoted_text": "",
            "prior_turns": [{"role": "user", "text": "show my shopping list"},
                            {"role": "assistant", "text": "1. Milk 2. Bread 3. Detergent"}],
            "keyword_tools": ["list_shopping_items", "query_finances"],
            "called_tools": ["list_shopping_items"], "live_outcome": "answered",
            "catalogue": CATALOGUE,
        }
        job.update(overrides)
        return job

    def test_off_by_default_reads_nothing_and_starts_nothing(self):
        actor = self.claim_actor("v0533-shadow-off", "hello there")
        with patch.object(shadow_router, "recent_turns",
                          side_effect=AssertionError("must not read")):
            self.assertIsNone(shadow_router.capture_context(actor))
        self.assertFalse(shadow_router.submit(actor, "hello", None, None))
        self.sign_in_fixture()  # signed in but mode off: still nothing
        with patch.object(shadow_router, "recent_turns",
                          side_effect=AssertionError("must not read")):
            self.assertIsNone(shadow_router.capture_context(actor))

    def test_request_is_schema_bound_and_never_sees_alex_reply(self):
        request = shadow_router.build_request(self.job(), "gpt-test")
        schema = request["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["properties"]["tools"]["items"]["enum"],
                         [t["name"] for t in CATALOGUE])
        self.assertIn("- update_shopping_item: Mark a shopping item bought.",
                      request["messages"][0]["content"])
        payload = json.loads(request["messages"][1]["content"])
        self.assertEqual(payload["current_message"], "bought 1 and 3")
        self.assertEqual(len(payload["recent_conversation"]), 2)
        self.assertNotIn("tools", request)  # it can never call a tool

    def test_run_records_comparison_and_summary_explains_it(self):
        client = FakePlanClient(json.dumps({
            "tools": ["update_shopping_item", "list_shopping_items", "made_up_tool",
                      "update_shopping_item"],
            "needs_clarification": False, "reason": "Marking listed items bought.",
        }))
        with patch.object(shadow_router, "get_settings", return_value=PLAN_SETTINGS):
            row = shadow_router._run(self.job(), plan_client=client)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["shadow_tools"], ["update_shopping_item", "list_shopping_items"])

        summary = shadow_router.summary(7)
        self.assertEqual(summary["compared"], 1)
        self.assertEqual(summary["turns_where_alex_used_tools"], 1)
        self.assertEqual(summary["ai_router_also_picked_what_alex_used"], 1)
        self.assertEqual(summary["ai_router_wanted_a_tool_keywords_hid"], 1)
        self.assertEqual(summary["recent_disagreements"][0]["ai_router_picked"],
                         ["update_shopping_item", "list_shopping_items"])

    def test_router_failure_is_logged_and_pauses_the_plan(self):
        client = FakePlanClient(error=chatgpt_plan.ChatGPTPlanError(
            "limit", status_code=429, code="subscription_sharing_usage_limit_exceeded"))
        with patch.object(shadow_router, "get_settings", return_value=PLAN_SETTINGS):
            row = shadow_router._run(self.job(), plan_client=client)
        self.assertEqual(row["status"], "error")
        self.assertEqual(shadow_router.summary(7)["errors"], 1)
        self.assertIn("chatgpt", brain.provider_cooldowns())

    def test_called_tools_ignore_errors_and_discovery(self):
        self.assertEqual(
            shadow_router._called_tools([
                "discover_alex_tools", "update_reminder:clarification",
                "create_reminder:error", "list_reminders", "list_reminders",
            ]),
            ["update_reminder", "list_reminders"],
        )

    def test_ingress_reply_is_identical_with_shadow_on_and_runs_after_it(self):
        def run(mid, shadow):
            payload = {
                "message_id": mid, "provider": "WHATSAPP", "conversation_id": DM,
                "conversation_type": "DIRECT_DM", "sender_phone": "+60111111111",
                "text": "How much did I spend today?",
            }
            started = []

            class SyncThread:
                def __init__(self, target, args=(), daemon=None, name=None):
                    self.target, self.args = target, args

                def start(self):
                    started.append(True)
                    self.target(*self.args)

            fake_plan = FakePlanClient(json.dumps({
                "tools": ["query_finances"], "needs_clarification": False, "reason": "spend"}))
            with patch.object(shadow_router, "enabled", return_value=shadow), \
                 patch.object(shadow_router, "catalogue", return_value=CATALOGUE), \
                 patch.object(shadow_router.threading, "Thread", SyncThread), \
                 patch.object(shadow_router.chatgpt_plan, "client", return_value=fake_plan), \
                 patch.object(shadow_router, "get_settings", return_value=PLAN_SETTINGS):
                result = core.AlexCoreTests._v0513_process_with_scripted_provider(
                    self, payload, [{"content": "You spent RM0 today."}])
            conn = db.connect()
            try:
                reply = conn.execute(
                    "SELECT text_body FROM outbound_messages WHERE source_message_id=? "
                    "AND kind='TEXT'", (mid,)).fetchone()["text_body"]
            finally:
                conn.close()
            return result, reply, started, fake_plan

        off_result, off_reply, off_started, _ = run("v0533-ingress-off", False)
        core.AlexCoreTests.setUp(self)  # same fresh state for the second run
        on_result, on_reply, on_started, plan = run("v0533-ingress-on", True)
        self.assertTrue(off_result["ok"] and on_result["ok"])
        self.assertEqual(off_reply, "You spent RM0 today.")
        self.assertEqual(on_reply, off_reply)
        self.assertEqual(off_started, [])
        self.assertEqual(on_started, [True])
        sent = json.loads(plan.requests[0]["messages"][1]["content"])
        self.assertEqual(sent["current_message"], "How much did I spend today?")
        # The router saw the turns BEFORE this message, never Alex's answer to it.
        self.assertNotIn("You spent RM0 today.", json.dumps(sent))
        self.assertEqual(shadow_router.summary(7)["compared"], 1)


if __name__ == "__main__":
    unittest.main()
