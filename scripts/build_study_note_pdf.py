#!/usr/bin/env python3
"""학습노트 Markdown을 A4 PDF로 결정적으로 조판한다.

조판은 내용을 바꾸지 않는다. 이 스크립트는 `<!-- ... -->` 추적 주석과 `후속 역할 인계 메모`
이후만 걷어내고 나머지 문장·표·목록을 그대로 렌더한다. 한 강의마다 조판 에이전트를 부르지
않기 위해 만든 공통 빌더라서 과목·차시·요약만 인자로 받는다.

    python scripts/build_study_note_pdf.py work/note_draft.md --output output/노트.pdf \
        --course "미디어빅뱅과방송" --session "1주차 2차시" --summary "미디어의 개념과 사회적 기능"

글꼴은 Windows에 흔한 한글 TrueType을 순서대로 찾는다(한컴 → 맑은 고딕). 다른 OS에서는
`--font-body` / `--font-head` 로 TTF 경로를 직접 준다.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        BaseDocTemplate,
        CondPageBreak,
        Frame,
        HRFlowable,
        LongTable,
        NextPageTemplate,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    REPORTLAB_ERROR: ImportError | None = None
except ImportError as exc:
    # reportlab이 없어도 모듈은 import돼야 한다(순수 함수 테스트·CLI 안내). 렌더는 main()에서 막는다.
    REPORTLAB_ERROR = exc
    A4 = (595.2756, 841.8898)
    BaseDocTemplate = object  # type: ignore[assignment,misc]


def _color(value: str):
    return colors.HexColor(value) if REPORTLAB_ERROR is None else value


PAGE_W, PAGE_H = A4
ACCENT = _color("#1F4E79")
ACCENT_DARK = _color("#173A5B")
ACCENT_LIGHT = _color("#EAF2F8")
INK = _color("#202A33")
MUTED = _color("#64707C")
LINE = _color("#D8E0E6")
SOFT = _color("#F6F8FA")
WARM = _color("#FFF7E8")
WARNING = _color("#A64B00")

WINDOWS_FONTS = Path(r"C:\Windows\Fonts")
# (본문, 본문 굵게, 제목, 제목 굵게) 후보를 순서대로 찾는다.
FONT_CANDIDATES = (
    ("HANBatang.ttf", "HANBatangB.ttf", "Hancom Gothic Regular.ttf", "Hancom Gothic Bold.ttf"),
    ("malgun.ttf", "malgunbd.ttf", "malgun.ttf", "malgunbd.ttf"),
)
COMMENT_RE = re.compile(r"[ \t]*<!--.*?-->[ \t]*\n?", re.DOTALL)
HANDOFF_MARKER = "## 후속 역할 인계 메모"


def resolve_fonts(body: Path | None, head: Path | None) -> dict[str, Path]:
    """사용자 지정 글꼴이 있으면 그것을, 없으면 Windows 한글 글꼴을 순서대로 쓴다."""
    if body or head:
        body_path = body or head
        head_path = head or body
        assert body_path is not None and head_path is not None
        for path in (body_path, head_path):
            if not path.is_file():
                raise FileNotFoundError(f"글꼴 파일이 없습니다: {path}")
        return {"BodyKR": body_path, "BodyKRBold": body_path, "HeadKR": head_path, "HeadKRBold": head_path}
    for candidate in FONT_CANDIDATES:
        paths = [WINDOWS_FONTS / name for name in candidate]
        if all(path.is_file() for path in paths):
            return {"BodyKR": paths[0], "BodyKRBold": paths[1], "HeadKR": paths[2], "HeadKRBold": paths[3]}
    raise FileNotFoundError(
        "한글 TrueType 글꼴을 찾지 못했습니다. --font-body/--font-head 로 TTF 경로를 지정하십시오."
    )


def register_fonts(fonts: dict[str, Path]) -> None:
    for name, path in fonts.items():
        pdfmetrics.registerFont(TTFont(name, str(path)))
    pdfmetrics.registerFont(TTFont("MonoKR", str(fonts["HeadKR"])))
    pdfmetrics.registerFontFamily("BodyKR", normal="BodyKR", bold="BodyKRBold", italic="BodyKR", boldItalic="BodyKRBold")
    pdfmetrics.registerFontFamily("HeadKR", normal="HeadKR", bold="HeadKRBold", italic="HeadKR", boldItalic="HeadKRBold")


def normalize_dashes(text: str) -> str:
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        text = text.replace(dash, "-")
    return text


def inline_markup(text: str) -> str:
    text = normalize_dashes(text.strip())
    code_chunks: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_chunks.append(html.escape(match.group(1)))
        return f"@@CODE{len(code_chunks) - 1}@@"

    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    for idx, chunk in enumerate(code_chunks):
        text = text.replace(f"@@CODE{idx}@@", f'<font name="MonoKR" color="#1F4E79" size="8.7">{chunk}</font>')
    return text


def make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}
    styles["Body"] = ParagraphStyle(
        "Body", parent=base["BodyText"], fontName="BodyKR", fontSize=10.7, leading=15.5, textColor=INK,
        spaceAfter=6.2, splitLongWords=True, wordWrap="CJK", allowWidows=0, allowOrphans=0,
    )
    styles["Lead"] = ParagraphStyle("Lead", parent=styles["Body"], fontName="HeadKR", fontSize=11.2, leading=17, textColor=ACCENT_DARK, spaceAfter=9)
    styles["Section"] = ParagraphStyle(
        "Section", parent=base["Heading1"], fontName="HeadKRBold", fontSize=16.2, leading=21, textColor=ACCENT_DARK,
        spaceBefore=14, spaceAfter=9, keepWithNext=1, wordWrap="CJK",
    )
    styles["Subsection"] = ParagraphStyle(
        "Subsection", parent=base["Heading2"], fontName="HeadKRBold", fontSize=12.6, leading=17, textColor=ACCENT,
        spaceBefore=9, spaceAfter=5, keepWithNext=1, wordWrap="CJK",
    )
    styles["Minor"] = ParagraphStyle(
        "Minor", parent=base["Heading3"], fontName="HeadKRBold", fontSize=10.8, leading=15, textColor=INK,
        spaceBefore=7, spaceAfter=4, keepWithNext=1, wordWrap="CJK",
    )
    styles["Bullet"] = ParagraphStyle("Bullet", parent=styles["Body"], leftIndent=15, firstLineIndent=-9, bulletIndent=3, bulletFontName="HeadKR", bulletFontSize=9.4, spaceAfter=3.5)
    styles["Number"] = ParagraphStyle("Number", parent=styles["Body"], leftIndent=19, firstLineIndent=-14, bulletIndent=2, bulletFontName="HeadKR", bulletFontSize=9.4, spaceAfter=4)
    styles["Citation"] = ParagraphStyle(
        "Citation", parent=styles["Body"], fontName="HeadKR", fontSize=8.7, leading=12.5, textColor=MUTED, leftIndent=7,
        borderColor=LINE, borderWidth=0.7, borderPadding=(0, 0, 0, 6), spaceBefore=1, spaceAfter=7,
    )
    styles["TableCell"] = ParagraphStyle("TableCell", parent=styles["Body"], fontName="HeadKR", fontSize=8.55, leading=12.2, spaceAfter=0, wordWrap="CJK")
    styles["TableHeader"] = ParagraphStyle("TableHeader", parent=styles["TableCell"], fontName="HeadKRBold", fontSize=8.7, leading=12.2, textColor=colors.white, alignment=TA_CENTER)
    styles["Callout"] = ParagraphStyle("Callout", parent=styles["Body"], fontName="HeadKR", fontSize=9.7, leading=14.5, spaceAfter=3)
    styles["CalloutBullet"] = ParagraphStyle("CalloutBullet", parent=styles["Callout"], leftIndent=13, firstLineIndent=-8, bulletIndent=2, spaceAfter=2.5)
    styles["TOCTitle"] = ParagraphStyle("TOCTitle", parent=styles["Section"], fontSize=18, leading=23, spaceBefore=5, spaceAfter=15)
    styles["CoverKicker"] = ParagraphStyle("CoverKicker", fontName="HeadKRBold", fontSize=10.2, leading=13, textColor=ACCENT, alignment=TA_LEFT, spaceAfter=11)
    styles["CoverTitle"] = ParagraphStyle("CoverTitle", fontName="HeadKRBold", fontSize=29, leading=37, textColor=ACCENT_DARK, alignment=TA_LEFT, spaceAfter=9, wordWrap="CJK")
    styles["CoverSub"] = ParagraphStyle("CoverSub", fontName="HeadKR", fontSize=15.2, leading=21, textColor=INK, alignment=TA_LEFT, spaceAfter=20)
    styles["CoverMeta"] = ParagraphStyle("CoverMeta", fontName="HeadKR", fontSize=9.2, leading=14, textColor=MUTED, alignment=TA_LEFT, spaceAfter=3)
    styles["CoverSummary"] = ParagraphStyle("CoverSummary", fontName="HeadKR", fontSize=11.2, leading=17, textColor=INK, alignment=TA_LEFT, wordWrap="CJK")
    return styles


class StudyDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str, header_text: str, **kwargs) -> None:
        super().__init__(filename, **kwargs)
        self.header_text = header_text
        cover_frame = Frame(20 * mm, 18 * mm, PAGE_W - 40 * mm, PAGE_H - 36 * mm, id="cover")
        body_frame = Frame(20 * mm, 18 * mm, PAGE_W - 40 * mm, PAGE_H - 37 * mm, id="body")
        self.addPageTemplates(
            [
                PageTemplate(id="cover", frames=[cover_frame], onPage=self.draw_cover, autoNextPageTemplate="body"),
                PageTemplate(id="body", frames=[body_frame], onPage=self.draw_body, autoNextPageTemplate="body"),
            ]
        )

    def draw_cover(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        canvas.setFillColor(ACCENT)
        canvas.rect(0, PAGE_H - 12 * mm, PAGE_W, 12 * mm, stroke=0, fill=1)
        canvas.setFillColor(ACCENT_LIGHT)
        canvas.circle(PAGE_W - 18 * mm, 26 * mm, 42 * mm, stroke=0, fill=1)
        canvas.setFillColor(ACCENT)
        canvas.circle(PAGE_W - 8 * mm, 17 * mm, 18 * mm, stroke=0, fill=1)
        canvas.restoreState()

    def draw_body(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(20 * mm, PAGE_H - 15 * mm, PAGE_W - 20 * mm, PAGE_H - 15 * mm)
        canvas.setFont("HeadKR", 8.2)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, PAGE_H - 11.7 * mm, self.header_text)
        canvas.drawRightString(PAGE_W - 20 * mm, 11.5 * mm, f"{doc.page}")
        canvas.drawString(20 * mm, 11.5 * mm, "학습노트")
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        if not isinstance(flowable, Paragraph) or flowable.style.name not in {"Section", "Subsection"}:
            return
        level = 0 if flowable.style.name == "Section" else 1
        text = flowable.getPlainText()
        key = getattr(flowable, "_bookmark_key", None)
        if key is None:
            key = f"heading-{id(flowable)}"
            flowable._bookmark_key = key
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


def parse_table(lines: list[str], styles: dict[str, ParagraphStyle], width: float) -> LongTable:
    rows = [[cell.strip() for cell in raw.strip().strip("|").split("|")] for raw in lines]
    rows.pop(1)
    column_count = max(len(row) for row in rows)
    rows = [row + [""] * (column_count - len(row)) for row in rows]
    if column_count == 2:
        widths = [0.32 * width, 0.68 * width]
    elif column_count == 3:
        widths = [0.19 * width, 0.38 * width, 0.43 * width]
    else:
        widths = [width / column_count] * column_count
    data = [
        [Paragraph(inline_markup(cell), styles["TableHeader"] if row_idx == 0 else styles["TableCell"]) for cell in row]
        for row_idx, row in enumerate(rows)
    ]
    table = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT", splitByRow=1, spaceBefore=4, spaceAfter=9)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for row_idx in range(1, len(data)):
        commands.append(("BACKGROUND", (0, row_idx), (-1, row_idx), colors.white if row_idx % 2 else SOFT))
    table.setStyle(TableStyle(commands))
    return table


def make_callout(lines: list[str], styles: dict[str, ParagraphStyle], width: float) -> Table:
    meaningful = [line.strip() for line in lines if line.strip()]
    is_warning = any("주의" in line for line in meaningful[:1])
    inner = [
        Paragraph(inline_markup(line[2:]), styles["CalloutBullet"], bulletText="•")
        if line.startswith("- ")
        else Paragraph(inline_markup(line), styles["Callout"])
        for line in meaningful
    ]
    box = Table([[inner]], colWidths=[width], hAlign="LEFT", spaceBefore=5, spaceAfter=10)
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WARM if is_warning else ACCENT_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LINEBEFORE", (0, 0), (0, -1), 3.2, WARNING if is_warning else ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return box


def is_table_start(lines: list[str], idx: int) -> bool:
    return "|" in lines[idx] and idx + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[idx + 1]) is not None


def markdown_to_flowables(text: str, styles: dict[str, ParagraphStyle], width: float) -> list:
    lines = text.splitlines()
    output: list = []
    idx = 0
    first_body_paragraph = True
    in_code = False
    code_lines: list[str] = []
    while idx < len(lines):
        line = lines[idx].rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                output.append(Paragraph("<br/>".join(html.escape(item) for item in code_lines) or " ", styles["Citation"]))
                code_lines = []
            in_code = not in_code
            idx += 1
            continue
        if in_code:
            code_lines.append(line)
            idx += 1
            continue
        if not stripped:
            idx += 1
            continue
        if stripped.startswith("# "):
            idx += 1
            continue
        if stripped == "---":
            output.extend([Spacer(1, 3), HRFlowable(width="100%", thickness=0.55, color=LINE), Spacer(1, 4)])
            idx += 1
            continue
        if stripped.startswith("## "):
            output.extend([CondPageBreak(48 * mm), Paragraph(inline_markup(stripped[3:]), styles["Section"])])
            first_body_paragraph = True
            idx += 1
            continue
        if stripped.startswith("### "):
            output.extend([CondPageBreak(24 * mm), Paragraph(inline_markup(stripped[4:]), styles["Subsection"])])
            idx += 1
            continue
        if stripped.startswith("#### "):
            output.extend([CondPageBreak(18 * mm), Paragraph(inline_markup(stripped[5:]), styles["Minor"])])
            idx += 1
            continue
        if stripped.startswith(">"):
            quote_lines = []
            while idx < len(lines) and lines[idx].lstrip().startswith(">"):
                quote_lines.append(lines[idx].lstrip()[1:].lstrip())
                idx += 1
            output.append(make_callout(quote_lines, styles, width))
            continue
        if is_table_start(lines, idx):
            table_lines = [line, lines[idx + 1]]
            idx += 2
            while idx < len(lines) and "|" in lines[idx] and lines[idx].strip():
                table_lines.append(lines[idx])
                idx += 1
            output.append(parse_table(table_lines, styles, width))
            continue
        list_match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        bullet_match = re.match(r"^[-*]\s+(.*)$", stripped)
        if list_match:
            number, content = list_match.groups()
            output.append(Paragraph(inline_markup(content), styles["Number"], bulletText=f"{number}."))
            idx += 1
            continue
        if bullet_match:
            content = bullet_match.group(1)
            bullet = "•"
            if content.startswith("[ ] "):
                content, bullet = content[4:], "□"
            elif content.startswith("[x] ") or content.startswith("[X] "):
                content, bullet = content[4:], "■"
            output.append(Paragraph(inline_markup(content), styles["Bullet"], bulletText=bullet))
            idx += 1
            continue
        paragraph_lines = [stripped]
        idx += 1
        while idx < len(lines):
            nxt = lines[idx].strip()
            if (
                not nxt
                or nxt.startswith("#")
                or nxt.startswith(">")
                or nxt == "---"
                or nxt.startswith("```")
                or re.match(r"^(\d+)\.\s+", nxt)
                or re.match(r"^[-*]\s+", nxt)
                or is_table_start(lines, idx)
            ):
                break
            paragraph_lines.append(nxt)
            idx += 1
        paragraph = " ".join(paragraph_lines)
        if first_body_paragraph:
            style = styles["Lead"]
            first_body_paragraph = False
        else:
            style = styles["Body"]
        output.append(Paragraph(inline_markup(paragraph), style))
    return output


def public_text(markdown: str) -> str:
    """추적 주석과 내부 인계 메모를 제거한 학생용 본문."""
    text = COMMENT_RE.sub("", markdown)
    if HANDOFF_MARKER in text:
        text = text.split(HANDOFF_MARKER, 1)[0].rstrip()
    return text.strip() + "\n"


def build(
    source: Path,
    output: Path,
    course: str,
    session: str,
    summary: str | None,
    metas: list[str],
    kicker: str,
    fonts: dict[str, Path],
) -> Path:
    register_fonts(fonts)
    styles = make_styles()
    text = public_text(source.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    title = f"{course} {session} 학습노트"
    doc = StudyDocTemplate(
        str(output),
        header_text=f"{course} · {session}",
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=19 * mm,
        bottomMargin=18 * mm,
        title=title,
        subject=summary or "",
        creator="gongbu-haja",
    )
    usable_width = PAGE_W - 40 * mm
    story: list = [
        Spacer(1, 39 * mm),
        Paragraph(html.escape(kicker), styles["CoverKicker"]),
        Paragraph(html.escape(course), styles["CoverTitle"]),
        Paragraph(html.escape(f"{session} 학습노트"), styles["CoverSub"]),
        HRFlowable(width="35%", thickness=2.2, color=ACCENT, hAlign="LEFT", spaceAfter=15),
    ]
    if summary:
        story.append(
            Table(
                [[Paragraph(inline_markup(summary), styles["CoverSummary"])]],
                colWidths=[usable_width * 0.82],
                hAlign="LEFT",
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_LIGHT),
                        ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
                        ("LEFTPADDING", (0, 0), (-1, -1), 12),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                        ("TOPPADDING", (0, 0), (-1, -1), 10),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ]
                ),
            )
        )
    story.append(Spacer(1, 18 * mm))
    for meta in metas:
        story.append(Paragraph(inline_markup(meta), styles["CoverMeta"]))
    story.extend([NextPageTemplate("body"), PageBreak(), Paragraph("목차", styles["TOCTitle"])])
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("TOCLevel1", fontName="HeadKRBold", fontSize=9.9, leading=16, leftIndent=0, firstLineIndent=0, textColor=INK, spaceBefore=2),
        ParagraphStyle("TOCLevel2", fontName="HeadKR", fontSize=8.4, leading=12.6, leftIndent=13, firstLineIndent=0, textColor=MUTED),
    ]
    story.extend([toc, PageBreak()])
    story.extend(markdown_to_flowables(text, styles, usable_width))
    doc.multiBuild(story)
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="학습노트 Markdown을 A4 PDF로 결정적으로 조판합니다.")
    parser.add_argument("source", type=Path, help="집필 초안 또는 최종본 Markdown")
    parser.add_argument("--output", type=Path, required=True, help="만들 PDF 경로")
    parser.add_argument("--course", required=True, help="과목명(표지·머리글)")
    parser.add_argument("--session", required=True, help="차시 표기(예: 1주차 2차시)")
    parser.add_argument("--summary", default=None, help="표지 한 줄 요약(선택)")
    parser.add_argument("--meta", action="append", default=[], help="표지 하단 메타 줄(반복 가능)")
    parser.add_argument("--kicker", default="STUDY NOTE", help="표지 상단 작은 제목")
    parser.add_argument("--font-body", type=Path, default=None, help="본문 TTF 경로(선택)")
    parser.add_argument("--font-head", type=Path, default=None, help="제목 TTF 경로(선택)")
    parser.add_argument("--force", action="store_true", help="기존 PDF를 덮어쓴다")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        print(f"[오류] 원고가 없습니다: {source}", file=sys.stderr)
        return 2
    if REPORTLAB_ERROR is not None:
        print("[오류] reportlab이 없습니다. `python -m pip install reportlab` 후 다시 실행하십시오.", file=sys.stderr)
        return 2
    if output.exists() and not args.force:
        print(f"[오류] 기존 PDF를 덮어쓰지 않습니다(--force 로 교체): {output}", file=sys.stderr)
        return 2
    try:
        fonts = resolve_fonts(args.font_body, args.font_head)
    except FileNotFoundError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2
    build(source, output, args.course, args.session, args.summary, args.meta, args.kicker, fonts)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
