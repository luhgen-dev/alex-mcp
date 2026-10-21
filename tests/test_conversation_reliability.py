"""Conversation-state and orchestration regressions using an offline provider.

These checks exercise real Alex state/tool plumbing. Provider replies are
scripted: passing does NOT establish language-model understanding or live
WhatsApp latency. tools/conversation_comparison.py runs the same checks against
the unchanged baseline and a candidate in separate, disposable processes.
"""

import asyncio
import copy
import importlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import test_core as core

db = core.db
brain = core.brain
services = core.services


class ConversationFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.AlexCoreTests.setUpClass()

    def setUp(self):
        core.AlexCoreTests.setUp(self)
        self.evidence_path = "routing helper only; no provider or ingress"

    def tearDown(self):
        core.AlexCoreTests.setUp(self)

    def actor(self, mid, text, *, spouse=False, conversation=None):
        phone = "+60222222222" if spouse else "+60111111111"
        chat = conversation or phone[1:] + "@s.whatsapp.net"
        kind = "GROUP" if chat.endswith("@g.us") else "DIRECT_DM"
        db.claim_inbound({
            "message_id": mid, "provider": "WHATSAPP",
            "conversation_id": chat, "conversation_type": kind,
            "sender_phone": phone, "text": text,
        })
        result = db.resolve_actor(phone, chat, kind, mid, [])
        return replace(result, trusted_text=text, intent_text=text, read_scope="family",
                       received_at_utc="2026-10-21T08:00:00+00:00")

    def draft(self, mid="cr-draft", *, spouse=False, conversation=None,
              original="Remind me to water the plants", question=None):
        actor = self.actor(mid, original, spouse=spouse, conversation=conversation)
        question = question or "What time should I remind you to water the plants?"
        draft = db.create_pending_item(actor, "REMINDER_DRAFT", note="fixture")
        out = db.queue_outbound(
            actor.conversation_id, "TEXT", text=question,
            source_message_id=actor.source_message_id,
            context_kind="PENDING_ITEM", context_id=draft["item_id"],
        )
        db.add_turn(actor.user_id, actor.conversation_id, "user", original)
        db.add_turn(actor.user_id, actor.conversation_id, "assistant", question)
        return actor, draft, out

    @staticmethod
    def quote(draft, question="What time should I remind you to water the plants?"):
        return {
            "context_kind": "PENDING_ITEM", "context_id": draft["item_id"],
            "source_message_id": draft["source_message_id"],
            "quoted_alex_text": question,
            "pending_item": {
                "item_id": draft["item_id"], "kind": "REMINDER_DRAFT",
                "accumulated_text": draft.get("accumulated_text", ""),
                "source_message_id": draft["source_message_id"],
            },
        }

    def state(self, actor, text, quoted_context=None):
        self.evidence_path = "shared state builder with real disposable database; no provider or ingress"
        try:
            module = importlib.import_module("conversation_state")
        except ModuleNotFoundError:
            self.fail("This revision has no shared grounded conversation state")
        return module.build(actor, text, quoted_context=quoted_context)

    def capture_request(self, actor, text, quoted_context=None):
        self.evidence_path = "direct brain request with scripted text provider; bypasses ingress"
        requests = []

        class Completions:
            def create(self, **kwargs):
                requests.append(copy.deepcopy(kwargs))
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content="Please tell me the time you prefer.", tool_calls=[]))],
                    usage=None,
                )

        fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        route = {"provider": "grok", "model": "offline-fixture",
                 "reasoning_effort": "low", "role": "manual"}
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=fake), \
             patch.object(brain.shadow_router, "live_enabled", return_value=False):
            reply, _ = asyncio.run(brain.respond(
                actor, text, quoted_context=quoted_context))
        self.assertTrue(requests, f"No provider-visible request; reply={reply!r}")
        self.assertEqual(len(requests), 1, "Context assembly added a model call")
        # Consumed by the comparator; this is local payload size, not tokens.
        self.offline_metrics = {
            "model_calls": len(requests),
            "message_characters": len(json.dumps(requests[0]["messages"], ensure_ascii=False)),
            "tool_count": len(requests[0].get("tools") or []),
        }
        return requests[0]

    def scripted(self, steps, *, payload=None, actor=None, text=None,
                 quoted_context=None, allow_unexposed=False):
        """Run real orchestration and tools; only the provider's decisions are fake."""
        remaining = list(steps)
        requests = []
        self.evidence_path = (
            "ingress + brain + real MCP/backend; scripted provider decisions"
            if payload is not None else
            "direct brain + real MCP/backend; scripted provider decisions; bypasses ingress"
        )

        class Message:
            def __init__(self, step, number):
                self.content = step.get("content", "")
                self.tool_calls = []
                calls = step.get("calls", [step] if "tool" in step else [])
                for index, call in enumerate(calls):
                    self.tool_calls.append(SimpleNamespace(
                        id=f"conversation-script-{number}-{index}",
                        function=SimpleNamespace(name=call["tool"],
                                                 arguments=json.dumps(call.get("args", {})))))

            def model_dump(self, exclude_none=True):
                value = {"role": "assistant", "content": self.content}
                if self.tool_calls:
                    value["tool_calls"] = [{
                        "id": call.id, "type": "function", "function": {
                            "name": call.function.name, "arguments": call.function.arguments,
                        }} for call in self.tool_calls]
                return value

        class Completions:
            def create(inner, **kwargs):
                requests.append(copy.deepcopy(kwargs))
                self.assertTrue(remaining, "Scripted provider exhausted")
                step = remaining.pop(0)
                names = {x["function"]["name"] for x in kwargs.get("tools", [])}
                for call in step.get("calls", [step] if "tool" in step else []):
                    if not allow_unexposed:
                        self.assertIn(call["tool"], names, "Required tool was not exposed")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=Message(step, len(requests)))], usage=None)

        fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        route = {"provider": "grok", "model": "offline-fixture",
                 "reasoning_effort": "low", "role": "manual"}
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=fake), \
             patch.object(brain.shadow_router, "live_enabled", return_value=False):
            result = (core.ingress.process(payload) if payload is not None
                      else asyncio.run(brain.respond(actor, text, quoted_context=quoted_context)))
        self.assertFalse(remaining, "Unused scripted provider responses")
        self.offline_metrics = {
            "model_calls": len(requests),
            "message_characters": len(json.dumps(requests[0]["messages"], ensure_ascii=False)),
            "tool_count": len(requests[0].get("tools") or []),
            "scripted_execution": True,
        }
        return result, requests

    @staticmethod
    def payload(mid, text, **extra):
        return {"message_id": mid, "provider": "WHATSAPP",
                "conversation_id": "60111111111@s.whatsapp.net",
                "conversation_type": "DIRECT_DM", "sender_phone": "+60111111111",
                "text": text, **extra}

    def saved_list(self, actor, marker, labels, *, scope="family"):
        ids = []
        for index, label in enumerate(labels):
            creator = replace(actor, action_key=f"{marker}-save-{index}",
                              trusted_text="save this note" if scope == "family"
                              else "save this note privately")
            item = services.save_item(creator, label, f"content of {label}",
                                      shared=scope == "family")
            ids.append(item.get("item_id") or item.get("id"))
        conn = db.connect()
        try:
            selection_id = services._store_selection(conn, actor, "SAVED_ITEM", ids)
            conn.commit()
        finally:
            conn.close()
        return selection_id, ids

    @staticmethod
    def restore_pending_for_defensive_fixture(item_id):
        """Simulate imported/legacy overlapping drafts despite normal supersession."""
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE pending_items SET status='PENDING',resolved_at_utc=NULL,
                   resolution_message_id=NULL WHERE item_id=?""", (item_id,))
            conn.commit()
        finally:
            conn.close()


class GroundedConversationStateTests(ConversationFixture):
    """State identity/visibility contract, not an NLU benchmark."""

    def test_pending_question_and_exact_id_survive_unquoted_followup(self):
        owner, draft, _ = self.draft()
        text = "Around seven tomorrow evening would work for me"
        current = self.actor("cr-current", text)
        state = self.state(current, text)
        self.assertEqual(state.get("active", {}).get("id"), draft["item_id"])
        self.assertIn("water the plants", state["active"].get("summary", ""))
        self.assertIn("What time", state["active"].get("question", ""))
        self.assertEqual(state["current_time"].get("received_at_utc"), current.received_at_utc)

    def test_quoted_pending_wins_over_newer_draft(self):
        _, old, _ = self.draft("cr-old", original="Remind me to collect the parcel")
        _, newer, _ = self.draft("cr-new", original="Remind me to call the dentist")
        self.restore_pending_for_defensive_fixture(old["item_id"])
        text = "in forty minutes please"
        current = self.actor("cr-quote", text)
        state = self.state(current, text, self.quote(old))
        self.assertEqual(state.get("active", {}).get("id"), old["item_id"])
        self.assertNotEqual(state["active"]["id"], newer["item_id"])

    def test_two_unquoted_drafts_do_not_guess_a_target(self):
        _, first, _ = self.draft("cr-a", original="Remind me to collect the parcel")
        self.draft("cr-b", original="Remind me to call the dentist")
        self.restore_pending_for_defensive_fixture(first["item_id"])
        current = self.actor("cr-ambiguous", "in forty minutes please")
        state = self.state(current, current.trusted_text)
        self.assertFalse(state.get("active", {}).get("id"))

    def test_another_person_pending_record_is_not_in_context(self):
        _, foreign, _ = self.draft("cr-spouse", spouse=True,
                                  original="Remind me about private wife appointment")
        current = self.actor("cr-husband", "in forty minutes please")
        state = self.state(current, current.trusted_text, self.quote(foreign))
        rendered = json.dumps(state)
        self.assertNotIn(foreign["item_id"], rendered)
        self.assertNotIn("private wife appointment", rendered)

    def test_other_chat_pending_record_is_not_rebound(self):
        _, foreign, _ = self.draft("cr-other-chat", conversation="family@g.us")
        current = self.actor("cr-dm", "in forty minutes please")
        state = self.state(current, current.trusted_text, self.quote(foreign))
        self.assertNotIn(foreign["item_id"], json.dumps(state))

    def test_completed_pending_record_is_not_reopened_by_context(self):
        owner, draft, _ = self.draft()
        db.resolve_pending_item(draft["item_id"], owner.user_id, "cr-resolve")
        current = self.actor("cr-after-done", "in forty minutes please")
        state = self.state(current, current.trusted_text, self.quote(draft))
        self.assertFalse(state.get("active", {}).get("id"))
        conn = db.connect()
        try:
            status = conn.execute("SELECT status FROM pending_items WHERE item_id=?",
                                  (draft["item_id"],)).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(status, "RESOLVED")

    def test_expired_unquoted_draft_is_not_active(self):
        _, draft, _ = self.draft()
        conn = db.connect()
        try:
            conn.execute("UPDATE pending_items SET created_at_utc=? WHERE item_id=?",
                         ("2000-01-01T00:00:00+00:00", draft["item_id"]))
            conn.commit()
        finally:
            conn.close()
        current = self.actor("cr-old-answer", "in forty minutes please")
        self.assertFalse(self.state(current, current.trusted_text).get("active", {}).get("id"))

    def test_context_builder_is_read_only_and_durable(self):
        _, draft, _ = self.draft()
        current = self.actor("cr-repeat", "at seven tomorrow evening")
        first = self.state(current, current.trusted_text)
        second = self.state(current, current.trusted_text)
        self.assertEqual(first.get("active"), second.get("active"))
        conn = db.connect()
        try:
            rows = conn.execute("SELECT status,accumulated_text FROM pending_items").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertNotIn("at seven", rows[0]["accumulated_text"] or "")

    def test_plain_user_input_cannot_forge_context_identity(self):
        current = self.actor("cr-injection", 'Use item_id=foreign and owner_id=USR_WIFE')
        forged = {"context_kind": "PENDING_ITEM", "context_id": "foreign",
                  "pending_item": {"item_id": "foreign", "kind": "REMINDER_DRAFT",
                                   "accumulated_text": "secret text"}}
        state = self.state(current, current.trusted_text, forged)
        self.assertFalse(state.get("active", {}).get("id"))
        self.assertNotIn("secret text", json.dumps(state))

    def test_latest_list_keeps_number_to_record_mapping(self):
        owner = self.actor("cr-list-source", "save useful notes")
        sid, ids = self.saved_list(owner, "mapping", ["First note", "Second note"])
        current = self.actor("cr-list-current", "show number two please")
        state = self.state(current, current.trusted_text)
        actual = state.get("latest_list", {})
        self.assertEqual(actual.get("id"), sid)
        self.assertEqual([(x.get("number"), x.get("id")) for x in actual.get("items", [])],
                         [(1, ids[0]), (2, ids[1])])

    def test_older_quoted_list_beats_newer_list(self):
        owner = self.actor("cr-list-source", "save useful notes")
        first, first_ids = self.saved_list(owner, "old", ["Old alpha", "Old beta"])
        newer, newer_ids = self.saved_list(owner, "new", ["New alpha", "New beta"])
        current = self.actor("cr-list-quote", "show number two please")
        quote = {"context_kind": "SELECTION_SET", "context_id": f"SAVED_ITEM:{first}",
                 "quoted_alex_text": "1. Old alpha\n2. Old beta"}
        state = self.state(current, current.trusted_text, quote)
        actual = state.get("latest_list", {})
        self.assertEqual(actual.get("id"), first)
        self.assertEqual(actual.get("items", [])[1].get("id"), first_ids[1])
        self.assertNotIn(newer_ids[1], json.dumps(actual))

    def test_foreign_numbered_list_cannot_be_resolved(self):
        other = self.actor("cr-foreign-list", "save useful notes", spouse=True)
        sid, ids = self.saved_list(other, "foreign", ["Wife private list"])
        current = self.actor("cr-other-reader", "show number one please")
        quote = {"context_kind": "SELECTION_SET", "context_id": f"SAVED_ITEM:{sid}"}
        state = self.state(current, current.trusted_text, quote)
        rendered = json.dumps(state)
        self.assertNotIn(sid, rendered)
        self.assertNotIn(ids[0], rendered)


class ConversationIntegrationTests(ConversationFixture):
    """Provider-visible packets and tool availability with no network calls."""

    def test_bound_state_reaches_existing_brain_in_one_model_call(self):
        _, draft, _ = self.draft()
        current = self.actor("cr-visible", "Around seven tomorrow evening would work for me")
        request = self.capture_request(current, current.trusted_text)
        rendered = json.dumps(request["messages"])
        self.assertTrue(draft["item_id"] in rendered, "Exact pending item ID was absent from the model packet")
        self.assertTrue("What time should I remind you" in rendered, "Pending question was absent from the model packet")
        self.assertTrue("water the plants" in rendered, "Pending task was absent from the model packet")

    def test_fresh_request_does_not_consume_or_cancel_pending_draft(self):
        _, draft, _ = self.draft()
        current = self.actor("cr-new-request", "Add milk to our shopping list")
        request = self.capture_request(current, current.trusted_text)
        names = {x["function"]["name"] for x in request.get("tools", [])}
        self.assertIn("add_shopping_item", names)
        conn = db.connect()
        try:
            row = conn.execute("SELECT status,accumulated_text FROM pending_items WHERE item_id=?",
                               (draft["item_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "PENDING")
        self.assertNotIn("milk", row["accumulated_text"] or "")

    def test_semantic_merge_cannot_displace_original_tools(self):
        originals = {"query_finances", "list_reminders", "list_shopping_items",
                     "get_agenda", "list_tasks", "list_leave_records"}
        picks = ["list_work_roster", "planning_brief", "bills_list", "finance_report",
                 "search_saved_items", "list_savings_goals"]
        keyword = asyncio.run(brain._tool_specs_for_names(originals))

        async def route(*args, **kwargs):
            return {"tools": picks, "needs_clarification": False, "reason": "fixture"}

        with patch.object(brain.shadow_router, "live_route", new=route):
            result = asyncio.run(brain._apply_ai_route(
                {}, keyword, "what is coming up", None, [], None))
        names = {x["function"]["name"] for x in result}
        self.assertTrue(originals <= names, f"Lost original tools: {sorted(originals - names)}")
        self.assertLessEqual(len(result), brain.AI_ROUTE_MERGED_MAX)

    def test_router_failure_preserves_exact_original_tool_objects(self):
        keyword = asyncio.run(brain._tool_specs_for_names({"query_finances", "list_reminders"}))

        async def failed(*args, **kwargs):
            return None

        with patch.object(brain.shadow_router, "live_route", new=failed):
            result = asyncio.run(brain._apply_ai_route(
                {}, keyword, "what is coming up", None, [], None))
        self.assertIs(result, keyword)

    def closed_pending_quote(self):
        owner, draft, outbound_id = self.draft("cr-closed-source")
        db.cancel_pending_item(draft["item_id"], owner.user_id, "cr-closed-cancel")
        conn = db.connect()
        try:
            conn.execute("""UPDATE outbound_messages SET provider_message_id=?,delivery_status='SENT'
                         WHERE outbound_id=?""", ("wa-cr-closed-question", outbound_id))
            conn.commit()
        finally:
            conn.close()
        quote = db.resolve_quoted_context(owner.conversation_id, "wa-cr-closed-question", owner.phone)
        self.assertEqual(quote["context_id"], draft["item_id"])
        self.assertNotIn("pending_item", quote)
        return quote

    def test_real_closed_pending_quote_does_not_restore_write_tools(self):
        quote = self.closed_pending_quote()
        current = self.actor("cr-closed-followup", "Tomorrow at seven in the evening please")
        request = self.capture_request(current, current.trusted_text, quote)
        names = {x["function"]["name"] for x in request.get("tools", [])}
        self.assertFalse(names & {"create_reminder", "resolve_pending_item", "cancel_pending_item", "update_reminder"})

    def test_explicit_new_reminder_still_works_when_quoting_closed_prompt(self):
        quote = self.closed_pending_quote()
        current = self.actor("cr-closed-new", "Create a new reminder to check the window tomorrow at seven in the evening")
        request = self.capture_request(current, current.trusted_text, quote)
        names = {x["function"]["name"] for x in request.get("tools", [])}
        self.assertIn("create_reminder", names)


class ScriptedExecutionTests(ConversationFixture):
    """Actual domain effects; the provider is explicitly supplied each decision."""

    def test_new_shopping_action_preserves_pending_reminder(self):
        _, draft, _ = self.draft()
        result, _ = self.scripted([
            {"tool": "add_shopping_item", "args": {"item": "milk"}},
            {"content": "Added milk to the shopping list."},
        ], payload=self.payload("cr-exec-shopping", "Add milk to our shopping list"))
        self.assertTrue(result["ok"])
        conn = db.connect()
        try:
            rows = conn.execute("SELECT item_name,space_id FROM shopping_items").fetchall()
            pending = conn.execute("SELECT status,accumulated_text FROM pending_items WHERE item_id=?",
                                   (draft["item_id"],)).fetchone()
            reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual([(r["item_name"], r["space_id"]) for r in rows], [("milk", "FAMILY_SHARED")])
        self.assertEqual(pending["status"], "PENDING")
        self.assertNotIn("milk", pending["accumulated_text"])
        self.assertEqual(reminder_count, 0)

    def test_quoted_expense_corrects_exact_original_once(self):
        original_actor = self.actor("cr-lunch", "Log RM18 lunch")
        original = services.log_expense(replace(original_actor, action_key="cr-lunch-create"),
                                        "Lunch", 18, "food", currency="MYR")
        other_actor = self.actor("cr-parking", "Log RM5 parking")
        other = services.log_expense(replace(other_actor, action_key="cr-parking-create"),
                                     "Parking", 5, "transport", currency="MYR")
        outbound = db.queue_outbound(original_actor.conversation_id, "TEXT",
                                     text="Logged lunch: RM18.00.",
                                     source_message_id=original_actor.source_message_id)
        conn = db.connect()
        try:
            conn.execute("UPDATE outbound_messages SET provider_message_id=? WHERE outbound_id=?",
                         ("wa-cr-lunch", outbound))
            conn.commit()
        finally:
            conn.close()
        result, requests = self.scripted([
            {"tool": "correct_expense", "args": {"event_id": original["event_id"],
                                                     "amount": 12.80, "reason": "user correction"}},
            {"content": "Corrected lunch to RM12.80."},
        ], payload=self.payload("cr-correction", "Actually RM12.80", quoted_message_id="wa-cr-lunch"))
        self.assertTrue(result["ok"])
        self.assertIn(original["event_id"], json.dumps(requests[0]["messages"]))
        conn = db.connect()
        try:
            rows = {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM financial_events")}
            links = conn.execute("SELECT * FROM financial_event_corrections").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[original["event_id"]]["status"], "SUPERSEDED")
        self.assertEqual(rows[other["event_id"]]["amount_minor"], 500)
        self.assertEqual(rows[other["event_id"]]["status"], "ACTIVE")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["parent_event_id"], original["event_id"])
        self.assertEqual(rows[links[0]["child_event_id"]]["amount_minor"], 1280)
        self.assertEqual(rows[links[0]["child_event_id"]]["space_id"], rows[original["event_id"]]["space_id"])

    def test_replayed_inbound_does_not_repeat_tool_or_domain_write(self):
        payload = self.payload("cr-duplicate", "Add apples to our shopping list")
        first, _ = self.scripted([
            {"tool": "add_shopping_item", "args": {"item": "apples"}},
            {"content": "Added apples to the shopping list."},
        ], payload=payload)
        self.assertTrue(first["ok"])
        with patch.object(brain, "respond", side_effect=AssertionError("Replay reached model")):
            repeated = core.ingress.process(payload)
        self.assertTrue(repeated["ok"])
        conn = db.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shopping_items").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tool_audit WHERE tool_name='add_shopping_item'")
                             .fetchone()[0], 1)
        finally:
            conn.close()

    def test_unexposed_provider_mutation_cannot_remove_shopping_item(self):
        owner = self.actor("cr-shopping-seed", "Add milk to our shopping list")
        item = services.add_shopping_item(replace(owner, action_key="cr-shopping-seed-action"), "milk")
        current = self.actor("cr-malicious-call", "What is on the shopping list?")
        _, requests = self.scripted([
            {"tool": "update_shopping_item", "args": {"item_id": item["item_id"], "status": "removed"}},
            {"content": "Your shopping list contains milk."},
        ], actor=current, text=current.trusted_text, allow_unexposed=True)
        self.assertNotIn("update_shopping_item", {x["function"]["name"] for x in requests[0]["tools"]})
        conn = db.connect()
        try:
            actual = conn.execute("SELECT status FROM shopping_items WHERE item_id=?", (item["item_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(actual["status"], "OPEN", "An unoffered provider tool was executed")

    def test_quoted_older_list_binds_real_numbered_tool_to_older_ids(self):
        owner = self.actor("cr-list-exec-seed", "save useful notes")
        first, first_ids = self.saved_list(owner, "old-exec", ["Old alpha", "Old beta"])
        newer, newer_ids = self.saved_list(owner, "new-exec", ["New alpha", "New beta"])
        current = self.actor("cr-list-exec", "show number two please")
        quote = {"context_kind": "SELECTION_SET", "context_id": f"SAVED_ITEM:{first}",
                 "quoted_alex_text": "1. Old alpha\n2. Old beta"}
        _, requests = self.scripted([
            {"tool": "resolve_numbered_choice", "args": {"choice": 2}},
            {"content": "The second note is Old beta."},
        ], actor=current, text=current.trusted_text, quoted_context=quote)
        tool_results = [m for m in requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_results), 1)
        rendered = tool_results[0]["content"]
        self.assertIn(first_ids[1], rendered)
        self.assertNotIn(newer_ids[1], rendered)

    def test_negation_does_not_resurrect_contextual_mutation_tools(self):
        _, draft, _ = self.draft()
        current = self.actor("cr-no-followup", "Don't create a reminder or change anything yet")
        request = self.capture_request(current, current.trusted_text, self.quote(draft))
        names = {x["function"]["name"] for x in request.get("tools", [])}
        self.assertFalse(names & {"create_reminder", "resolve_pending_item", "cancel_pending_item", "update_reminder"})

    def test_fresh_topic_does_not_offer_contextual_reminder_mutations(self):
        _, draft, _ = self.draft()
        current = self.actor("cr-topic-switch", "Add milk to our shopping list")
        request = self.capture_request(current, current.trusted_text, self.quote(draft))
        names = {x["function"]["name"] for x in request.get("tools", [])}
        self.assertIn("add_shopping_item", names)
        self.assertNotIn("create_reminder", names)

    def test_quoted_list_stays_bound_after_unrelated_finance_read(self):
        owner = self.actor("cr-list-read-seed", "save useful notes")
        first, first_ids = self.saved_list(owner, "read-old", ["Old alpha", "Old beta"])
        _, newer_ids = self.saved_list(owner, "read-new", ["New alpha", "New beta"])
        current = self.actor("cr-list-read", "Show today's spending, then show number two from this list")
        quote = {"context_kind": "SELECTION_SET", "context_id": f"SAVED_ITEM:{first}",
                 "quoted_alex_text": "1. Old alpha\n2. Old beta"}
        _, requests = self.scripted([
            {"tool": "query_finances", "args": {}},
            {"tool": "resolve_numbered_choice", "args": {"choice": 2}},
            {"content": "There is no spending today. The second note is Old beta."},
        ], actor=current, text=current.trusted_text, quoted_context=quote)
        results = [m["content"] for m in requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(results), 2)
        self.assertIn(first_ids[1], results[-1])
        self.assertNotIn(newer_ids[1], results[-1])

    def test_multiple_numbered_choices_use_same_quoted_old_list(self):
        owner = self.actor("cr-list-multi-seed", "save useful notes")
        first, first_ids = self.saved_list(owner, "multi-old", ["Old alpha", "Old beta"])
        _, newer_ids = self.saved_list(owner, "multi-new", ["New alpha", "New beta"])
        current = self.actor("cr-list-multi", "Show number one and number two from this list")
        quote = {"context_kind": "SELECTION_SET", "context_id": f"SAVED_ITEM:{first}",
                 "quoted_alex_text": "1. Old alpha\n2. Old beta"}
        _, requests = self.scripted([
            {"tool": "resolve_numbered_choice", "args": {"choice": 1}},
            {"tool": "resolve_numbered_choice", "args": {"choice": 2}},
            {"content": "The notes are Old alpha and Old beta."},
        ], actor=current, text=current.trusted_text, quoted_context=quote)
        results = [m["content"] for m in requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(results), 2)
        self.assertIn(first_ids[0], results[0])
        self.assertIn(first_ids[1], results[1])
        self.assertNotIn(newer_ids[1], json.dumps(results))

    def test_discovery_cannot_authorize_unoffered_call_in_same_response(self):
        owner = self.actor("cr-discovery-seed", "Add milk to our shopping list")
        item = services.add_shopping_item(replace(owner, action_key="cr-discovery-seed-action"), "milk")
        current = self.actor("cr-discovery-attack", "What is on the shopping list?")
        discovered = asyncio.run(brain._tool_specs_for_names({"update_shopping_item"}))

        async def discover(*args, **kwargs):
            return discovered

        with patch.object(brain, "_discover_tool_specs", new=discover):
            _, requests = self.scripted([
                {"calls": [
                    {"tool": brain.DISCOVERY_TOOL_NAME, "args": {"intent": "fixture discovery"}},
                    {"tool": "update_shopping_item", "args": {"item_id": item["item_id"], "status": "removed"}},
                ]},
                {"content": "Your shopping list contains milk."},
            ], actor=current, text=current.trusted_text, allow_unexposed=True)
        offered_before = {x["function"]["name"] for x in requests[0]["tools"]}
        offered_after = {x["function"]["name"] for x in requests[1]["tools"]}
        self.assertIn(brain.DISCOVERY_TOOL_NAME, offered_before)
        self.assertNotIn("update_shopping_item", offered_before)
        self.assertIn("update_shopping_item", offered_after)
        conn = db.connect()
        try:
            actual = conn.execute("SELECT status FROM shopping_items WHERE item_id=?", (item["item_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(actual["status"], "OPEN")

    def assert_invalid_quoted_list_does_not_fall_back(self, *, missing=False):
        owner = self.actor("cr-stale-list-seed", "save useful notes")
        first, _ = self.saved_list(owner, "stale-old", ["Old alpha", "Old beta"])
        _, newer_ids = self.saved_list(owner, "stale-new", ["New alpha", "New beta"])
        conn = db.connect()
        try:
            conn.execute("UPDATE selection_sets SET expires_at_utc=? WHERE selection_id=?",
                         ("2000-01-01T00:00:00+00:00", first))
            conn.commit()
        finally:
            conn.close()
        context_id = "nonexistent-selection" if missing else first
        current = self.actor("cr-stale-list", "show 2")
        quote = {"context_kind": "SELECTION_SET", "context_id": f"SAVED_ITEM:{context_id}",
                 "quoted_alex_text": "1. Old alpha\n2. Old beta"}
        _, requests = self.scripted([
            {"tool": "resolve_numbered_choice", "args": {"choice": 2}},
            {"content": "Please show me the list again so I can identify the right note."},
        ], actor=current, text=current.trusted_text, quoted_context=quote, allow_unexposed=True)
        results = [m["content"] for m in requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(results), 1)
        self.assertIn("resolve_numbered_choice", {x["function"]["name"] for x in requests[0]["tools"]})
        self.assertNotIn(newer_ids[1], results[0], "An invalid quoted list silently selected a different list")
        self.assertNotIn("New beta", results[0])
        self.assertEqual(json.loads(results[0]).get("status"), "needs_clarification")

    def test_expired_quoted_list_does_not_fall_back_to_unrelated_newer_list(self):
        self.assert_invalid_quoted_list_does_not_fall_back()

    def test_unresolvable_quoted_list_does_not_fall_back_to_unrelated_newer_list(self):
        self.assert_invalid_quoted_list_does_not_fall_back(missing=True)


FOLLOWUP_VARIATIONS = (
    "Around seven tomorrow evening would work for me",
    "Forty minutes from the message I just sent please",
    "Tomorrow after dinner at eight would be best",
    "The time should be tomorrow evening at seven",
    "Roughly half an hour from the current time please",
    "Tonight, sometime after eight in the evening",
    "Tomorrow around 7ish would be convenient",
    "Later at 11.30 in the evening please",
    "At about seven o'clock tomorrow evening",
    "Forty minutes later would be alright",
)


def _followup_case(text):
    def test(self):
        self.draft()
        current = self.actor("cr-varied-answer", text)
        request = self.capture_request(current, text)
        self.assertTrue("What time should I remind you to water the plants?"
                        in json.dumps(request["messages"]),
                        "Natural follow-up reached the model without its pending question")
    return test


for index, phrase in enumerate(FOLLOWUP_VARIATIONS, 1):
    setattr(ConversationIntegrationTests, f"test_followup_variation_{index:02d}", _followup_case(phrase))


NEGATIVE_OR_READ_VARIATIONS = (
    "What reminders do I have?",
    "How much did I spend today?",
    "Just explain how you would add a reminder without actually doing it",
    "Hypothetically, how would you log an expense?",
    "Do not delete the reminder",
    "Don't remove milk from the shopping list",
    "I don't want you to add anything",
    "Do I have any leave coming up?",
    "Could you show me the shopping list?",
    "What would happen if I cancelled that reminder?",
)


def _negative_case(text):
    def test(self):
        keyword = asyncio.run(brain._tool_specs_for_names({"query_finances", "list_reminders"}))

        async def malicious(*args, **kwargs):
            return {"tools": ["log_expense", "create_reminder", "remove_shopping_item"],
                    "intent": "CREATE_REMINDER", "confidence": 1.0,
                    "needs_clarification": False, "reason": "bad fixture interpretation"}

        with patch.object(brain.shadow_router, "live_route", new=malicious):
            output = asyncio.run(brain._apply_ai_route({}, keyword, text, None, [], None))
        names = {x["function"]["name"] for x in output}
        self.assertFalse(names & {"log_expense", "create_reminder", "remove_shopping_item"}, names)
    return test


for index, phrase in enumerate(NEGATIVE_OR_READ_VARIATIONS, 1):
    setattr(ConversationIntegrationTests, f"test_no_write_variation_{index:02d}", _negative_case(phrase))


if __name__ == "__main__":
    unittest.main()
