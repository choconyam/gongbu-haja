#!/usr/bin/env python3
"""생성된 학습노트 파일에 결정적인 무결성 검사를 수행한다."""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# -----------------------------------------------------------------------------
# 1. 지원 형식과 공통 오류 표지
# 내용의 정답 여부가 아니라 파일에서 기계적으로 확인 가능한 기준만 정의한다.
# -----------------------------------------------------------------------------

SUPPORTED_SUFFIXES = {".md", ".markdown", ".tex", ".docx", ".pdf"}
UNCERTAINTY_PATTERNS = {
    "판독 불명": re.compile(r"\[판독 불명(?:\s+[^\]]+)?\]"),
    "전사 불명확": re.compile(r"\[전사 불명확(?:\s+[^\]]+)?\]"),
    "자료에 명시 없음": re.compile(r"\[자료에 명시 없음(?:\s+[^\]]+)?\]"),
    "문맥상 추정": re.compile(r"\[문맥상 추정(?:\s+[^\]]+)?\]"),
    "확인 필요": re.compile(r"\[확인 필요(?:\s+[^\]]+)?\]"),
}
HARD_PLACEHOLDER_RE = re.compile(r"\b(?:FIXME|TBD)\b|<placeholder>", re.IGNORECASE)
TODO_RE = re.compile(r"\bTODO\b", re.IGNORECASE)
MARKDOWN_FENCE_RE = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
MARKDOWN_INLINE_CODE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
TEX_CODE_ENV_RE = re.compile(
    r"\\begin\{(?P<env>verbatim\*?|Verbatim|lstlisting|minted)\}.*?\\end\{(?P=env)\}",
    re.DOTALL,
)
TEX_VERB_RE = re.compile(r"\\verb(?P<delimiter>[^\w\s]).*?(?P=delimiter)")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
TEX_INCLUDE_RE = re.compile(
    r"\\(?P<kind>input|include|includegraphics)(?:\[[^\]]*\])?\{(?P<path>[^}]+)\}"
)


# -----------------------------------------------------------------------------
# 2. 검사 결과와 측정값 수집
# 오류·경고와 페이지 수·글자 수 같은 참고 지표를 함께 기록한다.
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    location: str | None = None


class Report:
    def __init__(self) -> None:
        self.issues: list[Issue] = []
        self.metrics: dict[str, int | str | bool] = {}

    def add(self, severity: str, code: str, message: str, location: Path | str | None = None) -> None:
        self.issues.append(
            Issue(severity, code, message, str(location) if location is not None else None)
        )

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "warning"]


# -----------------------------------------------------------------------------
# 3. 모든 텍스트 형식에 공통으로 적용하는 검사
# 지나치게 짧은 내용, 미완성 표지, 깨진 문자 등을 먼저 찾는다.
# -----------------------------------------------------------------------------

def read_text(path: Path, report: Report) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        report.add("error", "encoding", f"UTF-8 텍스트가 아닙니다: {exc}", path)
    except OSError as exc:
        report.add("error", "read", f"파일을 읽을 수 없습니다: {exc}", path)
    return None


def blank_protected_region(match: re.Match[str]) -> str:
    """코드 영역의 줄 수는 유지하면서 placeholder 검사 대상에서만 숨긴다."""

    return "".join("\n" if character == "\n" else " " for character in match.group(0))


def text_for_placeholder_scan(text: str, path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        text = MARKDOWN_FENCE_RE.sub(blank_protected_region, text)
        return MARKDOWN_INLINE_CODE_RE.sub(blank_protected_region, text)
    if suffix == ".tex":
        text = TEX_CODE_ENV_RE.sub(blank_protected_region, text)
        return TEX_VERB_RE.sub(blank_protected_region, text)
    return text


def check_common_text(text: str, path: Path, report: Report, minimum: int) -> None:
    visible = re.sub(r"\s+", " ", text).strip()
    report.metrics["characters"] = len(visible)
    if len(visible) < minimum:
        report.add(
            "warning",
            "short-content",
            f"추출 텍스트가 권장 최소 길이보다 짧습니다: {len(visible)} < {minimum}",
            path,
        )
    placeholder_text = text_for_placeholder_scan(text, path)
    for match in HARD_PLACEHOLDER_RE.finditer(placeholder_text):
        report.add("error", "placeholder", f"미완성 표지가 남아 있습니다: {match.group(0)}", path)
    todo_count = len(TODO_RE.findall(placeholder_text))
    if todo_count:
        report.add(
            "warning",
            "todo-review",
            f"본문 TODO가 {todo_count}개 남아 있습니다. 미완성 작업인지 학습에 필요한 과제인지 최종 검수자가 판단해야 합니다.",
            path,
        )
    for label, pattern in UNCERTAINTY_PATTERNS.items():
        count = len(pattern.findall(text))
        if count:
            report.add(
                "warning",
                "unresolved-uncertainty",
                f"미해결 표지 [{label}] 계열이 {count}개 남아 있습니다.",
                path,
            )


def normalize_link(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    if not value or value.startswith(("http://", "https://", "mailto:", "#")):
        return None
    return unquote(value.split("#", 1)[0]) or None


# -----------------------------------------------------------------------------
# 4. 형식별 검사기
# Markdown 링크, TeX 괄호·참조, DOCX 내부 XML, PDF 페이지를 각각 확인한다.
# -----------------------------------------------------------------------------

def validate_markdown(path: Path, text: str, report: Report) -> None:
    headings = re.findall(r"(?m)^#{1,6}\s+\S", text)
    report.metrics["headings"] = len(headings)
    if not headings:
        report.add("error", "missing-heading", "Markdown 제목이 없습니다.", path)
    if text.count("```") % 2:
        report.add("error", "unclosed-code-fence", "닫히지 않은 코드 블록이 있습니다.", path)
    for match in MARKDOWN_LINK_RE.finditer(text):
        raw = match.group(1)
        link = normalize_link(raw)
        if link is None:
            continue
        target = (path.parent / link).resolve()
        if not target.exists():
            report.add("error", "broken-link", f"로컬 링크 대상이 없습니다: {raw}", path)


def tex_brace_balance(text: str) -> int:
    without_comments = re.sub(r"(?m)(?<!\\)%.*$", "", text)
    without_escaped = without_comments.replace(r"\{", "").replace(r"\}", "")
    return without_escaped.count("{") - without_escaped.count("}")


def candidate_tex_paths(base: Path, raw: str, kind: str) -> list[Path]:
    path = base / raw
    if path.suffix:
        return [path]
    if kind in {"input", "include"}:
        return [path.with_suffix(".tex"), path]
    return [path.with_suffix(suffix) for suffix in (".pdf", ".png", ".jpg", ".jpeg", ".svg")] + [path]


def validate_tex(path: Path, text: str, report: Report) -> None:
    for required in (r"\documentclass", r"\begin{document}", r"\end{document}"):
        if required not in text:
            report.add("error", "missing-tex-structure", f"필수 TeX 구문이 없습니다: {required}", path)
    balance = tex_brace_balance(text)
    report.metrics["brace_balance"] = balance
    if balance:
        report.add("error", "unbalanced-braces", f"중괄호 균형이 맞지 않습니다: {balance:+d}", path)
    for match in TEX_INCLUDE_RE.finditer(text):
        kind = match.group("kind")
        raw = match.group("path")
        if any(token in raw for token in ("#", "\\", "{")):
            continue
        candidates = candidate_tex_paths(path.parent, raw, kind)
        if not any(candidate.exists() for candidate in candidates):
            report.add("error", "missing-tex-asset", f"TeX 참조 파일이 없습니다: {raw}", path)


def validate_docx(path: Path, report: Report) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                report.add("error", "corrupt-docx", f"손상된 ZIP 항목입니다: {bad_member}", path)
                return ""
            required = "word/document.xml"
            if required not in archive.namelist():
                report.add("error", "missing-docx-part", f"필수 DOCX 구성요소가 없습니다: {required}", path)
                return ""
            root = ElementTree.fromstring(archive.read(required))
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        report.add("error", "invalid-docx", f"DOCX를 열 수 없습니다: {exc}", path)
        return ""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    heading_count = 0
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
        if text:
            paragraphs.append(text)
        style = paragraph.find("./w:pPr/w:pStyle", namespace)
        if style is not None:
            value = style.get(f"{{{namespace['w']}}}val", "")
            if value.lower().startswith(("heading", "제목")):
                heading_count += 1
    report.metrics["headings"] = heading_count
    report.metrics["paragraphs"] = len(paragraphs)
    if not paragraphs:
        report.add("error", "empty-docx", "DOCX에서 본문 문단을 찾지 못했습니다.", path)
    if not heading_count:
        report.add("warning", "no-docx-headings", "DOCX에서 제목 스타일을 찾지 못했습니다.", path)
    return "\n".join(paragraphs)


def fallback_pdf_page_count(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)\b", data))


def validate_pdf(path: Path, report: Report) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        report.add("error", "read", f"PDF를 읽을 수 없습니다: {exc}", path)
        return ""
    if not data.startswith(b"%PDF-"):
        report.add("error", "invalid-pdf-header", "PDF 헤더가 올바르지 않습니다.", path)
        return ""

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        pages = fallback_pdf_page_count(data)
        report.metrics["pages"] = pages
        report.metrics["pdf_parser"] = "fallback"
        if pages < 1:
            report.add("error", "no-pdf-pages", "PDF 페이지를 확인하지 못했습니다.", path)
        report.add("warning", "no-pypdf", "pypdf가 없어 PDF 텍스트 검증을 생략했습니다.", path)
        return ""

    try:
        reader = PdfReader(str(path), strict=False)
        pages = len(reader.pages)
        report.metrics["pages"] = pages
        report.metrics["pdf_parser"] = "pypdf"
        if pages < 1:
            report.add("error", "no-pdf-pages", "PDF에 페이지가 없습니다.", path)
            return ""
        text_parts: list[str] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text_parts.append(page.extract_text() or "")
            except Exception as exc:  # pypdf exposes several backend exceptions
                report.add(
                    "warning",
                    "pdf-text-extraction",
                    f"PDF {page_number}페이지 텍스트 추출 실패: {exc}",
                    path,
                )
        text = "\n".join(text_parts)
        if not text.strip():
            report.add("warning", "image-only-pdf", "추출 가능한 텍스트가 없습니다. 이미지형 PDF일 수 있습니다.", path)
        return text
    except Exception as exc:
        report.add("error", "invalid-pdf", f"PDF 구조를 읽을 수 없습니다: {exc}", path)
        return ""


# -----------------------------------------------------------------------------
# 5. 사용자 지정 필수 문구와 원자료 폴더 검사
# 자동 검증 옵션으로 요청된 최소 내용과 근거 파일 존재를 확인한다.
# -----------------------------------------------------------------------------

def validate_required_phrases(text: str, phrases: list[str], path: Path, report: Report) -> None:
    for phrase in phrases:
        if phrase not in text:
            report.add("error", "missing-required-text", f"필수 문구를 찾지 못했습니다: {phrase}", path)


def validate_source_dir(source_dir: Path | None, report: Report) -> None:
    if source_dir is None:
        return
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        report.add("error", "missing-source-dir", "원자료 폴더가 없습니다.", source_dir)
        return
    files = [path for path in source_dir.rglob("*") if path.is_file()]
    report.metrics["source_files"] = len(files)
    if not files:
        report.add("warning", "empty-source-dir", "원자료 폴더에 파일이 없습니다.", source_dir)


# -----------------------------------------------------------------------------
# 6. 검사 라우팅과 결과 출력
# 확장자에 맞는 검사기를 선택하고 CI에서 쓸 수 있는 JSON도 지원한다.
# -----------------------------------------------------------------------------

def validate(args: argparse.Namespace) -> Report:
    report = Report()
    path = args.note.expanduser().resolve()
    report.metrics["path"] = str(path)
    if not path.is_file():
        report.add("error", "missing-note", "검증 대상 파일이 없습니다.", path)
        return report
    report.metrics["bytes"] = path.stat().st_size
    if path.stat().st_size == 0:
        report.add("error", "empty-note", "검증 대상 파일이 비어 있습니다.", path)
        return report

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        report.add("error", "unsupported-format", f"지원하지 않는 형식입니다: {suffix}", path)
        return report

    text = ""
    if suffix in {".md", ".markdown", ".tex"}:
        loaded = read_text(path, report)
        if loaded is None:
            return report
        text = loaded
        if suffix in {".md", ".markdown"}:
            validate_markdown(path, text, report)
        else:
            validate_tex(path, text, report)
    elif suffix == ".docx":
        text = validate_docx(path, report)
    elif suffix == ".pdf":
        text = validate_pdf(path, report)

    if text:
        check_common_text(text, path, report, args.min_characters)
        validate_required_phrases(text, args.require_text, path, report)
    elif args.require_text:
        report.add("error", "text-unavailable", "필수 문구를 검사할 텍스트를 얻지 못했습니다.", path)

    pages = report.metrics.get("pages")
    if isinstance(pages, int) and pages < args.min_pages:
        report.add(
            "error",
            "too-few-pages",
            f"페이지 수가 최소값보다 작습니다: {pages} < {args.min_pages}",
            path,
        )
    validate_source_dir(args.source_dir, report)
    return report


def print_report(report: Report, as_json: bool) -> None:
    payload = {
        "status": "fail" if report.errors else "pass",
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "metrics": report.metrics,
        "issues": [asdict(issue) for issue in report.issues],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for issue in report.issues:
        location = f" ({issue.location})" if issue.location else ""
        print(f"[{issue.severity.upper()}] {issue.code}: {issue.message}{location}")
    metrics = ", ".join(f"{key}={value}" for key, value in report.metrics.items())
    print(
        f"검증 결과: {payload['status'].upper()} | "
        f"오류 {payload['errors']}개 | 경고 {payload['warnings']}개"
    )
    if metrics:
        print(f"측정값: {metrics}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="생성된 학습노트 Markdown, TeX, DOCX 또는 PDF의 기본 무결성을 검증합니다."
    )
    parser.add_argument("note", type=Path, help="검증할 학습노트 파일")
    parser.add_argument("--source-dir", type=Path, help="원자료 폴더 존재와 파일 수를 함께 확인")
    parser.add_argument("--require-text", action="append", default=[], help="반드시 포함해야 하는 문구")
    parser.add_argument("--min-characters", type=int, default=200, help="권장 최소 추출 글자 수")
    parser.add_argument("--min-pages", type=int, default=1, help="PDF 최소 페이지 수")
    parser.add_argument("--strict", action="store_true", help="경고도 실패로 처리")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_characters < 0 or args.min_pages < 1:
        print("min-characters는 0 이상, min-pages는 1 이상이어야 합니다.", file=sys.stderr)
        return 2
    report = validate(args)
    print_report(report, args.json)
    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
