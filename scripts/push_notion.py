#!/usr/bin/env python3
"""학습노트를 노션 과목 페이지 아래의 차시 페이지로 올린다(공부하자 › 학기 › 과목 › 차시).

    python scripts/push_notion.py login                        # 사용자가 자기 터미널에서 직접 실행
    python scripts/push_notion.py logout
    python scripts/push_notion.py setup [<상위 페이지 링크>] --course <과목명> [--new-page]
    python scripts/push_notion.py check <노트.md>             # 네트워크 없이 변환·검사
    python scripts/push_notion.py push <노트.md> [--dry-run] [--overwrite] [--no-dashboard]
    python scripts/push_notion.py rekey <예전 원고> <새 원고>   # 네트워크 없이 노트 기록만 옮김
    python scripts/push_notion.py semester create|move [--root <링크>] [--semester ID] [--dry-run]
    python scripts/push_notion.py dashboard [--semester ID] [--preview]

학기 페이지와 현황판은 `notion_dashboard.py`가 맡고, 이 파일은 필요한 함수 안에서만 그 모듈을 읽는다.

과목 폴더에서는 `gongbu notion ...`으로 부른다. 변환과 업로드는 Python이 하므로 AI 토큰이 들지 않고
옮기다 내용이 빠지지 않는다. 노션 통신은 표준 라이브러리만 쓰고, 토큰은 선택 설치 keyring으로 OS
비밀번호 보관소(Windows 자격 증명 관리자, macOS 키체인)에만 둔다. 토큰을 파일·환경 변수·로그·명령
인자로 받거나 남기지 않으며, login은 입력이 화면에 보이지 않는 터미널에서만 동작한다.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import warnings
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from .build_study_note_pdf import HandoffMemoError, public_text
    from .tex_to_notion import convert_tex, input_paths
except ImportError:  # `python scripts/push_notion.py`로 직접 실행할 때
    from build_study_note_pdf import HandoffMemoError, public_text
    from tex_to_notion import convert_tex, input_paths

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
DASHBOARD_VERSION = "2026-03-11"  # 위치(position)를 준 블록 덧붙이기와 페이지 옮기기(move)에만 쓴다
KEYRING_SERVICE = "gongbu-haja"
KEYRING_ACCOUNT = "notion"
STATE_DIR = ".gongbu"
STATE_NAME = "notion.json"
STATE_VERSION = 2  # 1은 과목 페이지 안의 "차시별 노트" 표에 줄로 올리던 방식
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
    """`과목 1주차 — 주제`를 차시 페이지 제목 `1주차 · 주제`로 줄인다."""
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

    def send_file(self, upload_id: str, filename: str, data: bytes, content_type: str) -> dict[str, Any]:
        """만들어 둔 파일 업로드 자리에 파일 내용을 multipart로 보낸다."""
        boundary = f"gongbu-{uuid.uuid4().hex}"
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
        return self.request("POST", f"/file_uploads/{upload_id}/send", raw=head + data + f"\r\n--{boundary}--\r\n".encode(),
                            content_type=f"multipart/form-data; boundary={boundary}")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, raw: bytes | None = None,
                content_type: str = "application/json", version: str | None = None) -> dict[str, Any]:
        """version을 주면 그 요청만 다른 Notion-Version으로 보낸다(학기 현황판의 위치 지정·페이지 옮기기)."""
        data = raw if raw is not None else None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(6):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:  # 무료 요금제 한도(초당 평균 3회)
                self._sleep(wait)
            request = urllib.request.Request(API_URL + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Notion-Version": version or NOTION_VERSION,
                "Content-Type": content_type})
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


# ------------------------------------------------------------------ 교안 슬라이드 그림

SLIDE_PREFIX = "slide:"


def slide_numbers(items: list[dict[str, Any]]) -> list[int]:
    found: list[int] = []
    for block in items:
        body = block[block["type"]]
        if block["type"] == "image" and str(body.get("file_upload", {}).get("id", "")).startswith(SLIDE_PREFIX):
            found.append(int(body["file_upload"]["id"][len(SLIDE_PREFIX):]))
        found.extend(slide_numbers(body.get("children", [])))
    return found


def without_slides(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """슬라이드 업로드를 끈 과목은 그림 자리에 캡션 글자(원본 PDF p.N)만 남긴다."""
    out = []
    for block in items:
        body = block[block["type"]]
        if block["type"] == "image" and str(body.get("file_upload", {}).get("id", "")).startswith(SLIDE_PREFIX):
            caption = "".join(item["text"]["content"] for item in body.get("caption", []))
            out.append({"type": "paragraph", "paragraph": {"rich_text": [text_item(caption, italic=True, color="gray")]}})
            continue
        if body.get("children"):
            block = {**block, block["type"]: {**body, "children": without_slides(body["children"])}}
        out.append(block)
    return out


def with_uploads(items: list[dict[str, Any]], uploads: dict[int, str]) -> list[dict[str, Any]]:
    out = []
    for block in items:
        body = block[block["type"]]
        if block["type"] == "image" and str(body.get("file_upload", {}).get("id", "")).startswith(SLIDE_PREFIX):
            number = int(body["file_upload"]["id"][len(SLIDE_PREFIX):])
            block = {**block, "image": {**body, "file_upload": {"id": uploads[number]}}}
        elif body.get("children"):
            block = {**block, block["type"]: {**body, "children": with_uploads(body["children"], uploads)}}
        out.append(block)
    return out


SLIDE_WIDTH_PX = 1600  # 고해상도 화면에서도 노션 본문 폭에 글자가 또렷한 가로 픽셀 수


def render_slide(pdf: Path, number: int) -> tuple[bytes, str]:
    """교안 PDF 한 쪽을 판형과 상관없이 가로 1600px로 그리고 1px 회색 테두리를 두른다.

    작은 Beamer 판형(453.5pt)도 같은 폭이 되게 배율에 상한을 두지 않는다. 무료 요금제 한도(파일당 5MiB) 안으로 줄인다.
    """
    try:
        import pypdfium2 as pdfium
        from PIL import ImageOps
    except ImportError as exc:
        raise NotionError("교안 슬라이드 그림을 올리려면 pypdfium2와 Pillow가 필요합니다. "
                          "`python -m pip install pypdfium2 Pillow` 후 다시 실행하십시오.") from exc
    document = pdfium.PdfDocument(str(pdf))
    try:
        if not 1 <= number <= len(document):
            raise NotionError(f"교안 PDF에 {number}쪽이 없습니다: {pdf}")
        page = document[number - 1]
        image = page.render(scale=SLIDE_WIDTH_PX / page.get_width()).to_pil().convert("RGB")
        page.close()
    finally:
        document.close()
    image = ImageOps.expand(image, border=1, fill=(208, 208, 208))
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    if buffer.tell() <= 4_500_000:
        return buffer.getvalue(), "image/png"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue(), "image/jpeg"


def upload_slides(client: Any, pdf: Path, numbers: list[int]) -> dict[int, str]:
    """쪽마다 그림을 만들어 노션 파일 업로드에 올리고 {쪽: 업로드 ID}를 돌려준다."""
    uploads: dict[int, str] = {}
    for number in dict.fromkeys(numbers):
        data, content_type = render_slide(pdf, number)
        filename = f"slide-p{number:03d}.{'png' if content_type == 'image/png' else 'jpg'}"
        created = client.request("POST", "/file_uploads", {"filename": filename, "content_type": content_type})
        client.send_file(created["id"], filename, data, content_type)
        uploads[number] = created["id"]
    return uploads


def file_sha256(path: Path) -> str:
    digest_ = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest_.update(chunk)
    return digest_.hexdigest()


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


def id_key(value: Any) -> str:
    """노션 ID 비교용 형태(대시 없이 소문자). 노션은 같은 ID를 대시가 있게도 없게도 돌려준다."""
    return str(value or "").replace("-", "").lower()


def parent_of(page: dict[str, Any]) -> str | None:
    """페이지의 실제 상위 페이지 ID. 상위가 페이지가 아니면(작업 공간 등) None."""
    parent = page.get("parent") or {}
    return parent.get("page_id") if parent.get("type") == "page_id" else None


BLOCK_PARENT_HOPS = 20  # 단·토글처럼 페이지를 감싼 블록을 거슬러 올라가는 최대 횟수


def page_parent(client: Any, page: dict[str, Any]) -> str | None:
    """페이지를 품은 상위 페이지 ID. 단(column)·토글 같은 블록 안에 있으면 블록 부모를 따라 페이지까지 올라간다.

    상위가 작업 공간 등 페이지가 아니거나, 블록을 읽을 수 없거나(연결 권한 밖), 너무 깊으면 None. 공개 여부 확인에만 쓴다.
    """
    parent = page.get("parent") or {}
    for _ in range(BLOCK_PARENT_HOPS):
        kind = parent.get("type")
        if kind == "page_id":
            return parent.get("page_id")
        if kind != "block_id" or not parent.get("block_id"):
            return None
        try:
            parent = client.request("GET", f"/blocks/{parent['block_id']}").get("parent") or {}
        except NotionError as exc:
            if exc.status in (400, 403, 404):
                return None
            raise
    return None


PUBLIC_HELP = "공개된 페이지 아래라 올리지 않습니다. 노션에서 웹 공개를 끈 뒤 다시 실행하십시오."
_PRIVATE_SEEN: weakref.WeakKeyDictionary[Any, dict[str, str | None]] = weakref.WeakKeyDictionary()


def ensure_private(client: Any, target: str, *, page: dict[str, Any] | None = None, levels: int = 3,
                   message: str = PUBLIC_HELP) -> None:
    """페이지와 그 위로 levels단계까지의 상위 페이지가 웹에 공개돼 있지 않은지 본다.

    공개된 페이지 아래에 쓰면 노트·현황판이 웹에 그대로 보이므로 거부한다. 이미 읽은 페이지(page)는 다시 읽지 않고,
    확인을 마친 페이지는 이 프로세스에서 같은 클라이언트로 다시 읽지 않는다. 연결 권한 밖의 상위 페이지는 읽을 수 없어 거기서 멈춘다.
    단·토글 안에 놓인 페이지는 그 블록을 품은 페이지로 올라간다(`page_parent`). levels는 페이지 단계만 센다.
    """
    try:
        seen = _PRIVATE_SEEN.setdefault(client, {})
    except TypeError:  # 약한 참조를 걸 수 없는 클라이언트는 기억하지 않는다
        seen = {}
    current, found = target, page
    for level in range(levels + 1):
        if not current:
            return
        key = id_key(current)
        if found is None and key in seen:
            current = seen[key]
            continue
        if found is None:
            try:
                found = client.request("GET", f"/pages/{current}")
            except NotionError as exc:
                if level and exc.status in (400, 403, 404):
                    return
                raise
        if found.get("public_url"):
            raise NotionError(message)
        seen[key] = page_parent(client, found)
        current, found = seen[key], None


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


LOCK_WAIT_SECONDS = 30.0
LOCK_STALE_SECONDS = 600.0  # 이보다 오래된 잠금은 끝나지 못한 프로세스가 남긴 것으로 보고 치운다


@contextlib.contextmanager
def file_lock(lock: Path, wait: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    """기록 파일을 읽고-고치고-쓰는 동안 다른 프로세스(예약 실행과 대화 실행)가 끼어들지 못하게 한다."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise NotionError(f"다른 작업이 기록 파일을 쓰는 중입니다. 잠시 뒤 다시 실행하십시오. (잠금: {lock})") from None
            time.sleep(0.2)
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def update_state(course_dir: Path, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """잠금 아래에서 디스크의 기록을 다시 읽고 바꾼 부분만 반영해 저장한다. 다른 노트의 기록을 덮지 않는다."""
    with file_lock(state_path(course_dir).with_suffix(".lock")):
        state = load_state(course_dir, required=False)
        mutate(state)
        save_state(course_dir, state)
        return state


def save_note_record(course_dir: Path, state: dict[str, Any], key: str, record: dict[str, Any]) -> None:
    """노트 기록 하나를 저장한다. 메모리의 state도 같이 맞춘다."""
    state.setdefault("notes", {})[key] = record
    update_state(course_dir, lambda disk: disk.setdefault("notes", {}).__setitem__(key, record))


MOVED_ELSEWHERE_HELP = ("이 과목은 이미 다른 페이지 아래에 과목 페이지가 있습니다. 학기 아래로 옮기려면 "
                        "gongbu notion semester move, 정말 새 과목 페이지를 만들려면 --new-page.")


def dashboard_module() -> Any:
    """학기 페이지·현황판 모듈. 서로 import하는 순환을 피하려고 필요한 함수 안에서만 읽는다."""
    try:
        from . import notion_dashboard
    except ImportError:  # gongbu가 scripts/를 sys.path에 넣고 이 파일을 직접 실행할 때
        import notion_dashboard
    return notion_dashboard


def semester_page(course_dir: Path, quiet: bool = True) -> str | None:
    """이 과목 폴더가 속한 학기의 노션 페이지 ID(학기 등록부). 학기나 학기 페이지가 없으면 None.

    quiet이면 등록부를 읽지 못해도 None으로 넘어간다(링크를 준 setup이 등록부 문제로 막히지 않게 한다).
    """
    try:
        return dashboard_module().semester_page_of(course_dir)
    except NotionError:
        if quiet:
            return None
        raise
    except Exception as exc:
        if quiet:
            return None
        raise NotionError(f"학기 등록부를 읽지 못했습니다: {exc}") from exc


def setup(client: Any, course_dir: Path, parent_reference: str | None, course: str, slides: bool | None = None,
          new_page: bool = False) -> dict[str, Any]:
    """상위 페이지 아래에 과목 페이지를 만든다. 이미 있으면 그대로 쓴다. 차시 노트는 이 과목 페이지의 하위 페이지가 된다.

    상위 페이지 링크를 생략하면 이 과목이 속한 학기 페이지를 쓴다. 과목 페이지가 살아 있으면 실제 상위 페이지가 준 링크나
    학기 페이지일 때만 그대로 쓰고(사이드바에서 옮긴 경우 포함), 다른 곳에 있으면 new_page가 아닌 한 거부한다.
    slides는 교안 슬라이드 그림을 노션에 올릴지다. None이면 기존 과목은 그대로 두고 새 과목은 올린다.
    웹에 공개된 상위 페이지는 거부하므로 그림은 비공개 페이지에만 올라간다.
    예전 방식(과목 페이지 안의 "차시별 노트" 표)으로 연결한 과목은 과목 페이지를 그대로 쓰고 표 안 줄의 기록만 내려놓는다.
    """
    semester = semester_page(course_dir, quiet=bool(parent_reference))
    if parent_reference:
        parent_id = page_id(parent_reference)
    elif semester:
        parent_id = semester
    else:
        raise NotionError("상위 페이지 링크가 필요합니다. 이 과목은 노션 학기 페이지가 있는 학기에 들어 있지 않습니다. "
                          "`gongbu notion setup <상위 페이지 링크>`로 실행하십시오.")
    parent = client.request("GET", f"/pages/{parent_id}")
    ensure_private(client, parent_id, page=parent,
                   message="상위 페이지나 그 위 페이지가 웹에 공개돼 있어 올리지 않습니다. 노션에서 웹 공개를 끈 뒤 다시 실행하십시오.")
    if parent.get("in_trash"):
        raise NotionError("상위 페이지가 휴지통에 있습니다.")
    state = load_state(course_dir, required=False)
    if state.get("course_page_id"):
        try:
            current = client.request("GET", f"/pages/{state['course_page_id']}")
            alive = not current.get("in_trash")
        except NotionError as exc:
            if exc.status not in (400, 404):
                raise
            current, alive = {}, False
        actual = parent_of(current)
        allowed = {id_key(parent_id), id_key(semester)}
        if "data_source_id" in state:  # 예전 표 방식은 링크 없이도 원래 상위 페이지에서 그대로 바꾼다(그다음 semester move)
            allowed.add(id_key(state.get("parent_page_id")))
        if alive and actual and id_key(actual) in allowed - {""}:
            updates: dict[str, Any] = {}
            if id_key(state.get("parent_page_id")) != id_key(actual):  # 사이드바나 semester move로 옮긴 과목 페이지
                updates["parent_page_id"] = actual
            if slides is not None and state.get("slides") != slides:
                updates["slides"] = slides
            if "data_source_id" in state:  # 표의 줄은 하위 페이지가 아니므로 기록을 비우고, 다시 올리면 하위 페이지로 들어간다
                state = {key: value for key, value in state.items() if key not in ("database_id", "data_source_id")}
                state.update(version=STATE_VERSION, notes={}, **updates)
                final = state
                update_state(course_dir, lambda disk: (disk.clear(), disk.update(final)))
            elif updates:  # 다른 노트의 기록을 덮지 않게 바뀐 칸만 쓴다
                state.update(updates)
                update_state(course_dir, lambda disk: disk.update(updates))
            return state
        if alive and not new_page:
            raise NotionError(MOVED_ELSEWHERE_HELP)
    course_page = client.request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": parent_id}, "icon": {"type": "emoji", "emoji": "📚"},
        "properties": {"title": {"title": [text_item(course)]}}})
    state = {"version": STATE_VERSION, "course": course, "parent_page_id": parent_id, "course_page_id": course_page["id"],
             "course_page_url": course_page.get("url", ""), "slides": True if slides is None else slides, "notes": {}}
    created = state
    update_state(course_dir, lambda disk: (disk.clear(), disk.update(created)))
    return state


def note_key(course_dir: Path, note: Path) -> str:
    try:
        return note.resolve().relative_to(course_dir.resolve()).as_posix()
    except ValueError:
        return note.resolve().as_posix()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def header_block(mode: str, handout: str, summary: str) -> dict[str, Any]:
    """페이지 맨 위의 회색 한 줄(모드 · 교안 범위 · 내용)."""
    text = " · ".join(part for part in (MODES[mode][0], handout, summary) if part)
    return {"type": "paragraph", "paragraph": {"rich_text": [text_item(text)], "color": "gray"}}


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
    slide_pdf: Path | None = None
    slides: list[int] = field(default_factory=list)
    local_changed: bool = False  # 노션에서 고친 흔적이 있을 때, 원고도 마지막으로 올린 뒤 바뀌었는지
    seen_time: str | None = None  # 수정 시각은 바뀌었지만 빈 문단만 생긴 페이지의 지금 수정 시각
    remote_time: str | None = None  # 노션 페이지를 읽었을 때의 지금 수정 시각


# ------------------------------------------------------------------ 노션에서 고쳤는지 비교

def run_text(items: list[dict[str, Any]]) -> tuple[str, str]:
    """rich text를 (글자, 글자+서식) 두 줄로. 같은 서식의 조각은 이어 붙여 노션이 조각을 나누는 방식과 상관없게 한다."""
    runs: list[list[str]] = []
    for item in items:
        if "plain_text" in item:
            text = item["plain_text"]
        elif item.get("type") == "equation":
            text = item["equation"]["expression"]
        else:
            text = item.get("text", {}).get("content", "")
        annotations = item.get("annotations", {})
        flags = [name for name in ("bold", "italic", "strikethrough", "underline", "code") if annotations.get(name)]
        if annotations.get("color", "default") != "default":
            flags.append(annotations["color"])
        if item.get("type") == "equation":
            flags.append("equation")
        link = item.get("href") or (item.get("text", {}).get("link") or {}).get("url")
        if link:
            flags.append(link)
        mark = ",".join(flags)
        if runs and runs[-1][1] == mark:
            runs[-1][0] += text
        else:
            runs.append([text, mark])
    return "".join(text for text, _ in runs), "".join(text + (f"⟦{mark}⟧" if mark else "") for text, mark in runs)


def outline_line(kind: str, body: dict[str, Any], has_children: bool) -> str | None:
    """블록 하나를 비교할 한 줄로. 빈 문단은 None이다(노션에서 빈 곳을 누르면 생겨 고친 것으로 보지 않는다)."""
    if kind == "equation":
        plain = marked = body.get("expression", "")
    elif kind == "image":
        plain, marked = run_text(body.get("caption", []))
    elif kind == "table_row":
        pairs = [run_text(cell) for cell in body.get("cells", [])]
        plain, marked = " | ".join(p for p, _ in pairs), " | ".join(m for _, m in pairs)
    else:
        plain, marked = run_text(body.get("rich_text", []))
        if kind == "to_do":
            marked = ("[x] " if body.get("checked") else "[ ] ") + marked
    if kind == "paragraph" and not plain.strip() and not has_children:
        return None
    return f"{kind}({body.get('color', 'default')}):{marked}"


def outline(items: list[dict[str, Any]], depth: int = 0) -> list[str]:
    """보낼 블록의 비교용 줄 목록."""
    lines: list[str] = []
    for block in items:
        body = block[block["type"]]
        line = outline_line(block["type"], body, bool(body.get("children")))
        if line is not None:
            lines.append(f"{depth}|{line}")
        lines.extend(outline(body.get("children", []), depth + 1))
    return lines


def remote_outline(client: Any, block_id: str, depth: int = 0) -> list[str]:
    """노션에 있는 페이지의 비교용 줄 목록. 하위 블록이 있는 블록은 한 번씩 더 읽는다."""
    lines: list[str] = []
    for block in children(client, block_id):
        line = outline_line(block["type"], block.get(block["type"], {}), bool(block.get("has_children")))
        if line is not None:
            lines.append(f"{depth}|{line}")
        if block.get("has_children"):
            lines.extend(remote_outline(client, block["id"], depth + 1))
    return lines


def page_items(plan: Plan) -> list[dict[str, Any]]:
    return [header_block(plan.mode, plan.handout, plan.summary)] + plan.note.blocks


def untouched(client: Any, plan: Plan, record: dict[str, Any]) -> bool:
    """노션 페이지의 글자·구조·서식이 마지막으로 올린 그대로인지. 빈 문단이 생긴 것은 고친 것으로 보지 않는다."""
    expected = record.get("outline_sha256")
    if expected is None:  # 비교 기록이 없는 예전 기록: 원고가 그대로일 때만 지금 변환 결과가 올린 내용이다
        if (plan.content_hash, plan.properties_hash) != (record.get("content_sha256"), record.get("properties_sha256")):
            return False
        expected = digest(outline(page_items(plan)))
    return digest(remote_outline(client, record["page_id"])) == expected


def read_note(note_path: Path, course: str, slides_on: bool) -> tuple[Note, str, Path | None, list[int]]:
    """원고를 노션 블록으로 옮긴다. `.tex`는 심화 이해형 원고, 그 밖은 자료 충실형 Markdown으로 본다."""
    if note_path.suffix.lower() != ".tex":
        return convert(note_path.read_text(encoding="utf-8"), course), "faithful", None, []
    tex = convert_tex(note_path)
    note = Note(tex.title or note_path.stem, tex.summary, tex.blocks, list(tex.problems))
    numbers = sorted(set(tex.slides))
    if not slides_on:
        note.blocks = without_slides(note.blocks)
    elif numbers and (tex.slide_pdf is None or not tex.slide_pdf.is_file()):
        note.problems.append(f"오류: 슬라이드 그림을 만들 교안 PDF를 찾지 못했습니다: {tex.slide_pdf}")
    return note, "deep", tex.slide_pdf, numbers


@dataclass
class Prepared:
    """원고를 네트워크 없이 변환한 결과. 올릴지 판단하는 해시는 `push`와 `gongbu status`가 이것 하나로 같이 계산한다."""
    key: str
    note: Note
    mode: str
    slide_pdf: Path | None
    numbers: list[int]
    slides_on: bool
    content_hash: str | None  # 변환 오류가 있으면 None


def prepare_note(course_dir: Path, note_path: Path, state: dict[str, Any]) -> Prepared:
    """원고를 노션 블록으로 바꾸고 내용 해시를 낸다. 변환이 아예 안 되는 원고는 ValueError 등 예외를 그대로 낸다."""
    slides_on = bool(state.get("slides"))
    note, default_mode, slide_pdf, numbers = read_note(note_path, state.get("course", ""), slides_on)
    content_hash = None
    if not note.errors:
        pdf_hash = file_sha256(slide_pdf) if slides_on and numbers and slide_pdf else None
        content_hash = digest([note.blocks, pdf_hash])
    return Prepared(note_key(course_dir, note_path), note, default_mode, slide_pdf, numbers, slides_on, content_hash)


def note_dependencies(note_path: Path) -> list[Path]:
    """원고와, 변환할 때 함께 읽는 본문 조각 파일들(TeX `\\input`)."""
    if note_path.suffix.lower() != ".tex":
        return [note_path]
    return [note_path, *input_paths(note_path)]


def block_text(block: dict[str, Any]) -> str:
    """블록 하나의 글자(서식 없이). 수식·표 칸·그림 캡션 포함."""
    body = block.get(block["type"], {})
    parts = [run_text(body.get("rich_text", []))[0], run_text(body.get("caption", []))[0]]
    parts += [run_text(cell)[0] for cell in body.get("cells", [])]
    if block["type"] == "equation":
        parts.append(body.get("expression", ""))
    return " ".join(part for part in parts if part)


def uncertain_markers(items: list[dict[str, Any]]) -> list[str]:
    """노트 블록에 남은 불확실성 표시(`[확인 필요: …]`, `[전사 불명확]` 등)를 차례대로 모은다."""
    found: list[str] = []
    for block in items:
        found += UNCERTAIN_RE.findall(block_text(block))
        found += uncertain_markers(block.get(block["type"], {}).get("children", []))
    return found


def plan_push(client: Any, course_dir: Path, note_path: Path, state: dict[str, Any], *, title: str | None = None,
              summary: str | None = None, mode: str | None = None, handout: str = "", overwrite: bool = False) -> Plan:
    if "data_source_id" in state:  # 표의 줄을 차시 페이지로 착각해 고치지 않게 한다
        raise NotionError("예전 방식(과목 페이지 안의 표)으로 연결된 과목입니다. 과목 폴더에서 `gongbu notion setup <상위 페이지 링크>`를 "
                          "한 번 다시 실행하면 차시 페이지 방식으로 바뀝니다.")
    prepared = prepare_note(course_dir, note_path, state)
    note, numbers, slides_on = prepared.note, prepared.numbers, prepared.slides_on
    if note.errors:
        raise NotionError("노션 변환 검사에서 오류가 나 올리지 않았습니다:\n" + "\n".join(note.errors))
    final_title = title or note.title or note_path.stem
    final_summary = note.summary if summary is None else summary
    final_mode = mode or prepared.mode
    final_handout = handout or (f"교안 p.{numbers[0]}–{numbers[-1]}" if numbers else "")
    plan = Plan(prepared.key, note, final_title, final_summary, final_mode, final_handout,
                prepared.content_hash or "", digest([final_title, final_summary, final_mode, final_handout]),
                slide_pdf=prepared.slide_pdf if slides_on else None, slides=numbers if slides_on else [])
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
    plan.remote_time = page.get("last_edited_time")
    edited = page.get("last_edited_time") != record.get("last_edited_time")
    if edited and not overwrite and untouched(client, plan, record):
        edited = False  # 빈 문단만 생겼다(노션에서 빈 곳을 누름)
        plan.seen_time = page.get("last_edited_time")
    if edited:
        # 노션에서 고쳤다: 수정이 두 페이지로 갈라지지 않게 새 페이지를 만들지 않고 멈춘다.
        # 고친 내용을 원고에 반영한 뒤(또는 버리기로 한 뒤) overwrite로 같은 페이지를 원고 내용으로 바꾼다.
        plan.local_changed = plan.content_hash != record.get("content_sha256")
        plan.action = "replace" if overwrite else "edited"
    elif plan.content_hash == record.get("content_sha256"):
        plan.action = "skip" if plan.properties_hash == record.get("properties_sha256") else "properties"
    else:
        plan.action = "replace"
    return plan


def children(client: Any, block_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    cursor = ""
    while True:
        page = client.request("GET", f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
        found.extend(page.get("results", []))
        if not page.get("has_more"):
            return found
        cursor = page["next_cursor"]


def page_blocks(client: Any, plan: Plan) -> list[dict[str, Any]]:
    """머리 줄과 본문. 슬라이드 그림을 먼저 올려 두어 중간에 실패해도 반쯤 바뀐 페이지가 덜 남게 한다."""
    body = plan.note.blocks
    if plan.slides and plan.slide_pdf:
        body = with_uploads(body, upload_slides(client, plan.slide_pdf, plan.slides))
    return [header_block(plan.mode, plan.handout, plan.summary)] + body


def append_blocks(client: Any, target: str, items: list[dict[str, Any]]) -> None:
    for chunk in request_chunks(items):
        client.request("PATCH", f"/blocks/{target}/children", {"children": chunk})


def finish_page(client: Any, target: str, plan: Plan, title: str) -> dict[str, Any]:
    return client.request("PATCH", f"/pages/{target}", {"icon": {"type": "emoji", "emoji": MODES[plan.mode][1]},
                                                         "properties": {"title": {"title": [text_item(title)]}}})


def create_page(client: Any, state: dict[str, Any], plan: Plan, title: str) -> dict[str, Any]:
    """과목 페이지 맨 아래에 "업로드 중" 하위 페이지를 만들고 블록을 붙인 뒤 제목을 확정한다. 실패하면 그 페이지를 휴지통으로 보낸다."""
    items = page_blocks(client, plan)
    page = client.request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": state["course_page_id"]},
        "icon": {"type": "emoji", "emoji": MODES[plan.mode][1]},
        "properties": {"title": {"title": [text_item(f"업로드 중 · {title}")]}}})
    try:
        append_blocks(client, page["id"], items)
        return finish_page(client, page["id"], plan, title)
    except Exception:
        try:
            client.request("PATCH", f"/pages/{page['id']}", {"in_trash": True})
        except NotionError:
            pass
        raise


def replace_page(client: Any, plan: Plan, target: str, title: str) -> dict[str, Any]:
    """같은 페이지의 내용만 바꾼다. 과목 페이지 안의 자리와 링크가 그대로다. 새 블록을 다 붙인 뒤 예전 블록을 지운다."""
    items = page_blocks(client, plan)
    old = [block["id"] for block in children(client, target)]
    client.request("PATCH", f"/pages/{target}", {"properties": {"title": {"title": [text_item(f"업로드 중 · {title}")]}}})
    append_blocks(client, target, items)
    for block_id in old:
        client.request("DELETE", f"/blocks/{block_id}")
    return finish_page(client, target, plan, title)


def without_edit_mark(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "notion_edited_at"}


def mark_notion_edit(course_dir: Path, state: dict[str, Any], plan: Plan) -> None:
    """노션에서 고친 흔적을 노트 기록의 notion_edited_at에 남긴다(로컬만). 현황판과 `gongbu status`가 이것을 본다."""
    edited_at = plan.remote_time
    if plan.action != "edited" or not edited_at or (plan.previous or {}).get("notion_edited_at") == edited_at:
        return

    def mark(disk: dict[str, Any]) -> None:
        record = disk.get("notes", {}).get(plan.key)
        if record is not None:
            record["notion_edited_at"] = edited_at

    update_state(course_dir, mark)
    if plan.previous is not None:
        plan.previous["notion_edited_at"] = edited_at
        state.setdefault("notes", {})[plan.key] = plan.previous


def push(client: Any, course_dir: Path, plan: Plan, state: dict[str, Any], today: str) -> dict[str, Any]:
    """계획대로 올리고 기록을 남긴다. 돌려주는 값은 기록된 노트 항목이다."""
    previous = plan.previous or {}
    if plan.action == "skip":
        if plan.seen_time:  # 빈 문단만 생긴 페이지: 다음에 다시 비교하지 않게 지금 수정 시각을 기록한다
            previous = {**without_edit_mark(previous), "last_edited_time": plan.seen_time}
            save_note_record(course_dir, state, plan.key, previous)
        return previous
    if plan.action == "edited":
        mark_notion_edit(course_dir, state, plan)
        raise NotionError(EDITED_HELP)
    if state.get("course_page_id"):  # 쓰기 전에: 과목 페이지와 그 위가 웹에 공개돼 있으면 올리지 않는다
        ensure_private(client, state["course_page_id"])
    title = plan.title
    if plan.action == "properties":
        first = client.request("GET", f"/blocks/{previous['page_id']}/children?page_size=1").get("results", [])
        if first and first[0]["type"] == "paragraph":
            client.request("PATCH", f"/blocks/{first[0]['id']}",
                           {"paragraph": header_block(plan.mode, plan.handout, plan.summary)["paragraph"]})
        page = finish_page(client, previous["page_id"], plan, title)
    elif plan.action == "replace":
        try:
            page = replace_page(client, plan, previous["page_id"], title)
        except Exception:
            # 이번에 바뀐 수정 시각을 다음번에 사용자의 수정으로 보지 않게 맞추고, 내용은 다음에 다시 올리게 한다
            try:
                current = client.request("GET", f"/pages/{previous['page_id']}")
                save_note_record(course_dir, state, plan.key, {**without_edit_mark(previous), "content_sha256": None,
                                                               "last_edited_time": current.get("last_edited_time")})
            except NotionError:
                pass
            raise
    else:  # create: 과목 페이지 맨 아래에 새 하위 페이지
        page = create_page(client, state, plan, title)
    record = {"page_id": page["id"], "url": page.get("url", ""), "title": title,
              "content_sha256": plan.content_hash, "properties_sha256": plan.properties_hash,
              "outline_sha256": digest(outline(page_items(plan))),  # 노션에서 고쳤는지 볼 때 비교할 올린 내용
              "last_edited_time": page.get("last_edited_time"), "uploaded_at": today}
    save_note_record(course_dir, state, plan.key, record)
    return record


# ------------------------------------------------------------------ 명령

ACTION_TEXT = {"create": "과목 페이지 맨 아래에 새 페이지로 올림", "replace": "같은 페이지의 내용을 새로 바꿈(자리·링크 그대로)",
               "edited": "노션에서 고친 흔적이 있어 멈춤", "properties": "제목과 머리 줄만 고침", "skip": "바뀐 것 없음"}
EDITED_HELP = ("노션에서 이 페이지를 고친 흔적이 있습니다. 수정이 두 페이지로 갈라지지 않게 새 페이지를 만들지 않습니다. "
               "노션에서 고친 내용을 원고에 반영한 뒤 `--overwrite`로 다시 올리면 같은 페이지가 원고 내용으로 바뀝니다. "
               "노션의 수정을 버릴 때도 `--overwrite`를 씁니다.")


def rekey(course_dir: Path, old: Path, new: Path) -> tuple[str, str]:
    """원고 파일 이름이 바뀌었을 때 노트 기록을 새 이름으로 옮긴다. 다음 push는 같은 노션 페이지를 이어 쓴다.

    네트워크와 토큰을 쓰지 않는다. 경로는 과목 폴더 기준이거나 절대 경로다. 새 원고가 노션 블록으로 바뀌지 않으면 옮기지 않는다.
    """
    state = load_state(course_dir)
    old_path, new_path = (path if path.is_absolute() else course_dir / path for path in (old, new))
    old_key, new_key = note_key(course_dir, old_path), note_key(course_dir, new_path)

    def check(notes: dict[str, Any]) -> None:
        if old_key not in notes:
            raise NotionError(f"옮길 노트 기록이 없습니다: {old_key}")
        if new_key in notes:
            raise NotionError(f"새 원고에 이미 노트 기록이 있습니다: {new_key}")

    check(state.get("notes", {}))
    if not new_path.is_file():
        raise NotionError(f"새 원고 파일이 없습니다: {new_path}")
    try:
        prepared = prepare_note(course_dir, new_path, state)
    except Exception as exc:  # 변환 자체가 안 되는 원고(닫히지 않은 수식 등)
        raise NotionError(f"변환 오류가 있어 옮기지 않았습니다: {exc}") from exc
    if prepared.content_hash is None:
        raise NotionError("변환 오류가 있어 옮기지 않았습니다:\n" + "\n".join(prepared.note.errors))

    def move(disk: dict[str, Any]) -> None:
        notes = disk.setdefault("notes", {})
        check(notes)
        notes[new_key] = notes.pop(old_key)

    update_state(course_dir, move)
    return old_key, new_key


def refresh_after(client: Any, course_dir: Path, enabled: bool) -> None:
    """노트를 올린 뒤 그 과목이 속한 학기 현황판을 갱신한다. 실패해도 경고만 하고 push 결과는 바꾸지 않는다."""
    if not enabled:
        return
    try:
        result = dashboard_module().refresh_for_course(client, course_dir)  # 학기 페이지가 없으면 None
        if result == "updated":
            print("학기 현황판도 갱신했습니다.")
    except Exception as exc:  # 노트는 이미 올라갔다
        print(f"[경고] 학기 현황판을 갱신하지 못했습니다(노트 업로드는 끝났습니다): {exc}", file=sys.stderr)


def describe(note: Note, title: str, summary: str) -> str:
    tables = sum(block["type"] == "table" for block in note.blocks)
    equations = sum(block["type"] == "equation" for block in note.blocks)
    images = len(slide_numbers(note.blocks))
    requests = len(request_chunks(note.blocks)) + 2 + 2 * images
    lines = [f"제목: {title}", f"내용: {summary or '(없음)'}",
             f"블록: {len(note.blocks)}개(표 {tables}개, 독립 수식 {equations}개, 슬라이드 그림 {images}장, "
             f"전체 요소 {count_elements(note.blocks)}개)", f"예상 요청: 약 {requests}회"]
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
    parser = argparse.ArgumentParser(description="학습노트를 노션 과목 페이지 아래의 차시 페이지로 올립니다.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="노션 API 토큰을 OS 비밀번호 보관소에 저장(자기 터미널에서 직접)")
    commands.add_parser("logout", help="저장된 토큰 삭제")
    setup_parser = commands.add_parser("setup", help="과목 페이지 만들기(차시 노트는 그 아래 페이지로 올라간다)")
    setup_parser.add_argument("parent", nargs="?", default=None,
                              help="노트를 모을 상위 노션 페이지 링크(연결을 추가해 둔 페이지). 생략하면 이 과목이 속한 학기 페이지")
    setup_parser.add_argument("--course", default=None, help="과목 페이지 이름(기본: 과목 폴더 이름)")
    setup_parser.add_argument("--slides", choices=("yes", "no"), default=None,
                              help="교안 슬라이드 그림을 노션에 올릴지(생략하면 기존 과목은 그대로, 새 과목은 올림)")
    setup_parser.add_argument("--new-page", action="store_true",
                              help="과목 페이지가 다른 페이지 아래에 있어도 새 과목 페이지를 만든다(노트 기록은 비운다)")
    for name in ("check", "push"):
        sub = commands.add_parser(name, help="네트워크 없이 변환·검사" if name == "check" else "과목 페이지 아래에 노트 한 페이지 올리기")
        sub.add_argument("note", type=Path, help="학습노트 원고(자료 충실형 Markdown 또는 심화 이해형 TeX)")
        sub.add_argument("--title", default=None, help="페이지 제목(기본: 원고의 # 제목에서 과목명을 뺀 것, TeX는 표지의 차시)")
        sub.add_argument("--summary", default=None, help="머리 줄의 내용(기본: 원고의 차시 제목을 이은 것, TeX는 표지의 요약)")
        sub.add_argument("--handout", default="", help="교안 범위(예: 교안 01·02, TeX는 기본으로 슬라이드 쪽 범위)")
        sub.add_argument("--mode", choices=tuple(MODES), default=None, help="기본: .tex는 deep, 그 밖은 faithful")
        if name == "push":
            sub.add_argument("--dry-run", action="store_true", help="올리지 않고 할 일만 보여 준다")
            sub.add_argument("--overwrite", action="store_true",
                             help="노션에서 고친 흔적이 있어도 같은 페이지를 원고 내용으로 바꾼다(노션의 수정을 원고에 반영했거나 버릴 때만)")
            sub.add_argument("--no-dashboard", action="store_true",
                             help="올린 뒤 학기 현황판을 갱신하지 않는다(여러 노트를 연달아 올리고 마지막에 dashboard 한 번)")
    rekey_parser = commands.add_parser("rekey", help="원고 이름이 바뀌었을 때 노트 기록을 옮겨 같은 노션 페이지를 이어 쓰기(네트워크 없음)")
    rekey_parser.add_argument("old", type=Path, help="예전 원고 경로(과목 폴더 기준 또는 절대 경로)")
    rekey_parser.add_argument("new", type=Path, help="새 원고 경로(과목 폴더 기준 또는 절대 경로)")
    semester_parser = commands.add_parser("semester", help="학기 노션 페이지 만들기(create)·과목 페이지를 그 아래로 옮기기(move)")
    semester_parser.add_argument("action", choices=("create", "move"), help="create: 학기 페이지 만들기, move: 과목 페이지 옮기기")
    semester_parser.add_argument("--root", default=None, help="학기 페이지를 둘 상위 페이지 링크(create, 기본: 등록부·과목 기록)")
    semester_parser.add_argument("--semester", default=None, help="학기 ID(기본: 현재 학기)")
    semester_parser.add_argument("--dry-run", action="store_true", help="노션을 바꾸지 않고 할 일만 보여 준다")
    dashboard_parser = commands.add_parser("dashboard", help="학기 페이지 맨 위 현황판을 과목 원장에서 다시 그리기")
    dashboard_parser.add_argument("--semester", default=None, help="학기 ID(기본: 이 과목의 학기, 없으면 현재 학기)")
    dashboard_parser.add_argument("--preview", action="store_true", help="토큰·네트워크 없이 그릴 현황판을 글자로 보여 준다")
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
            known = load_state(course_dir, required=False)
            note, _, _, _ = read_note(args.note, known.get("course", ""), bool(known.get("slides")))
            print(describe(note, args.title or note.title or args.note.stem, note.summary if args.summary is None else args.summary))
            return 1 if note.errors else 0
        if args.command == "rekey":  # 네트워크·토큰 없음
            old_key, new_key = rekey(course_dir, args.old, args.new)
            print(f"노트 기록을 옮겼습니다: {old_key} → {new_key}\n다음 push는 같은 노션 페이지를 이어 씁니다.")
            return 0
        if args.command == "dashboard" and args.preview:  # 네트워크·토큰 없음
            print(dashboard_module().preview(course_dir, args.semester))
            return 0
        client = NotionClient(load_token())
        if args.command == "dashboard":
            return dashboard_module().command_dashboard(client, course_dir, args.semester)
        if args.command == "semester":
            return dashboard_module().command_semester(client, args.action, root=args.root, semester=args.semester,
                                                       dry_run=args.dry_run)
        if args.command == "setup":
            slides = None if args.slides is None else args.slides == "yes"
            before = load_state(course_dir, required=False)
            state = setup(client, course_dir, args.parent, args.course or course_dir.name, slides, args.new_page)
            print(f"과목 페이지: {state['course_page_url']}\n차시 노트: 이 과목 페이지 아래 페이지로 올라갑니다\n"
                  f"교안 슬라이드 그림: {'올림' if state.get('slides') else '올리지 않음(캡션 글자만)'}\n기록: {state_path(course_dir)}")
            semester = semester_page(course_dir)
            if "data_source_id" in before and before.get("course_page_id") == state["course_page_id"]:
                print("예전 방식의 '차시별 노트' 표가 과목 페이지에 남아 있습니다. 노트를 다시 올리면 과목 페이지 아래 차시 페이지로 "
                      "들어가니, 다 올린 뒤 노션에서 그 표를 지우십시오.")
                if semester and id_key(state["parent_page_id"]) != id_key(semester):
                    print("과목 페이지를 학기 페이지 아래로 옮기려면 이어서 `gongbu notion semester move`를 실행하십시오.")
            created = before.get("course_page_id") != state["course_page_id"]
            if created and id_key(state["parent_page_id"]) == id_key(semester):
                refresh_after(client, course_dir, True)
            return 0
        state = load_state(course_dir)
        plan = plan_push(client, course_dir, args.note, state, title=args.title, summary=args.summary,
                         mode=args.mode, handout=args.handout, overwrite=args.overwrite)
        print(f"위치: {state['course']} 과목 페이지 아래\n할 일: {ACTION_TEXT[plan.action]}\n"
              + describe(plan.note, plan.title, plan.summary))
        if plan.action == "edited":
            mark_notion_edit(course_dir, state, plan)  # dry-run에서도 기록한다(로컬만)
            print(("원고도 마지막으로 올린 뒤 바뀌었습니다. " if plan.local_changed
                   else "원고는 마지막으로 올린 뒤 그대로입니다. ") + EDITED_HELP)
            refresh_after(client, course_dir, not (args.no_dashboard or args.dry_run))
            return 1
        if args.dry_run or (plan.action == "skip" and not plan.seen_time):
            return 0
        record = push(client, course_dir, plan, state, dt.date.today().isoformat())
        if plan.action != "skip":
            print(f"올림: {record.get('url', '')}")
            refresh_after(client, course_dir, not args.no_dashboard)
        return 0
    except (NotionError, HandoffMemoError, OSError, ValueError, KeyError, ImportError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # gongbu는 이 파일을 `__main__`으로 실행한다. 함께 쓰는 모듈(notion_dashboard 등)이 `import push_notion`으로
    # 같은 파일을 두 번째 사본으로 읽으면 NotionError 클래스가 둘이 되어 예외 처리가 어긋나므로 한 사본으로 맞춘다.
    sys.modules.setdefault("push_notion", sys.modules[__name__])
    sys.exit(main())
