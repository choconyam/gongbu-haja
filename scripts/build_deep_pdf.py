"""Compile an authored TeX body with the shared DEEP style (deep_note_style.tex).

No Markdown conversion or semantic rewriting is attempted. Only trusted,
locally authored TeX belongs here: no-shell-escape is not a TeX sandbox.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping


TEMPLATE = Path(__file__).with_name("deep_note_template.tex")
# 표지·머리말·슬라이드·상자·간격의 단일 원본. 직접 제작 문서도 같은 파일을 \input한다.
STYLE = Path(__file__).with_name("deep_note_style.tex")
# 기준 디자인의 가변 글꼴. Windows에서 "모든 사용자용"이 아닌 "설치"로 넣은 글꼴은 사용자 폴더에 있다.
FONT_FILES = ("NotoSerifKR-VF.ttf", "NotoSansKR-VF.ttf")
FALLBACK_RE = re.compile(r"GONGBU-FONT-FALLBACK (\w+)=([^\r\n]+)")


def font_dir(environ: Mapping[str, str] | None = None) -> str | None:
    """가변 글꼴 두 개가 모두 있는 폴더를 TeX 경로(끝에 /)로 돌려준다. 없으면 None.

    GONGBU_FONT_DIR → Windows 글꼴 폴더 → 사용자 글꼴 폴더 순서로 찾는다. TeX 특수문자가 든 경로는
    쓰지 않고 스타일의 기본 대체 순서에 맡긴다.
    """
    environ = os.environ if environ is None else environ
    candidates = []
    if environ.get("GONGBU_FONT_DIR"):
        candidates.append(Path(environ["GONGBU_FONT_DIR"]))
    candidates.append(Path(environ.get("WINDIR") or "C:/Windows") / "Fonts")
    if environ.get("LOCALAPPDATA"):
        candidates.append(Path(environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    for folder in candidates:
        if all((folder / name).is_file() for name in FONT_FILES):
            text = folder.resolve().as_posix().rstrip("/") + "/"
            if not any(char in text for char in '#%{}~^"\\'):
                return text
    return None


def tex_text(value: str) -> str:
    escapes = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%",
               "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
               "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(escapes.get(char, char) for char in value)


def render_document(body: str, course: str, session: str, summary: str | None,
                    korean_font: str | None = None, fonts: str | None = None) -> str:
    if not body.strip():
        raise ValueError("DEEP TeX 본문이 비어 있습니다.")
    if re.search(r"\\(?:documentclass\b|begin\s*\{document\}|end\s*\{document\})", body):
        raise ValueError("DEEP 입력은 문서 전체가 아니라 TeX 본문 조각이어야 합니다.")
    font_setup = ""
    if korean_font:
        if not re.fullmatch(r"[\w .+-]+", korean_font):
            raise ValueError("한글 글꼴은 설치된 글꼴 이름으로 지정하십시오.")
        # 스타일의 기본 글꼴 선택(Noto 가변 글꼴 → Noto CJK → OS 기본) 뒤에 본문 한글 글꼴만 바꾼다.
        font_setup = rf"\setmainhangulfont{{{korean_font}}}"
    style = STYLE.resolve().as_posix()
    if '"' in style:
        raise ValueError(f"DEEP 스타일 경로에 큰따옴표가 있어 TeX에서 불러올 수 없습니다: {style}")
    if fonts is not None and (not fonts.endswith("/") or any(char in fonts for char in '#%{}~^"\\')):
        raise ValueError(f"글꼴 폴더는 /로 끝나는 TeX 경로여야 합니다: {fonts}")
    values = {
        "FONT_DIR": rf"\newcommand{{\GongbuFontDir}}{{{fonts}}}" if fonts else "",
        "STYLE": style,
        "KOREAN_FONT": font_setup,
        "HEADER_LEFT": tex_text(course),
        "HEADER_RIGHT": tex_text(session),
        "COURSE": tex_text(course),
        "SESSION": tex_text(session),
        "SUMMARY": tex_text(summary) if summary else "",
        "BODY": body,
    }
    # Single substitution pass: literal placeholder-like text in the body is preserved.
    return re.sub(r"%%(FONT_DIR|STYLE|KOREAN_FONT|HEADER_LEFT|HEADER_RIGHT|COURSE|SESSION|SUMMARY|BODY)%%",
                  lambda match: values[match[1]], TEMPLATE.read_text(encoding="utf-8"))


def build_deep(source: Path, output: Path, course: str, session: str,
               summary: str | None, korean_font: str | None = None) -> Path:
    executable = shutil.which("xelatex")
    if not executable:
        raise ValueError("XeLaTeX가 없습니다. TeX 환경을 준비하십시오. 일반 텍스트 PDF로 대체하지 않습니다.")
    document = render_document(source.read_text(encoding="utf-8-sig"), course,
                               session, summary, korean_font, font_dir())
    # Prevent MiKTeX from installing packages during a build. TeX Live has no
    # on-demand installer and does not accept this MiKTeX-specific flag.
    version = subprocess.run([executable, "--version"], capture_output=True,
                             timeout=15, check=True).stdout.decode("utf-8", errors="replace")
    installer = ["-disable-installer"] if "miktex" in version.lower() else []
    with tempfile.TemporaryDirectory(prefix="gongbu-deep-") as temporary:
        work = Path(temporary)
        tex = work / "note.tex"
        tex.write_text(document, encoding="utf-8")
        command = [executable, *installer, "-no-shell-escape", "-interaction=nonstopmode",
                   "-halt-on-error", f"-output-directory={work}", str(tex)]
        # The source directory, not the engine checkout, owns relative slide paths.
        for _ in range(2):
            result = subprocess.run(command, cwd=source.parent, capture_output=True, timeout=60)
            log_file = work / "note.log"
            log = (log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists()
                   else result.stdout.decode("utf-8", errors="replace"))
            if result.returncode:
                raise ValueError("DEEP 조판 실패. 기존 출력은 유지합니다.\n" + log[-4000:])
        issues = [line for line in log.splitlines() if any(token in line for token in
                  ("Missing character:", "Overfull ", "undefined references", "multiply defined"))]
        if issues:
            raise ValueError("DEEP 조판 검수 실패. 기존 출력은 유지합니다.\n" + "\n".join(issues))
        pdf = work / "note.pdf"
        if not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
            raise ValueError("유효한 PDF가 생성되지 않았습니다.")
        fallbacks = dict(FALLBACK_RE.findall(log))
        if fallbacks:
            used = ", ".join(f"{'본문' if role == 'serif' else '제목'} {name.strip()}" for role, name in fallbacks.items())
            print(f"[안내] Noto 가변 글꼴을 찾지 못해 대체 글꼴로 조판했습니다({used}). "
                  "QA 기록에 남기고, 글꼴이 다른 폴더에 있으면 GONGBU_FONT_DIR로 지정하십시오.", file=sys.stderr)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Publish only a successful build; failed builds never truncate an old PDF.
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".pdf", delete=False) as staging:
            staged = Path(staging.name)
        try:
            shutil.copyfile(pdf, staged)
            staged.replace(output)
        finally:
            staged.unlink(missing_ok=True)
    return output
