"""v0.5.34: AI tool routing (ai_routing), off by default.

Proves: (1) the shipped default changes nothing; (2) when on, the model's tool
picks are shown to the brain first while keyword routing stays behind them;
(3) every failure falls back to the exact keyword result; (4) the existing
safety gates (blocked tools, write permission) still apply to AI picks.
"""

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_v0533_plan_router as base
import test_core as core
from test_v0533_plan_router import FakePlanClient, PLAN_SETTINGS, DM

db = core.db
brain = core.brain
shadow_router = base.shadow_router
chatgpt_plan = base.chatgpt_plan
Settings = core.Settings

import config as app_config  # noqa: E402  (path set up by test_core)

ON = Settings(
    ai_provider="auto", gemini_api_key="gemini-test", xai_api_key="xai-test",
    chatgpt_plan_mode="shadow_only", chatgpt_plan_model="gpt-test", ai_routing="on",
)

CATALOGUE = [
    {"name": "query_finances", "summary": "Read spending."},
    {"name": "log_expense", "summary": "Log an expense."},
    {"name": "update_shopping_item", "summary": "Mark a shopping item bought."},
    {"name": "list_shopping_items", "summary": "Show the shopping list."},
    {"name": "list_reminders", "summary": "Show reminders."},
]


def run(coro):
    return asyncio.run(coro)


def names(specs):
    return [s["function"]["name"] for s in specs]


class V0534Base(base.V0533Base):
    def setUp(self):
        super().setUp()
        shadow_router._live_state.update({"fails": 0, "pause_until": 0.0})

    def tearDown(self):
        shadow_router._live_state.update({"fails": 0, "pause_until": 0.0})
        super().tearDown()

    def specs(self, *tool_names):
        return run(brain._tool_specs_for_names(set(tool_names)))


class SettingsAndGateTests(V0534Base):
    def test_default_is_off_and_bad_values_fall_back_to_off(self):
        self.assertEqual(Settings().ai_routing, "off")
        for raw, expected in (("on", "on"), ("ON ", "on"), ("maybe", "off"), (None, "off")):
            with patch.object(app_config, "_read_options", return_value={"ai_routing": raw}):
                self.assertEqual(app_config.get_settings().ai_routing, expected)

    def test_live_routing_needs_the_switch_the_plan_mode_and_a_sign_in(self):
        self.sign_in_fixture()
        self.assertFalse(shadow_router.live_enabled(PLAN_SETTINGS))  # switch off
        self.assertTrue(shadow_router.live_enabled(ON))
        from dataclasses import replace
        self.assertFalse(shadow_router.live_enabled(replace(ON, chatgpt_plan_mode="off")))
        base.V0533Base._reset_plan_state()  # signed out
        self.assertFalse(shadow_router.live_enabled(ON))


class LiveRouteTests(V0534Base):
    def route(self, client, **kw):
        with patch.object(shadow_router, "get_settings", return_value=ON), \
             patch.object(shadow_router, "acatalogue",
                          new=lambda: asyncio.sleep(0, result=list(CATALOGUE))), \
             patch.object(shadow_router.chatgpt_plan, "resolved_model", return_value="gpt-test"):
            return run(shadow_router.live_route(
                kw.get("text", "bought 1 and 3"),
                kw.get("prior", [{"role": "user", "text": "show shopping list"}]),
                "", plan_client=client))

    def test_returns_only_real_tool_names_in_order(self):
        client = FakePlanClient(json.dumps({
            "tools": ["update_shopping_item", "made_up", "list_shopping_items",
                      "update_shopping_item"],
            "needs_clarification": False, "reason": "bought items"}))
        choice = self.route(client)
        self.assertEqual(choice["tools"], ["update_shopping_item", "list_shopping_items"])
        self.assertEqual(choice["model"], "gpt-test")
        self.assertNotIn("tools", client.requests[0])  # the router can never call a tool

    def test_failure_returns_none_and_three_in_a_row_pause_live_routing(self):
        client = FakePlanClient(error=RuntimeError("boom"))
        for _ in range(shadow_router.LIVE_FAIL_LIMIT):
            self.assertIsNone(self.route(client))
        self.sign_in_fixture()
        self.assertFalse(shadow_router.live_enabled(ON))  # paused, keywords only
        shadow_router._live_state["pause_until"] = time.monotonic() - 1
        self.assertTrue(shadow_router.live_enabled(ON))

    def test_slow_router_times_out_instead_of_delaying_the_reply(self):
        class Slow(FakePlanClient):
            def create(self, **kwargs):
                time.sleep(0.5)
                return super().create(**kwargs)

        client = Slow(json.dumps({"tools": ["query_finances"],
                                  "needs_clarification": False, "reason": "x"}))
        with patch.object(shadow_router, "LIVE_TIMEOUT_SECONDS", 0.05):
            self.assertIsNone(self.route(client))
        self.assertEqual(shadow_router._live_state["fails"], 1)
        self.assertNotIn("chatgpt", brain.provider_cooldowns())  # slow is not down


class ApplyAiRouteTests(V0534Base):
    def apply(self, choice, tools, text="bought 1 and 3", trace=None):
        trace = {} if trace is None else trace

        async def fake_live(*_a, **_k):
            return choice

        with patch.object(shadow_router, "live_route", new=fake_live):
            out = run(brain._apply_ai_route(trace, tools, text, None, [], None))
        return out, trace

    def test_ai_picks_come_first_and_keyword_tools_stay_behind_them(self):
        keyword = self.specs("query_finances", "list_reminders")
        out, trace = self.apply(
            {"tools": ["list_shopping_items"], "needs_clarification": False,
             "reason": "r", "model": "m", "latency_ms": 5}, keyword,
            text="what is on my list")
        self.assertEqual(names(out), ["list_shopping_items", "query_finances", "list_reminders"])
        self.assertEqual(trace["ai_route"]["applied"], ["list_shopping_items"])

    def test_failure_or_nothing_usable_returns_keyword_tools_untouched(self):
        keyword = self.specs("query_finances", "list_reminders")
        out, trace = self.apply(None, keyword)
        self.assertIs(out, keyword)
        self.assertNotIn("ai_route", trace)
        out, _ = self.apply({"tools": [], "needs_clarification": True, "reason": "?"}, keyword)
        self.assertIs(out, keyword)

    def test_exposure_is_capped_so_it_cannot_balloon(self):
        keyword = self.specs("query_finances", "list_reminders", "list_shopping_items")
        picks = ["list_work_roster", "list_leave_records", "list_tasks", "get_agenda",
                 "planning_brief", "bills_list"]
        out, _ = self.apply({"tools": picks, "needs_clarification": False, "reason": "r"},
                            keyword, text="what is coming up")
        self.assertLessEqual(len(out), brain.AI_ROUTE_MERGED_MAX)
        self.assertEqual(names(out)[:6].count("query_finances"), 0)

    def test_a_write_tool_is_not_offered_unless_the_user_asked_for_a_write(self):
        keyword = self.specs("query_finances")
        out, trace = self.apply(
            {"tools": ["log_expense", "query_finances"], "needs_clarification": False,
             "reason": "r"}, keyword, text="how much did I spend today")
        self.assertNotIn("log_expense", names(out))
        self.assertEqual(trace["ai_route"]["applied"], ["query_finances"])
        out, _ = self.apply(
            {"tools": ["log_expense"], "needs_clarification": False, "reason": "r"},
            keyword, text="add RM12 lunch expense")
        self.assertEqual(names(out)[0], "log_expense")

    def test_tools_the_keyword_layer_deliberately_blocks_stay_blocked(self):
        keyword = self.specs("list_leave_records")
        text = "do I have any leave coming up"
        self.assertIn("set_leave_record", brain._routing_refinements(text)[1])
        out, _ = self.apply(
            {"tools": ["set_leave_record", "list_leave_records"],
             "needs_clarification": False, "reason": "r"}, keyword, text=text)
        self.assertNotIn("set_leave_record", names(out))
        self.assertIn("list_leave_records", names(out))

    def test_the_discovery_tool_is_never_taken_from_the_ai(self):
        keyword = self.specs("query_finances")
        out, trace = self.apply(
            {"tools": [brain.DISCOVERY_TOOL_NAME], "needs_clarification": False,
             "reason": "r"}, keyword)
        self.assertIs(out, keyword)


class EndToEndTests(V0534Base):
    TEXT = "How much did I spend today?"

    def process(self, mid, settings, live=None, live_enabled=True):
        payload = {
            "message_id": mid, "provider": "WHATSAPP", "conversation_id": DM,
            "conversation_type": "DIRECT_DM", "sender_phone": "+60111111111",
            "text": self.TEXT,
        }
        threads = []

        class Quiet:
            def __init__(self, *a, **k):
                threads.append(True)

            def start(self):
                pass

        async def fake_live(*_a, **_k):
            return live

        with patch.object(shadow_router, "live_enabled", return_value=live_enabled), \
             patch.object(shadow_router, "enabled", return_value=live_enabled), \
             patch.object(shadow_router, "live_route", new=fake_live), \
             patch.object(shadow_router, "catalogue", return_value=CATALOGUE), \
             patch.object(shadow_router.threading, "Thread", Quiet), \
             patch.object(shadow_router, "get_settings", return_value=settings):
            result = core.AlexCoreTests._v0513_process_with_scripted_provider(
                self, payload, [{"content": "You spent RM0 today."}])
        conn = db.connect()
        try:
            reply = conn.execute(
                "SELECT text_body FROM outbound_messages WHERE source_message_id=? "
                "AND kind='TEXT'", (mid,)).fetchone()["text_body"]
        finally:
            conn.close()
        return result, reply, brain.recent_trace(mid), threads

    def test_reply_path_unchanged_when_off_or_when_the_router_fails(self):
        _, off_reply, off_trace, _ = self.process("v0534-off", PLAN_SETTINGS, live_enabled=False)
        core.AlexCoreTests.setUp(self)
        _, fail_reply, fail_trace, _ = self.process("v0534-fail", ON, live=None)
        self.assertEqual(off_reply, "You spent RM0 today.")
        self.assertEqual(fail_reply, off_reply)
        self.assertEqual(fail_trace["exposed_tools"], off_trace["exposed_tools"])
        self.assertIsNone(off_trace["ai_route"])
        self.assertIsNone(fail_trace["ai_route"])

    def test_ai_picks_reach_the_brain_and_are_logged_once_without_a_second_call(self):
        _, _, off_trace, _ = self.process("v0534-base", PLAN_SETTINGS, live_enabled=False)
        core.AlexCoreTests.setUp(self)
        choice = {"tools": ["list_shopping_items"], "needs_clarification": False,
                  "reason": "wants list", "model": "gpt-test", "latency_ms": 7}
        result, reply, trace, threads = self.process("v0534-on", ON, live=choice)
        self.assertTrue(result["ok"])
        self.assertEqual(reply, "You spent RM0 today.")
        self.assertIn("list_shopping_items", trace["exposed_tools"])
        self.assertNotIn("list_shopping_items", off_trace["exposed_tools"])
        self.assertEqual(sorted(trace["keyword_tools"]), off_trace["exposed_tools"])
        self.assertEqual(threads, [])  # already routed: no background re-comparison
        summary = shadow_router.summary(7)
        self.assertEqual(summary["turns_routed_by_ai"], 1)
        self.assertEqual(summary["compared"], 1)
        self.assertEqual(summary["ai_router_wanted_a_tool_keywords_hid"], 1)


if __name__ == "__main__":
    unittest.main()
