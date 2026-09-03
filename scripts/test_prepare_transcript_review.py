#!/usr/bin/env python3
"""prepare_transcript_review.py의 표준 라이브러리 회귀 테스트."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import prepare_transcript_review as ptr


class PrepareTranscriptReviewTests(unittest.TestCase):
    def write(self, root: Path, name: str, value: str) -> Path:
        path = root / name
        path.write_text(value, encoding="utf-8")
        return path

    def test_repeated_korean_term_has_exact_line_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handout = self.write(root, "handout.txt", "공진 주파수는 중요하다.\n다시 공진 주파수를 계산한다.\n")
            lines = ptr.extract_handout_lines(handout)
            candidates = ptr.extract_term_candidates(lines)
            candidate = next(item for item in candidates if item["normalized_term"] == "공진 주파수")
            self.assertIn("repeated", candidate["reasons"])
            self.assertEqual([1, 2], [item["line"] for item in candidate["evidence"]])
            self.assertLessEqual(len(candidate["evidence"]), 3)
            source_id = candidate["evidence"][0]["source_id"]
            self.assertEqual(ptr.source_id_for_hash(ptr.sha256_file(handout)), source_id)
            self.assertNotIn("source", candidate["evidence"][0])
            self.assertNotIn("source_sha256", candidate["evidence"][0])

    def test_pdf_uses_local_extractor_and_fails_image_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = self.write(root, "slides.pdf", "not really a pdf")
            with patch.object(ptr, "_extract_pdf_pypdf", return_value=["공진 주파수\n공진 주파수", "공진 주파수"]):
                lines = ptr.extract_handout_lines(pdf)
            self.assertEqual((1, 1, "공진 주파수"), (lines[0].page, lines[0].line, lines[0].text))
            with patch.object(ptr, "_extract_pdf_pypdf", return_value=["공진 주파수\n공진 주파수", "공진 주파수"]):
                result = ptr.run(
                    ptr.build_parser().parse_args(
                        ["--handout", str(pdf), "--output-dir", str(root / "out")]
                    )
                )
            payload = json.loads(Path(result["term_candidates"]).read_text(encoding="utf-8"))
            candidate = next(item for item in payload["candidates"] if item["normalized_term"] == "공진 주파수")
            source_id = candidate["evidence"][0]["source_id"]
            self.assertEqual(str(pdf.resolve()), payload["sources"][source_id]["path"])
            self.assertEqual([(1, 1), (1, 2)], [(item["page"], item["line"]) for item in candidate["evidence"]])
            with (
                patch.object(ptr, "_extract_pdf_pypdf", return_value=None),
                patch.object(ptr, "_extract_pdf_pdftotext", return_value=None),
            ):
                with self.assertRaises(ptr.PreparationError) as raised:
                    ptr.extract_handout_lines(pdf)
            self.assertIn("이미지형 PDF", str(raised.exception))

    def test_review_flags_confidence_duplicate_assessment_and_number(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handout = self.write(root, "handout.md", "시험 범위는 12 Hz이다.\n공진 주파수\n")
            segments = self.write(
                root,
                "segments.json",
                json.dumps(
                    {
                        "segments": [
                            {"id": 1, "start": 0, "end": 0.1, "text": "시험 범위는 12 Hz", "avg_logprob": -1.5},
                            {"id": 2, "start": 0.1, "end": 0.2, "text": "시험 범위는 12 Hz", "no_speech_prob": 0.8, "compression_ratio": 3.0},
                            {"id": 3, "start": 0.2, "end": 0.3, "text": "짧은 구간"},
                        ]
                    },
                    ensure_ascii=False,
                ),
            )
            source_hash, loaded = ptr.load_segments(segments)
            self.assertEqual(ptr.sha256_file(segments), source_hash)
            packets, _samples, counts = ptr.build_review_packets(loaded, ptr.extract_handout_lines(handout), char_cap=100)
            reasons = set(reason for packet in packets for reason in packet["candidate_reasons"])
            self.assertTrue({"low_avg_logprob", "consecutive_duplicate", "assessment_sensitive", "number_sensitive"} <= reasons)
            self.assertIn("high_no_speech_prob", reasons)
            self.assertIn("high_compression_ratio", reasons)
            self.assertGreaterEqual(counts["timing_density"], 1)

    def test_samples_are_start_middle_end_and_context_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handout = self.write(root, "handout.txt", "공진 주파수 설명 " + "긴 문장 " * 100)
            segments = [
                ptr.Segment(i, i, float(i), float(i) + 0.1, "공진 주파수 " + ("x" * 100), None, None, None, {})
                for i in range(5)
            ]
            packets, samples, _counts = ptr.build_review_packets(segments, ptr.extract_handout_lines(handout), char_cap=24)
            self.assertEqual(0, samples["start"][0]["index"])
            self.assertEqual(2, samples["middle"][0]["index"])
            self.assertEqual(4, samples["end"][0]["index"])
            for packet in packets:
                self.assertTrue(packet["target_segments"])
                self.assertLessEqual(len(packet["context_segments"]), 2)
                self.assertTrue(all(len(item["excerpt"]) <= 24 for item in packet["handout_excerpts"]))
                context_chars = sum(len(item["text"]) for item in packet["target_segments"])
                context_chars += sum(len(item["text"]) for item in packet["context_segments"])
                context_chars += sum(len(item["excerpt"]) for item in packet["handout_excerpts"])
                self.assertLessEqual(context_chars, 24)

    def test_adjacent_flags_are_grouped_and_ids_reasons_are_preserved(self) -> None:
        segments = [
            ptr.Segment(index, index, float(index), float(index) + 1, f"시험 항목 {index}", -2.0, None, None, {})
            for index in range(5)
        ]
        packets, _samples, counts = ptr.build_review_packets(
            segments, [], char_cap=100, max_targets_per_packet=2
        )
        self.assertEqual(3, len(packets))
        target_ids = [item for packet in packets for item in packet["target_segment_ids"]]
        self.assertEqual([0, 1, 2, 3, 4], target_ids)
        self.assertTrue(all("low_avg_logprob" in packet["candidate_reasons"] for packet in packets))
        self.assertEqual(5, counts["low_avg_logprob"])

    def test_cli_writes_candidates_without_modifying_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handout = self.write(root, "handout.txt", "공진 주파수\n공진 주파수\n")
            segments = self.write(root, "segments.json", '{"segments": [{"start": 0, "end": 1, "text": "공진 주파수", "avg_logprob": -2}]}')
            original = segments.read_bytes()
            output = root / "out"
            result = ptr.run(
                ptr.build_parser().parse_args(
                    ["--handout", str(handout), "--segments", str(segments), "--output-dir", str(output), "--prefix", "week1"]
                )
            )
            self.assertTrue(Path(result["term_candidates"]).is_file())
            self.assertTrue(Path(result["review_packets"]).is_file())
            self.assertEqual(original, segments.read_bytes())
            term_payload = json.loads(Path(result["term_candidates"]).read_text(encoding="utf-8"))
            self.assertEqual("candidates_only", term_payload["semantic_status"])
            self.assertFalse(term_payload["model_input"])
            self.assertFalse(term_payload["replacement_applied"])
            source_id = term_payload["candidates"][0]["evidence"][0]["source_id"]
            self.assertEqual(str(handout.resolve()), term_payload["sources"][source_id]["path"])
            self.assertEqual(ptr.sha256_file(handout), term_payload["sources"][source_id]["sha256"])
            packet_payload = json.loads(Path(result["review_packets"]).read_text(encoding="utf-8"))
            self.assertFalse(packet_payload["model_input"])
            self.assertNotIn("packets", packet_payload)
            manifest_path = Path(result["review_packet_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["model_input"])
            self.assertEqual(str(handout.resolve()), manifest["sources"][source_id]["path"])
            self.assertEqual(1, len(manifest["packets"]))
            packet_path = manifest_path.parent / manifest["packets"][0]["path"]
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            excerpt = packet["handout_excerpts"][0]
            self.assertEqual(source_id, excerpt["source_id"])
            self.assertNotIn("source_sha256", excerpt)
            self.assertTrue(packet["related_term_candidates"])

    def test_individual_packet_index_preserves_flagged_ids_and_local_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handout = self.write(root, "handout.txt", "공진 주파수 시험\n공진 주파수 시험\n")
            segments = self.write(
                root,
                "segments.json",
                json.dumps({"segments": [
                    {"id": "a", "start": 0, "end": 1, "text": "공진 주파수", "avg_logprob": -2},
                    {"id": "b", "start": 1, "end": 2, "text": "공진 주파수", "avg_logprob": -2},
                    {"id": "c", "start": 2, "end": 3, "text": "시험", "avg_logprob": -2},
                ]}, ensure_ascii=False),
            )
            output = root / "out"
            result = ptr.run(ptr.build_parser().parse_args([
                "--handout", str(handout), "--segments", str(segments), "--output-dir", str(output),
            ]))
            manifest = json.loads(Path(result["review_packet_manifest"]).read_text(encoding="utf-8"))
            indexed_ids = [item for entry in manifest["packets"] for item in entry["target_segment_ids"]]
            self.assertEqual(["a", "b", "c"], indexed_ids)
            indexed_reasons = [reason for entry in manifest["packets"] for reasons in entry["target_reasons"] for reason in reasons]
            self.assertEqual(3, indexed_reasons.count("low_avg_logprob"))
            for entry in manifest["packets"]:
                packet = json.loads((Path(result["review_packet_manifest"]).parent / entry["path"]).read_text(encoding="utf-8"))
                self.assertLessEqual(len(packet["related_term_candidates"]), ptr.MAX_RELATED_TERMS)
                self.assertEqual(entry["target_segment_ids"], packet["target_segment_ids"])


if __name__ == "__main__":
    unittest.main()
