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
\begin{keybox}[핵심]
같은 위치의 성분끼리 더합니다. \textbf{행렬의 크기가 같아야 합니다.}
\end{keybox}
\begin{warnbox}
행과 열의 위치를 서로 바꾸지 않습니다.
\end{warnbox}
\begin{goodbox}
왼쪽 아래 성분을 먼저 검산하면 부호 실수를 찾기 쉽습니다.
\end{goodbox}
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
    def test_template_uses_shared_style_cover_and_header(self):
        doc = deep.render_document(BODY, "과목_A", "00", "행렬 & 벡터")
        self.assertIn(BODY, doc)
        self.assertIn('\\input{"' + deep.STYLE.resolve().as_posix() + '"}', doc)
        # 표지는 과목·차시·주제를 나눠 쓰고, 밑줄로 이어 붙인 제목을 만들지 않는다.
        self.assertIn(r"\gongbucover{과목\_A}{00}{행렬 \& 벡터}", doc)
        self.assertIn(r"\gongbuheader{과목\_A}{00}", doc)
        self.assertIn(r"\gongbucover{과목}{00}{}", deep.render_document(BODY, "과목", "00", None))
        for forbidden in (r"\tableofcontents", "STUDY NOTE", r"\setmainfont", r"\linespread", r"\GongbuFontDir"):
            self.assertNotIn(forbidden, doc)
        # 자리표시자 치환은 한 번만: 본문에 같은 글자가 있어도 그대로 둔다.
        self.assertIn("%%COURSE%%", deep.render_document("%%COURSE%%", "과목", "00", None))
        # 찾은 글꼴 폴더는 스타일을 불러오기 전에 정한다. 한글·공백 경로도 그대로 쓴다.
        with_fonts = deep.render_document(BODY, "과목", "00", None, fonts="C:/Users/홍 길동/글꼴/")
        self.assertLess(with_fonts.index(r"\newcommand{\GongbuFontDir}{C:/Users/홍 길동/글꼴/}"),
                        with_fonts.index(r"\input{"))
        for bad in ("C:/글꼴", "C:/a#b/", "C:\\Windows\\Fonts\\"):
            with self.assertRaises(ValueError):
                deep.render_document(BODY, "과목", "00", None, fonts=bad)

    def test_font_dir_checks_override_system_then_user_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            def folder(*parts, fonts=deep.FONT_FILES):
                path = root.joinpath(*parts)
                path.mkdir(parents=True, exist_ok=True)
                for name in fonts:
                    (path / name).write_bytes(b"font")
                return path

            windows = folder("win", fonts=())                         # 글꼴 없는 Windows 폴더
            user = folder("local", "Microsoft", "Windows", "Fonts")
            env = {"WINDIR": str(windows), "LOCALAPPDATA": str(root / "local")}
            self.assertEqual(user.as_posix() + "/", deep.font_dir(env))
            custom = folder("내 글꼴")
            self.assertEqual(custom.as_posix() + "/", deep.font_dir({**env, "GONGBU_FONT_DIR": str(custom)}))
            # 두 파일이 모두 있어야 하고, TeX 특수문자가 든 경로는 쓰지 않는다.
            half = folder("half", fonts=deep.FONT_FILES[:1])
            self.assertIsNone(deep.font_dir({"WINDIR": str(half), "GONGBU_FONT_DIR": str(folder("a#b"))}))

    def test_style_is_the_single_design_source(self):
        style = deep.STYLE.read_text(encoding="utf-8")
        for required in (r"\providecommand{\GongbuSlideScale}{0.74}",       # 승인된 슬라이드 배율
                         r"\fcolorbox{Hairline}{white}",                       # 원본 슬라이드 테두리
                         "원본 PDF p.#3", "원본 PDF p.#2",                     # 두 호출 형식의 캡션
                         r"\newcommand{\gongbucover}", r"\newcommand{\gongbuheader}",
                         r"\newtcolorbox{keybox}", r"\newtcolorbox{warnbox}", r"\newtcolorbox{goodbox}",
                         "NotoSerifKR-VF.ttf", "NotoSansKR-VF.ttf", "AutoFakeBold=2.0",
                         r"\pretocmd{\section}{\Needspace*",
                         r"{\thesection.}",                                    # 절 번호는 1. 2. 형식
                         r"\newcommand{\gongbu@imagepage}[3]",                 # 이미지 교안의 쪽 캡션
                         "GONGBU-FONT-FALLBACK",                               # 대체 글꼴 알림
                         r"\newenvironment{notefigure}", r"그림~\thenotefigure",  # 노트가 그린 그림과 번호 캡션
                         "{FigMain}{HTML}{1F5FA0}", "{FigAlt}{HTML}{B45309}",   # 색각 이상 구분을 통과한 2도 강조색
                         "{FigMain},{FigInk,densely dashed},{FigAlt,dashdotted}",  # 흑백 인쇄용 선 모양
                         "Latex[length=1.9mm,width=1.3mm]",                    # 작은 고전형 화살촉
                         "every node/.append style={text=Ink}",                # 그림 글자는 곡선 색을 쓰지 않는다
                         "guide/.style={/tikz/guide,forget plot}",             # 보조선은 곡선 순서를 차지하지 않는다
                         r"\IfFileExists{pgfplots.sty}",                       # pgfplots 없이도 그림 없는 노트는 빌드
                         r"\providecommand{\GongbuLayout}{standard}",           # 기본 배치
                         "top=15mm,bottom=14mm,left=13mm,right=13mm",          # compact 여백
                         r"\providecommand{\GongbuSlideScale}{0.68}"):          # compact 슬라이드 배율
            self.assertIn(required, style)
        for forbidden in (r"\tableofcontents", "FakeBold=0", "UprightFeatures", "TeX Gyre"):
            self.assertNotIn(forbidden, style)

    def test_layout_is_chosen_before_the_style(self):
        compact = deep.render_document(BODY, "과목", "00", None, layout="compact")
        self.assertLess(compact.index(r"\newcommand{\GongbuLayout}{compact}"), compact.index(r"\input{"))
        self.assertNotIn(r"\GongbuLayout", deep.render_document(BODY, "과목", "00", None))
        with self.assertRaises(ValueError):
            deep.render_document(BODY, "과목", "00", None, layout="tiny")

    def test_layout_option_reaches_the_deep_builder_only(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            source = root / "body.tex"
            source.write_text(BODY, encoding="utf-8")
            common = [str(source), "--course", "과목", "--session", "00", "--layout", "compact"]
            with patch.object(deep, "build_deep") as compile_pdf:
                self.assertEqual(0, builder.main(common + ["--output", str(root / "out.pdf")]))
            self.assertEqual("compact", compile_pdf.call_args.args[-1])
            draft = root / "draft.md"
            draft.write_text("# 제목\n\n본문\n", encoding="utf-8")
            self.assertEqual(2, builder.main([str(draft), "--course", "과목", "--session", "00", "--layout", "compact",
                                              "--output", str(root / "draft.pdf")]))

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
            for expected in ("수학", "행렬의 성분과 덧셈", "같은 위치", "Matrix addition", "왼쪽 아래", "원본 PDF p.1"):
                self.assertIn(expected, text)
            for forbidden in ("목차", "STUDY NOTE", "원문 슬라이드", "a_ij", r"\frac", "수학_00"):
                self.assertNotIn(forbidden, text)

    @unittest.skipUnless(shutil.which("xelatex") and builder.REPORTLAB_ERROR is None,
                         "requires XeLaTeX packages, a Korean font and reportlab")
    def test_slide_forms_share_one_scale(self):
        """세 호출 형식이 모두 지정한 크기에 같은 배율(0.74)을 곱하는지 슬라이드 안 글자 크기로 확인한다."""
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf not installed")
        with tempfile.TemporaryDirectory(prefix="deep forms ") as temporary:
            root = Path(temporary)
            render_fixture(root)                          # slide.pdf: 520pt 폭, 제목 20pt
            body = "\n\n".join([
                r"\newcommand{\sourcepdf}{slide.pdf}",
                r"\section{행렬 덧셈}",
                r"\sourceslide[0.10\textheight]{1}", "높이를 지정한 쪽 형식.",
                r"\sourceslide{slide.pdf}{1}", "파일과 쪽을 함께 쓴 형식.",
                r"\sourceimage[0.5\linewidth]{slide.pdf}{1}", "폭과 쪽을 지정한 그림 형식.",
                r"\sourceimage[0.5\linewidth]{slide.pdf}", "쪽을 모르는 그림 형식.",
            ])
            source = root / "forms.tex"
            source.write_text(body, encoding="utf-8")
            output = root / "forms.pdf"
            self.assertEqual(0, builder.main([str(source), "--note-mode", "deep", "--output", str(output),
                                              "--course", "수학", "--session", "00", "--force"]))
            with pymupdf.open(output) as doc:   # Windows에서 임시 폴더를 지울 수 있게 바로 닫는다
                sizes = [round(span["size"], 1) for page in doc for block in page.get_text("dict")["blocks"]
                         for line in block.get("lines", []) for span in line["spans"]
                         if "Matrix addition" in span["text"]]
                # pymupdf는 한글 사이 공백을 빼고 추출하기도 하므로 공백 없이 비교한다.
                captions = "".join(page.get_text() for page in doc).replace(" ", "")
            text_width = (210 - 38) / 25.4 * 72              # A4 폭 - 좌우 여백 19mm, pt
            text_height = (297 - 19 - 17) / 25.4 * 72       # A4 높이 - 위 19mm·아래 17mm, pt
            by_height = 0.74 * 0.10 * text_height / 170 * 20   # 높이 지정: 높이 × 0.74가 먼저 닿음
            by_default = 0.74 * 0.84 * text_width / 520 * 20   # 생략: 기본 상자 × 0.74 (폭이 먼저 닿음)
            by_width = 0.74 * 0.50 * text_width / 520 * 20     # 0.5\linewidth 폭 지정 × 0.74
            self.assertEqual(4, len(sizes), sizes)
            for size, expected in zip(sizes, (by_height, by_default, by_width, by_width)):
                self.assertAlmostEqual(expected, size, delta=0.3)
            self.assertEqual(3, captions.count("원본PDFp.1"))
            self.assertIn("원본교안", captions)
            self.assertIn("1.행렬덧셈", captions)     # 절 번호는 1. 형식

    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("kpsewhich"), "requires XeLaTeX, pgfplots and a Korean font")
    def test_figure_gallery_compiles_cleanly(self):
        """예시 그림 다섯 가지가 넘침·글꼴 누락·참조 오류 없이 조판되고 그림 번호가 붙는다."""
        if subprocess.run(["kpsewhich", "pgfplots.sty"], capture_output=True).returncode:
            self.skipTest("pgfplots not installed")
        from pypdf import PdfReader
        gallery = deep.STYLE.with_name("deep_figure_gallery.tex")
        with tempfile.TemporaryDirectory(prefix="deep gallery ") as temporary:
            work = Path(temporary)
            command = ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
                       f"-output-directory={work}", gallery.name]
            for _ in range(2):
                result = subprocess.run(command, cwd=gallery.parent, capture_output=True, timeout=120)
                self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace")[-2000:])
            log = (work / "deep_figure_gallery.log").read_text(encoding="utf-8", errors="replace")
            for problem in ("Overfull", "Missing character", "undefined references", "Size substitutions"):
                self.assertNotIn(problem, log)
            text = "".join(page.extract_text() for page in PdfReader(work / "deep_figure_gallery.pdf").pages)
            compact = text.replace(" ", "")
            for number in range(1, 6):
                self.assertIn(f"그림{number}", compact)

    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("kpsewhich"), "requires XeLaTeX, pgfplots and a Korean font")
    def test_figures_wider_than_the_text_stop_the_build_and_compact_saves_pages(self):
        if subprocess.run(["kpsewhich", "pgfplots.sty"], capture_output=True).returncode:
            self.skipTest("pgfplots not installed")
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory(prefix="deep figure ") as temporary, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temporary)
            source = root / "body.tex"
            source.write_text("\\begin{notefigure}{너무 넓은 그림}\n\\begin{tikzpicture}\\draw[curve] (0,0) -- (25,1);"
                              "\\end{tikzpicture}\n\\end{notefigure}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Overfull"):
                deep.build_deep(source, root / "wide.pdf", "과목", "00", None)
            paragraph = "축전기 전압은 시정수마다 남은 차이의 약 37퍼센트만 남기며 최종값에 다가간다. " * 6
            source.write_text("\n\n".join([paragraph] * 40), encoding="utf-8")
            pages = {}
            for layout in deep.LAYOUTS:
                deep.build_deep(source, root / f"{layout}.pdf", "과목", "00", None, layout=layout)
                pages[layout] = len(PdfReader(root / f"{layout}.pdf").pages)
            self.assertLess(pages["compact"], pages["standard"])

    @unittest.skipUnless(shutil.which("xelatex"), "requires XeLaTeX and a Korean fallback font")
    def test_fallback_fonts_are_reported(self):
        with tempfile.TemporaryDirectory(prefix="deep fallback ") as temporary:
            root = Path(temporary).resolve()
            source = root / "body.tex"
            source.write_text("대체 글꼴 확인용 본문입니다.\n", encoding="utf-8")
            notice = io.StringIO()
            # 가변 글꼴이 없는 폴더를 넘기면 스타일이 대체 글꼴을 쓰고 빌더가 알린다.
            with patch.object(deep, "font_dir", return_value=root.as_posix() + "/"), contextlib.redirect_stderr(notice):
                deep.build_deep(source, root / "out.pdf", "과목", "00", None)
            self.assertTrue((root / "out.pdf").is_file())
            self.assertIn("대체 글꼴", notice.getvalue())
            self.assertIn("본문", notice.getvalue())


if __name__ == "__main__":
    unittest.main()
