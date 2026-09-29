import importlib.util
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "alex-mcp" / "app"
spec = importlib.util.spec_from_file_location("external_lab", APP / "external_lab.py")
external_lab = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(external_lab)

class ExternalLabTests(unittest.TestCase):
    def test_triage_groups_prompt_and_conversation_failures(self):
        report={
            "mode":"live","status":"FAIL","summary":{"failures":2},
            "prompt_results":[{
                "contract":"p2.home.read","phase":"phase2","domain":"home_assistant",
                "source":"text","prompt":"is the hall ac on","status":"FAIL",
                "problems":["required capability was not actually executed: home.state"],
                "trace":{"calls":[]},"durable_state_diff":{},"elapsed_ms":10
            }],
            "conversation_results":[{
                "contract":"conv.receipt.followup","phase":"phase1","domain":"receipts",
                "source":"text","status":"FAIL","steps":[{
                    "step":3,"prompt":"send that again","status":"FAIL",
                    "problems":["expected original attachment was not queued"],
                    "trace":{"calls":[]},"durable_state_diff":{},"elapsed_ms":11
                }]
            }]
        }
        out=external_lab.triage_report(report)
        self.assertEqual(out["failure_count"],2)
        self.assertEqual(out["problem_buckets"]["routing"],1)
        self.assertEqual(out["problem_buckets"]["attachment"],1)
        self.assertEqual(out["failures_by_domain"]["home_assistant"],1)
        self.assertEqual(out["failures_by_domain"]["receipts"],1)

    def test_contract_args_are_repeatable(self):
        self.assertEqual(
            external_lab.contract_args(["p2.plan.update","conv.plan.refine"]),
            ["--contract","p2.plan.update","--contract","conv.plan.refine"],
        )

if __name__ == "__main__": unittest.main()
