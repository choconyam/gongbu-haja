#!/usr/bin/env python3
"""select_review_packets.py의 결정적 선택·경로 검증 테스트."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import prepare_transcript_review as preparation
from scripts import select_review_packets as selector


class SelectReviewPacketsTests(unittest.TestCase):
    def write_packet(self, path: Path, payload: dict[str, object], byte_count: int) -> None:
        payload = dict(payload)
        payload["_padding"] = ""
        empty = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        padding = byte_count - len(empty)
        if padding < 0:
            raise AssertionError(f"fixture packet is larger than requested: {byte_count} < {len(empty)}")
        payload["_padding"] = "x" * padding
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(byte_count, len(encoded))
        path.write_bytes(encoded)

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
            self.write_packet(
                path,
                {
                    "schema_version": 1,
                    "kind": "transcript_review_packet",
                    "model_input": True,
                    "packet_id": packet_id,
                    "target_segment_ids": target_ids,
                    "candidate_reasons": reasons,
                },
                byte_count,
            )
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

    def rewrite_packet(self, manifest: Path, packet_id: str, mutate) -> None:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        entry = next(item for item in payload["packets"] if item["packet_id"] == packet_id)
        path = manifest.parent / entry["path"]
        packet = json.loads(path.read_text(encoding="utf-8"))
        mutate(packet)
        self.write_packet(path, packet, entry["bytes"])
        manifest.write_text(json.dumps(payload), encoding="utf-8")

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

    def test_unselected_invalid_packet_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.make_fixture(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in payload["packets"] if item["packet_id"] == "packet_0001")
            path = manifest.parent / entry["path"]
            path.write_bytes(b"{" + b"x" * (entry["bytes"] - 2) + b"}")
            result = selector.select_packets(manifest, max_total_bytes=1_300)
            self.assertEqual(["packet_0002", "packet_0003"], [item["packet_id"] for item in result["selected"]])

    def test_selected_packet_rejects_missing_or_false_model_input(self) -> None:
        for mutate in (
            lambda packet: packet.pop("model_input"),
            lambda packet: packet.__setitem__("model_input", False),
        ):
            with self.subTest(mutate=mutate):
                with tempfile.TemporaryDirectory() as temporary:
                    manifest = self.make_fixture(Path(temporary))
                    self.rewrite_packet(manifest, "packet_0002", mutate)
                    with self.assertRaises(selector.SelectionError):
                        selector.select_packets(manifest, reasons=["low_avg_logprob"])

    def test_selected_packet_rejects_schema_kind_identity_and_reasons_mismatch(self) -> None:
        mutations = (
            lambda packet: packet.__setitem__("schema_version", 2),
            lambda packet: packet.__setitem__("kind", "transcript_review_packets"),
            lambda packet: packet.__setitem__("packet_id", "packet_9999"),
            lambda packet: packet.__setitem__("target_segment_ids", ["wrong"]),
            lambda packet: packet.__setitem__("candidate_reasons", ["wrong"]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with tempfile.TemporaryDirectory() as temporary:
                    manifest = self.make_fixture(Path(temporary))
                    self.rewrite_packet(manifest, "packet_0002", mutate)
                    with self.assertRaises(selector.SelectionError):
                        selector.select_packets(manifest, reasons=["low_avg_logprob"])

    def test_selected_packet_rejects_malformed_json_and_same_size_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.make_fixture(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in payload["packets"] if item["packet_id"] == "packet_0002")
            path = manifest.parent / entry["path"]
            path.write_bytes(b"{" + b"x" * (entry["bytes"] - 2) + b"}")
            self.assertEqual(entry["bytes"], path.stat().st_size)
            with self.assertRaises(selector.SelectionError):
                selector.select_packets(manifest, reasons=["low_avg_logprob"])

    def test_generated_packet_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handout = root / "handout.txt"
            handout.write_text("공진 주파수\n공진 주파수\n", encoding="utf-8")
            segments = root / "segments.json"
            segments.write_text(
                json.dumps(
                    {"segments": [{"start": 0, "end": 1, "text": "공진 주파수", "avg_logprob": -2}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = preparation.run(
                preparation.build_parser().parse_args(
                    ["--handout", str(handout), "--segments", str(segments), "--output-dir", str(root / "out")]
                )
            )
            selected = selector.select_packets(Path(result["review_packet_manifest"]))
            self.assertEqual(1, selected["selected_count"])


if __name__ == "__main__":
    unittest.main()
