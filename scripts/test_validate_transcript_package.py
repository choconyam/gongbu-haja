#!/usr/bin/env python3
"""간결형 Markdown과 시간표시 전사의 검증 계약을 확인한다."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
