"""Keep fresh review evidence separate from inherited/engineering decisions."""
import copy
import unittest

import test_core  # Initializes only disposable test paths before runtime imports.
import human_ai_lab
import real_ai_snapshot_review


class ReviewProvenanceTests(unittest.TestCase):
    def fixture(self):
        packets = [{"packet_id": "old", "prompt": "Hello"},
                   {"packet_id": "new", "prompt": "Hi"}]
        snapshot = {
            "snapshot_version": 4,
            "corpus_fingerprint": human_ai_lab._corpus_fingerprint(packets),
            "decision_count": 2,
            "parent_review": {"carried_forward_unchanged": 1},
            "delta_review": {"changed_existing": 1, "newly_added": 0,
                             "reviewed_packet_ids": ["new"]},
            "decisions_by_packet_id": {
                "old": {"decision": "answer", "tools": [],
                        "provenance": "parent_snapshot_unchanged",
                        "parent_provenance": "ancestor_engineering_review"},
                "new": {"decision": "clarify", "tools": [],
                        "provenance": "delta_independent_model"},
            },
        }
        return packets, snapshot

    def test_keeps_independent_disagreement_and_parent_provenance(self):
        packets, snapshot = self.fixture()
        before = copy.deepcopy(snapshot)
        result = real_ai_snapshot_review._decisions_from_snapshot(packets, snapshot)
        self.assertEqual(result[-1]["decision"], "clarify")
        self.assertEqual(snapshot, before)

    def test_rejects_swapped_provenance_with_same_counts(self):
        packets, snapshot = self.fixture()
        snapshot["delta_review"]["reviewed_packet_ids"] = ["old"]
        with self.assertRaisesRegex(ValueError, "provenance"):
            real_ai_snapshot_review._decisions_from_snapshot(packets, snapshot)

    def test_requires_original_provenance_for_carried_row(self):
        packets, snapshot = self.fixture()
        del snapshot["decisions_by_packet_id"]["old"]["parent_provenance"]
        with self.assertRaisesRegex(ValueError, "original provenance"):
            real_ai_snapshot_review._decisions_from_snapshot(packets, snapshot)

    def test_packet_changes_invalidate_review(self):
        packets, snapshot = self.fixture()
        packets[-1]["prompt"] = "Different request"
        with self.assertRaisesRegex(RuntimeError, "corpus mismatch"):
            real_ai_snapshot_review._decisions_from_snapshot(packets, snapshot)
