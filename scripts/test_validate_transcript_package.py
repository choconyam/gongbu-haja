#!/usr/bin/env python3
"""간결형 Markdown과 시간표시 전사의 검증 계약을 확인한다."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts import validate_transcript_package as vtp


def args_for(path: Path, *, require_timestamps: bool = False) -> Namespace:
    return Namespace(
        transcript=path,
        audio=None,
        manifest=None,
        min_characters=0,
        require_timestamps=require_timestamps,
        strict=False,
        json=False,
    )


class CompactTranscriptValidationTests(unittest.TestCase):
    def test_markdown_without_timestamps_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "lecture.md"
            transcript.write_text("# 전사본\n\n첫 설명\n둘째 설명\n", encoding="utf-8")
            report = vtp.validate(args_for(transcript))
            self.assertFalse(report.errors)
            self.assertFalse(any(issue.code == "missing-timestamps" for issue in report.issues))

    def test_explicit_timestamp_requirement_still_fails_compact_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "lecture.md"
            transcript.write_text("# 전사본\n\n설명\n", encoding="utf-8")
            report = vtp.validate(args_for(transcript, require_timestamps=True))
            self.assertTrue(any(issue.code == "missing-timestamps" for issue in report.errors))

    def test_audio_hash_match_is_accepted_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "lecture.md"
            transcript.write_text("설명\n", encoding="utf-8")
            audio = root / "lecture.wav"
            audio.write_bytes(b"audio")
            digest = hashlib.sha256(audio.read_bytes()).hexdigest().upper()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "source_audio": audio.name,
                        "source_audio_sha256": digest,
                        "transcription_method": "test",
                        "language": "ko",
                        "status": "raw",
                        "reviewed_against_audio": False,
                        "unresolved_spans": [],
                    }
                ),
                encoding="utf-8",
            )
            report = vtp.validate(
                Namespace(
                    transcript=transcript,
                    audio=audio,
                    manifest=manifest,
                    min_characters=0,
                    require_timestamps=False,
                    strict=False,
                    json=False,
                )
            )
            self.assertFalse(any(issue.code in {"invalid-audio-hash", "audio-hash-mismatch"} for issue in report.issues))

    def test_audio_hash_is_required_and_mismatch_fails_even_with_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "lecture.md"
            transcript.write_text("설명\n", encoding="utf-8")
            audio = root / "lecture.wav"
            audio.write_bytes(b"actual")
            base = {
                "source_audio": audio.name,
                "transcription_method": "test",
                "language": "ko",
                "status": "raw",
                "reviewed_against_audio": False,
                "unresolved_spans": [],
            }

            def validate_manifest(payload: dict) -> set[str]:
                manifest = root / "manifest.json"
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                report = vtp.validate(
                    Namespace(
                        transcript=transcript,
                        audio=audio,
                        manifest=manifest,
                        min_characters=0,
                        require_timestamps=False,
                        strict=False,
                        json=False,
                    )
                )
                return {issue.code for issue in report.errors}

            self.assertIn("invalid-audio-hash", validate_manifest(base))
            mismatch = {**base, "source_audio_sha256": "0" * 64}
            self.assertIn("audio-hash-mismatch", validate_manifest(mismatch))

    def test_transcript_only_validation_does_not_require_audio_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "lecture.md"
            transcript.write_text("설명\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "source_audio": None,
                        "transcription_method": "provided_transcript",
                        "language": "ko",
                        "status": "transcript_only",
                        "reviewed_against_audio": False,
                        "unresolved_spans": [],
                    }
                ),
                encoding="utf-8",
            )
            report = vtp.validate(
                Namespace(
                    transcript=transcript,
                    audio=None,
                    manifest=manifest,
                    min_characters=0,
                    require_timestamps=False,
                    strict=False,
                    json=False,
                )
            )
            self.assertFalse(report.errors)


if __name__ == "__main__":
    unittest.main()
