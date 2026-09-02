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
    from .project_types import AUDIO_SUFFIXES
except ImportError:  # `python scripts/manage_run.py`로 직접 실행할 때
    from project_types import AUDIO_SUFFIXES

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# -----------------------------------------------------------------------------
# 1. 실행 계획의 고정 정의
# 역할 순서와 파일 확장자 분류 기준은 모든 실행 상태가 공유한다.
# -----------------------------------------------------------------------------

SCHEMA_VERSION = 1
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


def inventory(input_root: Path, overrides: dict[str, str] | None = None) -> list[dict[str, Any]]:
    if not input_root.exists() or not input_root.is_dir():
        raise RunError(f"입력 폴더가 없습니다: {input_root}")
    files = sorted((path for path in input_root.rglob("*") if path.is_file()), key=lambda p: p.as_posix().lower())
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
        "started_at": None,
        "completed_at": None,
        "artifacts": [],
        "failure_reason": None,
    }


def make_roles(items: list[dict[str, Any]], output_format: str) -> dict[str, dict[str, Any]]:
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
    roles["pedagogy_editor"] = role_entry(False, "설명 부족 판정 시에만 활성화", ["writer"])

    layout_dependencies = ["writer"]
    for role in ("instructor_integrator", "formula_code_checker", "pedagogy_editor"):
        if roles[role]["active"]:
            layout_dependencies.append(role)
    roles["layout_builder"] = role_entry(True, f"{output_format} 최종 형식 생성", layout_dependencies)
    roles["final_reviewer"] = role_entry(True, "독립 최종 검수", ["layout_builder"])
    roles["maintainer"] = role_entry(
        False,
        "복수 최종 파일의 이동·패키징·전달 정리가 필요할 때만 활성화",
        ["final_reviewer"],
    )

    for name, entry in roles.items():
        entry["prompt"] = ROLE_PROMPTS[name]
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
    roles["final_reviewer"]["dependencies"] = ["layout_builder"]
    roles["maintainer"]["dependencies"] = ["final_reviewer"]


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
    validate_state_shape(state)
    return state


def validate_state_shape(state: dict[str, Any]) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise RunError(f"지원하지 않는 실행 상태 버전입니다: {state.get('schema_version')}")
    roles = state.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLE_ORDER):
        raise RunError("실행 상태의 역할 목록이 현재 프로젝트와 일치하지 않습니다.")
    for name, entry in roles.items():
        if entry.get("status") not in {"skipped", "blocked", "ready", "running", "passed", "failed"}:
            raise RunError(f"알 수 없는 역할 상태입니다: {name}={entry.get('status')}")
        for dependency in entry.get("dependencies", []):
            if dependency not in roles:
                raise RunError(f"알 수 없는 선행 역할입니다: {name} -> {dependency}")


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
            if entry["active"] and entry["status"] == "passed":
                entry["status"] = "blocked"
                entry["completed_at"] = None
                entry["artifacts"] = []
                entry["failure_reason"] = "선행 역할이 다시 실행되어 기존 통과가 무효화됨"


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
    new_roles = make_roles(new_items, state["output_format"])
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
        new["attempts"] = old.get("attempts", 0)
        if role in affected:
            if new["active"]:
                new["failure_reason"] = "입력 변경으로 기존 통과가 무효화됨"
            continue
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
    state_file = state_path_for(root, args.lecture_id)
    if state_file.exists():
        raise RunError(f"이미 실행 상태가 있습니다. 덮어쓰지 않았습니다: {state_file}")
    # 입력 검증과 해시 계산은 락·폴더 생성 전에 끝내, 실패한 init이
    # 빈 workspace/<강의ID>/ 폴더를 남기지 않게 한다.
    classification_overrides = parse_classification_overrides(input_root, args.classify)
    items = inventory(input_root, classification_overrides)
    with state_write_lock(state_file, create_parent=True):
        return locked_init(args, root, input_root, state_file, classification_overrides, items)


def locked_init(
    args: argparse.Namespace,
    root: Path,
    input_root: Path,
    state_file: Path,
    classification_overrides: dict[str, str],
    items: list[dict[str, Any]],
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
        "output_format": args.output_format,
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
        "roles": make_roles(items, args.output_format),
        "events": [{"at": timestamp, "event": "initialized", "detail": f"입력 {len(items)}개"}],
    }
    write_json(state_file, state)
    print(state_file)
    return 0


def command_status(args: argparse.Namespace) -> int:
    state = read_state(args.state.expanduser().resolve())
    print(f"강의: {state['lecture_id']} | 출력: {state['output_format']} | 입력: {len(state['inputs'])}개")
    for role in ROLE_ORDER:
        entry = state["roles"][role]
        active = "활성" if entry["active"] else "비활성"
        deps = ",".join(entry["dependencies"]) or "-"
        print(f"{role:24} {entry['status']:8} {active:4} 선행={deps}")
    return 0


def command_next(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    state = read_state(state_file)
    refresh_statuses(state["roles"])
    ready = []
    for role in ROLE_ORDER:
        entry = state["roles"][role]
        if entry["status"] == "ready":
            ready.append(
                {
                    "role": role,
                    "prompt": entry["prompt"],
                    "reason": entry["reason"],
                    "attempts": entry["attempts"],
                }
            )
    print(json.dumps({"state": str(state_file), "ready": ready}, ensure_ascii=False, indent=2))
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
    entry["failure_reason"] = None
    normalize_dependencies(state["roles"])
    state.setdefault("role_overrides", {})[args.role] = {"mode": "inactive", "reason": args.reason}
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
    append_event(state, "inputs_refreshed", "manager", detail)
    save_state(state_file, state)
    print(f"입력 갱신: 변경 {len(changes)}개 | 영향 단계만 재실행")
    for change in changes:
        print(f"- {change['path']}")
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

        entry = get_role(state, args.role)
        if not entry["active"]:
            raise RunError(f"비활성 역할은 재실행할 수 없습니다: {args.role}")
        if entry["status"] != "passed":
            raise RunError(f"통과한 역할만 선택 재실행할 수 있습니다: {args.role}={entry['status']}")

        invalidate_downstream(state["roles"], args.role)
        entry["status"] = "blocked"
        entry["started_at"] = None
        entry["completed_at"] = None
        entry["artifacts"] = []
        entry["failure_reason"] = args.reason
        refresh_statuses(state["roles"])
        append_event(state, "rerun_requested", args.role, args.reason)
        save_state(state_file, state)
        print(f"재실행 예약: {args.role} | {entry['status']} | {args.reason}")
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
    if entry["status"] == "failed":
        unmet = [dep for dep in entry["dependencies"] if state["roles"][dep]["status"] != "passed"]
        if unmet:
            raise RunError(f"선행 역할이 통과하지 않았습니다: {', '.join(unmet)}")
    invalidate_downstream(state["roles"], args.role)
    entry["status"] = "running"
    entry["attempts"] += 1
    entry["started_at"] = now_iso()
    entry["completed_at"] = None
    entry["artifacts"] = []
    entry["failure_reason"] = None
    append_event(state, "started", args.role)
    save_state(state_file, state)
    print(f"시작: {args.role} | 시도 {entry['attempts']}회")
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
    artifacts = [resolve_artifact(raw, state_file) for raw in args.artifact]
    entry["status"] = "passed"
    entry["completed_at"] = now_iso()
    entry["artifacts"] = [artifact_record(path) for path in artifacts]
    entry["failure_reason"] = None
    append_event(state, "passed", args.role, f"산출물 {len(artifacts)}개")
    save_state(state_file, state)
    print(f"통과: {args.role} | 산출물 {len(artifacts)}개")
    return 0


def command_fail(args: argparse.Namespace) -> int:
    state_file = args.state.expanduser().resolve()
    with state_write_lock(state_file):
        return locked_fail(args, state_file)


def locked_fail(args: argparse.Namespace, state_file: Path) -> int:
    state = read_state(state_file)
    entry = get_role(state, args.role)
    if entry["status"] != "running":
        raise RunError(f"실행 중인 역할만 실패 처리할 수 있습니다: {args.role}={entry['status']}")
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
    current_paths = {path.relative_to(input_root).as_posix(): path for path in input_root.rglob("*") if path.is_file()}
    for relative, item in recorded.items():
        path = current_paths.get(relative)
        if path is None:
            changed.append(f"삭제됨: {relative}")
        elif path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            changed.append(f"변경됨: {relative}")
    for relative in sorted(set(current_paths) - set(recorded)):
        changed.append(f"추가됨: {relative}")
    return changed


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
    if check_inputs:
        errors.extend(changed_inputs(state))
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
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="학습노트 역할 실행 계획과 상태를 관리합니다.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="입력 해시와 선택적 역할 실행 계획을 만듭니다.")
    init_parser.add_argument("input_dir", type=Path)
    init_parser.add_argument("--lecture-id", required=True)
    init_parser.add_argument("--output-format", choices=("md", "pdf", "docx"), default="pdf")
    init_parser.add_argument("--root", type=Path, default=default_root)
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
    rerun_parser.set_defaults(func=command_rerun)

    start_parser = subparsers.add_parser("start", help="준비된 역할의 실제 실행을 기록합니다.")
    start_parser.add_argument("state", type=Path)
    start_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    start_parser.set_defaults(func=command_start)

    complete_parser = subparsers.add_parser("complete", help="실행 중 역할을 산출물과 함께 통과 처리합니다.")
    complete_parser.add_argument("state", type=Path)
    complete_parser.add_argument("--role", required=True, choices=ROLE_ORDER)
    complete_parser.add_argument("--artifact", action="append", required=True)
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
