#!/usr/bin/env python3
"""외부 패키지 없이 학습노트 에이전트 프로젝트 구조를 검사한다."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# -----------------------------------------------------------------------------
# 1. 저장소에 반드시 있어야 하는 역할·규칙·금지 잔여물
# 이 목록이 GitHub 배포본의 최소 구조 계약이다.
# -----------------------------------------------------------------------------

ROLE_FILES = (
    "manager.md",
    "transcriber.md",
    "transcript_auditor.md",
    "source_mapper.md",
    "writer.md",
    "instructor_integrator.md",
    "formula_code_checker.md",
    "pedagogy_editor.md",
    "layout_builder.md",
    "final_reviewer.md",
    "maintainer.md",
)

RULE_FILES = (
    "workflow.md",
    "orchestration.md",
    "transcription-workflow.md",
    "note-production-modes.md",
    "content-modes.md",
    "output-and-layout.md",
    "review-checklists.md",
)

REQUIRED_ROLE_HEADINGS = ("## 역할", "## 반드시 읽을 기준", "## 완료 조건")
# 과거 스킬 시절의 호출 토큰이 규칙 문서에 되살아나는 것만 막는다.
# (다중 도구 패키징 자체는 이제 공식 배포 채널이다 — validate_packaging 참조.)
FORBIDDEN_TOKENS = (
    "$study-notes-builder",
    "allow_implicit_invocation",
)
PACKAGE_NAME = "gongbu-haja"
REQUIRED_PRIVATE_GITIGNORE_PATTERNS = (
    "input/",
    "workspace/*",
    "*.wav",
    "*.mp3",
    "*.m4a",
    "*.webm",
    "*.mp4",
    ".env",
    ".env.*",
    "browser-profile/",
    ".auth/",
    "cookies*.json",
    "storage-state*.json",
)

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
BACKTICK_PATH_RE = re.compile(r"`([^`\n]+\.(?:md|py))`")


# -----------------------------------------------------------------------------
# 2. 오류·경고 수집
# 검사를 중간에 멈추지 않고 가능한 문제를 한 번에 모아 보여준다.
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
# 3. Markdown 파일과 내부 참조 검사
# UTF-8, 제목, 역할 필수 절, 깨진 상대경로를 확인한다.
# -----------------------------------------------------------------------------

def read_utf8(path: Path, report: Report) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        report.add("error", "encoding", f"UTF-8로 읽을 수 없습니다: {exc}", path)
    except OSError as exc:
        report.add("error", "read", f"파일을 읽을 수 없습니다: {exc}", path)
    return None


def normalize_reference(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    if value.startswith(("http://", "https://", "mailto:", "#")):
        return None
    value = unquote(value.split("#", 1)[0])
    return value or None


def validate_references(path: Path, text: str, report: Report) -> None:
    references = [match.group(1) for match in MARKDOWN_LINK_RE.finditer(text)]
    references.extend(match.group(1) for match in BACKTICK_PATH_RE.finditer(text))
    for raw in references:
        reference = normalize_reference(raw)
        if reference is None or any(token in reference for token in ("[", "]", "*")):
            continue
        target = (path.parent / reference).resolve()
        if not target.exists():
            report.add(
                "error",
                "broken-reference",
                f"참조 대상이 없습니다: {raw}",
                path,
            )


def validate_markdown_file(path: Path, report: Report, role: bool = False) -> str | None:
    if not path.exists():
        report.add("error", "missing-file", "필수 파일이 없습니다.", path)
        return None
    if path.stat().st_size == 0:
        report.add("error", "empty-file", "파일이 비어 있습니다.", path)
        return None

    text = read_utf8(path, report)
    if text is None:
        return None
    if not text.lstrip().startswith("# "):
        report.add("error", "missing-title", "첫 Markdown 제목이 없습니다.", path)
    if role:
        for heading in REQUIRED_ROLE_HEADINGS:
            if heading not in text:
                report.add("error", "missing-role-heading", f"필수 절이 없습니다: {heading}", path)
    for token in FORBIDDEN_TOKENS:
        if token in text:
            report.add("error", "skill-residue", f"설치형 스킬 잔여 표현이 있습니다: {token}", path)
    validate_references(path, text, report)
    return text


# -----------------------------------------------------------------------------
# 4. 프로젝트 전체 구조 검사
# 역할·규칙·Python 스크립트·배포 필수 파일을 하나의 게이트로 검사한다.
# -----------------------------------------------------------------------------

def validate_packaging(root: Path, report: Report) -> None:
    """스킬·플러그인·Codex 배포 채널의 파일들이 존재하고 서로 어긋나지 않는지 검사한다."""
    skill_root = root / "SKILL.md"
    skill_copy = root / "skills" / PACKAGE_NAME / "SKILL.md"
    if not skill_root.is_file():
        report.add("error", "missing-packaging", "루트 SKILL.md가 없습니다.", skill_root)
    else:
        text = read_utf8(skill_root, report)
        if text is not None:
            if not text.startswith("---"):
                report.add("error", "invalid-skill", "SKILL.md 앞머리(frontmatter)가 없습니다.", skill_root)
            if f"name: {PACKAGE_NAME}" not in text:
                report.add("error", "invalid-skill", f"SKILL.md name이 {PACKAGE_NAME}이 아닙니다.", skill_root)
    if not skill_copy.is_file():
        report.add("error", "missing-packaging", "플러그인용 SKILL.md 사본이 없습니다.", skill_copy)
    elif skill_root.is_file() and skill_root.read_bytes() != skill_copy.read_bytes():
        report.add(
            "error",
            "packaging-drift",
            "루트 SKILL.md와 skills/ 사본의 내용이 다릅니다. 한쪽만 고치지 말고 복사로 동기화하십시오.",
            skill_copy,
        )

    for relative, required_text in (
        (".claude-plugin/plugin.json", f'"name": "{PACKAGE_NAME}"'),
        (".claude-plugin/marketplace.json", f'"name": "{PACKAGE_NAME}"'),
    ):
        path = root / Path(relative)
        if not path.is_file():
            report.add("error", "missing-packaging", "플러그인 매니페스트가 없습니다.", path)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.add("error", "invalid-packaging", f"매니페스트 JSON을 읽을 수 없습니다: {exc}", path)
            continue
        if payload.get("name") != PACKAGE_NAME:
            report.add("error", "packaging-drift", f"매니페스트 name이 {PACKAGE_NAME}이 아닙니다.", path)
        if required_text not in path.read_text(encoding="utf-8-sig"):
            report.add("error", "packaging-drift", f"매니페스트에 {required_text}가 없습니다.", path)

    openai_yaml = root / "agents" / "openai.yaml"
    if not openai_yaml.is_file():
        report.add("error", "missing-packaging", "Codex 등록 파일(agents/openai.yaml)이 없습니다.", openai_yaml)
    else:
        text = read_utf8(openai_yaml, report)
        if text is not None and f"${PACKAGE_NAME}" not in text:
            report.add("error", "packaging-drift", f"openai.yaml에 ${PACKAGE_NAME} 호출 토큰이 없습니다.", openai_yaml)

    codex_config = root / ".codex" / "config.toml"
    codex_workers = {
        "study-note-worker.toml": ("gpt-5.6-luna", "high"),
        "faithful-note-reviewer.toml": ("gpt-5.6-luna", "max"),
        "quality-note-worker.toml": ("gpt-5.6-sol", "high"),
        "deep-note-reviewer.toml": ("gpt-5.6-sol", "xhigh"),
    }
    if not codex_config.is_file():
        report.add("error", "missing-packaging", "프로젝트 범위 Codex 하위 에이전트 설정이 없습니다.", codex_config)
    else:
        try:
            config_payload = tomllib.loads(codex_config.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            report.add("error", "invalid-packaging", f"Codex 설정 TOML을 읽을 수 없습니다: {exc}", codex_config)
        else:
            agents = config_payload.get("agents", {})
            if agents.get("default_subagent_model") != "gpt-5.6-luna":
                report.add("error", "cost-routing-drift", "기본 Codex 하위 모델이 gpt-5.6-luna가 아닙니다.", codex_config)
            if agents.get("default_subagent_reasoning_effort") != "high":
                report.add("error", "cost-routing-drift", "기본 Codex 하위 추론 강도가 high가 아닙니다.", codex_config)
            concurrency = agents.get("max_concurrent_threads_per_session")
            if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 2:
                report.add("error", "cost-routing-drift", "동시 하위 에이전트 수는 1~2여야 합니다.", codex_config)

    codex_agent_dir = root / ".codex" / "agents"
    for path in codex_agent_dir.glob("*.toml") if codex_agent_dir.is_dir() else ():
        text = read_utf8(path, report)
        if text is not None and "gpt-5.6-terra" in text:
            report.add(
                "error",
                "cost-routing-drift",
                "Terra는 이 프로젝트의 실행 프로필에서 사용하지 않습니다.",
                path,
            )
    for filename, (expected_model, expected_effort) in codex_workers.items():
        path = codex_agent_dir / filename
        if not path.is_file():
            report.add("error", "missing-packaging", "Codex 역할 프로필이 없습니다.", path)
            continue
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            report.add("error", "invalid-packaging", f"Codex 역할 TOML을 읽을 수 없습니다: {exc}", path)
            continue
        for field in ("name", "description", "developer_instructions"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                report.add("error", "invalid-packaging", f"Codex 역할에 {field}가 없습니다.", path)
        if payload.get("model") != expected_model or payload.get("model_reasoning_effort") != expected_effort:
            report.add(
                "error",
                "cost-routing-drift",
                f"Codex 역할 모델은 {expected_model}/{expected_effort}여야 합니다.",
                path,
            )


def validate(root: Path) -> Report:
    report = Report()
    if not root.exists() or not root.is_dir():
        report.add("error", "missing-root", "에이전트 루트 폴더가 없습니다.", root)
        return report

    validate_packaging(root, report)

    main_path = root / "note_final_rules.md"
    main_text = validate_markdown_file(main_path, report)

    # README는 GitHub용 문서이므로 일반 작업 문맥에서 제외한다는 경계가
    # 프로젝트 진입 지침에 실제로 적혀 있는지 확인한다.
    agents_path = root / "AGENTS.md"
    agents_text = validate_markdown_file(agents_path, report)
    if agents_text is not None:
        if "`README.md`는 GitHub" not in agents_text or "일반 학습노트 작업에서는 읽지 않으며" not in agents_text:
            report.add(
                "error",
                "missing-readme-runtime-boundary",
                "README를 일반 실행 문맥에서 제외하는 지침이 없습니다.",
                agents_path,
            )

    role_dir = root / "agent_prompts"
    if not role_dir.is_dir():
        report.add("error", "missing-directory", "agent_prompts 폴더가 없습니다.", role_dir)
    else:
        actual_roles = {path.name for path in role_dir.glob("*.md")}
        expected_roles = set(ROLE_FILES)
        for name in sorted(expected_roles - actual_roles):
            report.add("error", "missing-role", "필수 역할 프롬프트가 없습니다.", role_dir / name)
        for name in sorted(actual_roles - expected_roles):
            report.add("warning", "extra-role", "정의되지 않은 추가 역할 프롬프트입니다.", role_dir / name)
        for name in ROLE_FILES:
            text = validate_markdown_file(role_dir / name, report, role=True)
            if main_text is not None and f"agent_prompts/{name}" not in main_text:
                report.add(
                    "error",
                    "unregistered-role",
                    "공통 최종 규칙에 역할이 등록되어 있지 않습니다.",
                    role_dir / name,
                )

    rule_dir = root / "rules"
    if not rule_dir.is_dir():
        report.add("error", "missing-directory", "rules 폴더가 없습니다.", rule_dir)
    else:
        for name in RULE_FILES:
            path = rule_dir / name
            validate_markdown_file(path, report)
            if main_text is not None and f"rules/{name}" not in main_text:
                report.add(
                    "error",
                    "unregistered-rule",
                    "공통 최종 규칙에 세부 규칙이 등록되어 있지 않습니다.",
                    path,
                )

    script_dir = root / "scripts"
    for name in (
        "validate_agent_setup.py",
        "manage_run.py",
        "test_manage_run.py",
        "test_validate_note_output.py",
        "test_record_lecture.py",
        "test_transcribe_lecture.py",
        "test_transcribe_batch.py",
        "test_prepare_transcript_review.py",
        "test_select_review_packets.py",
        "test_apply_transcript_corrections.py",
        "test_validate_source_coverage.py",
        "project_types.py",
        "record_lecture.py",
        "transcribe_lecture.py",
        "transcribe_batch.py",
        "prepare_transcript_review.py",
        "select_review_packets.py",
        "apply_transcript_corrections.py",
        "validate_source_coverage.py",
        "validate_transcript_package.py",
        "validate_note_output.py",
    ):
        path = script_dir / name
        if not path.exists():
            report.add("error", "missing-script", "필수 Python 스크립트가 없습니다.", path)

    for name in (
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        "README.md",
        ".gitignore",
        ".gitattributes",
        "requirements-recording.txt",
        "requirements-transcription.txt",
        "강의녹음.bat",
        "강의전사.bat",
        "배치전사.bat",
    ):
        path = root / name
        if not path.exists():
            report.add("error", "missing-project-file", "통합 프로젝트 필수 파일이 없습니다.", path)

    gitignore_path = root / ".gitignore"
    if gitignore_path.is_file():
        gitignore_text = read_utf8(gitignore_path, report)
        if gitignore_text is not None:
            ignored_patterns = {
                line.strip()
                for line in gitignore_text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            for pattern in REQUIRED_PRIVATE_GITIGNORE_PATTERNS:
                if pattern not in ignored_patterns:
                    report.add(
                        "error",
                        "missing-private-gitignore",
                        f"강의·인증 정보 보호용 Git 제외 규칙이 없습니다: {pattern}",
                        gitignore_path,
                    )

    return report


# -----------------------------------------------------------------------------
# 5. 사람용/자동화용 출력과 종료 코드
# --json은 CI가 읽고, --strict는 경고까지 실패로 처리한다.
# -----------------------------------------------------------------------------

def print_report(root: Path, report: Report, as_json: bool) -> None:
    payload = {
        "root": str(root),
        "status": "fail" if report.errors else "pass",
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "issues": [asdict(issue) for issue in report.issues],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    for issue in report.issues:
        location = f" ({issue.location})" if issue.location else ""
        print(f"[{issue.severity.upper()}] {issue.code}: {issue.message}{location}")
    print(
        f"검증 결과: {payload['status'].upper()} | "
        f"오류 {payload['errors']}개 | 경고 {payload['warnings']}개 | {root}"
    )


def parse_args() -> argparse.Namespace:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="학습노트 에이전트 폴더 구조와 참조를 검증합니다.")
    parser.add_argument("root", nargs="?", type=Path, default=default_root)
    parser.add_argument("--strict", action="store_true", help="경고도 실패로 처리합니다.")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력합니다.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    report = validate(root)
    print_report(root, report, args.json)
    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
