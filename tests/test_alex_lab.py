import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "alex-mcp", "app"))

import alex_lab  # noqa: E402


class ExternalAlexLabTests(unittest.TestCase):
    def test_live_failures_become_compact_packets(self):
        report = {
            "prompt_results": [{
                "status": "FAIL",
                "contract": "p1.example",
                "phase": "phase1",
                "domain": "memory",
                "source": "text",
                "prompt": "show it",
                "reply": "here",
                "problems": ["wrong attachment"],
                "trace": {"calls": [{
                    "tool": "get_saved_item",
                    "status": "OK",
                    "arguments": {"item_id": "x"},
                    "result": {"status": "found"},
                }]},
                "durable_state_diff": {},
                "latency": {"total_brain_ingress_ms": 10},
            }],
            "conversation_results": [],
        }
        packets = alex_lab._live_failure_packets(report)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["contract"], "p1.example")
        self.assertEqual(packets[0]["tools"][0]["tool"], "get_saved_item")

    def test_live_passes_are_not_packetized(self):
        report = {
            "prompt_results": [{"status": "PASS", "contract": "p1.ok"}],
            "conversation_results": [],
        }
        self.assertEqual(alex_lab._live_failure_packets(report), [])

    def test_target_contract_arguments_are_repeatable(self):
        self.assertEqual(
            alex_lab._contract_args(["p2.plan.update", "conv.plan.refine"]),
            ["--contract", "p2.plan.update", "--contract", "conv.plan.refine"],
        )

    def test_offline_live_required_is_preserved(self):
        report = {
            "failures": [],
            "needs_live": [{
                "contract": "p2.plan.read",
                "phase": "phase2",
                "domain": "plans",
                "source": "text",
                "prompt": "show my plan",
                "variant_kind": "catalog",
                "kind": "discovery-dependent",
                "detail": "needs live",
                "required_capabilities": ["plan.read"],
                "provider_facing_capabilities": ["routing.discovery"],
                "provider_facing_tools": ["discover_alex_tools"],
            }],
        }
        packets = alex_lab._offline_failure_packets(report)
        self.assertEqual(packets[0]["kind"], "offline_live_required")
        self.assertEqual(packets[0]["contract"], "p2.plan.read")


if __name__ == "__main__":
    unittest.main()
