"""validate_agent_setup의 참조 검사를 작은 폴더로 확인한다."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import validate_agent_setup as vas


class ReferenceTests(unittest.TestCase):
    def check(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "AGENTS.md"
            path.write_text(text, encoding="utf-8")
            (Path(temporary) / "exists.md").write_text("# 있음\n", encoding="utf-8")
            report = vas.Report()
            vas.validate_references(path, text, report)
            return [issue.message for issue in report.issues if issue.code == "broken-reference"]

    def test_local_only_devlog_is_not_a_broken_reference(self) -> None:
        # DEVLOG.md는 .gitignore라 CI·다른 사람의 클론에는 없다
        self.assertEqual([], self.check("커밋 전에 `DEVLOG.md`에 남기고 `exists.md`를 본다.\n"))

    def test_other_missing_files_are_still_reported(self) -> None:
        self.assertEqual(["참조 대상이 없습니다: missing.md"],
                         self.check("`DEVLOG.md`, [없는 문서](missing.md)\n"))


if __name__ == "__main__":
    unittest.main()
