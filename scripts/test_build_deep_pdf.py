"""DEEP routing, content preservation, compile failure and optional real-render tests."""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_deep_pdf as deep
import build_study_note_pdf as builder

BODY = r"""\sourceslide[140mm]{slide.pdf}{1}
\textbf{같은 위치의 성분끼리 더하기}

행렬은 수를 줄과 칸에 맞춰 놓은 것입니다. $a_{ij}$에서 $i$는 행, $j$는 열을 뜻합니다.
두 행렬의 크기가 같으면 같은 위치의 수끼리 더합니다.
\[
\begin{aligned}
A+B&=\begin{bmatrix}1&2\\3&4\end{bmatrix}
      +\begin{bmatrix}2&0\\-1&3\end{bmatrix}\\
   &=\begin{bmatrix}1+2&2+0\\3-1&4+3\end{bmatrix}
    =\begin{bmatrix}3&2\\2&7\end{bmatrix}.
\end{aligned}
\]
예를 들어 왼쪽 아래 칸은 $3+(-1)=2$가 됩니다.
분수 $\frac{1}{2}$와 제곱근 $\sqrt{2}$, 위첨자 $x^2$도 수식으로 표시합니다.
"""


def render_fixture(directory: Path) -> Path:
    """Synthetic assets only; can also be called for local visual QA."""
    from reportlab.pdfgen.canvas import Canvas
    directory.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(directory / "slide.pdf"), pagesize=(520, 170))
    canvas.setFont("Helvetica", 20)
    canvas.drawString(30, 125, "Matrix addition")
    canvas.setFont("Helvetica", 14)
    canvas.drawString(30, 82, "Add entries in the same row and column.")
    canvas.drawString(30, 45, "Both matrices must have the same dimensions.")
    canvas.save()
    source = directory / "body.tex"
    source.write_text(BODY, encoding="utf-8")
    output = directory / "deep.pdf"
    result = builder.main([str(source), "--note-mode", "deep", "--output", str(output),
                           "--course", "수학", "--session", "00", "--summary", "행렬의 성분과 덧셈", "--force"])
    if result:
        raise AssertionError(f"fixture build returned {result}")
    return output


class DeepTests(unittest.TestCase):
    def test_template_keeps_math_and_has_no_automatic_furniture(self):
        doc = deep.render_document(BODY, "과목", "00", "행렬 & 벡터")
        self.assertIn(BODY, doc)
        self.assertIn(r"과목\_00", doc)
        self.assertIn(r"행렬 \& 벡터", doc)
        self.assertIn(r"\pagestyle{empty}", doc)
        for forbidden in (r"\tableofcontents", r"\fancyhead", r"\fancyfoot", r"\fcolorbox", "STUDY NOTE"):
            self.assertNotIn(forbidden, doc)
        self.assertIn("%%TITLE%%", deep.render_document("%%TITLE%%", "과목", "00", None))

    def test_rejects_empty_or_full_document(self):
        for body in (" ", r"\documentclass{article}", r"\begin{document}body"):
            with self.assertRaises(ValueError):
                deep.render_document(body, "과목", "00", None)

    def test_font_override(self):
        self.assertIn(r"\setmainhangulfont{Noto Serif CJK KR}",
                      deep.render_document(BODY, "과목", "00", None, "Noto Serif CJK KR"))
        with self.assertRaises(ValueError):
            deep.render_document(BODY, "과목", "00", None, r"bad}\input{x}")

    def test_deep_markdown_pdf_is_rejected_but_explicit_markdown_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            source = root / "draft.md"
            source.write_text("# 제목\n\n$a_{ij}$\n", encoding="utf-8")
            common = [str(source), "--note-mode", "deep", "--course", "과목", "--session", "00"]
            self.assertEqual(2, builder.main(common + ["--output", str(root / "out.pdf")]))
            self.assertFalse((root / "out.pdf").exists())
            self.assertEqual(2, builder.main(common + ["--format", "md", "--output", str(root / "out.pdf")]))
            self.assertEqual(0, builder.main(common + ["--output", str(root / "out.md")]))
            self.assertEqual(source.read_text(encoding="utf-8"), (root / "out.md").read_text(encoding="utf-8"))
            self.assertEqual(2, builder.main(common + ["--output", str(source), "--force"]))

    def test_tex_routing_and_legacy_options(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            source = root / "body.tex"
            source.write_text(BODY, encoding="utf-8")
            common = [str(source), "--course", "과목", "--session", "00"]
            for args in (["--output", str(root / "out.md")],
                         ["--output", str(root / "out.pdf"), "--meta", "불필요한 메타"]):
                self.assertEqual(2, builder.main(common + args))
            with patch.object(deep, "build_deep") as compile_pdf:
                self.assertEqual(0, builder.main(common + ["--output", str(root / "out.pdf")]))
                compile_pdf.assert_called_once()

    def test_missing_engine_does_not_replace_output(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deep.shutil, "which", return_value=None):
            root = Path(temporary)
            output = root / "out.pdf"
            output.write_bytes(b"old PDF")
            with self.assertRaisesRegex(ValueError, "XeLaTeX"):
                deep.build_deep(root / "body.tex", output, "과목", "00", None)
            self.assertEqual(b"old PDF", output.read_bytes())

    def test_compile_errors_overflow_and_missing_glyph_preserve_output(self):
        for error, returncode in (("! Bad TeX", 1), ("Overfull \\hbox (20pt)", 0),
                                  ("Missing character: There is no", 0)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "body.tex"
                source.write_text(BODY, encoding="utf-8")
                output = root / "out.pdf"
                output.write_bytes(b"old PDF")

                def fake_run(command, **kwargs):
                    if "--version" in command:
                        return subprocess.CompletedProcess(command, 0, b"MiKTeX", b"")
                    self.assertIn("-no-shell-escape", command)
                    self.assertIn("-disable-installer", command)
                    self.assertEqual(source.parent, kwargs["cwd"])
                    work = Path(command[-1]).parent
                    (work / "note.log").write_text(error, encoding="utf-8")
                    (work / "note.pdf").write_bytes(b"%PDF-test")
                    return subprocess.CompletedProcess(command, returncode, b"", b"")

                with patch.object(deep.shutil, "which", return_value="xelatex"), patch.object(deep.subprocess, "run", side_effect=fake_run):
                    with self.assertRaises(ValueError):
                        deep.build_deep(source, output, "과목", "00", None)
                self.assertEqual(b"old PDF", output.read_bytes())

    @unittest.skipUnless(shutil.which("xelatex") and builder.REPORTLAB_ERROR is None,
                         "requires XeLaTeX packages, a Korean font and reportlab")
    def test_actual_pdf(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf not installed")
        with tempfile.TemporaryDirectory(prefix="deep 한글 space ") as temporary:
            output = render_fixture(Path(temporary))
            reader = PdfReader(output)
            self.assertEqual(2, len(reader.pages))
            text = "".join(page.extract_text() for page in reader.pages)
            for expected in ("수학_00", "같은 위치", "Matrix addition", "왼쪽 아래"):
                self.assertIn(expected, text)
            for forbidden in ("목차", "STUDY NOTE", "원문 슬라이드", "a_ij", r"\frac"):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
