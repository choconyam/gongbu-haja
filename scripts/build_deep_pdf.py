"""Compile an authored TeX body using the minimal DEEP template.

No Markdown conversion or semantic rewriting is attempted. Only trusted,
locally authored TeX belongs here: no-shell-escape is not a TeX sandbox.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


TEMPLATE = Path(__file__).with_name("deep_note_template.tex")


def tex_text(value: str) -> str:
    escapes = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%",
               "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
               "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(escapes.get(char, char) for char in value)


def render_document(body: str, course: str, session: str, summary: str | None,
                    korean_font: str | None = None) -> str:
    if not body.strip():
        raise ValueError("DEEP TeX 본문이 비어 있습니다.")
    if re.search(r"\\(?:documentclass\b|begin\s*\{document\}|end\s*\{document\})", body):
        raise ValueError("DEEP 입력은 문서 전체가 아니라 TeX 본문 조각이어야 합니다.")
    if korean_font:
        if not re.fullmatch(r"[\w .+-]+", korean_font):
            raise ValueError("한글 글꼴은 설치된 글꼴 이름으로 지정하십시오.")
        font_setup = rf"\setmainhangulfont{{{korean_font}}}"
    else:
        font_setup = r"""\IfFontExistsTF{Malgun Gothic}{\setmainhangulfont{Malgun Gothic}}{
\IfFontExistsTF{Apple SD Gothic Neo}{\setmainhangulfont{Apple SD Gothic Neo}}{
\IfFontExistsTF{Noto Serif CJK KR}{\setmainhangulfont{Noto Serif CJK KR}}{
\PackageError{deep-note}{No Korean font found; use --tex-korean-font}{Install a Korean font first.}}}}"""
    values = {
        "KOREAN_FONT": font_setup,
        "TITLE": tex_text(f"{course}_{session}"),
        "SUMMARY": tex_text(summary) + r"\par" if summary else "",
        "BODY": body,
    }
    # Single substitution pass: literal placeholder-like text in the body is preserved.
    return re.sub(r"%%(KOREAN_FONT|TITLE|SUMMARY|BODY)%%",
                  lambda match: values[match[1]], TEMPLATE.read_text(encoding="utf-8"))


def build_deep(source: Path, output: Path, course: str, session: str,
               summary: str | None, korean_font: str | None = None) -> Path:
    executable = shutil.which("xelatex")
    if not executable:
        raise ValueError("XeLaTeX가 없습니다. TeX 환경을 준비하십시오. 일반 텍스트 PDF로 대체하지 않습니다.")
    document = render_document(source.read_text(encoding="utf-8-sig"), course,
                               session, summary, korean_font)
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
