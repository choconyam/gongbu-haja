"""심화 이해형 TeX 원고 → 노션 블록 변환을 작은 원고로 확인한다."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import tex_to_notion as tn

DOCUMENT = r"""\documentclass[a4paper,11pt]{article}
\input{"C:/engine/scripts/deep_note_style.tex"}
\newcommand{\sourcepdf}{\detokenize{slides.pdf}}
\newcommand{\unit}[1]{\,\mathrm{#1}}
\gongbuheader{일반물리학1 · 1장}{측정}
\begin{document}
\gongbucover{일반물리학1}{1장 측정}{물리량과 단위}
% 주석은 옮기지 않는다
\section{측정과 단위}
\sourceslide{2}
속력은 $v=3\unit{m/s}$이고 \textbf{굵게} 쓴다.\footnote{보충 설명이다.} 50\% 정도다.

\begin{equation}
F = ma \label{eq:newton}
\end{equation}
\begin{align}
x &= 1 \\
y &= 2 \notag \\
z &= 3
\end{align}
식~\eqref{eq:newton}을 쓴다 --- 끝.
\begin{warnbox}[조심]
단위를 쓴다.

둘째 문단이다.
\end{warnbox}
\Needspace*{5\baselineskip}
\subsection*{번호 없는 소절}
\begin{itemize}
\item 하나
\item 둘
\end{itemize}
\section{두 번째 절}
\sourceslide[0.8]{5}
\end{document}
"""


class ConvertTexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "note.tex"
        self.path.write_text(DOCUMENT, encoding="utf-8")
        self.note = tn.convert_tex(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def texts(self, kind: str) -> list[str]:
        return ["".join(item.get("text", {}).get("content", "") or item.get("equation", {}).get("expression", "")
                        for item in block[kind]["rich_text"]) for block in self.note.blocks if block["type"] == kind]

    def test_cover_slides_and_pdf_path(self) -> None:
        self.assertEqual(("일반물리학1", "1장 측정", "물리량과 단위"), (self.note.course, self.note.title, self.note.summary))
        self.assertEqual([2, 5], self.note.slides)
        self.assertEqual((Path(self.temp.name) / "slides.pdf").resolve(), self.note.slide_pdf)
        images = [block for block in self.note.blocks if block["type"] == "image"]
        self.assertEqual("slide:2", images[0]["image"]["file_upload"]["id"])
        self.assertEqual("원본 PDF p.5", images[1]["image"]["caption"][0]["text"]["content"])

    def test_headings_keep_pdf_numbers(self) -> None:
        self.assertEqual(["1. 측정과 단위", "2. 두 번째 절"], self.texts("heading_1"))
        self.assertEqual(["번호 없는 소절"], self.texts("heading_2"))

    def test_equation_numbers_labels_and_notag(self) -> None:
        expressions = [block["equation"]["expression"] for block in self.note.blocks if block["type"] == "equation"]
        self.assertEqual(r"F = ma \tag{1}", expressions[0])
        self.assertIn(r"x &= 1 \tag{2}", expressions[1])
        self.assertIn("y &= 2 \\\\", expressions[1])
        self.assertNotIn(r"\notag", expressions[1])
        self.assertIn(r"z &= 3 \tag{3}", expressions[1])
        self.assertTrue(expressions[1].startswith(r"\begin{align*}"))
        self.assertTrue(expressions[1].endswith(r"\end{align*}"))

    def test_inline_text_math_macros_footnotes_and_refs(self) -> None:
        paragraphs = self.texts("paragraph")
        self.assertIn(r"v=3\,\mathrm{m/s}", paragraphs[0])
        self.assertIn("[1]", paragraphs[0])
        self.assertIn("50% 정도다.", paragraphs[0])
        self.assertTrue(any("식 (1)을 쓴다 — 끝." in text for text in paragraphs))
        bold = [item for block in self.note.blocks if block["type"] == "paragraph"
                for item in block["paragraph"]["rich_text"] if item.get("annotations", {}).get("bold")]
        self.assertEqual("굵게", bold[0]["text"]["content"])
        self.assertEqual(["보충 설명이다."], self.texts("numbered_list_item")[-1:])
        dumped = json.dumps(self.note.blocks, ensure_ascii=False)
        for leftover in ("Needspace", "주석은", "gongbu", r"\label"):
            self.assertNotIn(leftover, dumped)

    def test_box_and_list(self) -> None:
        callout = next(block for block in self.note.blocks if block["type"] == "callout")["callout"]
        self.assertEqual(("⚠️", "orange_background"), (callout["icon"]["emoji"], callout["color"]))
        self.assertEqual("조심\n단위를 쓴다.", "".join(item["text"]["content"] for item in callout["rich_text"]))
        self.assertEqual("paragraph", callout["children"][0]["type"])
        bullets = [block for block in self.note.blocks if block["type"] == "bulleted_list_item"]
        self.assertEqual(2, len(bullets))

    def test_matrix_inside_inline_math_stays_in_the_paragraph(self) -> None:
        note = Path(self.temp.name) / "inline.tex"
        note.write_text("\\documentclass{article}\n\\begin{document}\n\\gongbucover{과목}{01 · 주제}{요약}\n"
                        "교안의 \\(\\left[\\begin{smallmatrix}2&1\\\\0&4\\end{smallmatrix}\\right]\\)은 삼각행렬이고 "
                        "$\\begin{pmatrix}1\\\\2\\end{pmatrix}$도 있다.\n\\end{document}\n", encoding="utf-8")
        converted = tn.convert_tex(note)
        self.assertEqual([], converted.problems)
        self.assertEqual(["paragraph"], [block["type"] for block in converted.blocks])
        expressions = [item["equation"]["expression"] for item in converted.blocks[0]["paragraph"]["rich_text"]
                       if item["type"] == "equation"]
        self.assertEqual([r"\left[\begin{smallmatrix}2&1\\0&4\end{smallmatrix}\right]",
                          r"\begin{pmatrix}1\\2\end{pmatrix}"], expressions)

    def test_math_inside_text_inside_inline_math_is_kept_whole(self) -> None:
        items = tn.inline_items(r"열공간 \(C(A)=\operatorname{span}\{\text{\(A\)의 열}\}\)이고 $x=\text{$y$일 때}$다.",
                                {"problems": []})
        expressions = [item["equation"]["expression"] for item in items if item["type"] == "equation"]
        self.assertEqual([r"C(A)=\operatorname{span}\{\text{\(A\)의 열}\}", r"x=\text{$y$일 때}"], expressions)
        self.assertEqual("열공간 이고 다.", "".join(item["text"]["content"] for item in items if item["type"] == "text"))

    def test_space_macro_in_slide_path_follows_tex(self) -> None:
        note = Path(self.temp.name) / "spaces.tex"
        note.write_text("\\documentclass{article}\n\\newcommand{\\sourcepdf}{../a\\space\\space b.pdf}\n"
                        "\\begin{document}\n\\gongbucover{과목}{01 · 주제}{요약}\n\\end{document}\n", encoding="utf-8")
        self.assertEqual("a  b.pdf", tn.convert_tex(note).slide_pdf.name)

    def test_input_parts_are_inlined(self) -> None:
        part = Path(self.temp.name) / "part01.tex"
        part.write_text("\\section{부분}\n본문이다.\n", encoding="utf-8")
        wrapper = Path(self.temp.name) / "wrapper.tex"
        wrapper.write_text("\\documentclass{article}\n\\begin{document}\n\\gongbucover{과목}{01 · 주제}{요약}\n"
                           "\\input{part01.tex}\n\\end{document}\n", encoding="utf-8")
        note = tn.convert_tex(wrapper)
        self.assertEqual("01 · 주제", note.title)
        self.assertEqual("heading_1", note.blocks[0]["type"])


class KatexArrayTests(unittest.TestCase):
    """노션 수식 엔진(KaTeX)의 array는 열 지정으로 l·c·r·|·:만 받는다."""

    def test_text_between_columns_becomes_its_own_column(self) -> None:
        source = r"\begin{array}{ccl@{\qquad}ccl}a&:&b & c&:&d\\ e&:&f & g&:&h\end{array}"
        self.assertEqual(r"\begin{array}{cclcccl}a&:&b &\qquad& c&:&d\\ e&:&f &\qquad& g&:&h\end{array}",
                         tn.katex_arrays(source))

    def test_other_specs_are_rewritten_and_supported_ones_kept(self) -> None:
        self.assertEqual(r"\begin{array}{ccc|c}1&2&3&4\end{array}",
                         tn.katex_arrays(r"\begin{array}{*{3}{c}|c}1&2&3&4\end{array}"))
        self.assertEqual(r"\begin{array}{ll}x&y\\\hline\end{array}",
                         tn.katex_arrays(r"\begin{array}{@{}l@{}p{2cm}}x&y\\\hline\end{array}"))
        supported = r"\left[\begin{array}{cc|c}1&\{0\}&2\end{array}\right]"
        self.assertEqual(supported, tn.katex_arrays(supported))

    def test_display_and_inline_math_both_use_it(self) -> None:
        self.assertEqual(r"\begin{array}{lcl}a&=&b\end{array}",
                         tn.equation(r"\begin{array}{l@{=}l}a&b\end{array}", [])["equation"]["expression"])
        items = tn.inline_items(r"표 $\begin{array}{l@{=}l}a&b\end{array}$", {"problems": []})
        self.assertEqual(r"\begin{array}{lcl}a&=&b\end{array}", items[-1]["equation"]["expression"])


if __name__ == "__main__":
    unittest.main()
