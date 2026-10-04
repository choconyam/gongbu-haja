#!/usr/bin/env python3
"""학습노트 placeholder와 불확실성 표지 검사의 회귀 테스트."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.validate_note_output import Report, check_common_text, validate_markdown, validate_tex


class ValidateNoteOutputTests(unittest.TestCase):
    def issues_for(self, text: str, suffix: str = ".md") -> list[tuple[str, str]]:
        report = Report()
        check_common_text(text, Path(f"note{suffix}"), report, 0)
        return [(issue.severity, issue.code) for issue in report.issues]

    def test_todo_in_markdown_code_is_not_a_placeholder(self) -> None:
        text = "# 실습\n```python\n# TODO: 학생이 구현\npass\n```\n본문 설명"
        self.assertNotIn(("warning", "todo-review"), self.issues_for(text))
        self.assertNotIn(("error", "placeholder"), self.issues_for(text))

    def test_todo_in_prose_requires_agent_judgment_but_is_not_an_error(self) -> None:
        issues = self.issues_for("# 노트\nTODO: 이 설명이 학습에 필요한지 검토")
        self.assertIn(("warning", "todo-review"), issues)
        self.assertNotIn(("error", "placeholder"), issues)

    def test_hard_placeholder_outside_code_is_an_error(self) -> None:
        self.assertIn(("error", "placeholder"), self.issues_for("# 노트\nFIXME 설명 미완성"))

    def test_timestamped_uncertainty_marker_is_counted(self) -> None:
        issues = self.issues_for("# 노트\n[전사 불명확 00:12:30]")
        self.assertIn(("warning", "unresolved-uncertainty"), issues)

    def test_todo_in_tex_code_environment_is_not_a_placeholder(self) -> None:
        text = r"\begin{lstlisting}\n# TODO: 학생이 구현\n\end{lstlisting}"
        self.assertNotIn(("warning", "todo-review"), self.issues_for(text, ".tex"))

    def test_markdown_link_examples_in_code_are_not_local_links(self) -> None:
        text = (
            "# Markdown 실습\n"
            "```markdown\n[예시](missing-example.md)\n```\n"
            "인라인 예시: `[예시](missing-inline.md)`\n"
        )
        report = Report()
        validate_markdown(Path("note.md"), text, report)
        self.assertEqual([], report.errors)

    def test_actual_missing_markdown_link_is_still_an_error(self) -> None:
        report = Report()
        validate_markdown(Path("note.md"), "# 노트\n[자료](missing-material.md)", report)
        self.assertEqual(["broken-link"], [issue.code for issue in report.errors])

    def test_quoted_tex_input_path_is_found(self) -> None:
        # DEEP 직접 제작 문서는 공백 있는 엔진 경로를 \input{"…"}로 감싸 부른다.
        with tempfile.TemporaryDirectory(prefix="엔진 경로 ") as temporary:
            style = Path(temporary).resolve() / "deep_note_style.tex"
            style.write_text("% style", encoding="utf-8")
            text = ("\\documentclass[a4paper,11pt]{article}\n"
                    f'\\input{{"{style.as_posix()}"}}\n'
                    "\\begin{document}\n본문\n\\end{document}\n")
            report = Report()
            validate_tex(Path(temporary) / "note.tex", text, report)
            self.assertEqual([], report.errors)
            report = Report()
            validate_tex(Path(temporary) / "note.tex", text.replace("deep_note_style", "missing_style"), report)
            self.assertEqual(["missing-tex-asset"], [issue.code for issue in report.errors])

    def test_tex_body_fragment_is_not_a_broken_document(self) -> None:
        report = Report()
        validate_tex(Path("body.tex"), "\\section{행렬}\n본문 $a_{ij}$.\n", report)
        self.assertEqual([], report.errors)
        self.assertEqual("fragment", report.metrics["tex_kind"])
        # 구조가 일부만 있으면 깨진 문서다.
        report = Report()
        validate_tex(Path("note.tex"), "\\begin{document}\n본문\n\\end{document}\n", report)
        self.assertEqual(["missing-tex-structure"], [issue.code for issue in report.errors])


if __name__ == "__main__":
    unittest.main()
