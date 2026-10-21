"""A grounded ingress interpretation must not trigger a duplicate router call."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_conversation_reliability import ConversationFixture, brain, db


class RouteReuseTests(ConversationFixture):
    def test_real_trace_carries_detached_pre_action_state_to_shadow(self):
        owner, draft, _ = self.draft()
        current = self.actor("trace-current", "tomorrow around seven in the evening")
        self.capture_request(current, current.trusted_text)
        trace = brain.recent_trace(current.source_message_id)
        self.assertEqual(trace["conversation_state"]["active"]["id"], draft["item_id"])
        trace["conversation_state"]["active"]["id"] = "changed-by-consumer"
        self.assertEqual(
            brain.recent_trace(current.source_message_id)["conversation_state"]["active"]["id"],
            draft["item_id"],
        )
        db.resolve_pending_item(draft["item_id"], owner.user_id, current.source_message_id)
        with patch.object(brain.shadow_router.threading, "Thread") as thread, \
             patch.object(brain.shadow_router, "catalogue", return_value=[]):
            self.assertTrue(brain.shadow_router.submit(
                current, current.trusted_text, None, {"prior_turns": []},
            ))
        thread.return_value.start.assert_called_once()
        job = thread.call_args.kwargs["args"][0]
        self.assertEqual(job["conversation_context"]["active"]["id"], draft["item_id"])
        self.assertEqual(job["conversation_context"]["active"]["status"], "PENDING")

    def test_grounded_reminder_interpretation_skips_second_router(self):
        _, draft, _ = self.draft()
        current = self.actor("reuse-current", "tomorrow around seven in the evening")
        normalized = "Tomorrow at 7:00 PM"
        current = replace(current, reminder_semantic_text=normalized,
                          reminder_context_text="Remind me to water the plants")
        requests = []

        def complete(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="What would you like to change?", tool_calls=[]))], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        route = {"provider": "grok", "model": "offline-fixture", "role": "manual",
                 "reasoning_effort": "low"}
        router = AsyncMock()
        with patch.object(brain, "_provider_routes", return_value=[route]), \
             patch.object(brain, "_client_for", return_value=client), \
             patch.object(brain.shadow_router, "live_enabled", return_value=True), \
             patch.object(brain, "_apply_ai_route", router):
            asyncio.run(brain.respond(current, current.trusted_text,
                                      quoted_context=self.quote(draft),
                                      semantic_user_text=normalized))
        router.assert_not_awaited()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["messages"][-1]["content"], normalized)
