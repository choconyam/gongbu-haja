#!/usr/bin/env python3
"""토큰을 낭비하지 않는 학습노트 에이전트 실행 상태를 만든다.

이 파일은 LLM을 직접 호출하지 않는다. 입력 파일의 증거를 기록하고,
필요한 역할만 활성화하며, 선행 역할을 건너뛴 완료 처리를 막는다.
실제 역할별 에이전트를 실행하는 주체는 관리자 에이전트다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .execution_profiles import (
        EXECUTION_PROFILES,
        RUNTIMES,
        ProfileError,
        detect_runtime,
        resolve as resolve_execution_profile,
        runtime_table,
    )
    from .project_types import AUDIO_SUFFIXES
    from .validate_source_coverage import CoverageValidationError, validate_coverage
except ImportError:  # `python scripts/manage_run.py`로 직접 실행할 때
    from execution_profiles import (
        EXECUTION_PROFILES,
        RUNTIMES,
        ProfileError,
        detect_runtime,
        resolve as resolve_execution_profile,
        runtime_table,
    )
    from project_types import AUDIO_SUFFIXES
    from validate_source_coverage import CoverageValidationError, validate_coverage

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# -----------------------------------------------------------------------------
# 1. 실행 계획의 고정 정의
# 역할 순서와 파일 확장자 분류 기준은 모든 실행 상태가 공유한다.
# -----------------------------------------------------------------------------

SCHEMA_VERSION = 3
# 이 스크립트가 속한 엔진 사본의 루트. 저장소를 열었으면 저장소 루트, pipx 설치본이면
# gongbu_haja/engine/ 이다. 역할 프롬프트·규칙은 여기서 상대 경로로 찾는다.
ENGINE_ROOT = Path(__file__).resolve().parent.parent
LEGACY_SCHEMA_VERSIONS = {1, 2}
DEFAULT_NOTE_MODE = "faithful"
NOTE_MODE_CONFIG = {
    "faithful": {
        "label": "자료 충실형",
        "description": "교안과 검수된 교수 설명만 간결하게 정리하고 외부 배경지식·새 유도는 추가하지 않음",
        "external_enrichment": False,
        "pedagogy_editor_default": False,
    },
    "deep": {
        "label": "심화 이해형",
        "description": "필요한 배경지식·중간 사고·유도 과정·예시를 보강하고 보강 내용을 원자료와 구분함",
        "external_enrichment": True,
        "pedagogy_editor_default": True,
        "output_rules": "rules/deep-output-contract.md",
        "pdf_build": "--note-mode deep; authored TeX body; XeLaTeX; full-page visual QA",
    },
}
NOTE_MODES = tuple(NOTE_MODE_CONFIG)

# 모델 이름 자체보다 실행 책임을 먼저 고정한다. 결정적으로 끝낼 수 있는 일은
# 모델에 보내지 않고, 의미 판단이 필요한 좁은 입력만 저비용 서브에이전트에
# 전달한다. 프로필(EXECUTION_PROFILES)에는 모델명이 없고, 런타임(Codex·Claude)별
# 실제 모델·effort는 execution_profiles.py의 표를 resolve_state_profile()로 해석한다.

COST_POLICY = {
    "deterministic_first": True,
    "default_subagent_profile": "economy_high",
    "faithful_writer_profile": "quality_high",
    "faithful_final_review_profile": "review_high",
    "deep_writer_profile": "quality_high",
    "deep_final_review_profile": "quality_xhigh",
    "targeted_escalation_profiles": ["quality_high", "quality_xhigh"],
    "automatic_flagship_escalation": False,
    "terra_enabled": False,
    "full_role_retries": 0,
    "targeted_repairs": 1,
    "targeted_escalations": 1,
    # 최종 검수가 반려한 내용 결함을 고치려고 집필 이후 역할을 다시 여는 횟수(강의당).
    "review_repairs": 2,
    "premium_final_reviews_per_cycle": 1,
    "max_agent_packet_bytes": 16 * 1024,
    "rule": "결정적 누락 게이트 뒤 모드별 작성·검수를 한 번 실행하고 실패 범위만 국소 재검수",
}

MAX_REPAIR_SCOPE_CHARS = 240
# 모드별 기본 최종 형식. 자료 충실형은 바로 읽고 고치는 md, 심화 이해형은 인쇄용 pdf.
DEFAULT_OUTPUT_FORMATS = {"faithful": "md", "deep": "pdf"}
MAX_AGENT_PACKET_BYTES = int(COST_POLICY["max_agent_packet_bytes"])
CRITICAL_REVIEW_LIMIT = int(COST_POLICY["targeted_escalations"])
PREMIUM_FINAL_REVIEW_LIMIT = int(COST_POLICY["premium_final_reviews_per_cycle"])
REVIEW_REPAIR_LIMIT = int(COST_POLICY["review_repairs"])
CRITICAL_REVIEW_CATEGORIES = (
    "number",
    "proper_noun",
    "formula",
    "assessment_condition",
    "source_conflict",
    "coverage_ambiguity",
    "logic_gap",
    "derivation_gap",
    "instructor_distortion",
    "final_blocker",
)

# 국소 승격은 역할 이름만 보고 허용하지 않는다. 첫 의미 작업에서 실제로 남은
# 위험 종류까지 일치해야 하며, 강의 전체에서 패킷 하나만 사용할 수 있다.
ESCALATION_RULES: dict[str, dict[str, set[str]]] = {
    "faithful": {
        "transcript_auditor": {"number", "proper_noun", "formula", "assessment_condition"},
        "source_mapper": {
            "number",
            "proper_noun",
            "formula",
            "assessment_condition",
            "source_conflict",
            "coverage_ambiguity",
            "instructor_distortion",
        },
        "writer": {"source_conflict", "coverage_ambiguity", "instructor_distortion"},
        "instructor_integrator": {"source_conflict", "instructor_distortion"},
        "formula_code_checker": {"number", "formula", "source_conflict"},
        "pedagogy_editor": {"coverage_ambiguity", "instructor_distortion"},
        "final_reviewer": {
            "source_conflict",
            "coverage_ambiguity",
            "instructor_distortion",
            "final_blocker",
        },
    },
    "deep": {
        "transcript_auditor": {"number", "proper_noun", "formula", "assessment_condition"},
        "source_mapper": {
            "number",
            "proper_noun",
            "formula",
            "assessment_condition",
            "source_conflict",
            "coverage_ambiguity",
            "instructor_distortion",
        },
        "writer": {"source_conflict", "logic_gap", "derivation_gap", "instructor_distortion"},
        "instructor_integrator": {"source_conflict", "instructor_distortion"},
        "formula_code_checker": {"number", "formula", "source_conflict", "derivation_gap"},
        "pedagogy_editor": {"logic_gap", "derivation_gap", "instructor_distortion"},
    },
}

PREMIUM_FINAL_REVIEW_ROUTES = {
    "faithful": {
        "route": "faithful_final_review_high",
        "profile": "review_high",
    },
    "deep": {
        "route": "deep_final_sol_xhigh",
        "profile": "quality_xhigh",
    },
}

def role_execution_policy(note_mode: str) -> dict[str, dict[str, Any]]:
    """제작 모드별로 실제 모델 책임을 고정한다.

    자료 추출·색인·빌드는 모드와 무관하게 Python/economy 경로를 유지한다.
    집필·의미 통합은 두 모드 모두 quality_high다. 독립 최종 검수는 faithful이
    review_high(상위 모델 high, source unit 대조), deep이 quality_xhigh(완성본 논리 검수)다.
    """

    if note_mode not in NOTE_MODE_CONFIG:
        raise RunError(f"지원하지 않는 학습노트 제작 모드입니다: {note_mode}")
    deep = note_mode == "deep"
    # 집필·의미 통합은 두 모드 모두 상위 모델(quality_high)이다. 경량 모델 집필은
    # 교수 설명을 축약한다는 실전 결과에 따라 faithful에서도 쓰지 않는다.
    author_profile = "quality_high"
    author_escalation = "quality_xhigh"
    final_profile = "quality_xhigh" if deep else "review_high"
    return {
        "transcriber": {
            "executor": "python",
            "primary_profile": "local_python",
            "agent_profile": None,
            "repair_profile": "local_python",
            "escalation_profile": None,
            "scope": "로컬 음성 인식과 전사 패키지 생성",
        },
        "transcript_auditor": {
            "executor": "hybrid",
            "primary_profile": "local_python",
            "agent_profile": "economy_high",
            "repair_profile": "economy_high",
            "escalation_profile": "quality_high",
            "scope": "Python이 이상 후보와 문맥 패킷을 만들고 Luna high가 의미를 판정",
        },
        "source_mapper": {
            "executor": "hybrid",
            "primary_profile": "local_python",
            "agent_profile": "economy_high",
            "repair_profile": "economy_high",
            "escalation_profile": "quality_high",
            "scope": "Python 인벤토리·안정 ID 뒤 Luna high가 자료 간 의미 대응을 판정",
        },
        "writer": {
            "executor": "subagent",
            "primary_profile": author_profile,
            "agent_profile": author_profile,
            "repair_profile": author_profile,
            "escalation_profile": author_escalation,
            "scope": (
                "단원별 근거 패킷으로 배경·중간 사고·유도를 포함한 심화 노트 작성"
                if deep
                else "페이지별 근거 패킷만 사용해 자료 충실형 노트 작성"
            ),
        },
        "instructor_integrator": {
            "executor": "subagent",
            "primary_profile": author_profile,
            "agent_profile": author_profile,
            "repair_profile": author_profile,
            "escalation_profile": author_escalation,
            "scope": "누락이 확인된 교수 고유 설명만 현재 노트에 반영",
        },
        "formula_code_checker": {
            "executor": "hybrid",
            "primary_profile": "local_python",
            "agent_profile": author_profile,
            "repair_profile": author_profile,
            "escalation_profile": author_escalation,
            "scope": "Python 계산·실행·컴파일 뒤 선택 모드의 의미 모델이 설명을 검수",
        },
        "pedagogy_editor": {
            "executor": "subagent",
            "primary_profile": author_profile,
            "agent_profile": author_profile,
            "repair_profile": author_profile,
            "escalation_profile": author_escalation,
            "scope": "지정된 절의 이해 흐름과 필요한 중간 사고만 보강",
        },
        "layout_builder": {
            # 조판은 내용을 바꾸지 않는 결정적 작업이다. scripts/build_study_note_pdf.py로
            # 빌드하고 validate_note_output.py로 검사하며, 렌더 표본은 관리자가 확인한다.
            # 그래서 final_reviewer는 조판을 기다리지 않고 집필 초안을 바로 검수한다.
            "executor": "python",
            "primary_profile": "local_python",
            "agent_profile": None,
            "repair_profile": "local_python",
            "escalation_profile": None,
            "scope": (
                "scripts/build_study_note_pdf.py --note-mode deep: 승인 원고와 TeX 동등성 대조·XeLaTeX 빌드·전체 쪽 및 수식·슬라이드 시각 검수"
                if deep else "scripts/build_study_note_pdf.py 결정적 빌드·구조 검사·렌더 표본 확인"
            ),
        },
        "final_reviewer": {
            "executor": "subagent",
            "primary_profile": final_profile,
            "agent_profile": final_profile,
            "repair_profile": None,
            "escalation_profile": "quality_xhigh" if not deep else None,
            "scope": (
                "완성본 전체를 한 번 읽고 논리·유도·교수 설명 왜곡을 독립 검수"
                if deep
                else "전 source unit을 최종 노트와 대조해 누락·왜곡·중복을 독립 검수"
            ),
        },
        "maintainer": {
            "executor": "python",
            "primary_profile": "local_python",
            "agent_profile": None,
            "repair_profile": "local_python",
            "escalation_profile": None,
            "scope": "검증된 최종 파일의 이동·목록·해시 기록",
        },
    }

ROLE_ORDER = (
    "transcriber",
    "transcript_auditor",
    "source_mapper",
    "writer",
    "instructor_integrator",
    "formula_code_checker",
    "pedagogy_editor",
    "layout_builder",
    "final_reviewer",
    "maintainer",
)
ROLE_PROMPTS = {role: f"agent_prompts/{role}.md" for role in ROLE_ORDER}
OPTIONAL_ROLES = {
    "transcriber",
    "transcript_auditor",
    "instructor_integrator",
    "formula_code_checker",
    "pedagogy_editor",
    "maintainer",
}
INPUT_KINDS = {"audio", "transcript", "code", "document", "image", "text", "other"}

TRANSCRIPT_EXTENSIONS = {".srt", ".vtt"}
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".epub", ".hwp", ".hwpx", ".odp", ".pdf", ".ppt", ".pptx"}
IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".html", ".ipynb", ".java", ".js", ".jsx",
    ".kt", ".m", ".mat", ".mlx", ".py", ".r", ".rs", ".sql", ".swift", ".ts", ".tsx",
}
TEXT_EXTENSIONS = {".csv", ".json", ".md", ".rst", ".tex", ".tsv", ".txt", ".yaml", ".yml"}
TRANSCRIPT_NAME_RE = re.compile(r"(?:transcript|transcription|caption|subtitle|전사|자막|녹취)", re.IGNORECASE)
# Windows가 경로 끝에서 조용히 제거하는 점·공백을 금지해, 서로 다른 lecture_id가
# 같은 폴더로 정규화되어 상태·락이 충돌하는 사고를 막는다.
LECTURE_ID_RE = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]+(?<![. ])$")


# -----------------------------------------------------------------------------
# 2. 파일 증거와 입력 인벤토리
# 원본과 산출물에 SHA-256을 남겨, 통과 후 바뀐 파일의 재사용을 막는다.
# -----------------------------------------------------------------------------

class RunError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_cost_usage() -> dict[str, Any]:
    """새 실행의 누적 비용 원장을 만든다."""

    return {
        "critical_review_limit": CRITICAL_REVIEW_LIMIT,
        "critical_reviews": [],
        "premium_final_review_limit_per_cycle": PREMIUM_FINAL_REVIEW_LIMIT,
        "premium_final_reviews": [],
        "review_repair_limit": REVIEW_REPAIR_LIMIT,
        "review_repairs": [],
    }


def resolve_state_profile(state: dict[str, Any], profile: str) -> dict[str, Any]:
    """상태에 기록된 런타임과 모델표 스냅샷으로 프로필을 실제 모델·effort로 해석한다.

    프로젝트 표가 나중에 바뀌어도 과거 실행은 자기 스냅샷 기준으로 해석·검증된다.
    """

    table = state.get("runtime_model_table")
    try:
        return resolve_execution_profile(
            profile, state.get("runtime"), table if isinstance(table, dict) else None
        )
    except ProfileError as exc:
        raise RunError(str(exc)) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: Path) -> dict[str, Any]:
    if path.is_file():
        return {
            "path": str(path),
            "kind": "file",
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        digest = hashlib.sha256()
        file_count = 0
        total_bytes = 0
        for child in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda p: p.as_posix().lower()):
            relative = child.relative_to(path).as_posix()
            child_hash = sha256_file(child)
            child_bytes = child.stat().st_size
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(child_hash.encode("ascii"))
            digest.update(b"\0")
            file_count += 1
            total_bytes += child_bytes
        return {
            "path": str(path),
            "kind": "directory",
            "files": file_count,
            "bytes": total_bytes,
            "sha256": digest.hexdigest(),
        }
    raise RunError(f"산출물 유형을 확인할 수 없습니다: {path}")


def artifact_matches(record: dict[str, Any]) -> bool:
    path = Path(record["path"])
    if not path.exists():
        return False
    current = artifact_record(path)
    return all(current.get(key) == record.get(key) for key in ("kind", "bytes", "sha256"))


def classify(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in TRANSCRIPT_EXTENSIONS or (suffix in {".md", ".txt"} and TRANSCRIPT_NAME_RE.search(path.stem)):
        return "transcript"
    if suffix in CODE_EXTENSIONS:
        return "code"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return "other"


def parse_classification_overrides(
    input_root: Path,
    values: list[str],
    existing: dict[str, str] | None = None,
) -> dict[str, str]:
    overrides = dict(existing or {})
    for raw in values:
        if "=" not in raw:
            raise RunError(f"분류 지정은 파일=유형 형식이어야 합니다: {raw}")
        raw_path, kind = raw.rsplit("=", 1)
        kind = kind.strip().lower()
        if kind not in INPUT_KINDS:
            raise RunError(f"지원하지 않는 입력 유형입니다: {kind}")
        candidate = Path(raw_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = input_root / candidate
        candidate = candidate.resolve()
        try:
            relative = candidate.relative_to(input_root).as_posix()
        except ValueError as exc:
            raise RunError(f"분류 대상은 입력 폴더 안에 있어야 합니다: {candidate}") from exc
        if not candidate.is_file():
            raise RunError(f"분류할 입력 파일이 없습니다: {candidate}")
        overrides[relative] = kind
    return overrides


# 과목 폴더를 통째로 입력으로 쓰면 우리가 만든 상태(.gongbu/)·최종 노트(output/)와
# 숨김 파일이 입력으로 되돌아와 해시가 계속 바뀐다. 입력 목록에서 제외한다.
IGNORED_INPUT_DIR_NAMES = frozenset({".gongbu", "output", "workspace", "__pycache__"})


def iter_input_files(input_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in input_root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(input_root).parts
        if any(part in IGNORED_INPUT_DIR_NAMES or part.startswith(".") for part in parts):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.as_posix().lower())


def inventory(input_root: Path, overrides: dict[str, str] | None = None) -> list[dict[str, Any]]:
    if not input_root.exists() or not input_root.is_dir():
        raise RunError(f"입력 폴더가 없습니다: {input_root}")
    files = iter_input_files(input_root)
    if not files:
        raise RunError(f"입력 폴더가 비어 있습니다: {input_root}")
    result: list[dict[str, Any]] = []
    for path in files:
        stat = path.stat()
        relative = path.relative_to(input_root).as_posix()
        result.append(
            {
                "path": relative,
                "kind": (overrides or {}).get(relative, classify(path)),
                "bytes": stat.st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


# -----------------------------------------------------------------------------
# 3. 조건부 역할 계획
# 녹음·전사·코드 존재 여부로 초기 역할을 고르고 선행 관계를 연결한다.
# PDF 속 수식처럼 확장자만으로 알 수 없는 항목은 관리자가 나중에 활성화한다.
# -----------------------------------------------------------------------------

def role_entry(active: bool, reason: str, dependencies: list[str]) -> dict[str, Any]:
    return {
        "active": active,
        "activation_source": "automatic" if active else None,
        "reason": reason,
        "status": "blocked" if active and dependencies else ("ready" if active else "skipped"),
        "dependencies": dependencies,
        "prompt": "",
        "attempts": 0,
        "max_attempts": 2,
        "repair_scope": None,
        "repair_packet": None,
        "active_profile": None,
        "critical_review_call_id": None,
        "premium_call_id": None,
        "coverage_gate": None,
        "rerun_count": 0,
        "started_at": None,
        "completed_at": None,
        "artifacts": [],
        "failure_reason": None,
    }


def make_roles(
    items: list[dict[str, Any]],
    output_format: str,
    note_mode: str = DEFAULT_NOTE_MODE,
) -> dict[str, dict[str, Any]]:
    if note_mode not in NOTE_MODE_CONFIG:
        raise RunError(f"지원하지 않는 학습노트 제작 모드입니다: {note_mode}")
    kinds = {item["kind"] for item in items}
    has_audio = "audio" in kinds
    has_transcript = "transcript" in kinds
    has_code = "code" in kinds

    transcriber_active = has_audio and not has_transcript
    auditor_active = has_audio or has_transcript
    # 전사가 있다는 사실만으로 교수 고유 설명이 있다고 단정하지 않는다.
    # 전사 검수·자료 매핑에서 실제 고유 설명이 발견된 뒤 관리자가 활성화한다.
    integrator_active = False
    formula_active = has_code

    roles: dict[str, dict[str, Any]] = {}
    roles["transcriber"] = role_entry(
        transcriber_active,
        "녹음은 있으나 사용할 전사본이 없음" if transcriber_active else "기존 전사 사용 또는 녹음 없음",
        [],
    )
    auditor_deps = ["transcriber"] if transcriber_active else []
    roles["transcript_auditor"] = role_entry(
        auditor_active,
        "녹음 또는 전사본이 있음" if auditor_active else "녹음과 전사본이 없음",
        auditor_deps,
    )
    mapper_deps = ["transcript_auditor"] if auditor_active else []
    roles["source_mapper"] = role_entry(True, "모든 작업의 필수 자료 매핑", mapper_deps)
    roles["writer"] = role_entry(True, "학습노트 초안 작성", ["source_mapper"])
    roles["instructor_integrator"] = role_entry(
        integrator_active,
        (
            "전사에서 교수 고유 설명이 확인됨"
            if integrator_active
            else (
                "전사 검수·자료 매핑 후 고유 설명 발견 시 활성화"
                if has_audio or has_transcript
                else "교수 발언 계층이 없음"
            )
        ),
        ["writer", "transcript_auditor"],
    )
    roles["formula_code_checker"] = role_entry(
        formula_active,
        "코드 파일이 발견됨" if formula_active else "초기 자동 판정에서 코드 파일 없음; 수식 발견 시 활성화",
        ["writer"],
    )
    pedagogy_active = bool(NOTE_MODE_CONFIG[note_mode]["pedagogy_editor_default"])
    roles["pedagogy_editor"] = role_entry(
        pedagogy_active,
        (
            "심화 이해형의 배경지식·중간 사고·유도 과정 보강"
            if pedagogy_active
            else "자료 충실형에서는 추가 설명 보강을 기본 생략"
        ),
        ["writer"],
    )

    layout_dependencies = ["writer"]
    for role in ("instructor_integrator", "formula_code_checker", "pedagogy_editor"):
        if roles[role]["active"]:
            layout_dependencies.append(role)
    roles["layout_builder"] = role_entry(True, f"{output_format} 최종 형식 생성", layout_dependencies)
    # 최종 검수는 집필 초안(추적 주석 포함)을 대상으로 하며 조판과 병렬로 돈다.
    roles["final_reviewer"] = role_entry(True, "독립 최종 검수", list(layout_dependencies))
    # 완성본 전체를 읽는 고비용 검수는 한 review cycle에 정확히 한 번만 실행한다.
    # 발견된 문제는 같은 호출 안에서 국소 수정·해당 위치 재확인까지 끝낸다.
    roles["final_reviewer"]["max_attempts"] = 1
    roles["maintainer"] = role_entry(
        False,
        "복수 최종 파일의 이동·패키징·전달 정리가 필요할 때만 활성화",
        ["layout_builder", "final_reviewer"],
    )

    execution_policy = role_execution_policy(note_mode)
    for name, entry in roles.items():
        entry["prompt"] = ROLE_PROMPTS[name]
        entry["execution"] = dict(execution_policy[name])
    refresh_statuses(roles)
    return roles


def dependencies_for_activation(roles: dict[str, dict[str, Any]], role: str) -> list[str]:
    if role == "transcriber":
        return []
    if role == "transcript_auditor":
        return ["transcriber"] if roles["transcriber"]["active"] else []
    if role == "instructor_integrator":
        return ["writer", "transcript_auditor"]
    if role in {"formula_code_checker", "pedagogy_editor"}:
        return ["writer"]
    if role == "maintainer":
        return ["final_reviewer"]
    return list(roles[role]["dependencies"])


def normalize_dependencies(roles: dict[str, dict[str, Any]]) -> None:
    """활성 상태를 기준으로 파이프라인의 선행 관계를 한곳에서 다시 계산한다."""

    roles["transcriber"]["dependencies"] = []
    roles["transcript_auditor"]["dependencies"] = (
        ["transcriber"]
        if roles["transcript_auditor"]["active"] and roles["transcriber"]["active"]
        else []
    )
    roles["source_mapper"]["dependencies"] = (
        ["transcript_auditor"] if roles["transcript_auditor"]["active"] else []
    )
    roles["writer"]["dependencies"] = ["source_mapper"]
    roles["instructor_integrator"]["dependencies"] = ["writer", "transcript_auditor"]
    roles["formula_code_checker"]["dependencies"] = ["writer"]
    roles["pedagogy_editor"]["dependencies"] = ["writer"]
    roles["layout_builder"]["dependencies"] = ["writer"] + [
        role
        for role in ("instructor_integrator", "formula_code_checker", "pedagogy_editor")
        if roles[role]["active"]
    ]
    roles["final_reviewer"]["dependencies"] = list(roles["layout_builder"]["dependencies"])
    roles["maintainer"]["dependencies"] = ["layout_builder", "final_reviewer"]


def apply_role_overrides(
    roles: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, str]],
) -> None:
    for role, override in overrides.items():
        if role not in OPTIONAL_ROLES:
            continue
        entry = roles[role]
        if override.get("mode") == "active":
            entry["active"] = True
            entry["activation_source"] = "manual"
            entry["reason"] = override.get("reason", "관리자 수동 활성화")
            entry["dependencies"] = dependencies_for_activation(roles, role)
            entry["status"] = "blocked" if entry["dependencies"] else "ready"
        elif override.get("mode") == "inactive":
            entry["active"] = False
            entry["activation_source"] = "manual"
            entry["reason"] = override.get("reason", "관리자 수동 비활성화")
            entry["status"] = "skipped"
            entry["artifacts"] = []
            entry["active_profile"] = None
            entry["critical_review_call_id"] = None
            entry["premium_call_id"] = None
            entry["coverage_gate"] = None

    normalize_dependencies(roles)
    if roles["instructor_integrator"]["active"] and not roles["transcript_auditor"]["active"]:
        raise RunError("instructor_integrator를 사용하려면 transcript_auditor가 활성 상태여야 합니다.")
    refresh_statuses(roles)


def refresh_statuses(roles: dict[str, dict[str, Any]]) -> None:
    changed = True
    while changed:
        changed = False
        for entry in roles.values():
            if not entry["active"] or entry["status"] in {"running", "passed", "failed"}:
                continue
            dependencies_passed = all(roles[dep]["status"] == "passed" for dep in entry["dependencies"])
            new_status = "ready" if dependencies_passed else "blocked"
            if entry["status"] != new_status:
                entry["status"] = new_status
                changed = True


# -----------------------------------------------------------------------------
# 4. 실행 상태 저장과 안전한 상태 전이
# JSON은 임시 파일에 먼저 쓴 뒤 교체하여 중간에 깨진 상태가 남지 않게 한다.
# -----------------------------------------------------------------------------

def state_path_for(root: Path, lecture_id: str) -> Path:
    return root / "workspace" / lecture_id / "run_state.json"


@contextmanager
def state_write_lock(state_file: Path, create_parent: bool = False):
    """같은 강의 상태를 두 프로세스가 동시에 고치지 못하게 락 파일로 막는다.

    강의(lecture_id)별 병렬 실행은 상태 파일이 서로 달라 자유롭게 허용되고,
    이 락은 동일 강의에 세션이 둘 붙는 사고만 차단한다. 락은 명령 하나가
    실행되는 동안만 유지되는 명령 단위 뮤텍스이며 세션 점유를 뜻하지 않는다.
    """
    lock_path = state_file.with_name(state_file.name + ".lock")
    if create_parent:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    elif not lock_path.parent.is_dir():
        # 잘못 입력한 경로에 흔적 디렉터리를 만들지 않는다.
        raise RunError(f"실행 상태 파일이 없습니다: {state_file}")
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = ""
        try:
            holder = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        detail = f" | 락 정보: {holder}" if holder else ""
        raise RunError(
            f"다른 프로세스가 같은 강의 상태를 수정 중입니다: {state_file}{detail}\n"
            "같은 강의에는 세션 하나만 사용하십시오. 중단된 실행이 남긴 락이 확실할 때만 "
            f"락 파일을 직접 삭제한 뒤 다시 시도하십시오: {lock_path}"
        ) from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} at={now_iso()}\n")
        yield
    finally:
        # Windows에서는 다른 프로세스가 락 파일을 읽는 순간 삭제가 거부될 수 있어
        # 짧게 재시도하고, 끝내 실패하면 조용히 잔류시키는 대신 경고를 남긴다.
        for delay in (0.0, 0.05, 0.1, 0.2, 0.5):
            if delay:
                time.sleep(delay)
            try:
                lock_path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                continue
        else:
            print(
                f"[경고] 락 파일을 정리하지 못했습니다. 직접 삭제하십시오: {lock_path}",
                file=sys.stderr,
            )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RunError(f"실행 상태 파일이 없습니다: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"실행 상태 파일을 읽을 수 없습니다: {exc}") from exc
    state = migrate_state(state)
    validate_state_shape(state)
    return state


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """v1 상태를 비용 원장이 있는 v2 계약으로 메모리에서 안전하게 올린다."""

    version = state.get("schema_version")
    if version == SCHEMA_VERSION:
        return state
    if version not in LEGACY_SCHEMA_VERSIONS:
        return state

    # v1·v2 상태는 Codex 런타임에서만 만들어졌다. 런타임과 모델표 스냅샷을 먼저 채워
    # 이후 해석·검증이 같은 기준을 쓰게 한다.
    state.setdefault("runtime", "codex")
    state.setdefault("runtime_model_table", runtime_table(state["runtime"]))
    if version == 2:
        state["schema_version"] = SCHEMA_VERSION
        state["execution_profiles"] = EXECUTION_PROFILES
        state.setdefault("events", []).append(
            {
                "at": now_iso(),
                "event": "state_migrated",
                "role": "manager",
                "detail": f"schema {version} -> {SCHEMA_VERSION}",
            }
        )
        return state

    note_mode = state.get("note_mode")
    if note_mode not in NOTE_MODE_CONFIG:
        note_mode = DEFAULT_NOTE_MODE
    state["schema_version"] = SCHEMA_VERSION
    state["note_mode"] = note_mode
    state["mode_contract"] = NOTE_MODE_CONFIG[note_mode]
    state["execution_profiles"] = EXECUTION_PROFILES
    state["cost_policy"] = COST_POLICY
    state.setdefault("review_cycle", 1)

    old_usage = state.get("cost_usage")
    usage = new_cost_usage()
    if isinstance(old_usage, dict):
        reviews = old_usage.get("critical_reviews")
        if isinstance(reviews, list):
            usage["critical_reviews"] = reviews[:CRITICAL_REVIEW_LIMIT]
        premium_reviews = old_usage.get("premium_final_reviews")
        if isinstance(premium_reviews, list):
            usage["premium_final_reviews"] = premium_reviews
    state["cost_usage"] = usage

    policy = role_execution_policy(note_mode)
    roles = state.get("roles")
    if isinstance(roles, dict):
        for name, entry in roles.items():
            if name not in policy or not isinstance(entry, dict):
                continue
            entry["execution"] = dict(policy[name])
            entry["max_attempts"] = 1 if name == "final_reviewer" else 2
            if name == "final_reviewer" and isinstance(entry.get("attempts"), int):
                entry["attempts"] = min(entry["attempts"], 1)
            entry.setdefault("active_profile", None)
            entry.setdefault("critical_review_call_id", None)
            entry.setdefault("premium_call_id", None)
            entry.setdefault("coverage_gate", None)

        final_entry = roles.get("final_reviewer")
        if isinstance(final_entry, dict) and final_entry.get("attempts", 0) > 0:
            route = PREMIUM_FINAL_REVIEW_ROUTES[note_mode]
            call_id = f"migrated-{note_mode}-cycle-{state['review_cycle']}"
            migrated_contract = resolve_state_profile(state, route["profile"])
            try:
                input_fingerprint = final_review_input_fingerprint(state)
            except RunError:
                input_fingerprint = hashlib.sha256(
                    f"legacy-unverified:{state.get('lecture_id')}:{note_mode}".encode("utf-8")
                ).hexdigest()
            status = final_entry.get("status")
            call_status = status if status in {"running", "passed", "failed"} else "failed"
            usage["premium_final_reviews"].append(
                {
                    "call_id": call_id,
                    "review_cycle": state["review_cycle"],
                    "note_mode": note_mode,
                    "role": "final_reviewer",
                    "route": route["route"],
                    "profile": route["profile"],
                    "runtime": migrated_contract["runtime"],
                    "agent": migrated_contract["agent"],
                    "model": migrated_contract["model"],
                    "reasoning_effort": migrated_contract["effort"],
                    "attempt_kind": "full_note_audit",
                    "input_fingerprint": input_fingerprint,
                    "status": call_status,
                    "started_at": final_entry.get("started_at") or state.get("created_at"),
                    "completed_at": final_entry.get("completed_at"),
                    "migrated_from_schema": version,
                }
            )
            final_entry["premium_call_id"] = call_id
    events = state.setdefault("events", [])
    events.append(
        {
            "at": now_iso(),
            "event": "state_migrated",
            "role": "manager",
            "detail": f"schema {version} -> {SCHEMA_VERSION}",
        }
    )
    return state


def validate_state_shape(state: dict[str, Any]) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise RunError(f"지원하지 않는 실행 상태 버전입니다: {state.get('schema_version')}")
    runtime = state.get("runtime")
    if runtime not in RUNTIMES:
        raise RunError(f"실행 상태의 런타임이 올바르지 않습니다: {runtime} (허용: {', '.join(RUNTIMES)})")
    table = state.get("runtime_model_table")
    if not isinstance(table, dict) or set(table) != set(runtime_table(runtime)):
        raise RunError("실행 상태의 런타임 모델표 스냅샷이 없거나 프로필 목록이 프로젝트와 다릅니다.")
    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    if note_mode not in NOTE_MODE_CONFIG:
        raise RunError(f"알 수 없는 학습노트 제작 모드입니다: {note_mode}")
    expected_execution_policy = role_execution_policy(note_mode)
    roles = state.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLE_ORDER):
        raise RunError("실행 상태의 역할 목록이 현재 프로젝트와 일치하지 않습니다.")
    for name, entry in roles.items():
        if entry.get("status") not in {"skipped", "blocked", "ready", "running", "passed", "failed"}:
            raise RunError(f"알 수 없는 역할 상태입니다: {name}={entry.get('status')}")
        for dependency in entry.get("dependencies", []):
            if dependency not in roles:
                raise RunError(f"알 수 없는 선행 역할입니다: {name} -> {dependency}")
        execution = entry.get("execution")
        if execution is not None:
            if not isinstance(execution, dict) or execution.get("executor") not in {
                "python",
                "subagent",
                "hybrid",
            }:
                raise RunError(f"알 수 없는 역할 실행 방식입니다: {name}={execution}")
            if execution != expected_execution_policy[name]:
                raise RunError(f"역할 실행 정책이 프로젝트 기준과 일치하지 않습니다: {name}")
        attempts = entry.get("attempts", 0)
        expected_max_attempts = 1 if name == "final_reviewer" else 2
        max_attempts = entry.get("max_attempts", expected_max_attempts)
        if (
            not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not 0 <= attempts <= expected_max_attempts
        ):
            raise RunError(f"역할 시도 횟수가 잘못됐습니다: {name}={attempts}")
        if max_attempts != expected_max_attempts:
            raise RunError(f"역할 시도 한도가 프로젝트 기준과 일치하지 않습니다: {name}={max_attempts}")
        active_profile = entry.get("active_profile")
        if active_profile is not None and active_profile not in EXECUTION_PROFILES:
            raise RunError(f"알 수 없는 실제 실행 프로필입니다: {name}={active_profile}")
    if state.get("execution_profiles") is not None and state["execution_profiles"] != EXECUTION_PROFILES:
        raise RunError("실행 프로필이 프로젝트 비용 정책과 일치하지 않습니다.")
    if state.get("cost_policy") is not None and state["cost_policy"] != COST_POLICY:
        raise RunError("비용 정책이 프로젝트 기준과 일치하지 않습니다.")
    cost_usage = state.get("cost_usage")
    if cost_usage is not None:
        if not isinstance(cost_usage, dict):
            raise RunError("cost_usage는 객체여야 합니다.")
        if cost_usage.get("critical_review_limit") != CRITICAL_REVIEW_LIMIT:
            raise RunError("고강도 국소 재검수 제한값이 프로젝트 정책과 일치하지 않습니다.")
        reviews = cost_usage.get("critical_reviews")
        if not isinstance(reviews, list) or len(reviews) > CRITICAL_REVIEW_LIMIT:
            raise RunError("고강도 국소 재검수 기록이 허용 한도를 초과했습니다.")
        critical_ids: set[str] = set()
        for review in reviews:
            if not isinstance(review, dict):
                raise RunError("고강도 국소 재검수 기록은 객체여야 합니다.")
            call_id = review.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in critical_ids:
                raise RunError("고강도 국소 재검수 call_id가 없거나 중복됐습니다.")
            critical_ids.add(call_id)
            review_mode = review.get("note_mode")
            if review_mode not in NOTE_MODE_CONFIG:
                raise RunError("고강도 국소 재검수 note_mode가 올바르지 않습니다.")
            role = review.get("role")
            category = review.get("category")
            review_policy = role_execution_policy(review_mode)
            allowed = ESCALATION_RULES.get(review_mode, {}).get(role, set())
            if category not in allowed:
                raise RunError("고강도 국소 재검수 역할·분류가 기록된 모드 정책과 다릅니다.")
            expected_profile = review_policy[role].get("escalation_profile")
            if review.get("profile") != expected_profile:
                raise RunError("고강도 국소 재검수 프로필이 기록된 모드 정책과 다릅니다.")
            contract = resolve_state_profile(state, expected_profile)
            if (
                review.get("runtime") != contract["runtime"]
                or review.get("model") != contract["model"]
                or review.get("reasoning_effort") != contract["effort"]
                or review.get("attempt_kind") != "targeted_escalation"
            ):
                raise RunError("고강도 국소 재검수 실행 계약이 올바르지 않습니다.")
            if review.get("status") not in {"running", "passed", "failed"}:
                raise RunError("고강도 국소 재검수 상태가 올바르지 않습니다.")
        if cost_usage.get("premium_final_review_limit_per_cycle") != PREMIUM_FINAL_REVIEW_LIMIT:
            raise RunError("완성본 고비용 검수 제한값이 프로젝트 정책과 일치하지 않습니다.")
        premium_reviews = cost_usage.get("premium_final_reviews")
        if not isinstance(premium_reviews, list):
            raise RunError("완성본 고비용 검수 원장이 올바르지 않습니다.")
        seen_cycles: set[tuple[int, str]] = set()
        for call in premium_reviews:
            if not isinstance(call, dict):
                raise RunError("완성본 고비용 검수 기록은 객체여야 합니다.")
            cycle = call.get("review_cycle")
            mode = call.get("note_mode")
            if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle < 1:
                raise RunError("완성본 고비용 검수 review_cycle이 올바르지 않습니다.")
            if mode not in PREMIUM_FINAL_REVIEW_ROUTES:
                raise RunError("완성본 고비용 검수 note_mode가 올바르지 않습니다.")
            cycle_key = (cycle, mode)
            if cycle_key in seen_cycles:
                raise RunError("같은 review cycle에 완성본 고비용 검수가 두 번 기록됐습니다.")
            seen_cycles.add(cycle_key)
            route = PREMIUM_FINAL_REVIEW_ROUTES[mode]
            if call.get("route") != route["route"] or call.get("profile") != route["profile"]:
                raise RunError("완성본 고비용 검수 라우팅 기록이 프로젝트 기준과 다릅니다.")
            fingerprint = call.get("input_fingerprint")
            if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
                raise RunError("완성본 고비용 검수 입력 지문이 올바르지 않습니다.")
            if call.get("status") not in {"running", "passed", "failed"}:
                raise RunError("완성본 고비용 검수 상태가 올바르지 않습니다.")
    review_cycle = state.get("review_cycle")
    if not isinstance(review_cycle, int) or isinstance(review_cycle, bool) or review_cycle < 1:
        raise RunError("review_cycle은 1 이상의 정수여야 합니다.")


def save_state(path: Path, state: dict[str, Any]) -> None:
    refresh_statuses(state["roles"])
    state["updated_at"] = now_iso()
    write_json(path, state)


def resolve_artifact(raw: str, state_file: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = state_file.parent / path
    path = path.resolve()
    if not path.exists():
        raise RunError(f"산출물이 존재하지 않습니다: {path}")
    return path


def resolve_model_packet(raw: str, state_file: Path) -> tuple[Path, dict[str, Any]]:
    """모델에 전달할 JSON 패킷이 작고 명시적으로 허용됐는지 검사한다."""

    path = resolve_artifact(raw, state_file)
    if not path.is_file():
        raise RunError(f"모델 입력 패킷은 JSON 파일이어야 합니다: {path}")
    size = path.stat().st_size
    if size < 1 or size > MAX_AGENT_PACKET_BYTES:
        raise RunError(
            f"모델 입력 패킷 크기는 1~{MAX_AGENT_PACKET_BYTES}바이트여야 합니다: "
            f"{size}바이트"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunError(f"모델 입력 패킷 JSON을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("model_input") is not True:
        raise RunError("모델 입력 패킷에는 model_input=true가 명시되어야 합니다.")
    kind = payload.get("kind")
    if (
        not isinstance(kind, str)
        or not kind.strip()
        or "manifest" in kind.lower()
        or "aggregate" in kind.lower()
        or not kind.lower().endswith("packet")
    ):
        raise RunError("모델 입력은 aggregate/manifest가 아닌 kind=*packet 개별 패킷이어야 합니다.")
    target_fields = (
        "target",
        "target_segments",
        "source_unit_id",
        "source_unit_ids",
        "note_refs",
    )
    if not any(payload.get(field) for field in target_fields):
        raise RunError("개별 모델 입력 패킷에는 제한된 target 범위가 있어야 합니다.")
    return path, artifact_record(path)


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunError(f"{label} JSON을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunError(f"{label} JSON의 최상위 값은 객체여야 합니다.")
    return payload


def artifact_was_recorded(records: list[dict[str, Any]], path: Path) -> bool:
    resolved = path.resolve()
    return any(
        isinstance(record, dict)
        and Path(str(record.get("path", ""))).resolve() == resolved
        and artifact_matches(record)
        for record in records
    )


def build_coverage_gate(
    state: dict[str, Any],
    state_file: Path,
    source_map_raw: str | None,
    coverage_report_raw: str | None,
) -> tuple[dict[str, Any], Path]:
    """최종 검수의 source-unit 완전성과 모드별 독립 검수 프로필을 강제한다."""

    if not source_map_raw or not coverage_report_raw:
        raise RunError(
            "final_reviewer 완료에는 --source-map과 --coverage-report가 모두 필요합니다."
        )
    source_map_path = resolve_artifact(source_map_raw, state_file)
    coverage_path = resolve_artifact(coverage_report_raw, state_file)
    mapper_artifacts = state["roles"]["source_mapper"].get("artifacts", [])
    if not artifact_was_recorded(mapper_artifacts, source_map_path):
        raise RunError(
            "--source-map은 통과한 source_mapper가 직접 기록한 변경되지 않은 산출물이어야 합니다."
        )
    try:
        _source_ids, summary = validate_coverage(source_map_path, coverage_path)
    except CoverageValidationError as exc:
        details = "; ".join(issue.message for issue in exc.report.errors[:5])
        if len(exc.report.errors) > 5:
            details += f"; 외 {len(exc.report.errors) - 5}개"
        raise RunError(f"source coverage 검증 실패: {details}") from exc

    coverage_payload = read_json_object(coverage_path, "coverage report")
    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    if coverage_payload.get("note_mode") != note_mode:
        raise RunError(
            "coverage report의 note_mode가 실행 상태와 일치하지 않습니다: "
            f"{coverage_payload.get('note_mode')} != {note_mode}"
        )
    expected_profile = role_execution_policy(note_mode)["final_reviewer"]["agent_profile"]
    if coverage_payload.get("reviewer_profile") != expected_profile:
        raise RunError(
            "coverage report의 reviewer_profile이 최종 검수 실행 계약과 일치하지 않습니다: "
            f"{coverage_payload.get('reviewer_profile')} != {expected_profile}"
        )
    return (
        {
            "note_mode": note_mode,
            "reviewer_profile": expected_profile,
            "source_map": artifact_record(source_map_path),
            "coverage_report": artifact_record(coverage_path),
            "summary": summary,
        },
        coverage_path,
    )


def invalidate_downstream(roles: dict[str, dict[str, Any]], changed_role: str) -> None:
    queue = [changed_role]
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        for name, entry in roles.items():
            if current not in entry["dependencies"] or name in seen:
                continue
            seen.add(name)
            queue.append(name)
            if entry["active"]:
                entry["status"] = "blocked"
                entry["completed_at"] = None
                entry["artifacts"] = []
                entry["failure_reason"] = "선행 역할이 다시 실행되어 기존 통과가 무효화됨"
                entry["attempts"] = 0
                entry["repair_scope"] = None
                entry["repair_packet"] = None
                entry["active_profile"] = None
                entry["critical_review_call_id"] = None
                entry["premium_call_id"] = None
                entry["coverage_gate"] = None


def inventory_changes(
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    old_by_path = {item["path"]: item for item in old_items}
    new_by_path = {item["path"]: item for item in new_items}
    changes: list[dict[str, Any]] = []
    for relative in sorted(set(old_by_path) | set(new_by_path)):
        old = old_by_path.get(relative)
        new = new_by_path.get(relative)
        if old != new:
            changes.append({"path": relative, "old": old, "new": new})
    return changes


def role_descendants(roles: dict[str, dict[str, Any]], seeds: set[str]) -> set[str]:
    affected = set(seeds)
    changed = True
    while changed:
        changed = False
        for role, entry in roles.items():
            if role not in affected and any(dependency in affected for dependency in entry["dependencies"]):
                affected.add(role)
                changed = True
    return affected


def rebuild_roles_after_input_change(
    state: dict[str, Any],
    new_items: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    old_roles = state["roles"]
    new_roles = make_roles(
        new_items,
        state["output_format"],
        state.get("note_mode", DEFAULT_NOTE_MODE),
    )
    apply_role_overrides(new_roles, state.get("role_overrides", {}))

    changed_kinds: set[str] = set()
    for change in changes:
        for item in (change["old"], change["new"]):
            if item is not None:
                changed_kinds.add(item["kind"])

    seeds = {"source_mapper"}
    if old_roles["transcript_auditor"]["active"] or new_roles["transcript_auditor"]["active"]:
        # 전사 검수는 교안 정렬도 담당하므로 어떤 강의 자료가 바뀌어도 다시 확인한다.
        seeds.add("transcript_auditor")
    if "audio" in changed_kinds:
        seeds.add("transcriber")
    for role in ROLE_ORDER:
        old = old_roles[role]
        new = new_roles[role]
        if old["active"] != new["active"] or old["dependencies"] != new["dependencies"]:
            seeds.add(role)

    affected = role_descendants(old_roles, seeds) | role_descendants(new_roles, seeds)
    for role in ROLE_ORDER:
        old = old_roles[role]
        new = new_roles[role]
        if role in affected:
            new["attempts"] = 0
            new["repair_scope"] = None
            new["repair_packet"] = None
            new["active_profile"] = None
            new["critical_review_call_id"] = None
            new["premium_call_id"] = None
            new["coverage_gate"] = None
            new["rerun_count"] = old.get("rerun_count", 0)
            if new["active"]:
                new["failure_reason"] = "입력 변경으로 기존 통과가 무효화됨"
            continue
        new["attempts"] = old.get("attempts", 0)
        new["repair_scope"] = old.get("repair_scope")
        new["repair_packet"] = old.get("repair_packet")
        new["active_profile"] = old.get("active_profile")
        new["critical_review_call_id"] = old.get("critical_review_call_id")
        new["premium_call_id"] = old.get("premium_call_id")
        new["coverage_gate"] = old.get("coverage_gate")
        new["rerun_count"] = old.get("rerun_count", 0)
        if old["active"] == new["active"] and old["dependencies"] == new["dependencies"]:
            for key in ("status", "started_at", "completed_at", "artifacts", "failure_reason"):
                new[key] = old.get(key)
    refresh_statuses(new_roles)
    return new_roles


def rebuild_roles_after_mode_change(
    state: dict[str, Any],
    new_mode: str,
) -> dict[str, dict[str, Any]]:
    """전사·자료 매핑은 보존하고 집필 이후 역할만 새 제작 모드로 다시 계획한다."""

    old_roles = state["roles"]
    new_roles = make_roles(state["inputs"], state["output_format"], new_mode)
    apply_role_overrides(new_roles, state.get("role_overrides", {}))
    affected = role_descendants(old_roles, {"writer"}) | role_descendants(new_roles, {"writer"})

    for role in ROLE_ORDER:
        old = old_roles[role]
        new = new_roles[role]
        if role in affected:
            new["attempts"] = 0
            new["repair_scope"] = None
            new["repair_packet"] = None
            new["active_profile"] = None
            new["critical_review_call_id"] = None
            new["premium_call_id"] = None
            new["coverage_gate"] = None
            new["rerun_count"] = old.get("rerun_count", 0)
            if new["active"]:
                new["failure_reason"] = "학습노트 제작 모드 변경으로 기존 집필 이후 통과가 무효화됨"
            continue
        new["attempts"] = old.get("attempts", 0)
        new["repair_scope"] = old.get("repair_scope")
        new["repair_packet"] = old.get("repair_packet")
        new["active_profile"] = old.get("active_profile")
        new["critical_review_call_id"] = old.get("critical_review_call_id")
        new["premium_call_id"] = old.get("premium_call_id")
        new["coverage_gate"] = old.get("coverage_gate")
        new["rerun_count"] = old.get("rerun_count", 0)
        if old["active"] == new["active"] and old["dependencies"] == new["dependencies"]:
            for key in ("status", "started_at", "completed_at", "artifacts", "failure_reason"):
                new[key] = old.get(key)
    refresh_statuses(new_roles)
    return new_roles


# -----------------------------------------------------------------------------
# 5. 관리자 에이전트가 사용하는 명령
# init/refresh/next/start/complete/fail/activate/deactivate가 실행 감사 기록을 만든다.
# -----------------------------------------------------------------------------

def command_init(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    input_root = args.input_dir.expanduser().resolve()
    if not LECTURE_ID_RE.fullmatch(args.lecture_id) or args.lecture_id in {".", ".."}:
        raise RunError("lecture_id에 파일 경로용 특수문자나 끝의 점·공백을 사용할 수 없습니다.")
    state_root = getattr(args, "state_root", None)
    if state_root is not None:
        state_file = state_root.expanduser().resolve() / args.lecture_id / "run_state.json"
    else:
        state_file = state_path_for(root, args.lecture_id)
    if state_file.exists():
        raise RunError(f"이미 실행 상태가 있습니다. 덮어쓰지 않았습니다: {state_file}")
    # 입력 검증과 해시 계산은 락·폴더 생성 전에 끝내, 실패한 init이
    # 빈 workspace/<강의ID>/ 폴더를 남기지 않게 한다.
    args.output_format_explicit = args.output_format is not None
    if args.output_format is None:
        args.output_format = DEFAULT_OUTPUT_FORMATS[args.note_mode]
    runtime = args.runtime or detect_runtime()
    if runtime is None:
        raise RunError(
            "실행 런타임을 감지하지 못했습니다. --runtime codex|claude 를 지정하십시오. "
            "(Claude Code는 CLAUDECODE, Codex는 CODEX_* 환경 변수로 감지하며 두 신호가 겹치면 명시가 필요합니다)"
        )
    classification_overrides = parse_classification_overrides(input_root, args.classify)
    items = inventory(input_root, classification_overrides)
    with state_write_lock(state_file, create_parent=True):
        return locked_init(args, root, input_root, state_file, classification_overrides, items, runtime)


def locked_init(
    args: argparse.Namespace,
    root: Path,
    input_root: Path,
    state_file: Path,
    classification_overrides: dict[str, str],
    items: list[dict[str, Any]],
    runtime: str,
) -> int:
    if state_file.exists():
        raise RunError(f"이미 실행 상태가 있습니다. 덮어쓰지 않았습니다: {state_file}")
    timestamp = now_iso()
    state = {
        "schema_version": SCHEMA_VERSION,
        "lecture_id": args.lecture_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "project_root": str(root),
        "input_root": str(input_root),
        "state_root": str(state_file.parent.parent),
        "engine_root": str(ENGINE_ROOT),
        "output_format": args.output_format,
        "output_format_explicit": args.output_format_explicit,
        "note_mode": args.note_mode,
        "mode_contract": NOTE_MODE_CONFIG[args.note_mode],
        "runtime": runtime,
        "runtime_model_table": runtime_table(runtime),
        "review_cycle": 1,
        "classification_overrides": classification_overrides,
        "role_overrides": {},
        "inputs": items,
        "routing_summary": {
            "audio_files": sum(item["kind"] == "audio" for item in items),
            "transcript_files": sum(item["kind"] == "transcript" for item in items),
            "document_files": sum(item["kind"] == "document" for item in items),
            "code_files": sum(item["kind"] == "code" for item in items),
        },
        "context_policy": {
            "manager_reads": "인벤토리, 실행 상태, 각 역할의 결과 요약",
            "worker_reads": "역할 프롬프트, 해당 단원의 근거 묶음, 필요한 선행 산출물만",
            "reuse": "입력 SHA-256이 같으면 검증된 중간 산출물 재사용",
            "source_pointer_required": True,
        },
        "execution_profiles": EXECUTION_PROFILES,
        "cost_policy": COST_POLICY,
        "cost_usage": new_cost_usage(),
        "roles": make_roles(items, args.output_format, args.note_mode),
        "events": [{"at": timestamp, "event": "initialized", "detail": f"입력 {len(items)}개"}],
    }
    write_json(state_file, state)
    print(state_file)
    return 0


def command_status(args: argparse.Namespace) -> int:
    state = read_state(args.state.expanduser().resolve())
    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    mode_label = NOTE_MODE_CONFIG[note_mode]["label"]
    print(
        f"강의: {state['lecture_id']} | 제작 모드: {mode_label}({note_mode}) | "
        f"런타임: {state.get('runtime')} | 출력: {state['output_format']} | 입력: {len(state['inputs'])}개"
    )
    critical_reviews = state.get("cost_usage", {}).get("critical_reviews", [])
    print(f"고강도 핵심 재검수: {len(critical_reviews)}/{CRITICAL_REVIEW_LIMIT}회")
    premium_reviews = state.get("cost_usage", {}).get("premium_final_reviews", [])
    print(
        f"완성본 고비용 검수: 누적 {len(premium_reviews)}회 | "
        f"현재 review cycle {state.get('review_cycle', 1)}"
    )
    execution_policy = role_execution_policy(note_mode)
    for role in ROLE_ORDER:
        entry = state["roles"][role]
        active = "활성" if entry["active"] else "비활성"
        deps = ",".join(entry["dependencies"]) or "-"
        execution = entry.get("execution", execution_policy[role])
        print(
            f"{role:24} {entry['status']:8} {active:4} "
            f"실행={execution['executor']:8} 선행={deps}"
        )
    return 0


def command_next(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    state = read_state(state_file)
    refresh_statuses(state["roles"])
    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    execution_policy = role_execution_policy(note_mode)
    ready = []
    for role in ROLE_ORDER:
        entry = state["roles"][role]
        if entry["status"] == "ready":
            execution = entry.get("execution", execution_policy[role])
            agent_profile = execution.get("agent_profile")
            ready.append(
                {
                    "role": role,
                    "prompt": entry["prompt"],
                    "reason": entry["reason"],
                    "attempts": entry["attempts"],
                    "max_attempts": entry.get("max_attempts", 2),
                    "repair_scope": entry.get("repair_scope"),
                    "repair_packet": entry.get("repair_packet"),
                    "execution": execution,
                    # 관리자가 실제로 호출할 런타임의 모델·effort·에이전트 이름.
                    "resolved_profile": (
                        resolve_state_profile(state, agent_profile) if agent_profile else None
                    ),
                }
            )
    print(
        json.dumps(
            {
                "state": str(state_file),
                # 관리자가 역할 프롬프트·규칙을 읽을 실제 위치(설치본이면 패키지 안 engine/).
                "engine_root": str(ENGINE_ROOT),
                "prompt_root": str(ENGINE_ROOT / "agent_prompts"),
                "note_mode": note_mode,
                "mode_contract": NOTE_MODE_CONFIG[note_mode],
                "runtime": state.get("runtime"),
                "runtime_model_table": state.get("runtime_model_table"),
                "execution_profiles": state.get("execution_profiles", EXECUTION_PROFILES),
                "cost_policy": state.get("cost_policy", COST_POLICY),
                "cost_usage": state.get(
                    "cost_usage",
                    new_cost_usage(),
                ),
                "ready": ready,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def get_role(state: dict[str, Any], role: str) -> dict[str, Any]:
    if role not in state["roles"]:
        raise RunError(f"알 수 없는 역할입니다: {role}")
    return state["roles"][role]


def append_event(state: dict[str, Any], event: str, role: str, detail: str | None = None) -> None:
    item = {"at": now_iso(), "event": event, "role": role}
    if detail:
        item["detail"] = detail
    state["events"].append(item)


def advance_review_cycle(state: dict[str, Any], reason: str) -> int:
    """내용·입력·모드 변경 뒤 새 최종 검수 1회를 명시적으로 연다."""

    state["review_cycle"] = int(state.get("review_cycle", 1)) + 1
    append_event(state, "review_cycle_advanced", "manager", reason)
    return state["review_cycle"]


def final_review_input_fingerprint(state: dict[str, Any]) -> str:
    """완성본 검수의 실제 의미 입력을 경로와 무관한 SHA-256으로 묶는다."""

    evidence: list[dict[str, Any]] = []
    # 검수 대상은 집필 초안이다. 조판은 내용을 바꾸지 않으므로 지문에 넣지 않는다.
    for role in ("source_mapper", "writer"):
        entry = state["roles"][role]
        if entry.get("status") != "passed" or not entry.get("artifacts"):
            raise RunError(f"최종 검수 지문을 만들 선행 산출물이 없습니다: {role}")
        for record in entry["artifacts"]:
            if not isinstance(record, dict) or not artifact_matches(record):
                raise RunError(f"최종 검수 선행 산출물이 변경되었거나 누락됐습니다: {role}")
            evidence.append(
                {
                    "role": role,
                    "kind": record.get("kind"),
                    "bytes": record.get("bytes"),
                    "file_count": record.get("file_count"),
                    "sha256": record.get("sha256"),
                }
            )
    payload = {
        "note_mode": state.get("note_mode", DEFAULT_NOTE_MODE),
        "evidence": evidence,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def reserve_premium_final_review(state: dict[str, Any], entry: dict[str, Any]) -> str:
    """현재 review cycle의 유일한 완성본 검수 호출을 시작 전에 예약한다."""

    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    route = PREMIUM_FINAL_REVIEW_ROUTES[note_mode]
    cycle = int(state.get("review_cycle", 1))
    input_fingerprint = final_review_input_fingerprint(state)
    usage = state.setdefault("cost_usage", new_cost_usage())
    calls = usage.setdefault("premium_final_reviews", [])
    matching = [
        call
        for call in calls
        if isinstance(call, dict)
        and call.get("review_cycle") == cycle
        and call.get("note_mode") == note_mode
    ]
    if len(matching) >= PREMIUM_FINAL_REVIEW_LIMIT:
        raise RunError(
            "현재 review cycle의 완성본 고비용 검수 1회를 이미 사용했습니다. "
            "같은 완성본을 다시 읽히지 말고 기존 검수의 국소 수정안을 적용하십시오."
        )
    if any(
        isinstance(call, dict)
        and call.get("route") == route["route"]
        and call.get("input_fingerprint") == input_fingerprint
        for call in calls
    ):
        raise RunError(
            "동일한 source map과 완성본은 이미 같은 고비용 프로필로 검수했습니다. "
            "review_cycle만 늘려 같은 완성본을 다시 호출할 수 없습니다."
        )
    profile = route["profile"]
    call_id = f"{route['route']}-cycle-{cycle}"
    profile_contract = resolve_state_profile(state, profile)
    calls.append(
        {
            "call_id": call_id,
            "review_cycle": cycle,
            "note_mode": note_mode,
            "role": "final_reviewer",
            "route": route["route"],
            "profile": profile,
            "runtime": profile_contract["runtime"],
            "agent": profile_contract["agent"],
            "model": profile_contract["model"],
            "reasoning_effort": profile_contract["effort"],
            "attempt_kind": "full_note_audit",
            "input_fingerprint": input_fingerprint,
            "status": "running",
            "started_at": now_iso(),
            "completed_at": None,
        }
    )
    entry["premium_call_id"] = call_id
    return profile


def finish_premium_final_review(
    state: dict[str, Any], entry: dict[str, Any], status: str
) -> None:
    call_id = entry.get("premium_call_id")
    if not call_id:
        raise RunError("최종 검수의 고비용 호출 예약 기록이 없습니다.")
    calls = state.get("cost_usage", {}).get("premium_final_reviews", [])
    for call in calls:
        if isinstance(call, dict) and call.get("call_id") == call_id:
            if call.get("status") != "running":
                raise RunError("최종 검수 호출은 running 상태에서만 종료할 수 있습니다.")
            call["status"] = status
            call["completed_at"] = now_iso()
            return
    raise RunError("최종 검수의 고비용 호출 원장 항목을 찾을 수 없습니다.")


def finish_critical_review(state: dict[str, Any], entry: dict[str, Any], status: str) -> None:
    call_id = entry.get("critical_review_call_id")
    if not call_id:
        return
    calls = state.get("cost_usage", {}).get("critical_reviews", [])
    for call in calls:
        if isinstance(call, dict) and call.get("call_id") == call_id:
            if call.get("status") != "running":
                raise RunError("국소 고강도 검수 호출은 running 상태에서만 종료할 수 있습니다.")
            call["status"] = status
            call["completed_at"] = now_iso()
            return
    raise RunError("국소 고강도 검수 호출 원장 항목을 찾을 수 없습니다.")


def command_activate(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_activate(args, state_file)


def locked_activate(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    entry = get_role(state, args.role)
    if args.role not in OPTIONAL_ROLES:
        raise RunError(f"기본 필수 역할은 별도로 활성화할 수 없습니다: {args.role}")
    if entry["active"]:
        raise RunError(f"이미 활성 상태인 역할입니다: {args.role}")
    if args.role == "instructor_integrator" and not state["roles"]["transcript_auditor"]["active"]:
        raise RunError("instructor_integrator를 활성화하려면 transcript_auditor가 먼저 활성 상태여야 합니다.")
    running = [role for role, candidate in state["roles"].items() if candidate["status"] == "running"]
    if running:
        raise RunError(f"실행 중 역할이 있어 라우팅을 바꿀 수 없습니다: {', '.join(running)}")
    entry["active"] = True
    entry["activation_source"] = "manual"
    entry["reason"] = args.reason
    entry["status"] = "blocked"
    normalize_dependencies(state["roles"])
    invalidate_downstream(state["roles"], args.role)
    state.setdefault("role_overrides", {})[args.role] = {"mode": "active", "reason": args.reason}
    advance_review_cycle(state, f"역할 활성화: {args.role}")
    append_event(state, "activated", args.role, args.reason)
    save_state(state_file, state)
    print(f"활성화: {args.role} | {entry['status']}")
    return 0


def command_deactivate(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_deactivate(args, state_file)


def locked_deactivate(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    entry = get_role(state, args.role)
    if args.role not in OPTIONAL_ROLES:
        raise RunError(f"기본 필수 역할은 비활성화할 수 없습니다: {args.role}")
    if not entry["active"]:
        raise RunError(f"이미 비활성 상태인 역할입니다: {args.role}")
    if entry["status"] == "running":
        raise RunError(f"실행 중인 역할은 비활성화할 수 없습니다: {args.role}")
    running = [role for role, candidate in state["roles"].items() if candidate["status"] == "running"]
    if running:
        raise RunError(f"실행 중 역할이 있어 라우팅을 바꿀 수 없습니다: {', '.join(running)}")
    if args.role == "transcript_auditor" and state["roles"]["instructor_integrator"]["active"]:
        raise RunError("먼저 instructor_integrator를 비활성화해야 합니다.")

    invalidate_downstream(state["roles"], args.role)
    entry["active"] = False
    entry["activation_source"] = "manual"
    entry["reason"] = args.reason
    entry["status"] = "skipped"
    entry["started_at"] = None
    entry["completed_at"] = None
    entry["artifacts"] = []
    entry["active_profile"] = None
    entry["critical_review_call_id"] = None
    entry["premium_call_id"] = None
    entry["coverage_gate"] = None
    entry["failure_reason"] = None
    normalize_dependencies(state["roles"])
    state.setdefault("role_overrides", {})[args.role] = {"mode": "inactive", "reason": args.reason}
    advance_review_cycle(state, f"역할 비활성화: {args.role}")
    append_event(state, "deactivated", args.role, args.reason)
    save_state(state_file, state)
    print(f"비활성화: {args.role} | {args.reason}")
    return 0


def command_refresh_inputs(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_refresh_inputs(args, state_file)


def locked_refresh_inputs(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    running = [role for role, entry in state["roles"].items() if entry["status"] == "running"]
    if running:
        raise RunError(f"실행 중 역할이 있어 입력을 갱신할 수 없습니다: {', '.join(running)}")
    input_root = Path(state["input_root"]).resolve()
    existing_overrides = {
        relative: kind
        for relative, kind in state.get("classification_overrides", {}).items()
        if (input_root / relative).is_file()
    }
    classification_overrides = parse_classification_overrides(
        input_root, args.classify, existing_overrides
    )
    new_items = inventory(input_root, classification_overrides)
    changes = inventory_changes(state["inputs"], new_items)
    state["classification_overrides"] = classification_overrides
    if not changes:
        append_event(state, "inputs_refreshed", "manager", "변경 없음")
        save_state(state_file, state)
        print("입력 갱신: 변경 없음 | 기존 통과 상태 유지")
        return 0

    state["roles"] = rebuild_roles_after_input_change(state, new_items, changes)
    state["inputs"] = new_items
    state["routing_summary"] = {
        "audio_files": sum(item["kind"] == "audio" for item in new_items),
        "transcript_files": sum(item["kind"] == "transcript" for item in new_items),
        "document_files": sum(item["kind"] == "document" for item in new_items),
        "code_files": sum(item["kind"] == "code" for item in new_items),
    }
    detail = ", ".join(change["path"] for change in changes)
    advance_review_cycle(state, f"입력 변경 {len(changes)}개")
    append_event(state, "inputs_refreshed", "manager", detail)
    save_state(state_file, state)
    print(f"입력 갱신: 변경 {len(changes)}개 | 영향 단계만 재실행")
    for change in changes:
        print(f"- {change['path']}")
    return 0


def upstream_roles(roles: dict[str, dict[str, Any]], role: str) -> set[str]:
    """role이 직·간접으로 의존하는 선행 역할 집합."""
    seen: set[str] = set()
    queue = list(roles[role]["dependencies"])
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(roles[current]["dependencies"])
    return seen


def reopen_role(entry: dict[str, Any], reason: str) -> None:
    """통과한 역할을 다시 연다. 선행 관계와 활성 여부는 그대로 두고 실행 기록만 비운다."""
    entry["status"] = "blocked"
    entry["started_at"] = None
    entry["completed_at"] = None
    entry["artifacts"] = []
    entry["failure_reason"] = reason
    entry["attempts"] = 0
    entry["repair_scope"] = None
    entry["repair_packet"] = None
    entry["active_profile"] = None
    entry["critical_review_call_id"] = None
    entry["premium_call_id"] = None
    entry["coverage_gate"] = None
    entry["rerun_count"] = entry.get("rerun_count", 0) + 1


def command_repair(args: argparse.Namespace) -> int:
    """최종 검수가 반려한 내용 결함을 고치려고 선행 역할을 다시 연다.

    rerun은 사용자 요청·출력 계약 변경용이고 실패 상태에서는 거부되므로, 검수 반려 뒤의
    수정에는 이 명령을 쓴다. 반려한 검수를 실패로 기록하고, 고칠 역할과 그 후속 단계를
    다시 연 뒤 review_cycle을 올려 새 검수 1회를 허용한다. 강의당 횟수 제한이 있다.
    """

    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        state = read_state(state_file)
        reviewer = get_role(state, args.from_role)
        if reviewer["status"] not in {"running", "failed"}:
            raise RunError(
                f"검수가 실행 중이거나 실패한 상태에서만 반려 수정을 열 수 있습니다: "
                f"{args.from_role}={reviewer['status']}"
            )
        target = get_role(state, args.reopen)
        if not target["active"] or target["status"] != "passed":
            raise RunError(f"통과한 활성 역할만 다시 열 수 있습니다: {args.reopen}={target['status']}")
        if args.reopen not in upstream_roles(state["roles"], args.from_role):
            raise RunError(
                f"{args.reopen}은(는) {args.from_role}의 선행 역할이 아닙니다. "
                "조판만 다시 만들 때는 rerun --change-kind output_contract 를 사용하십시오."
            )
        reason = args.reason.strip()
        if not reason or len(reason) > MAX_REPAIR_SCOPE_CHARS or "\n" in reason or "\r" in reason:
            raise RunError(f"반려 수정 이유는 한 줄 {MAX_REPAIR_SCOPE_CHARS}자 이하여야 합니다.")
        usage = state.setdefault("cost_usage", new_cost_usage())
        repairs = usage.setdefault("review_repairs", [])
        limit = int(usage.get("review_repair_limit", REVIEW_REPAIR_LIMIT))
        if limit != REVIEW_REPAIR_LIMIT:
            raise RunError("실행 상태의 검수 반려 수정 제한값이 프로젝트 정책과 일치하지 않습니다.")
        if len(repairs) >= limit:
            raise RunError(
                f"이 강의의 검수 반려 수정 {limit}회를 이미 사용했습니다. 남은 결함은 미해결로 사용자에게 보고하십시오."
            )
        findings_record = None
        if args.findings:
            findings_record = artifact_record(resolve_artifact(args.findings, state_file))
        if reviewer["status"] == "running":
            if args.from_role == "final_reviewer":
                finish_premium_final_review(state, reviewer, "failed")
            finish_critical_review(state, reviewer, "failed")
            reviewer["status"] = "failed"
            reviewer["failure_reason"] = reason
            reviewer["completed_at"] = now_iso()
            append_event(state, "failed", args.from_role, reason)
        invalidate_downstream(state["roles"], args.reopen)
        reopen_role(target, f"검수 반려 수정: {reason}")
        refresh_statuses(state["roles"])
        cycle = advance_review_cycle(state, f"검수 반려 수정: {args.from_role} → {args.reopen}")
        repairs.append(
            {
                "call_id": f"review-repair-{len(repairs) + 1}",
                "at": now_iso(),
                "from_role": args.from_role,
                "reopened_role": args.reopen,
                "reason": reason,
                "findings": findings_record,
                "review_cycle": cycle,
            }
        )
        append_event(state, "review_repair", args.reopen, f"{args.from_role} 반려 → {args.reopen} 재개: {reason}")
        save_state(state_file, state)
        print(
            f"반려 수정 예약: {args.reopen} 재개 | review_cycle {cycle} | "
            f"남은 반려 수정 {limit - len(repairs)}회 | {reason}"
        )
        return 0


def command_rerun(args: argparse.Namespace) -> int:
    """입력을 바꾸지 않고 선택 역할과 후속 역할만 다시 실행한다."""

    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        state = read_state(state_file)
        running = [
            role for role, entry in state["roles"].items() if entry["status"] == "running"
        ]
        if running:
            raise RunError(f"실행 중 역할이 있어 재실행을 예약할 수 없습니다: {', '.join(running)}")
        failed = [
            role for role, candidate in state["roles"].items() if candidate["status"] == "failed"
        ]
        if failed:
            raise RunError(
                "실패 복구에는 rerun을 사용할 수 없습니다. 실패한 범위의 국소 수정으로 처리하거나, "
                "최종 검수가 반려한 내용 결함이면 repair --reopen <역할> 로 선행 역할을 다시 열고, "
                f"그 밖에는 사용자에게 중단 상태를 보고하십시오: {', '.join(failed)}"
            )
        if args.role == "final_reviewer":
            raise RunError(
                "final_reviewer만 직접 재실행할 수 없습니다. 실제로 바뀐 입력·집필·조판 역할을 "
                "재실행하면 새 review cycle에서 변경된 완성본만 검수합니다."
            )

        entry = get_role(state, args.role)
        if not entry["active"]:
            raise RunError(f"비활성 역할은 재실행할 수 없습니다: {args.role}")
        if entry["status"] != "passed":
            raise RunError(f"통과한 역할만 선택 재실행할 수 있습니다: {args.role}={entry['status']}")

        invalidate_downstream(state["roles"], args.role)
        reopen_role(entry, args.reason)
        refresh_statuses(state["roles"])
        advance_review_cycle(state, f"선택 재실행: {args.role} ({args.change_kind})")
        append_event(
            state,
            "rerun_requested",
            args.role,
            f"{args.change_kind}: {args.reason}",
        )
        save_state(state_file, state)
        print(
            f"재실행 예약: {args.role} | {entry['status']} | "
            f"변경={args.change_kind} | {args.reason}"
        )
        return 0


def command_set_mode(args: argparse.Namespace) -> int:
    """입력과 검증된 매핑은 유지하면서 학습노트 제작 모드를 바꾼다."""

    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        state = read_state(state_file)
        running = [
            role for role, entry in state["roles"].items() if entry["status"] == "running"
        ]
        if running:
            raise RunError(f"실행 중 역할이 있어 제작 모드를 바꿀 수 없습니다: {', '.join(running)}")

        old_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
        if old_mode == args.note_mode:
            print(
                f"제작 모드 변경 없음: {NOTE_MODE_CONFIG[old_mode]['label']}({old_mode})"
            )
            return 0

        # Preserve explicit choices and older states whose choice origin is unknown.
        if state.get("output_format_explicit") is False:
            state["output_format"] = DEFAULT_OUTPUT_FORMATS[args.note_mode]
        state["roles"] = rebuild_roles_after_mode_change(state, args.note_mode)
        state["note_mode"] = args.note_mode
        state["mode_contract"] = NOTE_MODE_CONFIG[args.note_mode]
        detail = (
            f"{NOTE_MODE_CONFIG[old_mode]['label']}({old_mode}) -> "
            f"{NOTE_MODE_CONFIG[args.note_mode]['label']}({args.note_mode}) | {args.reason}"
        )
        advance_review_cycle(state, f"제작 모드 변경: {old_mode} -> {args.note_mode}")
        append_event(state, "note_mode_changed", "writer", detail)
        save_state(state_file, state)
        print(f"제작 모드 변경: {detail} | 집필 이후만 재실행")
        return 0


def command_escalate(args: argparse.Namespace) -> int:
    """작고 중요한 미해결 패킷 하나의 고강도 검수 호출을 예약·시작한다."""

    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        state = read_state(state_file)
        entry = get_role(state, args.role)
        note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
        allowed_categories = ESCALATION_RULES.get(note_mode, {}).get(args.role)
        if not allowed_categories:
            raise RunError(f"{note_mode} 모드에서 국소 고강도 승격을 지원하지 않는 역할입니다: {args.role}")
        if args.category not in allowed_categories:
            allowed = ", ".join(sorted(allowed_categories))
            raise RunError(
                f"{note_mode}/{args.role}에 허용되지 않은 승격 분류입니다: {args.category} "
                f"(허용: {allowed})"
            )
        execution = entry.get("execution", role_execution_policy(note_mode)[args.role])
        escalation_profile = execution.get("escalation_profile")
        if escalation_profile not in COST_POLICY["targeted_escalation_profiles"]:
            raise RunError(f"국소 고강도 승격을 지원하지 않는 역할입니다: {args.role}")
        if entry["status"] not in {"running", "failed"}:
            raise RunError(
                f"첫 의미 검수에서 미해결 항목이 생긴 running/failed 역할만 승격할 수 있습니다: "
                f"{args.role}={entry['status']}"
            )
        if entry.get("attempts", 0) != 1:
            raise RunError("고강도 승격은 첫 의미 작업 뒤의 유일한 국소 재검수로만 사용할 수 있습니다.")
        unmet = [
            dep for dep in entry["dependencies"] if state["roles"][dep]["status"] != "passed"
        ]
        if unmet:
            raise RunError(f"선행 역할이 통과하지 않았습니다: {', '.join(unmet)}")

        usage = state.setdefault(
            "cost_usage",
            new_cost_usage(),
        )
        reviews = usage.setdefault("critical_reviews", [])
        limit = int(usage.get("critical_review_limit", CRITICAL_REVIEW_LIMIT))
        if limit != CRITICAL_REVIEW_LIMIT:
            raise RunError("실행 상태의 고강도 국소 재검수 제한값이 프로젝트 정책과 일치하지 않습니다.")
        if len(reviews) >= limit:
            raise RunError("이 강의의 고강도 국소 재검수 1회를 이미 사용했습니다.")

        reason = args.reason.strip()
        if not reason or len(reason) > MAX_REPAIR_SCOPE_CHARS or "\n" in reason or "\r" in reason:
            raise RunError(f"승격 이유는 한 줄 {MAX_REPAIR_SCOPE_CHARS}자 이하여야 합니다.")
        packet_path, packet_record = resolve_model_packet(args.packet, state_file)
        resolved_contract = resolve_state_profile(state, escalation_profile)
        profile_contract = {
            **EXECUTION_PROFILES[escalation_profile],
            **resolved_contract,
            "reasoning_effort": resolved_contract["effort"],
        }
        call_id = f"critical-review-{len(reviews) + 1}"
        started_at = now_iso()
        review = {
            "call_id": call_id,
            "requested_at": started_at,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "attempt_kind": "targeted_escalation",
            "role": args.role,
            "category": args.category,
            "reason": reason,
            "profile": escalation_profile,
            "note_mode": note_mode,
            "runtime": profile_contract["runtime"],
            "agent": profile_contract["agent"],
            "model": profile_contract["model"],
            "reasoning_effort": profile_contract["effort"],
            "review_cycle": state.get("review_cycle", 1),
            "packet": packet_record,
        }
        reviews.append(review)
        entry["status"] = "running"
        entry["active_profile"] = escalation_profile
        entry["critical_review_call_id"] = call_id
        entry["repair_scope"] = f"{args.category}: {reason}"
        entry["repair_packet"] = packet_record
        entry["started_at"] = started_at
        entry["completed_at"] = None
        entry["failure_reason"] = None
        append_event(
            state,
            "critical_review_requested",
            args.role,
            f"{args.category}: {reason} | {packet_path.name}",
        )
        save_state(state_file, state)
        print(
            json.dumps(
                {
                    "state": str(state_file),
                    "role": args.role,
                    "call_id": call_id,
                    "category": args.category,
                    "packet": packet_record,
                    "execution": profile_contract,
                    "remaining_critical_reviews": limit - len(reviews),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0


def command_start(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_start(args, state_file)


def locked_start(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    refresh_statuses(state["roles"])
    entry = get_role(state, args.role)
    if entry["status"] not in {"ready", "failed"}:
        raise RunError(f"시작할 수 없는 상태입니다: {args.role}={entry['status']}")
    if entry.get("attempts", 0) >= entry.get("max_attempts", 2):
        raise RunError(
            f"역할 재검수 한도를 초과했습니다: {args.role}. "
            "전체 역할을 다시 호출하지 말고 미해결로 보고하거나 입력 변경을 확인하십시오."
        )
    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    execution = entry.get("execution", role_execution_policy(note_mode)[args.role])
    active_profile = execution.get("agent_profile") or execution.get("primary_profile")
    if entry["status"] == "failed":
        unmet = [dep for dep in entry["dependencies"] if state["roles"][dep]["status"] != "passed"]
        if unmet:
            raise RunError(f"선행 역할이 통과하지 않았습니다: {', '.join(unmet)}")
        repair_scope = (args.repair_scope or "").strip()
        if not repair_scope:
            raise RunError(
                "실패 역할의 재시작에는 --repair-scope로 고칠 페이지·절·전사 구간을 제한해야 합니다."
            )
        if len(repair_scope) > MAX_REPAIR_SCOPE_CHARS or "\n" in repair_scope or "\r" in repair_scope:
            raise RunError(f"repair-scope는 한 줄 {MAX_REPAIR_SCOPE_CHARS}자 이하여야 합니다.")
        if re.search(r"전체\s*(강의|전사|교안|자료|역할)|full\s+(lecture|transcript|handout|role)", repair_scope, re.IGNORECASE):
            raise RunError("repair-scope에 전체 강의·전사·교안·역할을 지정할 수 없습니다.")

        critical_reviews = state.get("cost_usage", {}).get("critical_reviews", [])
        if any(review.get("role") == args.role for review in critical_reviews if isinstance(review, dict)):
            raise RunError("이 역할은 고강도 국소 재검수를 이미 사용해 추가 repair를 실행할 수 없습니다.")

        executor = execution.get("executor")
        if executor in {"subagent", "hybrid"}:
            if not args.repair_packet:
                raise RunError(
                    "의미 역할의 국소 재검수에는 16KiB 이하의 --repair-packet JSON이 필요합니다."
                )
            _packet_path, packet_record = resolve_model_packet(args.repair_packet, state_file)
            entry["repair_packet"] = packet_record
        elif args.repair_packet:
            raise RunError("Python 전용 역할에는 모델 입력용 --repair-packet을 전달하지 않습니다.")
        entry["repair_scope"] = repair_scope
        active_profile = execution.get("repair_profile") or active_profile
    elif args.repair_scope or args.repair_packet:
        raise RunError("첫 실행에는 --repair-scope나 --repair-packet을 사용하지 않습니다.")
    if args.role == "final_reviewer":
        active_profile = reserve_premium_final_review(state, entry)
    invalidate_downstream(state["roles"], args.role)
    entry["status"] = "running"
    entry["attempts"] += 1
    entry["started_at"] = now_iso()
    entry["completed_at"] = None
    entry["artifacts"] = []
    entry["active_profile"] = active_profile
    entry["coverage_gate"] = None
    entry["failure_reason"] = None
    detail_parts = [f"profile={active_profile}"]
    if entry.get("repair_scope"):
        detail_parts.append(f"국소 재검수: {entry['repair_scope']}")
    detail = " | ".join(detail_parts)
    append_event(state, "started", args.role, detail)
    save_state(state_file, state)
    print(f"시작: {args.role} | 시도 {entry['attempts']}회 | 프로필 {active_profile}")
    return 0


def command_complete(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_complete(args, state_file)


def locked_complete(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    entry = get_role(state, args.role)
    if entry["status"] != "running":
        raise RunError(f"실행 중인 역할만 완료할 수 있습니다: {args.role}={entry['status']}")
    if args.role != "final_reviewer" and (args.source_map or args.coverage_report):
        raise RunError("--source-map과 --coverage-report는 final_reviewer 완료에만 사용합니다.")
    artifacts = [resolve_artifact(raw, state_file) for raw in args.artifact]
    patched_paths = [resolve_artifact(raw, state_file) for raw in (getattr(args, "patched", None) or [])]
    if patched_paths and args.role != "final_reviewer":
        raise RunError("--patched는 final_reviewer가 같은 호출 안에서 국소 수정한 선행 산출물을 다시 기록할 때만 사용합니다.")
    patched_records: list[dict[str, Any]] = []
    for path in patched_paths:
        owner = record_patched_artifact(state, path)
        patched_records.append({"role": owner, **artifact_record(path)})
    if patched_records:
        note_premium_patch(state, entry, patched_records)
    coverage_gate = None
    if args.role == "final_reviewer":
        coverage_gate, coverage_path = build_coverage_gate(
            state,
            state_file,
            args.source_map,
            args.coverage_report,
        )
        if coverage_path not in artifacts:
            artifacts.append(coverage_path)
        finish_premium_final_review(state, entry, "passed")
    finish_critical_review(state, entry, "passed")
    entry["status"] = "passed"
    entry["completed_at"] = now_iso()
    entry["artifacts"] = [artifact_record(path) for path in artifacts]
    entry["coverage_gate"] = coverage_gate
    entry["failure_reason"] = None
    append_event(state, "passed", args.role, f"산출물 {len(artifacts)}개")
    save_state(state_file, state)
    print(f"통과: {args.role} | 산출물 {len(artifacts)}개")
    return 0


def note_premium_patch(state: dict[str, Any], entry: dict[str, Any], patched: list[dict[str, Any]]) -> None:
    """검수 호출 안의 국소 수정을 고비용 호출 원장에 남기고 입력 지문을 수정 후 값으로 갱신한다."""
    call_id = entry.get("premium_call_id")
    for call in state.get("cost_usage", {}).get("premium_final_reviews", []):
        if isinstance(call, dict) and call.get("call_id") == call_id:
            call["patched_artifacts"] = patched
            call["input_fingerprint_before_patch"] = call.get("input_fingerprint")
            call["input_fingerprint"] = final_review_input_fingerprint(state)
            return
    raise RunError("최종 검수의 고비용 호출 원장 항목을 찾을 수 없습니다.")


def record_patched_artifact(state: dict[str, Any], path: Path) -> str:
    """최종 검수가 국소 수정한 선행 역할 산출물의 해시를 다시 기록한다.

    final_reviewer.md는 발견한 국소 문제를 같은 호출 안에서 고치라고 하므로, 고친 파일이
    통과 역할의 기록된 산출물이면 새 해시를 기록해 verify가 변조로 보지 않게 한다.
    """
    # 조판은 검수의 선행이 아니지만 패치된 초안에서 다시 만든 산출물이므로 함께 재기록한다.
    upstream = upstream_roles(state["roles"], "final_reviewer") | {"layout_builder"}
    for name in ROLE_ORDER:
        other = state["roles"][name]
        if other.get("status") != "passed":
            continue
        for index, record in enumerate(other.get("artifacts", [])):
            if not isinstance(record, dict) or not record.get("path"):
                continue
            if Path(record["path"]).resolve() != path:
                continue
            if name not in upstream:
                raise RunError(f"--patched 대상은 최종 검수의 선행 역할 산출물이어야 합니다: {name}: {path}")
            previous = str(record.get("sha256") or "")[:12]
            other["artifacts"][index] = artifact_record(path)
            current = str(other["artifacts"][index].get("sha256") or "")[:12]
            append_event(state, "review_patched", name, f"{path.name}: {previous or '?'} -> {current or '?'}")
            return name
    raise RunError(f"--patched 대상은 통과한 선행 역할의 기록된 산출물이어야 합니다: {path}")


def command_fail(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_fail(args, state_file)


def locked_fail(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    entry = get_role(state, args.role)
    if entry["status"] != "running":
        raise RunError(f"실행 중인 역할만 실패 처리할 수 있습니다: {args.role}={entry['status']}")
    if args.role == "final_reviewer":
        finish_premium_final_review(state, entry, "failed")
    finish_critical_review(state, entry, "failed")
    entry["status"] = "failed"
    entry["failure_reason"] = args.reason
    entry["completed_at"] = now_iso()
    append_event(state, "failed", args.role, args.reason)
    save_state(state_file, state)
    print(f"실패: {args.role} | {args.reason}")
    return 0


# -----------------------------------------------------------------------------
# 6. 최종 무결성 검사
# 시작할 때의 입력과 통과할 때의 산출물이 그대로인지 다시 계산한다.
# -----------------------------------------------------------------------------

def changed_inputs(state: dict[str, Any]) -> list[str]:
    input_root = Path(state["input_root"])
    changed: list[str] = []
    recorded = {item["path"]: item for item in state["inputs"]}
    current_paths = {path.relative_to(input_root).as_posix(): path for path in iter_input_files(input_root)}
    for relative, item in recorded.items():
        path = current_paths.get(relative)
        if path is None:
            changed.append(f"삭제됨: {relative}")
        elif path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            changed.append(f"변경됨: {relative}")
    for relative in sorted(set(current_paths) - set(recorded)):
        changed.append(f"추가됨: {relative}")
    return changed


def verify_coverage_gate(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    final_entry = state["roles"]["final_reviewer"]
    if final_entry.get("status") != "passed":
        return errors
    gate = final_entry.get("coverage_gate")
    if not isinstance(gate, dict):
        return ["최종 검수의 source coverage 게이트 기록이 없음"]
    source_record = gate.get("source_map")
    coverage_record = gate.get("coverage_report")
    if not isinstance(source_record, dict) or not artifact_matches(source_record):
        errors.append("최종 검수 source map이 변경되었거나 누락됨")
    if not isinstance(coverage_record, dict) or not artifact_matches(coverage_record):
        errors.append("최종 검수 coverage report가 변경되었거나 누락됨")
    if errors:
        return errors

    source_path = Path(source_record["path"])
    coverage_path = Path(coverage_record["path"])
    if not artifact_was_recorded(state["roles"]["source_mapper"].get("artifacts", []), source_path):
        errors.append("최종 검수 source map이 source_mapper의 기록된 산출물이 아님")
    try:
        _source_ids, summary = validate_coverage(source_path, coverage_path)
    except CoverageValidationError as exc:
        errors.extend(f"source coverage: {issue.message}" for issue in exc.report.errors)
        return errors

    payload = read_json_object(coverage_path, "coverage report")
    note_mode = state.get("note_mode", DEFAULT_NOTE_MODE)
    expected_profile = role_execution_policy(note_mode)["final_reviewer"]["agent_profile"]
    if gate.get("note_mode") != note_mode or payload.get("note_mode") != note_mode:
        errors.append("최종 검수 coverage report의 note_mode가 실행 상태와 다름")
    if (
        gate.get("reviewer_profile") != expected_profile
        or payload.get("reviewer_profile") != expected_profile
    ):
        errors.append("최종 검수 coverage report의 reviewer_profile이 실행 계약과 다름")
    if gate.get("summary") != summary:
        errors.append("최종 검수 coverage 집계가 기록 후 변경됨")
    return errors


def verify_premium_final_reviews(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    calls = state.get("cost_usage", {}).get("premium_final_reviews", [])
    call_by_id = {
        call.get("call_id"): call
        for call in calls
        if isinstance(call, dict) and isinstance(call.get("call_id"), str)
    }
    if len(call_by_id) != len(calls):
        errors.append("완성본 고비용 검수 call_id가 없거나 중복됨")
    for call in calls:
        if not isinstance(call, dict):
            continue
        mode = call.get("note_mode")
        route = PREMIUM_FINAL_REVIEW_ROUTES.get(mode)
        if route is None:
            continue
        contract = resolve_state_profile(state, route["profile"])
        if (
            call.get("runtime") != contract["runtime"]
            or call.get("model") != contract["model"]
            or call.get("reasoning_effort") != contract["effort"]
            or call.get("attempt_kind") != "full_note_audit"
        ):
            errors.append(f"완성본 고비용 검수 실행 계약이 변조됨: {call.get('call_id')}")

    final_entry = state["roles"]["final_reviewer"]
    if final_entry.get("status") in {"running", "passed", "failed"}:
        call_id = final_entry.get("premium_call_id")
        call = call_by_id.get(call_id)
        if call is None:
            errors.append("최종 검수 역할과 연결된 고비용 호출 기록이 없음")
        else:
            if call.get("status") != final_entry.get("status"):
                errors.append("최종 검수 역할 상태와 고비용 호출 상태가 다름")
            if call.get("review_cycle") != state.get("review_cycle"):
                errors.append("최종 검수가 현재 review cycle의 호출과 연결되지 않음")
            if call.get("note_mode") != state.get("note_mode"):
                errors.append("최종 검수 호출의 제작 모드가 현재 상태와 다름")
            try:
                current_fingerprint = final_review_input_fingerprint(state)
            except RunError as exc:
                errors.append(str(exc))
            else:
                if call.get("input_fingerprint") != current_fingerprint:
                    errors.append("최종 검수 호출 이후 source map 또는 완성본이 바뀜")
    return errors


def verify_state(state: dict[str, Any], check_inputs: bool) -> list[str]:
    errors: list[str] = []
    roles = state["roles"]
    for role in ROLE_ORDER:
        entry = roles[role]
        if entry["active"] and entry["status"] != "passed":
            errors.append(f"활성 역할 미통과: {role}={entry['status']}")
        if entry["status"] == "passed":
            if not entry["artifacts"]:
                errors.append(f"통과 역할의 산출물 없음: {role}")
            for record in entry["artifacts"]:
                if not isinstance(record, dict) or "path" not in record:
                    errors.append(f"잘못된 산출물 기록: {role}")
                elif not artifact_matches(record):
                    errors.append(f"기록 후 변경되었거나 누락된 산출물: {role}: {record['path']}")
            for dep in entry["dependencies"]:
                if roles[dep]["status"] != "passed":
                    errors.append(f"선행 역할 미통과 상태에서 통과됨: {role} <- {dep}")
        repair_packet = entry.get("repair_packet")
        if repair_packet is not None and not artifact_matches(repair_packet):
            errors.append(f"국소 재검수 패킷이 변경되었거나 누락됨: {role}")
    if check_inputs:
        errors.extend(changed_inputs(state))
    errors.extend(verify_coverage_gate(state))
    errors.extend(verify_premium_final_reviews(state))
    critical_reviews = state.get("cost_usage", {}).get("critical_reviews", [])
    if len(critical_reviews) > CRITICAL_REVIEW_LIMIT:
        errors.append("고강도 국소 재검수 기록이 허용 한도를 초과함")
    critical_by_id: dict[str, dict[str, Any]] = {}
    for review in critical_reviews:
        packet = review.get("packet") if isinstance(review, dict) else None
        if not isinstance(packet, dict) or not artifact_matches(packet):
            errors.append("고강도 국소 재검수 패킷이 변경되었거나 누락됨")
        if isinstance(review, dict) and isinstance(review.get("call_id"), str):
            critical_by_id[review["call_id"]] = review
    for role, entry in roles.items():
        call_id = entry.get("critical_review_call_id")
        if not call_id:
            continue
        call = critical_by_id.get(call_id)
        if call is None:
            errors.append(f"역할과 연결된 고강도 국소 재검수 호출이 없음: {role}")
            continue
        if call.get("role") != role or call.get("status") != entry.get("status"):
            errors.append(f"역할과 고강도 국소 재검수 호출 상태가 다름: {role}")
    linked_ids = {
        entry.get("critical_review_call_id") for entry in roles.values() if entry.get("critical_review_call_id")
    }
    for call_id, call in critical_by_id.items():
        if call.get("status") == "running" and call_id not in linked_ids:
            errors.append("실행 중인 고강도 국소 재검수 호출이 역할과 연결되지 않음")
    return errors


def command_verify(args: argparse.Namespace) -> int:
    state = read_state(args.state.expanduser().resolve())
    errors = verify_state(state, args.check_inputs)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        print(f"검증 결과: FAIL | 오류 {len(errors)}개")
        return 1
    print("검증 결과: PASS")
    return 0


# -----------------------------------------------------------------------------
# 7. 명령행 인터페이스
# 일반 사용자가 아니라 관리자 에이전트와 개발자가 호출하는 내부 제어면이다.
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    default_root = ENGINE_ROOT
    parser = argparse.ArgumentParser(description="학습노트 역할 실행 계획과 상태를 관리합니다.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="입력 해시와 선택적 역할 실행 계획을 만듭니다.")
    init_parser.add_argument("input_dir", type=Path)
    init_parser.add_argument("--lecture-id", required=True)
    init_parser.add_argument(
        "--output-format",
        choices=("md", "pdf", "docx"),
        default=None,
        help="최종 형식. 생략하면 faithful은 md(바로 읽고 고치는 용도), deep은 pdf(인쇄용). 사용자 지정이 우선.",
    )
    init_parser.add_argument(
        "--note-mode",
        choices=NOTE_MODES,
        required=True,
        help="학습노트 제작 모드(필수): faithful=자료 충실형, deep=심화 이해형. 사용자가 고르지 않았으면 먼저 물어본다.",
    )
    init_parser.add_argument(
        "--runtime",
        choices=RUNTIMES,
        default=None,
        help="실행 런타임. 생략하면 환경(Claude Code는 CLAUDECODE, Codex는 CODEX_*)에서 감지하고, 감지 실패 시 명시가 필요하다.",
    )
    init_parser.add_argument("--root", type=Path, default=default_root, help="저장소 루트. 상태는 <root>/workspace/<강의ID>/ 에 만든다.")
    init_parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="상태 폴더를 직접 지정: <state-root>/<강의ID>/run_state.json. 과목 폴더에서 `gongbu run init`을 쓰면 <과목>/.gongbu 가 들어간다. --root보다 우선.",
    )
    init_parser.add_argument(
        "--classify",
        action="append",
        default=[],
        metavar="파일=유형",
        help="자동 분류를 명시적으로 교정합니다. 예: 강의메모.txt=transcript",
    )
    init_parser.set_defaults(func=command_init)

    for name, func, help_text in (
        ("status", command_status, "전체 역할 상태를 표시합니다."),
        ("next", command_next, "현재 실행 가능한 역할만 JSON으로 표시합니다."),
        ("verify", command_verify, "완료 상태와 산출물을 검증합니다."),
    ):
        command_parser = subparsers.add_parser(name, help=help_text)
        command_parser.add_argument("state", type=Path)
        if name == "verify":
            command_parser.add_argument("--check-inputs", action="store_true")
        command_parser.set_defaults(func=func)

    activate_parser = subparsers.add_parser("activate", help="조건부 역할을 활성화합니다.")
    activate_parser.add_argument("state", type=Path)
    activate_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    activate_parser.add_argument("--reason", required=True)
    activate_parser.set_defaults(func=command_activate)

    deactivate_parser = subparsers.add_parser("deactivate", help="불필요한 조건부 역할을 비활성화합니다.")
    deactivate_parser.add_argument("state", type=Path)
    deactivate_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    deactivate_parser.add_argument("--reason", required=True)
    deactivate_parser.set_defaults(func=command_deactivate)

    refresh_parser = subparsers.add_parser(
        "refresh-inputs", help="변경된 입력 해시와 분류를 갱신하고 영향 단계만 무효화합니다."
    )
    refresh_parser.add_argument("state", type=Path)
    refresh_parser.add_argument(
        "--classify",
        action="append",
        default=[],
        metavar="파일=유형",
        help="입력 파일의 자동 분류를 교정합니다.",
    )
    refresh_parser.set_defaults(func=command_refresh_inputs)

    rerun_parser = subparsers.add_parser(
        "rerun", help="입력은 유지하고 선택한 통과 역할과 후속 역할만 다시 실행합니다."
    )
    rerun_parser.add_argument("state", type=Path)
    rerun_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    rerun_parser.add_argument("--reason", required=True)
    rerun_parser.add_argument(
        "--change-kind",
        required=True,
        choices=("user_request", "output_contract"),
        help="실패 재시도가 아니라 새 사용자 요청 또는 출력 계약 변경임을 기록합니다.",
    )
    rerun_parser.set_defaults(func=command_rerun)

    mode_parser = subparsers.add_parser(
        "set-mode", help="입력과 자료 매핑은 유지하고 학습노트 제작 모드를 변경합니다."
    )
    mode_parser.add_argument("state", type=Path)
    mode_parser.add_argument("--note-mode", required=True, choices=NOTE_MODES)
    mode_parser.add_argument("--reason", required=True)
    mode_parser.set_defaults(func=command_set_mode)

    escalation_parser = subparsers.add_parser(
        "escalate",
        help="작고 중요한 JSON 패킷 하나의 모드별 고강도 검수 호출을 한 번만 예약·시작합니다.",
    )
    escalation_parser.add_argument("state", type=Path)
    escalation_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    escalation_parser.add_argument("--packet", required=True)
    escalation_parser.add_argument("--category", required=True, choices=CRITICAL_REVIEW_CATEGORIES)
    escalation_parser.add_argument("--reason", required=True)
    escalation_parser.set_defaults(func=command_escalate)

    repair_parser = subparsers.add_parser(
        "repair", help="최종 검수가 반려한 내용 결함을 고치기 위해 선행 역할과 후속 단계를 다시 엽니다."
    )
    repair_parser.add_argument("state", type=Path)
    repair_parser.add_argument("--from-role", default="final_reviewer", choices=("final_reviewer",))
    repair_parser.add_argument("--reopen", required=True, choices=ROLE_ORDER, help="다시 열 선행 역할(보통 writer 또는 source_mapper)")
    repair_parser.add_argument("--reason", required=True, help="한 줄 반려 사유")
    repair_parser.add_argument("--findings", default=None, help="검수 보고서 경로(기록용, 상태 폴더 기준)")
    repair_parser.set_defaults(func=command_repair)

    start_parser = subparsers.add_parser("start", help="준비된 역할의 실제 실행을 기록합니다.")
    start_parser.add_argument("state", type=Path)
    start_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    start_parser.add_argument(
        "--repair-scope",
        help="실패 역할 재시작 시 다시 처리할 페이지·절·전사 구간. 전체 역할 재시도를 금지합니다.",
    )
    start_parser.add_argument(
        "--repair-packet",
        help="의미 역할 국소 재검수용 model_input=true JSON(최대 16KiB)",
    )
    start_parser.set_defaults(func=command_start)

    complete_parser = subparsers.add_parser("complete", help="실행 중 역할을 산출물과 함께 통과 처리합니다.")
    complete_parser.add_argument("state", type=Path)
    complete_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    complete_parser.add_argument("--artifact", action="append", required=True)
    complete_parser.add_argument(
        "--patched",
        action="append",
        default=[],
        help="final_reviewer 전용: 검수 호출 안에서 국소 수정한 선행 산출물(초안·최종본·PDF). 새 해시를 다시 기록한다.",
    )
    complete_parser.add_argument(
        "--source-map",
        help="final_reviewer가 대조한 source_mapper의 study_note_source_map JSON",
    )
    complete_parser.add_argument(
        "--coverage-report",
        help="final_reviewer가 만든 study_note_source_coverage JSON",
    )
    complete_parser.set_defaults(func=command_complete)

    fail_parser = subparsers.add_parser("fail", help="실행 중 역할의 실패 이유를 기록합니다.")
    fail_parser.add_argument("state", type=Path)
    fail_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    fail_parser.add_argument("--reason", required=True)
    fail_parser.set_defaults(func=command_fail)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except RunError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
