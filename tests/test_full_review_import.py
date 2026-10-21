"""Verify full review coverage and immutable raw evidence, without answer labels."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_core  # Disposable runtime paths before application imports.
import human_ai_lab
import real_ai_snapshot_review as review


class FullReviewImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.packets = [{"packet_id": "one", "prompt": "Hello"},
                        {"packet_id": "two", "prompt": "Please help"}]

    def source(self, name, ids, **overrides):
        document = {
            "format": "alex-independent-review-v1",
            "corpus_fingerprint": human_ai_lab._corpus_fingerprint(self.packets),
            "reviewer": name, "review_date": "2026-10-21",
            "review_scope": "public_packets_only", "expected_answers_visible": False,
            "decisions": [{"packet_id": pid, "decision": "clarify", "tools": [],
                           "reply_language": "en", "rationale": "Insufficient evidence"}
                          for pid in ids],
            **overrides,
        }
        path = self.directory / (name + ".json")
        path.write_bytes((json.dumps(document, indent=2) + "\n").replace("\n", "\r\n").encode())
        return path

    def snapshot(self):
        paths = [self.source("reviewer-a", ["one"]), self.source("reviewer-b", ["two"])]
        return review.import_full_review(self.packets, paths), paths

    def test_disjoint_shards_preserve_raw_bytes_and_all_answer_fields(self):
        snapshot, paths = self.snapshot()
        before = copy.deepcopy(snapshot)
        decisions = review._decisions_from_snapshot(self.packets, snapshot)
        self.assertEqual(snapshot, before)
        self.assertEqual([row["decision"] for row in decisions[1:]], ["clarify", "clarify"])
        self.assertEqual(snapshot["full_review"]["carried_forward_unchanged"], 0)
        for source, path in zip(snapshot["full_review"]["sources"], paths):
            self.assertEqual(source["raw_json"].encode(), path.read_bytes())
            self.assertEqual(source["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(snapshot["decisions_by_packet_id"]["one"]["rationale"], "Insufficient evidence")

    def test_rejects_missing_duplicate_and_unknown_decisions(self):
        for ids, message in [(["one"], "incomplete"),
                             (["one", "one", "two"], "duplicate"),
                             (["one", "two", "unknown"], "unknown")]:
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, message):
                review.import_full_review(self.packets, [self.source("bad", ids)])

    def test_rejects_cross_shard_overlap(self):
        with self.assertRaisesRegex(ValueError, "duplicate independent review decision"):
            review.import_full_review(self.packets, [
                self.source("a", ["one", "two"]), self.source("b", ["two"]),
            ])

    def test_rejects_unbound_or_nonblind_source(self):
        for changes, message in [({"corpus_fingerprint": "wrong"}, "corpus mismatch"),
                                 ({"expected_answers_visible": True}, "attestation"),
                                 ({"review_scope": "engineering_review"}, "attestation")]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                review.import_full_review(self.packets, [self.source("bad", ["one", "two"], **changes)])

    def test_answer_metadata_and_raw_tampering_are_detected(self):
        original, _ = self.snapshot()
        for mutate in (
            lambda snapshot: snapshot["decisions_by_packet_id"]["one"].update(decision="answer"),
            lambda snapshot: snapshot["decisions_by_packet_id"]["one"].update(rationale="Corrected"),
            lambda snapshot: snapshot["full_review"].update(carried_forward_unchanged=1),
            lambda snapshot: snapshot["full_review"]["sources"][0].update(raw_json="{}"),
        ):
            snapshot = copy.deepcopy(original)
            mutate(snapshot)
            with self.assertRaises(ValueError):
                review._decisions_from_snapshot(self.packets, snapshot)

    def test_packet_changes_invalidate_full_review(self):
        snapshot, _ = self.snapshot()
        self.packets[0]["prompt"] = "Changed evidence"
        with self.assertRaisesRegex(RuntimeError, "corpus mismatch"):
            review._decisions_from_snapshot(self.packets, snapshot)

    def test_duplicate_json_keys_are_rejected(self):
        path = self.source("bad", ["one", "two"])
        text = path.read_text().replace('"decision": "clarify"', '"decision": "answer", "decision": "clarify"')
        path.write_text(text)
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            review.import_full_review(self.packets, [path])


class FullReviewReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_judgment_is_preserved_and_reported_as_full_review(self):
        fixture = FullReviewImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.source("reviewer", ["one", "two"])
        output = fixture.directory / "snapshot-v5.json"
        with patch.object(human_ai_lab, "build_packets", AsyncMock(return_value=fixture.packets)), \
             patch.object(human_ai_lab, "score_packets", return_value={"status": "FAIL"}) as score:
            report = await review.run(output, [source])
            self.assertEqual(report["status"], "FAIL")
            self.assertTrue(report["external_model_review"]["fresh_full_independent_pass"])
            self.assertEqual(report["external_model_review"]["fresh_independent_review_packets"], 2)
            self.assertEqual(score.call_args.args[1][-1]["decision"], "clarify")
            self.assertEqual(json.loads(output.read_text())["decisions_by_packet_id"]["two"]["decision"], "clarify")
            with self.assertRaises(FileExistsError):
                await review.run(output, [source])
