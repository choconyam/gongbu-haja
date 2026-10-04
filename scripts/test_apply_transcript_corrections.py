#!/usr/bin/env python3
"""apply_transcript_corrections.py의 안전 적용 회귀 테스트."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import apply_transcript_corrections as atc


class ApplyTranscriptCorrectionsTests(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        path = root / "segments.json"
        path.write_text(
            json.dumps(
                {
                    "lecture_id": "week1",
                    "segments": [
                        {"id": 1, "start": 0.0, "end": 1.0, "text": "공진 주파수가 십이 헤르츠다"},
                        {"id": 2, "start": 1.0, "end": 2.0, "text": "다음 설명"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_decisions(self, root: Path, source: Path, decisions: list[dict]) -> Path:
        path = root / "decisions.json"
        path.write_text(
            json.dumps(
                {
                    "source_segments_sha256": atc.sha256_file(source),
                    "decisions": decisions,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_applies_only_exact_approved_replacement_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            original = source.read_bytes()
            decisions = self.write_decisions(
                root,
                source,
                [
                    {
                        "segment_id": 1,
                        "original": "공진 주파수가 십이 헤르츠다",
                        "action": "replace",
                        "replacement": "공진 주파수가 12Hz다",
                        "verification": "multiple",
                        "rationale": "음성과 교안 표기가 일치함",
                    },
                    {
                        "segment_id": 2,
                        "original": "다음 설명",
                        "action": "keep",
                        "verification": "audio",
                        "rationale": "원음과 일치함",
                    },
                ],
            )
            result = atc.apply_corrections(source, decisions, root / "out", "week1")
            reviewed = json.loads(Path(result["segments_reviewed"]).read_text(encoding="utf-8"))
            self.assertEqual("공진 주파수가 12Hz다", reviewed["segments"][0]["text"])
            self.assertEqual("다음 설명", reviewed["segments"][1]["text"])
            self.assertEqual(1, reviewed["review"]["replacement_count"])
            self.assertEqual(original, source.read_bytes())
            markdown = Path(result["transcript_reviewed"]).read_text(encoding="utf-8")
            self.assertIn("공진 주파수가 12Hz다\n다음 설명", markdown)
            self.assertNotIn("00:00:00", markdown)
            self.assertNotIn("화자 불명", markdown)

    def test_rejects_stale_hash_and_original_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            stale = root / "stale.json"
            stale.write_text(
                json.dumps({"source_segments_sha256": "0" * 64, "decisions": []}),
                encoding="utf-8",
            )
            with self.assertRaises(atc.CorrectionError):
                atc.apply_corrections(source, stale, root / "out1", "week1")

            mismatch = self.write_decisions(
                root,
                source,
                [{
                    "segment_id": 1,
                    "original": "다른 원문",
                    "action": "keep",
                    "verification": "audio",
                    "rationale": "테스트",
                }],
            )
            with self.assertRaises(atc.CorrectionError):
                atc.apply_corrections(source, mismatch, root / "out2", "week1")

    def test_rejects_duplicate_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            decision = {
                "segment_id": 1,
                "original": "공진 주파수가 십이 헤르츠다",
                "action": "keep",
                "verification": "audio",
                "rationale": "확인",
            }
            decisions = self.write_decisions(root, source, [decision, decision])
            with self.assertRaises(atc.CorrectionError):
                atc.apply_corrections(source, decisions, root / "out", "week1")

    def test_unresolved_is_logged_without_changing_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            decisions = self.write_decisions(
                root,
                source,
                [{
                    "segment_id": 1,
                    "original": "공진 주파수가 십이 헤르츠다",
                    "action": "unresolved",
                    "verification": "unverified",
                    "rationale": "음성이 겹쳐 확정 불가",
                }],
            )
            result = atc.apply_corrections(source, decisions, root / "out", "week1")
            reviewed = json.loads(Path(result["segments_reviewed"]).read_text(encoding="utf-8"))
            self.assertEqual("공진 주파수가 십이 헤르츠다", reviewed["segments"][0]["text"])
            self.assertEqual([1], reviewed["review"]["unresolved_segment_ids"])
            audit = json.loads(Path(result["correction_audit"]).read_text(encoding="utf-8"))
            self.assertEqual("unverified", audit["decisions"][0]["verification"])

    def test_rejects_unverified_or_missing_verification_for_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            original_bytes = source.read_bytes()
            for verification in ("unverified", None):
                decision = {
                    "segment_id": 1,
                    "original": "공진 주파수가 십이 헤르츠다",
                    "action": "replace",
                    "replacement": "근거 없는 교체",
                    "rationale": "확인하지 않음",
                }
                if verification is not None:
                    decision["verification"] = verification
                decisions = self.write_decisions(root, source, [decision])
                output = root / f"out_{verification}"
                with self.assertRaises(atc.CorrectionError):
                    atc.apply_corrections(source, decisions, output, "week1")
                self.assertFalse(output.exists())
                self.assertEqual(original_bytes, source.read_bytes())

    def test_verified_replacement_preserves_each_supported_evidence_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            for verification in ("audio", "handout", "context", "multiple"):
                with self.subTest(verification=verification):
                    decisions = self.write_decisions(root, source, [{
                        "segment_id": 1,
                        "original": "공진 주파수가 십이 헤르츠다",
                        "action": "replace",
                        "replacement": "공진 주파수가 12Hz다",
                        "verification": verification,
                        "rationale": "해당 근거로 표현을 확인함",
                    }])
                    result = atc.apply_corrections(source, decisions, root / verification, "week1")
                    reviewed = json.loads(Path(result["segments_reviewed"]).read_text(encoding="utf-8"))
                    self.assertEqual("공진 주파수가 12Hz다", reviewed["segments"][0]["text"])

    def test_does_not_overwrite_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            decisions = self.write_decisions(root, source, [])
            output = root / "out"
            atc.apply_corrections(source, decisions, output, "week1")
            with self.assertRaises(atc.CorrectionError):
                atc.apply_corrections(source, decisions, output, "week1")


if __name__ == "__main__":
    unittest.main()
