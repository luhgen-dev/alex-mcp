"""A delta review can reuse only independently reviewed, unchanged public input."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_core  # Configure disposable household paths before runtime imports.
import human_ai_lab
import real_ai_snapshot_review as review


class VerifiedDeltaReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.previous = [
            {"packet_id": "same", "prompt": "Public unchanged input", "available_tools": [], "flag": False},
            {"packet_id": "changed", "prompt": "Public old input", "available_tools": []},
            {"packet_id": "removed", "prompt": "Public removed input", "available_tools": []},
        ]
        self.current = [copy.deepcopy(self.previous[0]),
                        {"packet_id": "changed", "prompt": "Public new input", "available_tools": []},
                        {"packet_id": "added", "prompt": "Public added input", "available_tools": []}]
        parent_source = self.source("parent", ["same", "changed", "removed"], self.previous)
        self.parent = review.import_full_review(self.previous, [parent_source])
        self.parent_path = self.directory / "parent-v5.json"
        self.parent_path.write_text(json.dumps(self.parent, ensure_ascii=False, indent=2) + "\n")
        self.bundle_path = self.directory / "parent-public.json"
        self.bundle_path.write_text(json.dumps(human_ai_lab.public_review_bundle(self.previous)) + "\n")
        self.delta_path = self.source("delta", ["changed", "added"], self.current)

    def source(self, name, ids, packets, **overrides):
        value = {
            "format": "alex-independent-review-v1",
            "corpus_fingerprint": human_ai_lab._corpus_fingerprint(packets),
            "reviewer": name, "review_date": "2026-10-21",
            "review_scope": "public_packets_only", "expected_answers_visible": False,
            "decisions": [{"packet_id": pid, "decision": "clarify", "tools": [],
                           "reply_language": "en", "rationale": "Original judgment — not reconciled",
                           "extra_review_field": {"uncertain": True}}
                          for pid in ids], **overrides,
        }
        path = self.directory / (name + ".json")
        path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode())
        return path

    def snapshot(self, packets=None, sources=None):
        return review.import_delta_review(
            packets or self.current, sources or [self.delta_path], self.parent_path, self.bundle_path,
        )

    def test_exact_parent_decisions_and_raw_sources_are_preserved(self):
        snapshot = self.snapshot()
        original = copy.deepcopy(snapshot)
        decisions = review._decisions_from_snapshot(self.current, snapshot)
        self.assertEqual(snapshot, original)
        self.assertEqual([row["decision"] for row in decisions[1:]], ["clarify"] * 3)
        self.assertEqual(snapshot["decisions_by_packet_id"]["same"], self.parent["decisions_by_packet_id"]["same"])
        self.assertEqual(snapshot["parent_review"]["carried_packet_ids"], ["same"])
        self.assertEqual(snapshot["parent_review"]["removed_packet_ids"], ["removed"])
        self.assertEqual(snapshot["delta_review"]["changed_existing"], 1)
        self.assertEqual(snapshot["delta_review"]["newly_added"], 1)
        self.assertEqual(set(snapshot["decisions_by_packet_id"]), {"same", "changed", "added"})
        for name, path in (("snapshot", self.parent_path), ("public_bundle", self.bundle_path)):
            evidence = snapshot["parent_review"]["evidence"][name]
            self.assertEqual(evidence["raw_json"].encode(), path.read_bytes())
            self.assertEqual(evidence["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(snapshot["delta_review"]["sources"][0]["raw_json"].encode(), self.delta_path.read_bytes())
        self.assertEqual(snapshot["decisions_by_packet_id"]["added"]["extra_review_field"], {"uncertain": True})

    def test_delta_coverage_rejects_missing_duplicate_unknown_and_unchanged_rows(self):
        for ids, message in [(["changed"], "incomplete"),
                             (["changed", "changed", "added"], "duplicate"),
                             (["changed", "added", "unknown"], "unknown"),
                             (["changed", "added", "removed"], "unknown"),
                             (["changed", "added", "same"], "unknown")]:
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, message):
                self.snapshot(sources=[self.source("bad", ids, self.current)])

    def test_delta_sources_are_bound_to_current_corpus_and_public_only_attestation(self):
        for changes, message in [({"corpus_fingerprint": self.parent["corpus_fingerprint"]}, "corpus mismatch"),
                                 ({"expected_answers_visible": True}, "attestation")]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.snapshot(sources=[self.source("bad", ["changed", "added"], self.current, **changes)])

    def test_parent_public_drift_and_parent_decision_edits_are_rejected(self):
        bundle = json.loads(self.bundle_path.read_text())
        bundle["packets"][0]["prompt"] = "Changed parent public evidence"
        self.bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            self.snapshot()
        self.bundle_path.write_text(json.dumps(human_ai_lab.public_review_bundle(self.previous)))
        self.parent["decisions_by_packet_id"]["same"]["decision"] = "answer"
        self.parent_path.write_text(json.dumps(self.parent))
        with self.assertRaisesRegex(ValueError, "differs from raw"):
            self.snapshot()

    def test_carried_answers_provenance_metadata_and_raw_tampering_are_rejected(self):
        original = self.snapshot()
        for mutate in (
            lambda s: s["decisions_by_packet_id"]["same"].update(decision="answer"),
            lambda s: s["decisions_by_packet_id"]["same"].update(rationale="Corrected answer"),
            lambda s: s["decisions_by_packet_id"]["same"].update(review_source_id="invented"),
            lambda s: s["decisions_by_packet_id"]["changed"].update(provenance="parent_snapshot_unchanged"),
            lambda s: s["parent_review"].update(carried_forward_unchanged=2),
            lambda s: s["parent_review"]["evidence"]["snapshot"].update(raw_json="{}"),
            lambda s: s["delta_review"]["sources"][0].update(raw_json="{}"),
            lambda s: s.update(method="Claimed fresh review of every packet"),
        ):
            with self.subTest(mutate=mutate):
                snapshot = copy.deepcopy(original)
                mutate(snapshot)
                with self.assertRaises(ValueError):
                    review._decisions_from_snapshot(self.current, snapshot)

    def test_even_rehashed_raw_delta_edits_cannot_silently_change_stored_answers(self):
        snapshot = self.snapshot()
        source = snapshot["delta_review"]["sources"][0]
        raw = json.loads(source["raw_json"])
        raw["decisions"][0]["decision"] = "answer"
        source["raw_json"] = json.dumps(raw)
        source["sha256"] = source["source_id"] = hashlib.sha256(source["raw_json"].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "differs from verified"):
            review._decisions_from_snapshot(self.current, snapshot)

    def test_current_drift_invalidates_snapshot_and_requires_additional_delta_coverage(self):
        snapshot = self.snapshot()
        changed = copy.deepcopy(self.current)
        changed[0]["flag"] = 0  # JSON false and zero are distinct public evidence.
        with self.assertRaisesRegex(RuntimeError, "corpus mismatch"):
            review._decisions_from_snapshot(changed, snapshot)
        source = self.source("new-current", ["changed", "added"], changed)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.snapshot(packets=changed, sources=[source])

    def test_parent_must_be_v5_and_empty_delta_is_not_a_new_review(self):
        with self.assertRaisesRegex(ValueError, "requires changed or added"):
            self.snapshot(packets=self.previous)
        self.parent["snapshot_version"] = 4
        self.parent_path.write_text(json.dumps(self.parent))
        with self.assertRaisesRegex(ValueError, "verified v5 parent"):
            self.snapshot()


class VerifiedDeltaReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_delta_is_preserved_and_report_never_claims_a_fresh_full_pass(self):
        fixture = VerifiedDeltaReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        output = fixture.directory / "snapshot-v6.json"
        with patch.object(human_ai_lab, "build_packets", AsyncMock(return_value=fixture.current)), \
             patch.object(human_ai_lab, "score_packets", return_value={"status": "FAIL"}):
            result = await review.run(output, [fixture.delta_path], fixture.parent_path, fixture.bundle_path)
            self.assertEqual(result["status"], "FAIL")
            provenance = result["external_model_review"]
            self.assertFalse(provenance["fresh_full_independent_pass"])
            self.assertEqual(provenance["parent_snapshot_unchanged_packets"], 1)
            self.assertEqual(provenance["fresh_independent_delta_packets"], 2)
            self.assertEqual(provenance["removed_parent_packets"], 1)
            self.assertEqual(json.loads(output.read_text())["decisions_by_packet_id"]["same"]["decision"], "clarify")
            with self.assertRaises(FileExistsError):
                await review.run(output, [fixture.delta_path], fixture.parent_path, fixture.bundle_path)
