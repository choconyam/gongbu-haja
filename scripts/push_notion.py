#!/usr/bin/env python3
"""학습노트 Markdown을 노션 과목 페이지의 "차시별 노트" 표에 한 줄(노트 페이지)로 올린다.

    python scripts/push_notion.py login                        # 사용자가 자기 터미널에서 직접 실행
    python scripts/push_notion.py logout
    python scripts/push_notion.py setup <상위 페이지 링크> --course <과목명>
    python scripts/push_notion.py check <노트.md>             # 네트워크 없이 변환·검사
    python scripts/push_notion.py push <노트.md> [--dry-run]

과목 폴더에서는 `gongbu notion ...`으로 부른다. 변환과 업로드는 Python이 하므로 AI 토큰이 들지 않고
옮기다 내용이 빠지지 않는다. 노션 통신은 표준 라이브러리만 쓰고, 토큰은 선택 설치 keyring으로 OS
비밀번호 보관소(Windows 자격 증명 관리자, macOS 키체인)에만 둔다. 토큰을 파일·환경 변수·로그·명령
인자로 받거나 남기지 않으며, login은 입력이 화면에 보이지 않는 터미널에서만 동작한다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from .build_study_note_pdf import HandoffMemoError, public_text
except ImportError:  # `python scripts/push_notion.py`로 직접 실행할 때
    from build_study_note_pdf import HandoffMemoError, public_text

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
KEYRING_SERVICE = "gongbu-haja"
KEYRING_ACCOUNT = "notion"
STATE_DIR = ".gongbu"
STATE_NAME = "notion.json"
TABLE_TITLE = "차시별 노트"
COLUMN_TITLE, COLUMN_SUMMARY, COLUMN_MODE, COLUMN_HANDOUT, COLUMN_DATE = "차시", "내용", "모드", "교안", "올린 날"
MODES = {"faithful": ("자료 충실형", "📗", "green"), "deep": ("심화 이해형", "📘", "blue")}
CALLOUTS = {"NOTE": ("💡", "blue_background"), "IMPORTANT": ("💡", "blue_background"),
            "TIP": ("📌", "green_background"), "WARNING": ("⚠️", "orange_background"),
            "CAUTION": ("⚠️", "orange_background")}
UNCERTAIN_RE = re.compile(r"\[(?:판독 불명|전사 불명확|자료에 명시 없음|문맥상 추정|확인 필요|청취 불가)[^\]]*\]")
PAGE_ID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
TEXT_LIMIT = 2000
EQUATION_LIMIT = 1000
ARRAY_LIMIT = 100
REQUEST_ELEMENT_LIMIT = 900  # 노션 한도(요청당 블록 1000개·500KB)보다 여유를 둔다
REQUEST_BYTES_LIMIT = 450_000
UNSUPPORTED_TEX_RE = re.compile(r"\\(?:label|ref|eqref|usepackage|newcommand|renewcommand|input|include)\b")
CODE_LANGUAGES = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++", "c#", "css", "dart", "diff",
    "docker", "elixir", "elm", "erlang", "flow", "fortran", "f#", "gherkin", "glsl", "go", "graphql", "groovy",
    "haskell", "html", "java", "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "lua", "makefile", "markdown", "markup", "matlab", "mermaid", "nix", "objective-c", "ocaml", "pascal",
    "perl", "php", "plain text", "powershell", "prolog", "protobuf", "python", "r", "reason", "ruby", "rust",
    "sass", "scala", "scheme", "scss", "shell", "sql", "swift", "typescript", "vb.net", "verilog", "vhdl",
    "visual basic", "webassembly", "xml", "yaml"}
LANGUAGE_ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "shell", "zsh": "shell",
                    "ps1": "powershell", "pwsh": "powershell", "cpp": "c++", "cs": "c#", "md": "markdown",
                    "tex": "latex", "yml": "yaml", "text": "plain text", "txt": "plain text", "": "plain text"}

PROTECT_RE = re.compile(
    r"(?P<tick>`+)(?P<code>.+?)(?P=tick)"
    r"|\\\((?P<paren>.+?)\\\)"
    r"|(?<![\\$])\$(?=\S)(?P<dollar>[^$\n]*?\S)\$(?!\d)"
    r"|\\(?P<escaped>[\\`*_{}\[\]()#+\-.!$|~<>])"
)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
PLACEHOLDER_RE = re.compile("\ue000(\\d+)\ue001")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?:(?P<number>\d+)[.)]|(?P<bullet>[-*+]))[ \t]+(?P<body>.*)$")
IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src><[^>]+>|[^)]+?)(?:\s+\"[^\"]*\")?\)$")
RULE_RE = re.compile(r"(?:-{3,}|\*{3,}|_{3,})")
ALERT_RE = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", re.IGNORECASE)
MATH_ENV_RE = re.compile(r"^\\begin\{(equation|align|gather|multline|flalign)(\*?)\}")


class NotionError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ------------------------------------------------------------------ Markdown → 노션 블록

def text_item(content: str, *, bold: bool = False, italic: bool = False, code: bool = False,
              color: str = "default", link: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "text", "text": {"content": content}}
    if link:
        item["text"]["link"] = {"url": link}
    annotations: dict[str, Any] = {name: True for name, value in (("bold", bold), ("italic", italic), ("code", code)) if value}
    if color != "default":
        annotations["color"] = color
    if annotations:
        item["annotations"] = annotations
    return item


def equation_item(expression: str, bold: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "equation", "equation": {"expression": expression}}
    if bold:
        item["annotations"] = {"bold": True}
    return item


def split_marked(text: str, regex: re.Pattern[str]) -> list[tuple[str, re.Match[str] | None]]:
    """정규식에 맞는 부분과 아닌 부분을 순서대로 나눈다."""
    pieces: list[tuple[str, re.Match[str] | None]] = []
    position = 0
    for match in regex.finditer(text):
        if match.start() > position:
            pieces.append((text[position:match.start()], None))
        pieces.append((match.group(1), match))
        position = match.end()
    if position < len(text):
        pieces.append((text[position:], None))
    return pieces


def merge_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """모양이 같은 이웃 글자를 합치고, 노션 한도(2000자)를 넘는 글자는 나눈다."""
    merged: list[dict[str, Any]] = []
    for item in items:
        previous = merged[-1] if merged else None
        if (previous and item["type"] == previous["type"] == "text"
                and item.get("annotations") == previous.get("annotations")
                and item["text"].get("link") == previous["text"].get("link")):
            previous["text"]["content"] += item["text"]["content"]
        else:
            merged.append(item)
    result: list[dict[str, Any]] = []
    for item in merged:
        if item["type"] != "text" or len(item["text"]["content"]) <= TEXT_LIMIT:
            result.append(item)
            continue
        content = item["text"]["content"]
        while content:
            cut = TEXT_LIMIT if len(content) > TEXT_LIMIT else len(content)
            if cut < len(content):  # 문장·띄어쓰기 경계에서 자른다
                boundary = max(content.rfind(". ", 0, cut), content.rfind("다. ", 0, cut), content.rfind(" ", 0, cut))
                cut = boundary + 1 if boundary > TEXT_LIMIT // 2 else cut
            piece = json.loads(json.dumps(item))
            piece["text"]["content"] = content[:cut]
            result.append(piece)
            content = content[cut:]
    return [item for item in result if item["type"] != "text" or item["text"]["content"]]


def rich_text(text: str, problems: list[str] | None = None) -> list[dict[str, Any]]:
    """한 줄 글을 노션 rich text로 옮긴다: 굵게·기울임·코드·수식·링크·불확실성 표시(노란 형광)."""
    protected: list[tuple[str, str]] = []

    def protect(match: re.Match[str]) -> str:
        if match.group("tick"):
            protected.append(("code", match.group("code").strip()))
        elif match.group("escaped") is not None:
            protected.append(("text", match.group("escaped")))
        else:
            protected.append(("math", match.group("paren") or match.group("dollar")))
        return f"\ue000{len(protected) - 1}\ue001"

    items: list[dict[str, Any]] = []
    for bold_text, bold in split_marked(PROTECT_RE.sub(protect, text), BOLD_RE):
        for italic_text, italic in split_marked(bold_text, ITALIC_RE):
            for label, link_match in split_marked(italic_text, LINK_RE):
                url = link_match.group(2) if link_match else None
                if url and not re.match(r"^(?:https?://|mailto:)", url):
                    if problems is not None:
                        problems.append(f"주의: 로컬 경로 링크는 노션에서 열리지 않아 글자만 올립니다: {url}")
                    url = None
                for unit, placeholder in split_marked(label, PLACEHOLDER_RE):
                    style = {"bold": bool(bold), "italic": bool(italic)}
                    if placeholder:
                        kind, value = protected[int(placeholder.group(1))]
                        if kind == "math":
                            if problems is not None and UNSUPPORTED_TEX_RE.search(value):
                                problems.append(f"주의: 노션 수식이 그리지 못하는 명령이 있습니다: {value[:60]}")
                            items.append(equation_item(value, bool(bold)))
                        else:
                            items.append(text_item(value, code=kind == "code", link=url, **style))
                        continue
                    for plain, marker in split_marked(unit, re.compile(f"({UNCERTAIN_RE.pattern})")):
                        if plain:
                            items.append(text_item(plain, color="yellow_background" if marker else "default",
                                                   link=url, **style))
    return merge_items(items)


def paragraph_blocks(text: str, problems: list[str], kind: str = "paragraph", **extra: Any) -> list[dict[str, Any]]:
    """rich text가 100개를 넘으면 문단을 여러 블록으로 나눈다."""
    items = rich_text(text, problems)
    chunks = [items[start:start + ARRAY_LIMIT] for start in range(0, len(items), ARRAY_LIMIT)] or [[]]
    return [{"type": kind, kind: {"rich_text": chunk, **extra}} for chunk in chunks]


def split_row(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]


def is_table_start(lines: list[str], index: int) -> bool:
    return ("|" in lines[index] and index + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", lines[index + 1]) is not None)


def table_blocks(rows: list[list[str]], problems: list[str]) -> list[dict[str, Any]]:
    """GFM 표를 노션 표로 옮긴다. 줄이 100개를 넘으면 머리줄을 반복해 표를 나눈다."""
    width = max(len(row) for row in rows)
    header, body = rows[0] + [""] * (width - len(rows[0])), [row + [""] * (width - len(row)) for row in rows[1:]]

    def row_block(cells: list[str]) -> dict[str, Any]:
        return {"type": "table_row", "table_row": {"cells": [rich_text(cell, problems)[:ARRAY_LIMIT] for cell in cells]}}

    groups = [body[start:start + ARRAY_LIMIT - 1] for start in range(0, len(body), ARRAY_LIMIT - 1)] or [[]]
    return [{"type": "table", "table": {"table_width": width, "has_column_header": True, "has_row_header": False,
                                        "children": [row_block(header), *(row_block(row) for row in group)]}}
            for group in groups]


def list_items(lines: list[str], index: int) -> tuple[list[tuple[int, str, str]], int]:
    items: list[tuple[int, str, str]] = []
    while index < len(lines):
        current = lines[index]
        item = LIST_ITEM_RE.match(current.rstrip())
        if item:
            indent = len(item.group("indent").replace("\t", "    "))
            items.append((indent, item.group("number") or item.group("bullet"), item.group("body").strip()))
        elif current.strip() and items and (current.startswith((" ", "\t")) or not starts_block(lines, index)):
            indent, marker, text = items[-1]
            items[-1] = (indent, marker, f"{text} {current.strip()}")
        elif not (not current.strip() and index + 1 < len(lines)
                  and (LIST_ITEM_RE.match(lines[index + 1]) or lines[index + 1].startswith(("  ", "\t")))):
            break
        index += 1
    return items, index


def list_blocks(items: list[tuple[int, str, str]], problems: list[str]) -> list[dict[str, Any]]:
    """들여쓰기로 겹친 목록을 노션 목록 블록으로 옮긴다. 노션이 한 요청에 받는 두 단계까지만 겹친다."""
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []  # (들여쓰기, 블록)
    for indent, marker, text in items:
        checkbox = re.match(r"^\[( |x|X)\]\s+", text)
        if checkbox:
            block = {"type": "to_do", "to_do": {"rich_text": rich_text(text[checkbox.end():], problems)[:ARRAY_LIMIT],
                                                 "checked": checkbox.group(1) in "xX"}}
        else:
            kind = "numbered_list_item" if marker[0].isdigit() else "bulleted_list_item"
            block = {"type": kind, kind: {"rich_text": rich_text(text, problems)[:ARRAY_LIMIT]}}
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if len(stack) > 2:
            stack = stack[:2]
            problems.append("주의: 세 단계보다 깊은 목록은 두 단계까지만 들여 씁니다.")
        if stack:
            parent = stack[-1][1]
            parent[parent["type"]].setdefault("children", []).append(block)
        else:
            roots.append(block)
        stack.append((indent, block))
    return roots


def callout_blocks(lines: list[str], problems: list[str]) -> list[dict[str, Any]]:
    """`> [!WARNING]` 같은 GitHub 알림은 색 상자로, 그 밖의 인용은 인용 블록으로 옮긴다."""
    content = list(lines)
    alert = ALERT_RE.match(content[0].strip()) if content else None
    if alert:
        content = content[1:]
        icon, color = CALLOUTS[alert.group(1).upper()]
    else:
        first = next((line for line in content if line.strip()), "")
        # 예전 원고의 `> 주의:` 상자도 같은 색 상자로 옮긴다.
        style = ("WARNING" if re.match(r"^\W*(?:주의|경고)", first) else
                 "TIP" if re.match(r"^\W*기억", first) else "NOTE" if re.match(r"^\W*핵심", first) else None)
        if style is None:
            inner = blocks(content, problems)
            if inner and inner[0]["type"] == "paragraph":
                return [{"type": "quote", "quote": {"rich_text": inner[0]["paragraph"]["rich_text"],
                                                    **({"children": inner[1:]} if inner[1:] else {})}}]
            return [{"type": "quote", "quote": {"rich_text": [], "children": inner}}] if inner else []
        icon, color = CALLOUTS[style]
    while content and not content[0].strip():
        content.pop(0)
    # 첫 줄이 `**주의할 점**`처럼 굵은 이름표뿐이면 상자 제목으로 두고 본문은 다음 줄에 잇는다.
    label = re.match(r"^\*\*([^*]+?)\*\*\s*[:：]?\s*$", content[0].strip()) if content else None
    head: list[dict[str, Any]] = [text_item(label.group(1).strip(), bold=True)] if label else []
    inner = blocks(content[1:] if label else content, problems)
    if inner and inner[0]["type"] == "paragraph":
        head = head + ([text_item("\n")] if head else []) + inner[0]["paragraph"]["rich_text"]
        inner = inner[1:]
    callout: dict[str, Any] = {"rich_text": merge_items(head)[:ARRAY_LIMIT], "icon": {"type": "emoji", "emoji": icon},
                               "color": color}
    if inner:
        callout["children"] = inner
    return [{"type": "callout", "callout": callout}]


def equation_block(expression: str, problems: list[str]) -> dict[str, Any]:
    expression = expression.strip()
    if len(expression) > EQUATION_LIMIT:
        problems.append(f"오류: 수식 하나가 {len(expression)}자로 노션 한도 {EQUATION_LIMIT}자를 넘습니다. 원고에서 나누십시오: "
                        f"{expression[:40]}…")
    if UNSUPPORTED_TEX_RE.search(expression):
        problems.append(f"주의: 노션 수식이 그리지 못하는 명령이 있습니다: {expression[:60]}")
    return {"type": "equation", "equation": {"expression": expression}}


def code_block(lines: list[str], info: str) -> dict[str, Any]:
    language = LANGUAGE_ALIASES.get(info.strip().lower(), info.strip().lower())
    content = "\n".join(lines)
    chunks = [content[start:start + TEXT_LIMIT] for start in range(0, len(content), TEXT_LIMIT)] or [""]
    return {"type": "code", "code": {"rich_text": [text_item(chunk) for chunk in chunks][:ARRAY_LIMIT],
                                     "language": language if language in CODE_LANGUAGES else "plain text"}}


def starts_block(lines: list[str], index: int) -> bool:
    stripped = lines[index].strip()
    return (not stripped or stripped.startswith(("#", ">", "```", "~~~", "$$", r"\[", "<details"))
            or bool(MATH_ENV_RE.match(stripped)) or RULE_RE.fullmatch(stripped) is not None
            or bool(LIST_ITEM_RE.match(lines[index])) or bool(IMAGE_LINE_RE.match(stripped)) or is_table_start(lines, index))


def blocks(lines: list[str], problems: list[str], shift: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        fence = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        heading = HEADING_RE.match(stripped)
        environment = MATH_ENV_RE.match(stripped)
        if fence:
            end = next((number for number in range(index + 1, len(lines))
                        if lines[number].strip().startswith(fence.group(1))), len(lines))
            out.append(code_block(lines[index + 1:end], fence.group(2)))
            index = end + 1
        elif stripped.startswith(("$$", r"\[")):
            opener, closer = ("$$", "$$") if stripped.startswith("$$") else (r"\[", r"\]")
            rest = stripped[len(opener):]
            if closer in rest and rest[rest.index(closer) + len(closer):].strip():
                paragraph, index = paragraph_lines(lines, index)  # `$$a$$ 이므로`는 문단 속 수식
                out.extend(paragraph_blocks(paragraph, problems))
                continue
            if closer in rest:
                body, index = rest[:rest.index(closer)], index + 1
            else:
                end = next((number for number in range(index + 1, len(lines)) if closer in lines[number]), None)
                if end is None:
                    problems.append(f"오류: 닫히지 않은 수식 블록이 있습니다: {stripped[:40]}")
                    end = len(lines) - 1
                body = "\n".join([rest, *lines[index + 1:end], lines[end].split(closer)[0]])
                index = end + 1
            out.append(equation_block(body, problems))
        elif environment:
            closer = rf"\end{{{environment.group(1)}{environment.group(2)}}}"
            end = next((number for number in range(index, len(lines)) if closer in lines[number]), len(lines) - 1)
            out.append(equation_block("\n".join(lines[index:end + 1]), problems))
            index = end + 1
        elif heading:
            level = min(max(len(heading.group(1)) - shift, 1), 3)
            kind = f"heading_{level}"
            out.append({"type": kind, kind: {"rich_text": rich_text(heading.group(2), problems)[:ARRAY_LIMIT]}})
            index += 1
        elif RULE_RE.fullmatch(stripped):
            out.append({"type": "divider", "divider": {}})
            index += 1
        elif stripped.startswith("<details"):
            end = next((number for number in range(index, len(lines)) if "</details>" in lines[number]), len(lines) - 1)
            inner = "\n".join(lines[index:end + 1])
            summary = re.search(r"<summary>(.*?)</summary>", inner, re.S)
            body = re.sub(r"(?s)^.*?(?:</summary>|<details[^>]*>)|</details>.*$", "", inner)
            out.append({"type": "toggle", "toggle": {
                "rich_text": rich_text(summary.group(1).strip() if summary else "펼쳐 보기", problems)[:ARRAY_LIMIT],
                "children": blocks(body.splitlines(), problems, shift)}})
            index = end + 1
        elif stripped.startswith(">"):
            quoted = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            out.extend(callout_blocks(quoted, problems))
        elif is_table_start(lines, index):
            rows = [split_row(lines[index])]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(split_row(lines[index]))
                index += 1
            out.extend(table_blocks(rows, problems))
        elif LIST_ITEM_RE.match(lines[index]):
            items, index = list_items(lines, index)
            out.extend(list_blocks(items, problems))
        elif image := IMAGE_LINE_RE.match(stripped):
            problems.append(f"주의: 그림은 아직 올리지 않고 캡션 글자만 남깁니다: {image.group('src')}")
            caption = image.group("alt").strip() or "그림"
            out.append({"type": "paragraph", "paragraph": {"rich_text": [text_item(caption, italic=True, color="gray")]}})
            index += 1
        else:
            paragraph, index = paragraph_lines(lines, index)
            if "<" in paragraph and re.search(r"</?[A-Za-z][^>]*>", paragraph):
                problems.append(f"주의: HTML 태그는 노션에서 글자로 보입니다: {paragraph[:40]}")
            out.extend(paragraph_blocks(paragraph, problems))
    return out


def paragraph_lines(lines: list[str], index: int) -> tuple[str, int]:
    paragraph = [lines[index].strip()]
    index += 1
    while index < len(lines) and not starts_block(lines, index):
        paragraph.append(lines[index].strip())
        index += 1
    return " ".join(paragraph), index


def count_elements(items: list[dict[str, Any]]) -> int:
    total = 0
    for block in items:
        total += 1
        body = block[block["type"]]
        total += count_elements(body.get("children", []))
    return total


@dataclass
class Note:
    title: str
    summary: str
    blocks: list[dict[str, Any]]
    problems: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [problem for problem in self.problems if problem.startswith("오류")]


def convert(markdown: str, course: str = "") -> Note:
    """학생용 본문(추적 주석·인계 메모 제거)을 노션 블록으로 옮긴다.

    맨 앞의 `# 제목` 하나는 페이지 제목이 되고, 나머지 제목은 한 단계씩 올린다(## → 노션 제목 1).
    """
    lines = public_text(markdown).splitlines()
    headings = [(index, HEADING_RE.match(line.strip())) for index, line in enumerate(lines) if HEADING_RE.match(line.strip())]
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    title, shift = "", 0
    top_level = [match for _, match in headings if len(match.group(1)) == 1]
    if first is not None and headings and headings[0][0] == first and len(headings[0][1].group(1)) == 1 and len(top_level) == 1:
        title, shift = headings[0][1].group(2).strip(), 1
        lines = lines[first + 1:]
    problems: list[str] = []
    body = blocks(lines, problems, shift)
    sections = [match.group(2) for _, match in headings if len(match.group(1)) == 1 + shift]
    topics = [re.split(r"\s+[—–-]\s+", section, maxsplit=1)[-1].strip() for section in sections]
    return Note(note_title(title, course), " · ".join(topics)[:300], body, problems)


def note_title(heading: str, course: str) -> str:
    """`과목 1주차 — 주제`를 표의 한 줄 제목 `1주차 · 주제`로 줄인다."""
    title = heading.strip()
    if course and title.startswith(course):
        title = title[len(course):].strip()
    return re.sub(r"\s+[—–-]\s+", " · ", title, count=1)


def request_chunks(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """블록을 요청 하나의 한도(100개·요소 1000개·500KB) 안으로 나눈다."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    elements = size = 0
    for block in items:
        block_elements = count_elements([block])
        block_size = len(json.dumps(block, ensure_ascii=False).encode("utf-8"))
        if current and (len(current) >= ARRAY_LIMIT or elements + block_elements > REQUEST_ELEMENT_LIMIT
                        or size + block_size > REQUEST_BYTES_LIMIT):
            chunks.append(current)
            current, elements, size = [], 0, 0
        current.append(block)
        elements += block_elements
        size += block_size
    if current:
        chunks.append(current)
    return chunks


# ------------------------------------------------------------------ 노션 통신

class NotionClient:
    """노션 REST API. 토큰은 요청 머리글에만 쓰고 오류 메시지·repr에 넣지 않는다."""

    def __init__(self, token: str, sleep: Callable[[float], None] = time.sleep,
                 opener: Callable[..., Any] = urllib.request.urlopen, min_interval: float = 0.35) -> None:
        self._token = token
        self._sleep = sleep
        self._opener = opener
        self._min_interval = min_interval
        self._last = 0.0

    def __repr__(self) -> str:
        return "NotionClient(token=***)"

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(6):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:  # 무료 요금제 한도(초당 평균 3회)
                self._sleep(wait)
            request = urllib.request.Request(API_URL + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json"})
            self._last = time.monotonic()
            try:
                with self._opener(request, timeout=60) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read().decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    payload = {}
                if exc.code in (429, 500, 502, 503, 504, 529) and attempt < 5:
                    retry = exc.headers.get("Retry-After") if exc.headers else None
                    retry = retry or (payload.get("additional_data") or {}).get("retry_after") or 2 ** attempt
                    self._sleep(min(float(retry), 60))
                    continue
                raise NotionError(f"노션 요청이 실패했습니다({exc.code} {payload.get('code', '')}): {payload.get('message', '')}",
                                  exc.code, payload.get("code")) from None
            except urllib.error.URLError as exc:
                if attempt < 2:
                    self._sleep(2 ** attempt)
                    continue
                raise NotionError(f"노션에 연결하지 못했습니다: {exc.reason}") from None
        raise NotionError("노션 요청을 여러 번 다시 보냈지만 실패했습니다.")


def keyring_module() -> Any:
    try:
        import keyring
    except ImportError as exc:
        raise NotionError("토큰 보관에 keyring이 필요합니다. `python -m pip install keyring`(전역 CLI는 "
                          "`pipx inject gongbu-haja keyring`) 후 다시 실행하십시오.") from exc
    return keyring


def load_token() -> str:
    token = keyring_module().get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if not token:
        raise NotionError("저장된 노션 토큰이 없습니다. 사용자가 자기 터미널에서 `gongbu notion login`을 먼저 실행하십시오.")
    return token


def page_id(reference: str) -> str:
    """노션 페이지 링크나 ID에서 페이지 ID를 꺼낸다(링크 끝의 32자리)."""
    found = PAGE_ID_RE.findall(reference.split("?")[0])
    if not found:
        raise NotionError(f"노션 페이지 링크에서 페이지 ID를 찾지 못했습니다: {reference}")
    raw = found[-1].replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


# ------------------------------------------------------------------ 과목 상태

def state_path(course_dir: Path) -> Path:
    return course_dir / STATE_DIR / STATE_NAME


def load_state(course_dir: Path, required: bool = True) -> dict[str, Any]:
    path = state_path(course_dir)
    if not path.is_file():
        if required:
            raise NotionError(f"이 과목은 아직 노션과 연결되지 않았습니다. 과목 폴더에서 `gongbu notion setup <상위 페이지 링크>`를 "
                              f"먼저 실행하십시오. (찾은 위치: {path})")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(course_dir: Path, state: dict[str, Any]) -> None:
    path = state_path(course_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def schema() -> dict[str, Any]:
    return {
        COLUMN_TITLE: {"type": "title", "title": {}},
        COLUMN_SUMMARY: {"type": "rich_text", "rich_text": {}},
        COLUMN_MODE: {"type": "select", "select": {"options": [{"name": label, "color": color}
                                                               for label, _, color in MODES.values()]}},
        COLUMN_HANDOUT: {"type": "rich_text", "rich_text": {}},
        COLUMN_DATE: {"type": "date", "date": {}},
    }


def setup(client: Any, course_dir: Path, parent_reference: str, course: str) -> dict[str, Any]:
    """상위 페이지 아래에 과목 페이지와 "차시별 노트" 표를 만든다. 이미 있으면 그대로 쓴다."""
    parent_id = page_id(parent_reference)
    parent = client.request("GET", f"/pages/{parent_id}")
    if parent.get("public_url"):
        raise NotionError("상위 페이지가 웹에 공개돼 있어 올리지 않습니다. 노션에서 웹 공개를 끈 뒤 다시 실행하십시오.")
    if parent.get("in_trash"):
        raise NotionError("상위 페이지가 휴지통에 있습니다.")
    state = load_state(course_dir, required=False)
    if state.get("parent_page_id") == parent_id and state.get("data_source_id"):
        try:
            existing = client.request("GET", f"/pages/{state['course_page_id']}")
            if not existing.get("in_trash"):
                return state
        except NotionError as exc:
            if exc.status not in (400, 404):
                raise
    course_page = client.request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": parent_id}, "icon": {"type": "emoji", "emoji": "📚"},
        "properties": {"title": {"title": [text_item(course)]}}})
    database = client.request("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": course_page["id"]}, "title": [text_item(TABLE_TITLE)],
        "is_inline": True, "initial_data_source": {"properties": schema()}})
    state = {"version": 1, "course": course, "parent_page_id": parent_id, "course_page_id": course_page["id"],
             "course_page_url": course_page.get("url", ""), "database_id": database["id"],
             "data_source_id": database["data_sources"][0]["id"], "slides": False, "notes": state.get("notes", {})}
    save_state(course_dir, state)
    return state


def note_key(course_dir: Path, note: Path) -> str:
    try:
        return note.resolve().relative_to(course_dir.resolve()).as_posix()
    except ValueError:
        return note.resolve().as_posix()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def properties(title: str, summary: str, mode: str, handout: str, today: str) -> dict[str, Any]:
    return {
        COLUMN_TITLE: {"title": [text_item(title)]},
        COLUMN_SUMMARY: {"rich_text": [text_item(summary)] if summary else []},
        COLUMN_MODE: {"select": {"name": MODES[mode][0]}},
        COLUMN_HANDOUT: {"rich_text": [text_item(handout)] if handout else []},
        COLUMN_DATE: {"date": {"start": today}},
    }


@dataclass
class Plan:
    key: str
    note: Note
    title: str
    summary: str
    mode: str
    handout: str
    content_hash: str
    properties_hash: str
    action: str = "create"
    previous: dict[str, Any] | None = None


def plan_push(client: Any, course_dir: Path, note_path: Path, state: dict[str, Any], *, title: str | None = None,
              summary: str | None = None, mode: str = "faithful", handout: str = "") -> Plan:
    note = convert(note_path.read_text(encoding="utf-8"), state.get("course", ""))
    if note.errors:
        raise NotionError("노션 변환 검사에서 오류가 나 올리지 않았습니다:\n" + "\n".join(note.errors))
    final_title = title or note.title or note_path.stem
    final_summary = note.summary if summary is None else summary
    plan = Plan(note_key(course_dir, note_path), note, final_title, final_summary, mode, handout,
                digest(note.blocks), digest([final_title, final_summary, mode, handout]))
    record = state.get("notes", {}).get(plan.key)
    if record is None:
        return plan
    plan.previous = record
    try:
        page = client.request("GET", f"/pages/{record['page_id']}")
    except NotionError as exc:
        if exc.status in (400, 404):  # 노션에서 지웠거나 연결 권한에서 빠졌다
            return plan
        raise
    if page.get("in_trash") or page.get("archived"):
        return plan
    if page.get("last_edited_time") != record.get("last_edited_time"):
        plan.action = "revision"  # 노션에서 고친 흔적이 있으면 덮어쓰지 않는다
    elif plan.content_hash == record.get("content_sha256"):
        plan.action = "skip" if plan.properties_hash == record.get("properties_sha256") else "properties"
    else:
        plan.action = "replace"
    return plan


def create_row(client: Any, state: dict[str, Any], plan: Plan, title: str, today: str) -> dict[str, Any]:
    """표에 "업로드 중" 줄을 만들고 블록을 나눠 붙인 뒤 제목을 확정한다. 실패하면 만들던 줄을 휴지통으로 보낸다."""
    icon = MODES[plan.mode][1]
    page = client.request("POST", "/pages", {
        "parent": {"type": "data_source_id", "data_source_id": state["data_source_id"]},
        "icon": {"type": "emoji", "emoji": icon},
        "properties": properties(f"업로드 중 · {title}", plan.summary, plan.mode, plan.handout, today)})
    try:
        for chunk in request_chunks(plan.note.blocks):
            client.request("PATCH", f"/blocks/{page['id']}/children", {"children": chunk})
        return client.request("PATCH", f"/pages/{page['id']}", {"properties": {COLUMN_TITLE: {"title": [text_item(title)]}}})
    except Exception:
        try:
            client.request("PATCH", f"/pages/{page['id']}", {"in_trash": True})
        except NotionError:
            pass
        raise


def push(client: Any, course_dir: Path, plan: Plan, state: dict[str, Any], today: str) -> dict[str, Any]:
    """계획대로 올리고 기록을 남긴다. 돌려주는 값은 기록된 노트 항목이다."""
    previous = plan.previous or {}
    if plan.action == "skip":
        return previous
    if plan.action == "properties":
        page = client.request("PATCH", f"/pages/{previous['page_id']}", {
            "properties": properties(previous.get("title", plan.title), plan.summary, plan.mode, plan.handout, today)})
        record = {**previous, "properties_sha256": plan.properties_hash, "last_edited_time": page.get("last_edited_time")}
    else:
        revision = previous.get("revision", 1) + 1 if plan.action == "revision" else 1
        title = f"{plan.title} (수정본 {revision})" if plan.action == "revision" else plan.title
        page = create_row(client, state, plan, title, today)
        if plan.action == "replace":  # 노션에서 손대지 않은 이전 줄만 휴지통으로 보낸다(되살릴 수 있다)
            client.request("PATCH", f"/pages/{previous['page_id']}", {"in_trash": True})
        record = {"page_id": page["id"], "url": page.get("url", ""), "title": title, "revision": revision,
                  "content_sha256": plan.content_hash, "properties_sha256": plan.properties_hash,
                  "last_edited_time": page.get("last_edited_time"), "uploaded_at": today}
    state.setdefault("notes", {})[plan.key] = record
    save_state(course_dir, state)
    return record


# ------------------------------------------------------------------ 명령

ACTION_TEXT = {"create": "새 줄로 올림", "replace": "새 줄로 올리고 이전 줄은 휴지통으로", "revision":
               "노션에서 고친 흔적이 있어 덮어쓰지 않고 (수정본)으로 새 줄", "properties": "표의 속성만 고침", "skip": "바뀐 것 없음"}


def describe(note: Note, title: str, summary: str) -> str:
    tables = sum(block["type"] == "table" for block in note.blocks)
    equations = sum(block["type"] == "equation" for block in note.blocks)
    requests = len(request_chunks(note.blocks)) + 2
    lines = [f"제목: {title}", f"내용: {summary or '(없음)'}",
             f"블록: {len(note.blocks)}개(표 {tables}개, 독립 수식 {equations}개, 전체 요소 {count_elements(note.blocks)}개)",
             f"예상 요청: 약 {requests}회"]
    lines += [f"- {problem}" for problem in dict.fromkeys(note.problems)] or ["문제: 없음"]
    return "\n".join(lines)


def clean_token(raw: str) -> str:
    """붙여 넣을 때 터미널이 덧붙이는 표시(ESC[200~ … ESC[201~)와 공백·제어 글자를 걷어 낸다."""
    text = re.sub(r"\x1b\[20[01]~", "", raw)
    return "".join(char for char in text if char.isprintable() and not char.isspace()).strip("'\"")


def command_login() -> int:
    if not sys.stdin.isatty():
        raise NotionError("login은 사용자가 자기 터미널(PowerShell·명령 프롬프트·터미널 앱)에서 직접 실행합니다. "
                          "에이전트·파이프·Git Bash 입력으로는 토큰을 받지 않습니다.")
    keyring = keyring_module()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)  # 입력이 화면에 보일 상황이면 받지 않는다
        try:
            token = getpass.getpass("노션 API 토큰을 붙여 넣고 Enter(입력 내용은 화면에 보이지 않습니다): ").strip()
        except getpass.GetPassWarning as exc:
            raise NotionError("이 터미널은 입력을 숨길 수 없어 토큰을 받지 않았습니다. PowerShell이나 명령 프롬프트에서 실행하십시오.") from exc
    token = clean_token(token)
    if not token:
        raise NotionError("입력된 글자가 없습니다. 붙여 넣기가 터미널에 전달되지 않은 것 같습니다. 오른쪽 클릭이나 Ctrl+Shift+V로 "
                          "한 번 붙여 넣고 Enter를 누르거나, Windows 시작 메뉴의 PowerShell 창에서 다시 실행하십시오.")
    if len(token) < 20 or not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
        raise NotionError(f"노션 토큰으로 보기 어려운 입력이라 저장하지 않았습니다(받은 글자 {len(token)}자). 연결 화면에서 "
                          "복사 버튼으로 다시 복사해 한 번만 붙여 넣으십시오.")
    try:
        NotionClient(token).request("POST", "/search", {"page_size": 1})
    except NotionError as exc:
        if exc.status == 401:
            raise NotionError("노션이 이 토큰을 받아들이지 않아 저장하지 않았습니다. 연결 화면에서 토큰을 다시 복사하십시오.") from None
        raise
    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, token)
    print("토큰을 확인하고 OS 비밀번호 보관소에 저장했습니다. 노트를 모을 상위 페이지의 `⋯` → 연결에서 이 연결을 추가한 뒤 "
          "과목 폴더에서 `gongbu notion setup <상위 페이지 링크>`를 실행하십시오.")
    return 0


def command_logout() -> int:
    keyring = keyring_module()
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        print("저장된 노션 토큰이 없습니다.")
        return 0
    print("저장된 노션 토큰을 지웠습니다. 유출이 의심되면 노션 개발자 도구의 연결 설정에서 토큰도 재발급하십시오.")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="학습노트 Markdown을 노션 과목 표에 올립니다.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="노션 API 토큰을 OS 비밀번호 보관소에 저장(자기 터미널에서 직접)")
    commands.add_parser("logout", help="저장된 토큰 삭제")
    setup_parser = commands.add_parser("setup", help="과목 페이지와 차시별 노트 표 만들기")
    setup_parser.add_argument("parent", help="노트를 모을 상위 노션 페이지 링크(연결을 추가해 둔 페이지)")
    setup_parser.add_argument("--course", default=None, help="과목 페이지 이름(기본: 과목 폴더 이름)")
    for name in ("check", "push"):
        sub = commands.add_parser(name, help="네트워크 없이 변환·검사" if name == "check" else "표에 노트 한 줄 올리기")
        sub.add_argument("note", type=Path, help="학습노트 Markdown")
        sub.add_argument("--title", default=None, help="표의 줄 제목(기본: 원고의 # 제목에서 과목명을 뺀 것)")
        sub.add_argument("--summary", default=None, help="내용 한 줄(기본: 원고의 차시 제목을 이은 것)")
        sub.add_argument("--handout", default="", help="교안 범위(예: 교안 01·02)")
        sub.add_argument("--mode", choices=tuple(MODES), default="faithful")
        if name == "push":
            sub.add_argument("--dry-run", action="store_true", help="올리지 않고 할 일만 보여 준다")
    for sub in commands.choices.values():
        sub.add_argument("--course-dir", type=Path, default=Path.cwd(), help="과목 폴더(기본: 현재 폴더)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        if args.command == "login":
            return command_login()
        if args.command == "logout":
            return command_logout()
        course_dir = args.course_dir.expanduser().resolve()
        if args.command == "check":
            note = convert(args.note.read_text(encoding="utf-8"), load_state(course_dir, required=False).get("course", ""))
            print(describe(note, args.title or note.title or args.note.stem, note.summary if args.summary is None else args.summary))
            return 1 if note.errors else 0
        client = NotionClient(load_token())
        if args.command == "setup":
            state = setup(client, course_dir, args.parent, args.course or course_dir.name)
            print(f"과목 페이지: {state['course_page_url']}\n표: {TABLE_TITLE}\n기록: {state_path(course_dir)}")
            return 0
        state = load_state(course_dir)
        plan = plan_push(client, course_dir, args.note, state, title=args.title, summary=args.summary,
                         mode=args.mode, handout=args.handout)
        print(f"위치: {state['course']} › {TABLE_TITLE}\n할 일: {ACTION_TEXT[plan.action]}\n"
              + describe(plan.note, plan.title, plan.summary))
        if args.dry_run or plan.action == "skip":
            return 0
        record = push(client, course_dir, plan, state, dt.date.today().isoformat())
        print(f"올림: {record.get('url', '')}")
        return 0
    except (NotionError, HandoffMemoError, OSError, ValueError, KeyError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
