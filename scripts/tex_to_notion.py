"""심화 이해형(DEEP) TeX 원고를 노션 블록으로 옮긴다.

DEEP 원고는 deep_note_style.tex의 명령 몇 가지(\\sourceslide, keybox·warnbox·goodbox, \\gongbucover)와
일반 LaTeX(절, 수식 환경, 목록, 표, 각주)로 이루어진다. 이 모듈은 그 범위만 결정적으로 옮긴다:
절 제목은 PDF와 같은 번호로, 수식은 PDF와 같은 식 번호로, 슬라이드는 그림 자리(나중에 올릴 쪽 번호)로,
강조 상자는 색 상자로 바꾼다. 쪽 나눔 명령은 노션에 쪽이 없으므로 버린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BOXES = {"keybox": ("💡", "blue_background", "핵심"), "warnbox": ("⚠️", "orange_background", "주의"),
         "goodbox": ("📌", "green_background", "기억할 점")}
MATH_ENVIRONMENTS = ("equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
                     "flalign", "flalign*")
TRANSPARENT_ENVIRONMENTS = ("samepage", "center", "flushleft", "flushright", "minipage", "small", "footnotesize")
DROP_COMMANDS = re.compile(
    r"\\(?:Needspace\*?|needspace|vspace\*?|hspace\*?|setlength|addtocounter|setcounter)\s*(?:\{[^{}]*\}|\[[^\]]*\])*(?:\{[^{}]*\})?"
    r"|\\(?:nopagebreak|pagebreak|newpage|clearpage|noindent|par|medskip|bigskip|smallskip|centering|phantomsection"
    r"|raggedright|interfootnotelinepenalty\s*=?\s*\d+)\b(?:\[[^\]]*\])?"
)
SLIDE_RE = re.compile(r"\\(?:sourceslide|continuationslide)\s*(?:\[[^\]]*\])?\s*\{(\d+)\}")
TEXT_ESCAPES = {r"\%": "%", r"\&": "&", r"\_": "_", r"\#": "#", r"\$": "$", r"\{": "{", r"\}": "}", r"\ ": " ",
                r"\,": " ", r"\;": " ", r"\!": "", "~": " ", r"\ldots": "…", r"\dots": "…", r"\textbackslash": "\\"}


@dataclass
class TexNote:
    course: str
    title: str
    summary: str
    blocks: list[dict[str, Any]]
    slides: list[int] = field(default_factory=list)
    slide_pdf: Path | None = None
    problems: list[str] = field(default_factory=list)


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in text.splitlines())


def braced(text: str, start: int) -> tuple[str, int]:
    """text[start]가 `{`일 때 짝 맞는 `}`까지의 내용과 그 다음 위치를 돌려준다."""
    if start >= len(text) or text[start] != "{":
        raise ValueError(f"중괄호가 필요한 자리입니다: {text[start:start + 30]}")
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "\\":
            continue
        if char == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif char == "}" and text[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
    raise ValueError(f"닫히지 않은 중괄호입니다: {text[start:start + 40]}")


def command_arguments(text: str, name: str) -> list[list[str]]:
    """`\\name{a}{b}` 꼴의 모든 호출에서 인자 목록을 꺼낸다."""
    found = []
    for match in re.finditer(rf"\\{name}\s*(?=\{{)", text):
        position, arguments = match.end(), []
        while position < len(text) and text[position] == "{":
            argument, position = braced(text, position)
            arguments.append(argument)
            while position < len(text) and text[position] in " \t":
                position += 1
        found.append(arguments)
    return found


def macros(preamble: str) -> dict[str, tuple[int, str]]:
    """`\\newcommand{\\name}[n]{본문}` 정의를 모은다. \\sourcepdf는 따로 다룬다."""
    defined: dict[str, tuple[int, str]] = {}
    for match in re.finditer(r"\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z]+)\}?\s*(?:\[(\d)\])?\s*(?=\{)", preamble):
        body, _ = braced(preamble, match.end())
        if match.group(1) != "sourcepdf":
            defined[match.group(1)] = (int(match.group(2) or 0), body)
    return defined


def expand_macros(text: str, defined: dict[str, tuple[int, str]]) -> str:
    for _ in range(10):
        changed = False
        for name, (count, body) in defined.items():
            pattern = re.compile(rf"\\{name}(?![A-Za-z])\s*")
            position = 0
            out = []
            for match in pattern.finditer(text):
                if match.start() < position:
                    continue
                cursor, arguments = match.end(), []
                try:
                    for _ in range(count):
                        argument, cursor = braced(text, cursor)
                        arguments.append(argument)
                except ValueError:
                    continue
                replacement = body
                for number, argument in enumerate(arguments, start=1):
                    replacement = replacement.replace(f"#{number}", argument)
                out.append(text[position:match.start()] + replacement)
                position = cursor
                changed = True
            text = "".join(out) + text[position:]
        if not changed:
            break
    return text


def inline_input(text: str, base: Path) -> str:
    """본문의 `\\input{부분.tex}`을 그 파일 내용으로 바꾼다(진도별 원고)."""
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip().strip('"')
        path = (base / name)
        if path.suffix != ".tex":
            path = path.with_suffix(".tex")
        if "deep_note_style" in name or not path.is_file():
            return ""
        return strip_comments(path.read_text(encoding="utf-8-sig"))
    return re.sub(r"\\input\s*\{([^}]*)\}", replace, text)


# ------------------------------------------------------------------ 수식 번호

ROW_SPLIT_RE = re.compile(r"\\\\(?:\[[^\]]*\])?")


def split_rows(body: str) -> list[str]:
    """정렬 환경 본문을 맨 바깥 `\\\\`에서만 나눈다(행렬 안의 `\\\\`는 그대로)."""
    rows, depth, start, index = [], 0, 0, 0
    while index < len(body):
        if body.startswith(r"\begin{", index):
            depth += 1
        elif body.startswith(r"\end{", index):
            depth -= 1
        elif body.startswith("\\\\", index) and depth == 0:
            rows.append(body[start:index])
            match = ROW_SPLIT_RE.match(body, index)
            index = match.end() if match else index + 2
            start = index
            continue
        index += 1
    rows.append(body[start:])
    return [row for row in rows if row.strip()]


@dataclass
class Numbering:
    counter: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    def take(self, chunk: str) -> tuple[str, str | None]:
        """식 하나(또는 한 행)의 번호를 정하고 \\label·\\notag를 걷어 낸다."""
        label = re.search(r"\\label\{([^}]*)\}", chunk)
        chunk = re.sub(r"\\label\{[^}]*\}", "", chunk)
        custom = re.search(r"\\tag\*?\{([^}]*)\}", chunk)
        if re.search(r"\\(?:notag|nonumber)\b", chunk):
            return re.sub(r"\\(?:notag|nonumber)\b", "", chunk), None
        if custom:
            number = custom.group(1)
            chunk = re.sub(r"\\tag\*?\{[^}]*\}", "", chunk)
        else:
            self.counter += 1
            number = str(self.counter)
        if label:
            self.labels[label.group(1)] = number
        return chunk, number


def math_blocks(environment: str, body: str, numbering: Numbering, problems: list[str]) -> list[dict[str, Any]]:
    """수식 환경을 노션 수식 블록으로. 번호 있는 식·행에는 PDF와 같은 번호를 `\\tag`로 붙인다.

    정렬 환경은 `align*`·`gather*`로 옮겨 행마다 `\\tag`를 단다. 노션의 KaTeX(0.16)는 이 번호를
    한 줄 식과 같이 오른쪽 끝에 맞춘다.
    """
    numbered = not environment.endswith("*")
    base = environment.rstrip("*")
    if base in ("equation", "multline"):
        expression, number = numbering.take(body) if numbered else (re.sub(r"\\label\{[^}]*\}", "", body), None)
        expression = expression.strip()
        if base == "multline":
            expression = r"\begin{gathered}" + expression + r"\end{gathered}"
        if number:
            expression += rf" \tag{{{number}}}"
        return [equation(expression, problems)]
    rows = []
    for row in split_rows(body):
        content, number = numbering.take(row) if numbered else (re.sub(r"\\(?:label\{[^}]*\}|notag|nonumber)", "", row), None)
        rows.append(content.strip() + (rf" \tag{{{number}}}" if number else ""))
    environment_name = "gather*" if base == "gather" else "align*"
    chunks, current = [], []
    for row in rows:  # 수식 하나는 1000자까지라 긴 정렬은 여러 블록으로 나눈다
        candidate = current + [row]
        if current and len(rf"\begin{{{environment_name}}}" + r" \\ ".join(candidate) + rf"\end{{{environment_name}}}") > 990:
            chunks.append(current)
            candidate = [row]
        current = candidate
    if current:
        chunks.append(current)
    return [equation(rf"\begin{{{environment_name}}}" + " \\\\\n".join(chunk) + rf"\end{{{environment_name}}}", problems)
            for chunk in chunks]


def equation(expression: str, problems: list[str]) -> dict[str, Any]:
    expression = katex_arrays(expression)
    if len(expression) > 1000:
        problems.append(f"오류: 수식 하나가 {len(expression)}자로 노션 한도 1000자를 넘습니다: {expression[:40]}…")
    return {"type": "equation", "equation": {"expression": expression}}


# ------------------------------------------------------------------ KaTeX 호환

ARRAY_OPENER = r"\begin{array}"


def split_top(text: str, separator: str) -> tuple[list[str], list[str]]:
    """중괄호와 안쪽 환경 밖의 separator(`&` 또는 `\\\\`)에서 나누고, 조각과 그 사이 구분자를 돌려준다."""
    parts, separators, braces, environments, start, index = [], [], 0, 0, 0, 0
    while index < len(text):
        if text.startswith(r"\begin{", index):
            environments += 1
        elif text.startswith(r"\end{", index):
            environments -= 1
        char, top = text[index], braces == 0 and environments == 0
        if char == "\\":
            if top and separator == "\\\\" and text.startswith("\\\\", index):
                match = ROW_SPLIT_RE.match(text, index)
                parts.append(text[start:index])
                separators.append(match.group(0))
                index = start = match.end()
                continue
            index += 2  # \& 같은 이스케이프 글자는 나누지 않는다
            continue
        if char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        elif char == "&" and top and separator == "&":
            parts.append(text[start:index])
            separators.append("&")
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts, separators


def array_spec(spec: str) -> tuple[str, dict[int, str]]:
    """array 열 지정을 KaTeX가 읽는 l·c·r·|·:만으로 바꾼다.

    `*{n}{…}`는 펼치고, `p{…}` 같은 폭 지정 열은 l로, 빈 `@{}`·`!{}`와 `>{…}`·`<{…}`는 버린다.
    내용이 있는 `@{X}`는 열 하나로 바꾸고, 돌려주는 {열 위치: X}로 모든 행의 그 자리에 X를 넣게 한다.
    """
    while (star := spec.find("*")) >= 0:
        count, after = braced(spec, star + 1)
        repeated, after = braced(spec, after)
        spec = spec[:star] + repeated * int(count) + spec[after:]
    kept, inserts, columns, index = [], {}, 0, 0
    while index < len(spec):
        char = spec[index]
        if char in "lcr|:":
            kept.append(char)
            columns += char in "lcr"
            index += 1
        elif char in "pmb":
            _, index = braced(spec, index + 1)
            kept.append("l")
            columns += 1
        elif char in "@!<>":
            content, index = braced(spec, index + 1)
            if char in "@!" and content.strip():
                kept.append("c")
                inserts[columns] = content
                columns += 1
        else:
            index += 1
    return "".join(kept), inserts


def katex_arrays(expression: str) -> str:
    """노션 수식 엔진(KaTeX)의 array는 열 지정으로 l·c·r·|·:만 받는다. 그 밖의 지정은 고쳐서 오류 대신 표가 보이게 한다.

    `@{\\qquad}`처럼 열 사이에 넣은 내용은 그 내용만 든 열로 바꿔 PDF의 열 간격을 살린다.
    """
    out, position = [], 0
    while (start := expression.find(ARRAY_OPENER, position)) >= 0:
        spec_start = start + len(ARRAY_OPENER)
        if not expression.startswith("{", spec_start):
            out.append(expression[position:spec_start])
            position = spec_start
            continue
        spec, body_start = braced(expression, spec_start)
        body, end = environment_body(expression, "array", body_start)
        body = katex_arrays(body)
        if not re.fullmatch(r"[lcr|:\s]*", spec):
            spec, inserts = array_spec(spec)
            rows, breaks = split_top(body, "\\\\")
            for row_index, row in enumerate(rows):
                if not re.sub(r"\\h(?:dash)?line", "", row).strip():
                    continue  # 끝의 빈 행이나 가로줄만 있는 행에는 열을 끼우지 않는다
                cells, _ = split_top(row, "&")
                for column, content in sorted(inserts.items()):
                    if column <= len(cells):
                        cells.insert(column, content)
                rows[row_index] = "&".join(cells)
            body = "".join(row + (breaks[i] if i < len(breaks) else "") for i, row in enumerate(rows))
        out.append(expression[position:start] + ARRAY_OPENER + "{" + spec + "}" + body + r"\end{array}")
        position = end
    out.append(expression[position:])
    return "".join(out)


# ------------------------------------------------------------------ 문장

def text_from_tex(text: str) -> str:
    """수식 밖 글자의 TeX 표기를 보통 글자로 바꾼다."""
    text = text.replace("---", "—").replace("--", "–").replace("``", "“").replace("''", "”")
    for source, target in TEXT_ESCAPES.items():
        text = text.replace(source, target)
    text = re.sub(r"\\\\\s*", "\n", text)
    return re.sub(r"[ \t\r\n]+", " ", text).replace(" \n ", "\n")


def inline_items(tex: str, context: dict[str, Any], style: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """문단 TeX을 노션 rich text 목록으로. 굵게·기울임·코드·링크·인라인 수식·각주·식 참조를 옮긴다."""
    style = style or {}
    items: list[dict[str, Any]] = []
    position = 0
    pattern = re.compile(r"(?P<math>(?<!\\)\$|\\\()"
                         r"|\\(?P<command>textbf|emph|textit|texttt|underline|href|url|footnote|eqref|ref|mbox|text|textsf)\s*(?=\{)",
                         re.S)

    def plain(fragment: str) -> None:
        content = text_from_tex(fragment)
        content = re.sub(r"\\[A-Za-z]+\*?", "", content)  # 남은 서식 명령(\relax 등)은 글자로 드러내지 않는다
        if content:
            item: dict[str, Any] = {"type": "text", "text": {"content": content}}
            if style:
                item["annotations"] = dict(style)
            items.append(item)

    while position < len(tex):
        match = pattern.search(tex, position)
        if not match:
            plain(tex[position:])
            break
        plain(tex[position:match.start()])
        if match.group("math"):
            closer = "$" if match.group("math") == "$" else "\\)"
            end = math_end(tex, match.end(), closer)
            if end < 0:
                raise ValueError(f"닫히지 않은 인라인 수식이 있습니다: {tex[match.start():match.start() + 40]}")
            expression = katex_arrays(tex[match.end():end].strip())
            item = {"type": "equation", "equation": {"expression": expression}}
            if style:
                item["annotations"] = dict(style)
            items.append(item)
            position = end + len(closer)
            continue
        command = match.group("command")
        argument, position = braced(tex, match.end())
        if command == "href":
            label, position = braced(tex, position)
            for item in inline_items(label, context, style):
                if item["type"] == "text":
                    item["text"]["link"] = {"url": argument}
                items.append(item)
        elif command == "url":
            items.append({"type": "text", "text": {"content": argument, "link": {"url": argument}}})
        elif command == "footnote":
            context["footnotes"].append(argument)
            items.append({"type": "text", "text": {"content": f"[{len(context['footnotes'])}]"},
                          "annotations": {"color": "gray"}})
        elif command in ("eqref", "ref"):
            number = context["numbering"].labels.get(argument.strip())
            if number is None:
                context["problems"].append(f"주의: 참조할 식 번호를 찾지 못했습니다: {argument}")
            text = f"({number})" if command == "eqref" else str(number or "?")
            items.append({"type": "text", "text": {"content": text}})
        else:
            flags = dict(style)
            flags.update({"textbf": {"bold": True}, "emph": {"italic": True}, "textit": {"italic": True},
                          "texttt": {"code": True}, "underline": {"underline": True}}.get(command, {}))
            items.extend(inline_items(argument, context, flags))
    return items


def merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        last = merged[-1] if merged else None
        if (last and item["type"] == last["type"] == "text" and item.get("annotations") == last.get("annotations")
                and item["text"].get("link") == last["text"].get("link")):
            last["text"]["content"] += item["text"]["content"]
        else:
            merged.append(item)
    out = []
    for item in merged:  # 노션 한도: 글자 2000자, 수식 1000자
        if item["type"] == "text":
            content = item["text"]["content"]
            for start in range(0, max(len(content), 1), 2000):
                piece = {**item, "text": {**item["text"], "content": content[start:start + 2000]}}
                if piece["text"]["content"].strip() or piece["text"]["content"] == " ":
                    out.append(piece)
        else:
            out.append(item)
    if out and out[0]["type"] == "text":
        out[0]["text"]["content"] = out[0]["text"]["content"].lstrip()
    if out and out[-1]["type"] == "text":
        out[-1]["text"]["content"] = out[-1]["text"]["content"].rstrip()
    return [item for item in out if item["type"] != "text" or item["text"]["content"]]


def paragraphs(tex: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for chunk in re.split(r"\n\s*\n", tex):
        if not chunk.strip():
            continue
        items = merge(inline_items(chunk, context))
        for start in range(0, len(items), 100):
            if items[start:start + 100]:
                blocks.append({"type": "paragraph", "paragraph": {"rich_text": items[start:start + 100]}})
    return blocks


# ------------------------------------------------------------------ 블록

BLOCK_RE = re.compile(
    r"\\(?P<heading>section|subsection|subsubsection)(?P<star>\*?)\s*(?=\{)"
    r"|\\begin\{(?P<env>[A-Za-z*]+)\}(?:\[(?P<opt>[^\]]*)\])?"
    r"|\\\[|\$\$"
    r"|\\(?:sourceslide|continuationslide)\s*(?:\[[^\]]*\])?\s*\{(?P<slide>\d+)\}"
    r"|\\item\b"
)
# 독립 수식(`$$…$$`, `\[…\]`)도 한 덩어리로 읽어야 `$`의 짝이 어긋나지 않는다.
MATH_OPEN_RE = re.compile(r"\$\$|\\\[|(?<!\\)\$|\\\(")
MATH_CLOSE = {"$$": "$$", "\\[": "\\]", "$": "$", "\\(": "\\)"}


def math_end(tex: str, index: int, closer: str) -> int:
    """index부터 중괄호 밖에서 처음 나오는 closer의 위치(없으면 -1).

    `\\text{\\(A\\)의 열}`처럼 수식 안 글자에 든 수식은 중괄호 안이라 건너뛴다.
    """
    depth = 0
    while index < len(tex):
        if depth == 0 and tex.startswith(closer, index):
            return index
        char = tex[index]
        if char == "\\":
            index += 2
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        index += 1
    return -1


def inline_math_spans(tex: str) -> list[tuple[int, int]]:
    """문장 속 인라인 수식의 자리. 그 안의 `\\begin{smallmatrix}` 같은 환경은 문단을 끊는 블록이 아니다."""
    spans: list[tuple[int, int]] = []
    index = 0
    while match := MATH_OPEN_RE.search(tex, index):
        closer = MATH_CLOSE[match.group()]
        end = math_end(tex, match.end(), closer)
        if end < 0:
            break
        if match.group() in ("$", "\\("):
            spans.append((match.start(), end + len(closer)))
        index = end + len(closer)
    return spans


def environment_body(tex: str, name: str, start: int) -> tuple[str, int]:
    """\\begin{name} 다음 위치에서 짝 맞는 \\end{name}까지."""
    depth, index = 1, start
    opener, closer = rf"\begin{{{name}}}", rf"\end{{{name}}}"
    while depth:
        next_open, next_close = tex.find(opener, index), tex.find(closer, index)
        if next_close < 0:
            raise ValueError(f"닫히지 않은 환경입니다: {name}")
        if 0 <= next_open < next_close:
            depth, index = depth + 1, next_open + len(opener)
        else:
            depth, index = depth - 1, next_close + len(closer)
    return tex[start:index - len(closer)], index


def convert_body(tex: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    position = 0
    spans = inline_math_spans(tex)
    while position < len(tex):
        match = BLOCK_RE.search(tex, position)
        while match and (inside := next((span for span in spans if span[0] < match.start() < span[1]), None)):
            match = BLOCK_RE.search(tex, inside[1])  # 인라인 수식 안은 건너뛴다
        if not match:
            blocks.extend(paragraphs(tex[position:], context))
            break
        blocks.extend(paragraphs(tex[position:match.start()], context))
        token = match.group(0)
        if match.group("heading"):
            title, position = braced(tex, match.end())
            level = {"section": 1, "subsection": 2, "subsubsection": 3}[match.group("heading")]
            prefix = ""
            if not match.group("star") and level == 1:
                context["section"] += 1
                context["subsection"] = 0
                prefix = f"{context['section']}. "
            elif not match.group("star") and level == 2:
                context["subsection"] += 1
                prefix = f"{context['section']}.{context['subsection']} "
            kind = f"heading_{level}"
            blocks.append({"type": kind, kind: {"rich_text": merge([{"type": "text", "text": {"content": prefix}}]
                                                                    + inline_items(title, context))}})
        elif match.group("slide"):
            number = int(match.group("slide"))
            context["slides"].append(number)
            blocks.append({"type": "image", "image": {"type": "file_upload", "file_upload": {"id": f"slide:{number}"},
                                                       "caption": [{"type": "text", "text": {"content": f"원본 PDF p.{number}"}}]}})
            position = match.end()
        elif token in (r"\[", "$$"):
            closer = r"\]" if token == r"\[" else "$$"
            end = tex.find(closer, match.end())
            if end < 0:
                raise ValueError("닫히지 않은 독립 수식이 있습니다.")
            blocks.append(equation(tex[match.end():end].strip(), context["problems"]))
            position = end + len(closer)
        elif token.startswith(r"\item"):
            position = match.end()  # 목록 밖의 \item은 글자로 남기지 않는다
        else:
            name = match.group("env")
            body, position = environment_body(tex, name, match.end())
            if name in MATH_ENVIRONMENTS:
                blocks.extend(math_blocks(name, body, context["numbering"], context["problems"]))
            elif name in BOXES:
                blocks.append(box(name, match.group("opt"), body, context))
            elif name in ("itemize", "enumerate"):
                blocks.extend(list_blocks(name, body, context))
            elif name in ("tabular", "tabularx", "longtable", "table"):
                blocks.extend(table_blocks(body, context))
            elif name in TRANSPARENT_ENVIRONMENTS or name in ("document",):
                blocks.extend(convert_body(body, context))
            else:
                context["problems"].append(f"주의: 모르는 환경 {name}은 안의 내용만 옮깁니다.")
                blocks.extend(convert_body(body, context))
    return blocks


def box(name: str, title: str | None, body: str, context: dict[str, Any]) -> dict[str, Any]:
    icon, color, default_title = BOXES[name]
    inner = convert_body(body, context)
    head: list[dict[str, Any]] = [{"type": "text", "text": {"content": (title or default_title).strip()},
                                   "annotations": {"bold": True}}]
    if inner and inner[0]["type"] == "paragraph":
        head += [{"type": "text", "text": {"content": "\n"}}] + inner[0]["paragraph"]["rich_text"]
        inner = inner[1:]
    callout: dict[str, Any] = {"rich_text": merge(head)[:100], "icon": {"type": "emoji", "emoji": icon}, "color": color}
    if inner:
        callout["children"] = inner
    return {"type": "callout", "callout": callout}


def list_blocks(name: str, body: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    kind = "numbered_list_item" if name == "enumerate" else "bulleted_list_item"
    items = [chunk for chunk in re.split(r"\\item\b(?:\[[^\]]*\])?", body)[1:]]
    blocks = []
    for item in items:
        inner = convert_body(item, context)
        rich = inner[0]["paragraph"]["rich_text"] if inner and inner[0]["type"] == "paragraph" else []
        children = inner[1:] if rich else inner
        block: dict[str, Any] = {"type": kind, kind: {"rich_text": rich}}
        if children:
            block[kind]["children"] = children
        blocks.append(block)
    return blocks


def table_blocks(body: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    body = re.sub(r"^\s*\{[^{}]*\}", "", body)  # 열 지정
    body = re.sub(r"\\(?:hline|toprule|midrule|bottomrule|cline\{[^}]*\}|endhead|endfirsthead|endfoot|endlastfoot)", "", body)
    rows = [[cell.strip() for cell in re.split(r"(?<!\\)&", row)] for row in split_rows(body)]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    return [{"type": "table", "table": {"table_width": width, "has_column_header": True, "has_row_header": False,
             "children": [{"type": "table_row", "table_row": {"cells": [merge(inline_items(cell, context))[:100]
                                                                         for cell in row + [""] * (width - len(row))]}}
                          for row in rows[:100]]}}]


def convert_tex(path: Path) -> TexNote:
    """DEEP 원고 하나(전체 문서 또는 조각)를 노션 블록으로 옮긴다."""
    raw = strip_comments(path.read_text(encoding="utf-8-sig"))
    preamble, _, rest = raw.partition(r"\begin{document}")
    body = rest.partition(r"\end{document}")[0] if rest else raw
    if not rest:
        preamble = ""
    cover = command_arguments(raw, "gongbucover")
    course, title, summary = (cover[0] + ["", "", ""])[:3] if cover else ("", path.stem, "")
    slide_pdf = None
    source = re.search(r"\\newcommand\s*\{?\\sourcepdf\}?\s*\{(?:\\detokenize\s*\{)?([^{}]+)\}", raw)
    if source:
        slide_pdf = Path(re.sub(r"\\space\s*", " ", source.group(1).strip()))  # TeX처럼 \space 뒤 띄어쓰기는 버린다
        if not slide_pdf.is_absolute():
            slide_pdf = (path.parent / slide_pdf).resolve()
    body = inline_input(body, path.parent)
    body = expand_macros(body, macros(preamble + "\n" + body))
    body = re.sub(r"\\(?:re)?newcommand\*?\s*\{?\\[A-Za-z]+\}?\s*(?:\[\d\])?\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "", body)
    body = re.sub(r"\\gongbu(?:cover|header)\s*(?:\{[^{}]*\})+", "", body)
    body = DROP_COMMANDS.sub("", body)
    context: dict[str, Any] = {"numbering": Numbering(), "footnotes": [], "problems": [], "slides": [],
                               "section": 0, "subsection": 0}
    prescan_labels(body, context["numbering"])
    blocks = convert_body(body, context)
    if context["footnotes"]:
        blocks.append({"type": "divider", "divider": {}})
        blocks.append({"type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": "각주"}}]}})
        for note in context["footnotes"]:
            blocks.append({"type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": merge(inline_items(note, context))[:100]}})
    return TexNote(text_from_tex(course).strip(), text_from_tex(title).strip(), text_from_tex(summary).strip(),
                   blocks, context["slides"], slide_pdf, context["problems"])


def prescan_labels(body: str, numbering: Numbering) -> None:
    """본문보다 앞서 나오는 \\eqref도 번호를 알 수 있게 식 번호를 미리 매긴다(원래 번호기는 그대로 둔다)."""
    scout = Numbering()
    for match in re.finditer(r"\\begin\{(" + "|".join(re.escape(name) for name in MATH_ENVIRONMENTS) + r")\}", body):
        name = match.group(1)
        inner, _ = environment_body(body, name, match.end())
        if name.endswith("*"):
            continue
        for chunk in ([inner] if name.rstrip("*") in ("equation", "multline") else split_rows(inner)):
            scout.take(chunk)
    numbering.labels.update(scout.labels)
