#!/usr/bin/env python3
"""build_study_note_pdf.py가 내용을 바꾸지 않고 결정적으로 조판하는지 검증한다."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_study_note_pdf as builder  # noqa: E402

try:  # reportlab·한글 글꼴이 없는 CI에서는 렌더 테스트만 건너뛴다.
    FONTS = None if builder.REPORTLAB_ERROR is not None else builder.resolve_fonts(None, None)
except FileNotFoundError:
    FONTS = None

SAMPLE = """# 과목 1주차 학습노트

<!-- units: handout-p01 -->
첫 문단은 표지 요약처럼 쓰인다.

## 1. 개념
<!-- units: handout-p02, transcript-t01 -->

- **정의**: 미디어는 `매개체`다.
- 키워드: 전달 · 해석

교수 설명: 원문에 있는 설명만 옮긴다 [확인 필요].

| 항목 | 값 |
|---|---|
| A | 1 |
| B | 2 |

> 주의: 이 상자는 그대로 렌더된다.

---

## 후속 역할 인계 메모

이 부분은 학생용 PDF에 들어가면 안 된다.
"""


class PublicTextTests(unittest.TestCase):
    def test_strips_tracking_comments_and_handoff_memo_only(self) -> None:
        text = builder.public_text(SAMPLE)
        self.assertNotIn("<!--", text)
        self.assertNotIn("후속 역할 인계 메모", text)
        self.assertNotIn("학생용 PDF에 들어가면", text)
        # 본문 문장·표·표지는 그대로 남는다.
        self.assertIn("교수 설명: 원문에 있는 설명만 옮긴다 [확인 필요].", text)
        self.assertIn("| A | 1 |", text)
        self.assertIn("> 주의: 이 상자는 그대로 렌더된다.", text)

    def test_markdown_output_needs_no_reportlab_and_strips_only_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "note_draft.md"
            source.write_text(SAMPLE, encoding="utf-8")
            output = Path(temporary) / "out" / "note.md"
            code = builder.main([str(source), "--output", str(output), "--course", "과목", "--session", "1주차 1차시"])
            self.assertEqual(0, code)
            text = output.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# 과목 1주차 학습노트"))
            self.assertNotIn("<!--", text)
            self.assertNotIn("인계 메모", text)
            self.assertIn("| A | 1 |", text)
            # 기존 파일은 --force 없이는 덮어쓰지 않는다.
            self.assertEqual(2, builder.main([str(source), "--output", str(output), "--course", "과목", "--session", "1주차 1차시"]))
            headless = Path(temporary) / "headless.md"
            headless.write_text("본문만 있음\n", encoding="utf-8")
            builder.main([str(headless), "--output", str(Path(temporary) / "h.md"), "--course", "과목", "--session", "2차시"])
            self.assertTrue((Path(temporary) / "h.md").read_text(encoding="utf-8").startswith("# 과목 2차시 학습노트"))

    def test_inline_markup_keeps_code_and_bold(self) -> None:
        rendered = builder.inline_markup("**정의**: 미디어는 `매개체`다 — 끝")
        self.assertIn("<b>정의</b>", rendered)
        self.assertIn("매개체", rendered)
        self.assertNotIn("—", rendered)


@unittest.skipUnless(FONTS, "reportlab 또는 한글 TrueType 글꼴이 없는 환경")
class RenderTests(unittest.TestCase):
    def test_builds_pdf_with_cover_toc_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "note_draft.md"
            source.write_text(SAMPLE, encoding="utf-8")
            output = Path(temporary) / "out" / "note.pdf"
            code = builder.main(
                [str(source), "--output", str(output), "--course", "과목", "--session", "1주차 1차시", "--summary", "요약", "--meta", "메타 한 줄"]
            )
            self.assertEqual(0, code)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 5_000)
            try:
                from pypdf import PdfReader
            except ImportError:
                return
            reader = PdfReader(str(output))
            self.assertGreaterEqual(len(reader.pages), 3)
            body = "".join(page.extract_text() for page in reader.pages)
            self.assertIn("개념", body)
            self.assertNotIn("인계 메모", body)
            # 덮어쓰기는 --force 없이는 거부한다.
            self.assertEqual(2, builder.main([str(source), "--output", str(output), "--course", "과목", "--session", "1주차 1차시"]))


if __name__ == "__main__":
    unittest.main()
