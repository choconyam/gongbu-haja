#!/usr/bin/env python3
"""학습노트 placeholder와 불확실성 표지 검사의 회귀 테스트."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.validate_note_output import Report, check_common_text


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


if __name__ == "__main__":
    unittest.main()
