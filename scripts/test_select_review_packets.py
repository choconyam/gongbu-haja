#!/usr/bin/env python3
"""select_review_packets.py의 결정적 선택·경로 검증 테스트."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import select_review_packets as selector


class SelectReviewPacketsTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> Path:
        packet_dir = root / "sample_packets"
        packet_dir.mkdir()
        entries = []
        data = (
            ("packet_0001", ["number_sensitive"], ["n1"], 500),
            ("packet_0002", ["low_avg_logprob", "number_sensitive"], ["n2"], 700),
            ("packet_0003", ["high_no_speech_prob"], ["n3"], 600),
            ("packet_0004", ["assessment_sensitive"], ["n4"], 400),
        )
        for packet_id, reasons, target_ids, byte_count in data:
            path = packet_dir / f"{packet_id}.json"
            path.write_bytes(b"x" * byte_count)
            entries.append(
                {
                    "packet_id": packet_id,
                    "path": f"sample_packets/{packet_id}.json",
                    "target_segment_ids": target_ids,
                    "target_segment_indices": list(range(len(target_ids))),
                    "target_reasons": [reasons],
                    "candidate_reasons": reasons,
                    "target_count": 1,
                    "related_term_count": 0,
                    "bytes": byte_count,
                }
            )
        manifest = root / "review_packet_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "transcript_review_packet_manifest",
                    "model_input": False,
                    "packet_dir": "sample_packets",
                    "packets": entries,
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_risk_priority_and_byte_cap_without_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.make_fixture(Path(temporary))
            result = selector.select_packets(manifest, max_total_bytes=1_300)
            self.assertEqual(["packet_0002", "packet_0003"], [item["packet_id"] for item in result["selected"]])
            self.assertEqual(1_300, result["total_bytes"])

    def test_reason_and_segment_filters_are_intersected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.make_fixture(Path(temporary))
            result = selector.select_packets(manifest, reasons=["number_sensitive"], segment_ids=["n2"])
            self.assertEqual(["packet_0002"], [item["packet_id"] for item in result["selected"]])

    def test_manifest_rejects_aggregate_and_traversal_or_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.make_fixture(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["kind"] = "transcript_review_packets"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(selector.SelectionError):
                selector.load_manifest(manifest)
            payload["kind"] = "transcript_review_packet_manifest"
            payload["packets"][0]["path"] = "../review_packet_manifest.json"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(selector.SelectionError):
                selector.load_manifest(manifest)
            payload["packets"][0]["path"] = "sample_packets/packet_0001.json"
            payload["packets"][0]["bytes"] += 1
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(selector.SelectionError):
                selector.load_manifest(manifest)

    def test_limit_and_output_contains_only_selection_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.make_fixture(Path(temporary))
            result = selector.select_packets(manifest, reasons=["low_avg_logprob"], limit=1)
            self.assertEqual(1, result["selected_count"])
            self.assertEqual(
                {"packet_id", "path", "candidate_reasons", "bytes"},
                set(result["selected"][0]),
            )


if __name__ == "__main__":
    unittest.main()
