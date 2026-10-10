#!/usr/bin/env python3
"""과목 원장과 학기 목록을 관리하고, 네트워크 없이 과목 현황을 계산한다.

    gongbu status [--json] [--all | --semester ID]             # 읽기 전용
    gongbu course init --name 일반물리학1 --mode deep --materials handout+recording
    gongbu course import plan.json [--dry-run]                 # 강의·자료·노트를 한 번에 등록
    gongbu course note ch05 --source "output/source/05 학습노트.tex" --progress in_progress
    gongbu course material add ch05 "물리 5-1.txt" --kind transcript
    gongbu semester init 2026-2 --title 2026-2학기 --start 2026-08-31 --weeks 15
    gongbu semester add-course

과목 원장(`<과목>/.gongbu/course.json`)은 강의마다 어떤 자료와 노트가 있는지 적은 목록이다. 실행 상태나 검수
근거가 아니다. 학기 목록(`semesters.json`)은 사용자 설정 폴더에 두고 비밀 값을 담지 않는다. `status`와
`collect_status`는 아무것도 쓰지 않고 노션·토큰에 손대지 않는다. 원장 경로는 과목 폴더 기준 POSIX·NFC로 적는다.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from . import push_notion as pn
    from .project_types import AUDIO_SUFFIXES
except ImportError:  # gongbu가 scripts/를 sys.path에 넣고 스크립트를 직접 실행할 때
    import push_notion as pn
    from project_types import AUDIO_SUFFIXES

LEDGER_VERSION = 1
REGISTRY_VERSION = 1
LEDGER_NAME = "course.json"
REGISTRY_NAME = "semesters.json"
CONFIG_ENV = "GONGBU_HAJA_CONFIG"
STATUS_SCHEMA = "gongbu.status/1"
SETTLE_SECONDS = 300  # 이보다 최근에 바뀐 파일은 아직 녹음·다운로드 중일 수 있다
MODES = tuple(pn.MODES)
MATERIAL_DEFAULTS = {"handout": {"handout": 1, "transcript": 0}, "handout+recording": {"handout": 1, "transcript": 1}}
MATERIAL_LABELS = {"handout": "교안만", "handout+recording": "교안+녹음"}
KINDS = ("handout", "recording", "transcript")
KIND_LABELS = {"handout": "교안", "recording": "녹음", "transcript": "전사", "note": "노트 원고"}
PROGRESS = ("in_progress", "done")
ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,40}")
PRUNE_DIRS = frozenset({"output", "tmp", "temp", "scratch", "__pycache__", "node_modules"})
NEVER_DIRS = frozenset({"__pycache__", "node_modules"})
SKIP_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "README.md", ".gitignore"})
SKIP_SUFFIXES = (".recording.json", "_transcript_manifest.json", "_segments.json")
MANIFEST_SUFFIX = "_transcript_manifest.json"
UNSTABLE_SUFFIXES = (".part", ".part.wav", ".crdownload", ".download", ".partial", ".tmp")
UNSTABLE_PREFIXES = ("~$", ".~lock")
SUFFIXES = {"handout": frozenset({".pdf", ".ppt", ".pptx", ".hwp", ".hwpx", ".doc", ".docx"}),
            "recording": frozenset(AUDIO_SUFFIXES | {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}),
            "transcript": frozenset({".txt", ".md", ".srt"})}
NOTE_WORD = "학습노트"
NOTE_TEX_RE = re.compile(r"^[^%\n]*\\(?:gongbucover|documentclass)\b", re.M)
RELISTEN_LABELS = ("전사 불명확", "청취 불가")
HINT_DEPTH = 3
HINT_FILES = 5000
PLAN_KEYS = {"course": ("name", "aliases", "mode", "materials", "expect", "planned", "ignore", "conventions"),
             "lecture": ("id", "title", "weeks", "expect", "materials", "note"),
             "material": ("kind", "path", "part", "from"),
             "note": ("source", "outputs", "mode", "progress", "covers")}


class LedgerError(ValueError):
    """사용자가 고칠 수 있는 원장·학기 목록 오류. 명령은 `[오류]`로 알리고 2로 끝난다."""


# ------------------------------------------------------------------ 경로와 파일

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def path_key(path: str) -> str:
    """경로 비교용 열쇠: NFC로 맞추고 OS 규칙대로 대소문자를 접는다."""
    return os.path.normcase(nfc(path))


def safe_relative(rel: str) -> bool:
    parts = re.split(r"[\\/]", rel)
    return bool(rel) and not rel.startswith(("/", "\\")) and ":" not in parts[0] and ".." not in parts


def course_path(course: Path, value: str | Path) -> str:
    """사용자가 준 경로(과목 폴더 기준 또는 절대)를 원장에 적을 과목 기준 POSIX·NFC 경로로 바꾼다."""
    raw = str(value)
    if ".." in re.split(r"[\\/]", raw):
        raise LedgerError(f"경로에 '..'를 쓸 수 없습니다: {raw}")
    path = Path(raw).expanduser()
    resolved = (path if path.is_absolute() else course / path).resolve()  # 있는 파일은 디스크의 대소문자로 맞춰진다
    try:
        relative = resolved.relative_to(course)
    except ValueError:
        raise LedgerError(f"과목 폴더 밖의 파일은 등록할 수 없습니다: {raw}") from None
    if not relative.parts:
        raise LedgerError(f"과목 폴더 자체는 등록할 수 없습니다: {raw}")
    return nfc(relative.as_posix())


def locate(course: Path, rel: str) -> Path | None:
    """원장 경로의 실제 파일. 디스크의 이름이 NFC가 아니거나 대소문자가 달라도 찾는다. 없으면 None."""
    if not safe_relative(rel):
        return None
    direct = course / rel
    if direct.exists():
        return direct
    current = course
    for part in rel.split("/"):
        key = path_key(part)
        try:
            with os.scandir(current) as entries:
                match = next((entry.path for entry in entries if path_key(entry.name) == key), None)
        except OSError:
            return None
        if match is None:
            return None
        current = Path(match)
    return current


def rel_of(course: Path, path: Path) -> str:
    try:
        return nfc(path.resolve().relative_to(course).as_posix())
    except (OSError, ValueError):
        return nfc(path.as_posix())


def fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"sha256": pn.file_sha256(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def current_sha(path: Path, known: dict[str, Any] | None = None) -> str:
    """파일의 지금 sha256. 크기·수정 시각이 등록 때와 같으면 등록 때 값을 그대로 쓴다."""
    stat = path.stat()
    if known and known.get("sha256") and known.get("size") == stat.st_size and known.get("mtime_ns") == stat.st_mtime_ns:
        return known["sha256"]
    return pn.file_sha256(path)


def size_of(path: Path | None) -> int | None:
    try:
        return path.stat().st_size if path else None
    except OSError:
        return None


def mtime_of(path: Path | None) -> float | None:
    try:
        return path.stat().st_mtime if path else None
    except OSError:
        return None


def unstable_name(name: str) -> bool:
    return name.lower().endswith(UNSTABLE_SUFFIXES) or name.startswith(UNSTABLE_PREFIXES)


def material_kind(name: str) -> str | None:
    """이름으로 짐작한 자료 종류. 이름에 '학습노트'가 든 파일은 노트 산출물이라 자료로 보지 않는다."""
    if NOTE_WORD in nfc(name):
        return None
    suffix = Path(name).suffix.lower()
    return next((kind for kind, suffixes in SUFFIXES.items() if suffix in suffixes), None)


def is_note_name(name: str) -> bool:
    return NOTE_WORD in nfc(name) and name.lower().endswith(".md")


def dependencies(source: Path) -> list[Path]:
    try:
        return pn.note_dependencies(source)
    except (OSError, ValueError):
        return [source]


def source_digest(parts: list[Path]) -> str:
    """원고와 `\\input` 조각 파일의 바이트를 차례대로 이은 sha256."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.read_bytes())
    return digest.hexdigest()


def first_line(value: object) -> str:
    text = str(value).strip()
    return text.splitlines()[0] if text else ""


def now_iso(moment: float | None = None) -> str:
    stamp = dt.datetime.now() if moment is None else dt.datetime.fromtimestamp(moment)
    return stamp.astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ JSON 파일과 잠금

def read_json(path: Path, what: str, version: int) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{what}을 읽지 못했습니다(JSON 오류 {exc.lineno}행 {exc.colno}열): {path}") from None
    if not isinstance(data, dict):
        raise LedgerError(f"{what} 형식이 아닙니다(맨 바깥이 객체여야 합니다): {path}")
    found = data.get("version", 1)
    if not isinstance(found, int) or found > version:
        raise LedgerError(f"{what}이 이 엔진보다 새 형식(version {found})입니다. 엔진을 업데이트하십시오: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextlib.contextmanager
def locked(lock: Path) -> Iterator[None]:
    """pn.file_lock과 같다. 다만 실패한 첫 쓰기가 잠금 때문에 만든 빈 폴더를 남기지 않게 한다."""
    existed = lock.parent.exists()
    try:
        with pn.file_lock(lock):
            yield
    except BaseException:
        if not existed:
            with contextlib.suppress(OSError):
                lock.parent.rmdir()  # 비어 있을 때만 지워진다
        raise


def ledger_path(course: Path) -> Path:
    return course / pn.STATE_DIR / LEDGER_NAME


def require_folder(course: Path) -> Path:
    if not course.is_dir():
        raise LedgerError(f"과목 폴더가 없습니다: {course}")
    return course


def load_ledger(course: Path) -> dict[str, Any] | None:
    path = ledger_path(course)
    return read_json(path, "과목 원장", LEDGER_VERSION) if path.is_file() else None


def change_ledger(course: Path, change: Callable[[dict[str, Any] | None], tuple[dict[str, Any], dict[str, Any]]],
                  dry_run: bool = False) -> dict[str, Any]:
    """잠금 아래에서 원장을 다시 읽고 change로 바꿔 저장한다. change는 (바뀐 원장, 요약)을 돌려준다.

    dry_run이면 잠금 파일도 만들지 않고 바뀔 내용만 계산한다. 과목 폴더가 없으면(경로 오타) 만들지 않고 멈춘다.
    """
    require_folder(course)
    if dry_run:
        ledger, summary = change(copy.deepcopy(load_ledger(course)))
        check_ledger(ledger)
        return {"status": "ok", "dry_run": True, **summary}
    with locked(course / pn.STATE_DIR / "course.lock"):
        ledger, summary = change(load_ledger(course))
        check_ledger(ledger)
        write_json(ledger_path(course), ledger)
    return {"status": "ok", **summary}


def registry_dir() -> Path:
    """학기 목록을 두는 사용자 설정 폴더. GONGBU_HAJA_CONFIG가 있으면 그 폴더다."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "gongbu-haja"


def registry_path() -> Path:
    return registry_dir() / REGISTRY_NAME


def load_registry() -> dict[str, Any]:
    """학기 목록. 파일이 없으면 빈 목록이다(만들지 않는다)."""
    path = registry_path()
    registry = read_json(path, "학기 목록", REGISTRY_VERSION) if path.is_file() else {}
    for key, value in {"version": REGISTRY_VERSION, "current": None, "root_page_id": None, "semesters": {}}.items():
        registry.setdefault(key, value)
    if not isinstance(registry["semesters"], dict):
        raise LedgerError(f"학기 목록의 semesters가 객체가 아닙니다: {path}")
    return registry


def update_registry(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """잠금 아래에서 학기 목록을 다시 읽고 mutate로 바꿔 저장한다. 다른 프로세스가 바꾼 부분을 덮지 않는다."""
    with locked(registry_dir() / "semesters.lock"):
        registry = load_registry()
        mutate(registry)
        write_json(registry_path(), registry)
        return registry


def dir_key(path: Path | str) -> str:
    return path_key(Path(path).expanduser().resolve().as_posix())


def semester_of(course_dir: Path, registry: dict[str, Any] | None = None) -> str | None:
    """이 과목 폴더가 들어 있는 학기 ID. 여러 학기에 있으면 현재 학기를 고른다."""
    registry = load_registry() if registry is None else registry
    key = dir_key(course_dir)
    found = [sem_id for sem_id, semester in registry.get("semesters", {}).items()
             if any(dir_key(course) == key for course in semester.get("courses", []))]
    if registry.get("current") in found:
        return registry["current"]
    return found[0] if found else None


def current_week(semester: dict[str, Any], today: dt.date) -> int | None:
    try:
        start, weeks = dt.date.fromisoformat(str(semester["start"])), int(semester["weeks"])
    except (KeyError, TypeError, ValueError):
        return None
    return min(max((today - start).days // 7 + 1, 1), max(weeks, 1))


def has_course_files(directory: Path) -> bool:
    return ledger_path(directory).is_file() or pn.state_path(directory).is_file()


def find_course_root(start: Path) -> Path:
    """start에서 위로 올라가며 `.gongbu/course.json`이나 `.gongbu/notion.json`이 있는 가장 가까운 폴더를 찾는다.

    드라이브 맨 위나 사용자 홈 폴더에서 멈춘다. 못 찾으면 start다.
    """
    start = start.expanduser().resolve()
    home = Path.home().resolve()
    for candidate in (start, *start.parents):
        if candidate != start and (candidate == home or candidate.parent == candidate):
            break
        if has_course_files(candidate):
            return candidate
    return start


# ------------------------------------------------------------------ 원장 형식

def new_ledger(name: str, mode: str, materials: str, aliases: list[str] | None = None,
               planned: int | None = None) -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "name": name, "aliases": list(aliases or []), "mode": mode, "materials": materials,
            "expect": None, "planned": planned, "ignore": [], "conventions": [], "lectures": []}


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def check_weeks(value: Any, where: str) -> None:
    """강의가 다루는 학기 주차. null이거나 1 이상 정수가 하나 이상 든 목록이다."""
    if value is None:
        return
    if not isinstance(value, list) or not value or not all(is_count(item) and item >= 1 for item in value):
        raise LedgerError(f"{where}의 weeks는 null이거나 1 이상 정수 목록(예: [3, 4])이어야 합니다: {value}")


def weeks_text(weeks: list[int] | None) -> str:
    return "·".join(str(week) for week in sorted(set(weeks))) + "주" if weeks else "—"


def check_expect(value: Any, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"handout", "transcript"} or not all(map(is_count, value.values())):
        raise LedgerError(f'{where}의 expect는 null이거나 {{"handout": 0 이상 정수, "transcript": 0 이상 정수}}여야 합니다: {value}')


def check_ledger(ledger: dict[str, Any] | None) -> None:
    """원장 전체가 형식에 맞는지 본다. 하나라도 틀리면 아무것도 쓰지 않는다."""
    if ledger is None:
        raise LedgerError("과목 원장이 없습니다.")
    if not isinstance(ledger.get("name"), str) or not ledger["name"].strip():
        raise LedgerError("과목 이름(name)이 비었습니다.")
    if ledger.get("mode") not in MODES:
        raise LedgerError(f"mode는 {'|'.join(MODES)} 중 하나여야 합니다: {ledger.get('mode')}")
    if ledger.get("materials") not in MATERIAL_DEFAULTS:
        raise LedgerError(f"materials는 {'|'.join(MATERIAL_DEFAULTS)} 중 하나여야 합니다: {ledger.get('materials')}")
    for key in ("aliases", "ignore", "conventions"):
        if not is_text_list(ledger.get(key, [])):
            raise LedgerError(f"{key}는 글자 목록이어야 합니다.")
    for pattern in ledger.get("ignore", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise LedgerError(f"ignore 정규식이 잘못됐습니다({exc}): {pattern}") from None
    check_expect(ledger.get("expect"), "과목")
    if ledger.get("planned") is not None and not is_count(ledger["planned"]):
        raise LedgerError("planned는 null이거나 0 이상 정수여야 합니다.")
    if not isinstance(ledger.get("lectures"), list):
        raise LedgerError("lectures는 목록이어야 합니다.")
    ids: set[str] = set()
    sources: dict[str, str] = {}
    for lecture in ledger["lectures"]:
        lecture_id = lecture.get("id") if isinstance(lecture, dict) else None
        if not isinstance(lecture_id, str) or not ID_RE.fullmatch(lecture_id):
            raise LedgerError(f"강의 ID는 영문·숫자로 시작하고 영문·숫자·_.-만 쓴 41자 이내여야 합니다: {lecture_id}")
        if lecture_id in ids:
            raise LedgerError(f"강의 ID가 겹칩니다: {lecture_id}")
        ids.add(lecture_id)
        if lecture.get("title") is not None and not isinstance(lecture["title"], str):
            raise LedgerError(f"강의 {lecture_id}의 title은 글자여야 합니다.")
        check_weeks(lecture.get("weeks"), f"강의 {lecture_id}")
        check_expect(lecture.get("expect"), f"강의 {lecture_id}")
        if not isinstance(lecture.get("materials", []), list):
            raise LedgerError(f"강의 {lecture_id}의 materials는 목록이어야 합니다.")
        seen: set[str] = set()
        for material in lecture.get("materials", []):
            if not isinstance(material, dict) or material.get("kind") not in KINDS:
                raise LedgerError(f"강의 {lecture_id}의 자료 종류는 {'|'.join(KINDS)} 중 하나여야 합니다: {material}")
            if not isinstance(material.get("path"), str) or not safe_relative(material["path"]):
                raise LedgerError(f"강의 {lecture_id}의 자료 경로가 잘못됐습니다: {material.get('path')}")
            if path_key(material["path"]) in seen:
                raise LedgerError(f"강의 {lecture_id}에 같은 파일이 두 번 있습니다: {material['path']}")
            seen.add(path_key(material["path"]))
        note = lecture.get("note")
        if note is None:
            continue
        if not isinstance(note, dict) or not isinstance(note.get("source"), str) or note.get("progress") not in PROGRESS:
            raise LedgerError(f"강의 {lecture_id}의 note에는 source와 progress({'|'.join(PROGRESS)})가 있어야 합니다.")
        if not is_text_list(note.get("outputs", [])) or note.get("mode") not in (*MODES, None):
            raise LedgerError(f"강의 {lecture_id}의 note outputs·mode가 잘못됐습니다.")
        key = path_key(note["source"])
        if key in sources:
            raise LedgerError(f"같은 노트 원고가 강의 {sources[key]}와 {lecture_id}에 함께 등록돼 있습니다: {note['source']}")
        sources[key] = lecture_id


def require(ledger: dict[str, Any] | None) -> dict[str, Any]:
    if ledger is None:
        raise LedgerError("이 과목에는 아직 원장(.gongbu/course.json)이 없습니다. 먼저 `gongbu course init --name <과목명> "
                          "--mode faithful|deep --materials handout|handout+recording`을 실행하십시오.")
    return ledger


def find_lecture(ledger: dict[str, Any], lecture_id: str) -> dict[str, Any]:
    for lecture in ledger["lectures"]:
        if lecture["id"] == lecture_id:
            return lecture
    raise LedgerError(f"원장에 강의 {lecture_id}가 없습니다. 먼저 `gongbu course import <plan.json>`으로 강의를 등록하십시오.")


def check_stable(path: Path, rel: str, force: bool) -> None:
    if force:
        return
    if unstable_name(path.name):
        raise LedgerError(f"아직 받는 중인 파일 같습니다: {rel}. 다 받은 뒤 다시 실행하십시오.")
    if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
        raise LedgerError(f"최근 {SETTLE_SECONDS // 60}분 안에 바뀐 파일이라 아직 녹음·다운로드 중일 수 있습니다: {rel}. "
                          "잠시 뒤 다시 실행하거나, 다 된 파일이면 --force를 붙이십시오.")


def material_entry(course: Path, kind: str, value: str, part: str | None = None, source: str | None = None,
                   force: bool = False) -> dict[str, Any]:
    """자료 하나의 원장 항목. 파일이 있어야 하고 지금 지문(sha256·크기·수정 시각)을 적는다."""
    if kind not in KINDS:
        raise LedgerError(f"자료 종류는 {'|'.join(KINDS)} 중 하나여야 합니다: {kind}")
    rel = course_path(course, value)
    path = locate(course, rel)
    if path is None or not path.is_file():
        raise LedgerError(f"파일이 없습니다: {rel}")
    check_stable(path, rel, force)
    entry: dict[str, Any] = {"kind": kind, "path": rel}
    if part:
        entry["part"] = str(part)
    if source:
        entry["from"] = course_path(course, source)
    entry.update(fingerprint(path))
    return entry


def notion_record(state: dict[str, Any] | None, source: str) -> dict[str, Any] | None:
    notes = (state or {}).get("notes") or {}
    if source in notes:
        return notes[source]
    key = path_key(source)
    return next((record for name, record in notes.items() if path_key(name) == key), None)


def read_notion_state(course: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """notion.json(없으면 None). 읽지 못하면 연결 안 된 것으로 보고 오류 문장을 함께 돌려준다."""
    try:
        state = pn.load_state(course, required=False)
    except (OSError, ValueError) as exc:
        return None, [f"노션 기록(notion.json)을 읽지 못했습니다: {first_line(exc)}"]
    return (state or None), []


def register_note(course: Path, ledger: dict[str, Any], lecture: dict[str, Any], source_value: str, progress: str, *,
                  outputs: list[str] | None = None, covers: str | None = None, mode: str | None = None) -> list[str]:
    """강의 하나에 노트를 등록한다(그때의 원고 지문과 재료 지문을 적는다). 돌려주는 값은 알릴 말이다."""
    if progress not in PROGRESS:
        raise LedgerError(f"progress는 {'|'.join(PROGRESS)} 중 하나여야 합니다: {progress}")
    if mode is not None and mode not in MODES:
        raise LedgerError(f"mode는 {'|'.join(MODES)} 중 하나여야 합니다: {mode}")
    if outputs is not None and not is_text_list(outputs):
        raise LedgerError(f"노트 outputs는 경로 글자 목록이어야 합니다: {outputs}")
    if covers is not None and not isinstance(covers, str):
        raise LedgerError(f"노트 covers는 글자여야 합니다: {covers}")
    if not isinstance(source_value, str):
        raise LedgerError(f"노트 source는 경로 글자여야 합니다: {source_value}")
    source = course_path(course, source_value)
    path = locate(course, source)
    if path is None or not path.is_file():
        raise LedgerError(f"노트 원고가 없습니다: {source}")
    for other in ledger["lectures"]:
        if other is not lecture and other.get("note") and path_key(other["note"]["source"]) == path_key(source):
            raise LedgerError(f"이 원고는 이미 강의 {other['id']}에 등록돼 있습니다: {source}")
    notices: list[str] = []
    state, errors = read_notion_state(course)
    notices += [f"[경고] {error}" for error in errors]
    try:
        prepared = pn.prepare_note(course, path, state or {"course": ledger["name"], "slides": False})
        digest = prepared.content_hash
        if digest is None:
            notices.append(f"[경고] 노션 변환 오류가 있어 이 원고는 노션에 올릴 수 없습니다: {first_line(prepared.note.errors[0])}")
    except Exception as exc:  # noqa: BLE001 — 닫히지 않은 수식처럼 변환이 아예 안 되는 원고
        digest = None
        notices.append(f"[경고] 이 원고는 노션 형식으로 바꿀 수 없습니다: {first_line(exc) or type(exc).__name__}")
    previous = lecture.get("note") or {}
    same_source = bool(previous) and path_key(previous.get("source", "")) == path_key(source)
    if previous.get("source") and not same_source and notion_record(state, previous["source"]):
        notices.append(f'같은 노션 페이지를 이어 쓰려면 gongbu notion rekey "{previous["source"]}" "{source}"')
    inputs: dict[str, str] = {}
    for material in lecture.get("materials", []):
        if material["kind"] in ("handout", "transcript"):
            found = locate(course, material["path"])
            inputs[material["path"]] = current_sha(found, material) if found and found.is_file() else material.get("sha256")
    lecture["note"] = {
        "source": source,
        "outputs": [course_path(course, item) for item in outputs] if outputs is not None else previous.get("outputs", []),
        "mode": mode or (previous.get("mode") if same_source else None) or ("deep" if path.suffix.lower() == ".tex" else "faithful"),
        "progress": progress,
        "covers": covers if covers is not None else previous.get("covers"),
        "inputs": inputs,
        "digest": digest,
        "source_sha256": source_digest(dependencies(path)),
        "registered_at": now_iso(),
    }
    return notices


# ------------------------------------------------------------------ 훑기(읽기 전용)

@dataclass
class Found:
    rel: str
    path: Path
    size: int
    unstable: bool


@dataclass
class Scan:
    files: list[Found] = field(default_factory=list)  # 건너뛸 파일을 뺀 모든 파일
    notes: list[Found] = field(default_factory=list)  # 노트 원고로 보이는 파일
    manifests: set[tuple[str, int]] = field(default_factory=set)  # 전사된 녹음(이름 열쇠, 바이트)
    manifest_hashes: set[str] = field(default_factory=set)  # 전사된 녹음의 sha256(이름이 바뀌어도 맞춰 본다)
    errors: list[str] = field(default_factory=list)


def compile_ignore(patterns: list[str], errors: list[str]) -> list[re.Pattern[str]]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            errors.append(f"ignore 정규식이 잘못돼 쓰지 않았습니다({exc}): {pattern}")
    return compiled


def ignored(patterns: list[re.Pattern[str]], rel: str) -> bool:
    return any(pattern.search(rel) for pattern in patterns)


def skipped(name: str, beside_manifest: bool) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES) or (beside_manifest and name.lower().endswith(".srt"))


def read_manifest(path: Path, course: Path, scan: Scan) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scan.manifests.add((path_key(Path(str(data["source_audio"])).name), int(data["source_audio_bytes"])))
        if isinstance(data.get("source_audio_sha256"), str) and data["source_audio_sha256"]:
            scan.manifest_hashes.add(data["source_audio_sha256"].lower())
    except (OSError, ValueError, KeyError, TypeError):
        scan.errors.append(f"전사 기록을 읽지 못했습니다: {rel_of(course, path)}")


def scan_course(course: Path, patterns: list[re.Pattern[str]], settle: int, now: float) -> Scan:
    """과목 폴더를 훑어 파일, 노트 원고 후보, 전사 기록을 모은다. `.gongbu` 아래는 전사 기록만 읽는다."""
    scan = Scan()
    evidence_only: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(course):
        base = Path(dirpath)
        rel_dir = "" if base == course else nfc(base.relative_to(course).as_posix())
        only = dirpath in evidence_only
        kept = []
        for name in dirnames:
            child = base / name
            if (name.startswith(".") and name != pn.STATE_DIR) or name.lower() in NEVER_DIRS or (child / "pyvenv.cfg").is_file():
                continue
            if only or name == pn.STATE_DIR:
                evidence_only.add(os.path.join(dirpath, name))
            elif name.lower() in PRUNE_DIRS or ignored(patterns, f"{rel_dir}/{nfc(name)}/".lstrip("/")):
                continue
            kept.append(name)
        dirnames[:] = kept
        beside_manifest = any(name.endswith(MANIFEST_SUFFIX) for name in filenames)
        for name in filenames:
            path = base / name
            if name.endswith(MANIFEST_SUFFIX):
                read_manifest(path, course, scan)
                continue
            rel = f"{rel_dir}/{nfc(name)}".lstrip("/")
            if only or skipped(name, beside_manifest) or ignored(patterns, rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found = Found(rel, path, stat.st_size, unstable_name(name) or now - stat.st_mtime < settle)
            scan.files.append(found)
            if is_note_name(name):
                scan.notes.append(found)
    source_dir = course / "output" / "source"
    if source_dir.is_dir():
        for entry in sorted(source_dir.iterdir()):
            rel = f"output/source/{nfc(entry.name)}"
            if not entry.is_file() or entry.suffix.lower() != ".tex" or ignored(patterns, rel):
                continue
            try:
                text, stat = entry.read_text(encoding="utf-8-sig", errors="replace"), entry.stat()
            except OSError:
                continue
            if NOTE_TEX_RE.search(text):
                scan.notes.append(Found(rel, entry, stat.st_size, unstable_name(entry.name) or now - stat.st_mtime < settle))
    # 자료 충실형 노트는 기본으로 output/ 바로 아래에 생긴다. 자료로는 보지 않고 노트 원고 후보로만 센다.
    output_dir = course / "output"
    if output_dir.is_dir():
        for entry in sorted(output_dir.iterdir()):
            rel = f"output/{nfc(entry.name)}"
            if not entry.is_file() or not is_note_name(entry.name) or ignored(patterns, rel):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            scan.notes.append(Found(rel, entry, stat.st_size, unstable_name(entry.name) or now - stat.st_mtime < settle))
    return scan


def note_hints(course: Path) -> dict[str, Any]:
    """원장이 없는 과목에서 노트·자료로 보이는 파일 수(깊이 3, 파일 5000개까지)."""
    suffixes: dict[str, int] = {}
    kinds: dict[str, int] = {}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(course):
        base = Path(dirpath)
        depth = len(base.relative_to(course).parts)
        dirnames[:] = [] if depth >= HINT_DEPTH else [
            name for name in dirnames if not name.startswith(".") and name.lower() not in NEVER_DIRS]
        for name in filenames:
            seen += 1
            if seen > HINT_FILES:
                return {"note_suffixes": suffixes, "materials": kinds}
            suffix = Path(name).suffix.lower()
            if NOTE_WORD in nfc(name) or (suffix == ".tex" and base == course / "output" / "source"):
                suffixes[suffix] = suffixes.get(suffix, 0) + 1
            elif kind := material_kind(name):
                kinds[kind] = kinds.get(kind, 0) + 1
    return {"note_suffixes": suffixes, "materials": kinds}


# ------------------------------------------------------------------ 강의별 평가

@dataclass
class Survey:
    """과목 하나의 현황을 계산하는 동안 함께 쓰는 값."""
    root: Path
    ledger: dict[str, Any]
    notion: dict[str, Any] | None
    settle: int
    now: float
    scan: Scan = field(default_factory=Scan)
    moves: dict[str, tuple[str, Found]] = field(default_factory=dict)  # 없어진 등록 경로 열쇠 → (등록 경로, 옮겨 간 파일)
    unstable: dict[str, str] = field(default_factory=dict)  # 받는 중인 등록 파일(열쇠 → 경로)
    missing: list[dict[str, Any]] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)

    def find(self, rel: str) -> Path | None:
        path = locate(self.root, rel)
        if path is None and path_key(rel) in self.moves:
            return self.moves[path_key(rel)][1].path
        return path

    def sha(self, path: Path, known: dict[str, Any] | None = None) -> str:
        if str(path) not in self.hashes:
            self.hashes[str(path)] = current_sha(path, known)
        return self.hashes[str(path)]

    def is_unstable(self, path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        if unstable_name(path.name) or self.now - stat.st_mtime < self.settle:
            rel = rel_of(self.root, path)
            self.unstable.setdefault(path_key(rel), rel)
            return True
        return False


def registered_keys(survey: Survey) -> set[str]:
    """원장에 적힌 경로: 자료, 노트 원고와 그 `\\input` 조각, 노트 산출물."""
    keys: set[str] = set()
    for lecture in survey.ledger["lectures"]:
        keys.update(path_key(material["path"]) for material in lecture.get("materials", []))
        note = lecture.get("note")
        if not note:
            continue
        keys.add(path_key(note["source"]))
        keys.update(path_key(output) for output in note.get("outputs", []))
        source = locate(survey.root, note["source"])
        if source is not None and source.is_file():
            keys.update(path_key(rel_of(survey.root, part)) for part in dependencies(source)[1:])
    return keys


def find_moves(survey: Survey, registered: set[str]) -> None:
    """없어진 등록 자료와 크기·sha256이 같은 미등록 파일이 꼭 하나 있으면 옮겨진 것으로 본다."""
    candidates: dict[int, list[Found]] = {}
    for found in survey.scan.files:
        if not found.unstable and path_key(found.rel) not in registered:
            candidates.setdefault(found.size, []).append(found)
    for lecture in survey.ledger["lectures"]:
        for material in lecture.get("materials", []):
            key = path_key(material["path"])
            if key in survey.moves or locate(survey.root, material["path"]) is not None:
                continue
            same = [found for found in candidates.get(material.get("size", -1), [])
                    if survey.sha(found.path) == material.get("sha256")]
            if len(same) == 1:
                survey.moves[key] = (material["path"], same[0])


def convert_note(survey: Survey, source: Path, parts: list[Path]) -> tuple[str | None, str | None, list[str]]:
    """(노션 내용 해시, 변환 오류 첫 줄, 불확실성 표시). push와 같은 prepare_note로 계산한다."""
    state = survey.notion or {"course": survey.ledger["name"], "slides": False}
    try:
        prepared = pn.prepare_note(survey.root, source, state)
    except Exception as exc:  # noqa: BLE001 — 변환이 아예 안 되는 원고도 현황은 계산한다
        text = "\n".join(part.read_text(encoding="utf-8-sig", errors="replace") for part in parts if part.is_file())
        return None, first_line(exc) or type(exc).__name__, pn.UNCERTAIN_RE.findall(text)
    markers = pn.uncertain_markers(prepared.note.blocks)
    if prepared.content_hash is None:
        return None, first_line(prepared.note.errors[0]) if prepared.note.errors else "변환 오류", markers
    return prepared.content_hash, None, markers


def notion_status(survey: Survey, note: dict[str, Any], readable: bool, content_hash: str | None,
                  error: str | None) -> tuple[str | None, str | None]:
    state = survey.notion
    if state is None:
        return "not_connected", None
    if "data_source_id" in state:
        return "legacy", None
    record = notion_record(state, note["source"])
    url = (record or {}).get("url") or None
    if readable and error is not None:
        return "convert_error", url
    if record is None:
        return "not_uploaded", None
    if record.get("notion_edited_at"):
        return "edited_in_notion", url
    if record.get("content_sha256") is None:
        return "retry_pending", url
    if not readable:
        return None, url
    return ("up_to_date" if content_hash == record.get("content_sha256") else "local_changed"), url


def lecture_status(survey: Survey, lecture: dict[str, Any], notes_waiting: bool) -> dict[str, Any]:
    ledger = survey.ledger
    need = lecture.get("expect") or ledger.get("expect") or MATERIAL_DEFAULTS[ledger["materials"]]
    materials = lecture.get("materials", [])
    present: dict[str, list[tuple[dict[str, Any], Path]]] = {kind: [] for kind in KINDS}
    missing: list[str] = []
    unstable = False
    for material in materials:
        path = survey.find(material["path"])
        if path is None:
            missing.append(material["path"])
            survey.missing.append({"lecture": lecture["id"], "kind": material["kind"], "path": material["path"]})
            continue
        present[material["kind"]].append((material, path))
        unstable = survey.is_unstable(path) or unstable
    # 녹음은 `from`으로 이어진 전사 자료가 있거나 전사 기록(이름·크기 또는 sha256)이 있으면 전사된 것이다.
    # 녹음과 그 전사는 한 번만 센다. `from`이 없는 전사 자료는 `from` 없이 전사 기록만 있는 녹음과 짝지어 센다.
    all_from = {path_key(m["from"]) for m in materials if m["kind"] == "transcript" and m.get("from")}
    present_from = {path_key(m["from"]) for m, _ in present["transcript"] if m.get("from")}
    with_from = sum(bool(m.get("from")) for m, _ in present["transcript"])
    linked_gone = unlinked = 0  # 전사 자료가 없어진 `from` 녹음 · 전사 기록만 있는 녹음
    untranscribed: list[str] = []
    recording_times: list[float] = []
    for material in (m for m in materials if m["kind"] == "recording"):
        path, key = survey.find(material["path"]), path_key(material["path"])
        size = size_of(path) if path else material.get("size")
        names = {path_key(Path(material["path"]).name)} | ({path_key(path.name)} if path else set())
        if key in all_from:
            linked_gone += key not in present_from
        elif any((name, size) in survey.scan.manifests for name in names) or \
                str(material.get("sha256") or "").lower() in survey.scan.manifest_hashes:
            unlinked += 1
        elif path is not None:
            untranscribed.append(material["path"])
            if (moment := mtime_of(path)) is not None:
                recording_times.append(moment)
    transcripts = with_from + linked_gone + max(len(present["transcript"]) - with_from, unlinked)
    handouts = len(present["handout"])
    if handouts >= need["handout"] and transcripts >= need["transcript"]:
        materials_state = "complete"
    elif handouts >= need["handout"] and transcripts + len(untranscribed) >= need["transcript"]:
        materials_state = "needs_transcription"
    else:
        materials_state = "missing"

    note = lecture.get("note")
    note_json, notion_state, notion_url, markers = None, None, None, []
    if not note:
        note_state = "ready" if materials_state == "complete" else "none"
    else:
        source = locate(survey.root, note["source"])
        readable = source is not None and source.is_file()
        stale: list[str] = []
        content_hash = error = None
        if not readable:
            note_state = "source_missing"
            survey.missing.append({"lecture": lecture["id"], "kind": "note", "path": note["source"]})
        else:
            parts = dependencies(source)
            for part in parts:
                unstable = survey.is_unstable(part) or unstable
            known = set((note.get("inputs") or {}).values())
            stale = [m["path"] for kind in ("handout", "transcript") for m, path in present[kind]
                     if survey.sha(path, m) not in known]
            content_hash, error, markers = convert_note(survey, source, parts)
            edited = source_digest(parts) != note.get("source_sha256")
            note_state = "edited" if edited else "needs_update" if stale else note.get("progress", "in_progress")
        notion_state, notion_url = notion_status(survey, note, readable, content_hash, error)
        note_json = {"source": note["source"], "progress": note.get("progress"), "covers": note.get("covers"),
                     "outputs_missing": [item for item in note.get("outputs", []) if locate(survey.root, item) is None],
                     "stale_inputs": stale, "error": error}

    # 전사가 모자랄 때나, 이 강의의 마지막 전사 자료보다 나중에 들어온 녹음만 전사한다. 전사 자료가 이미 충분한 예전 녹음
    # (처음 정리 때 `from` 없이 등록한 녹음 등)을 다시 전사하지 않게 한다. 그런 녹음은 materials.untranscribed에만 보인다.
    # 노트 등록 시각과 비교하지 않는다: 노트를 다시 등록해도 그 사이에 들어온 녹음을 놓치지 않게 한다.
    transcript_times = [moment for _, path in present["transcript"] for moment in [mtime_of(path)] if moment is not None]
    newest_transcript = max(transcript_times, default=None)
    transcribe = bool(untranscribed) and (transcripts < need["transcript"] or newest_transcript is None or
                                          any(moment > newest_transcript for moment in recording_times))
    action, blocked = None, None
    if unstable:
        blocked = "unstable"
    elif transcribe:
        action = "transcribe"
    elif not note:
        if note_state == "ready":
            action, blocked = (None, "unregistered_notes") if notes_waiting else ("write_note", None)
    elif note_state == "needs_update":
        action = "update_note"
    elif note_state == "edited":
        action = "register_note"
    elif notion_state == "convert_error":
        action = "fix_note"
    elif note_state in PROGRESS and notion_state in ("not_uploaded", "local_changed", "retry_pending"):
        action = "push"
    return {
        "id": lecture["id"], "title": lecture.get("title") or lecture["id"], "weeks": lecture.get("weeks"),
        "materials": {"handout": {"have": handouts, "need": need["handout"]},
                      "transcript": {"have": transcripts, "need": need["transcript"]},
                      "recording": {"have": len(present["recording"])}, "untranscribed": untranscribed, "missing": missing},
        "materials_state": materials_state, "note": note_json, "note_state": note_state,
        "notion_state": notion_state, "notion_url": notion_url,
        "unresolved": {"total": len(markers), "relisten": sum(marker[1:].startswith(RELISTEN_LABELS) for marker in markers)},
        "unstable": unstable, "next": action, "blocked": blocked,
    }


# ------------------------------------------------------------------ 과목·학기 현황

def short_label(lecture: dict[str, Any]) -> str:
    """사람에게 보일 짧은 강의 이름: 제목 첫 낱말에 숫자가 있으면 그것(예: 5장), 아니면 강의 ID."""
    words = (lecture.get("title") or "").split()
    return words[0] if words and any(char.isdigit() for char in words[0]) else lecture["id"]


def notion_summary(state: dict[str, Any] | None) -> dict[str, Any]:
    return {"connected": bool(state and state.get("course_page_id")),
            "course_page_url": (state or {}).get("course_page_url") or None, "legacy": bool(state and "data_source_id" in state)}


def make_todo(name: str, course_dir: str, lecture: dict[str, Any] | None, kind: str, body: str,
              url: str | None) -> dict[str, Any]:
    prefix = f"{name} {short_label(lecture)}" if lecture else name
    return {"course": name, "dir": course_dir, "lecture": lecture["id"] if lecture else None, "kind": kind,
            "text": f"{prefix}: {body}", "url": url}


def overdue(lecture: dict[str, Any], week: int | None) -> bool:
    """강의 주차(weeks)가 이번 학기 주차보다 앞이면 자료가 이미 나왔어야 한다. 주차를 모르면 아니다."""
    weeks = lecture.get("weeks")
    return bool(weeks) and week is not None and min(weeks) < week


def course_status(root: Path, *, settle: int, now: float,
                  week: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """과목 하나의 (현황, 기계가 할 일, 사람이 할 일). week는 과목이 든 학기의 지금 주차(모르면 None)."""
    course_dir = root.as_posix()
    state, errors = read_notion_state(root) if root.is_dir() else (None, [])
    notion_json = notion_summary(state)
    fallback = (state or {}).get("course") or root.name
    if not root.is_dir():
        return ({"dir": course_dir, "name": fallback, "initialized": False, "notion": notion_json, "hints": {},
                 "errors": [f"과목 폴더가 없습니다: {course_dir}"]}, [], [])
    try:
        ledger = load_ledger(root)
        if ledger is not None:
            check_ledger(ledger)
    except LedgerError as exc:
        return ({"dir": course_dir, "name": fallback, "initialized": False, "notion": notion_json, "hints": {},
                 "errors": [*errors, str(exc)]}, [], [])
    if ledger is None:
        entry = {"dir": course_dir, "name": fallback, "initialized": False, "notion": notion_json,
                 "hints": note_hints(root), "errors": errors}
        return entry, [], [make_todo(fallback, course_dir, None, "setup_course", "처음 설정이 필요해요(기본 모드·재료 조건)",
                                     notion_json["course_page_url"])]

    name = ledger["name"]
    survey = Survey(root, ledger, state, settle, now)
    patterns = compile_ignore(ledger.get("ignore", []), errors)
    survey.scan = scan_course(root, patterns, settle, now)
    errors += survey.scan.errors
    registered = registered_keys(survey)
    find_moves(survey, registered)
    targets = {path_key(found.rel) for _, found in survey.moves.values()}
    unregistered = [{"path": found.rel, "kind": kind} for found in survey.scan.files
                    if (kind := material_kind(found.path.name)) and not found.unstable
                    and path_key(found.rel) not in registered and path_key(found.rel) not in targets]
    unregistered_notes = [{"path": found.rel} for found in survey.scan.notes
                          if not found.unstable and path_key(found.rel) not in registered]
    lectures = [lecture_status(survey, lecture, bool(unregistered_notes)) for lecture in ledger["lectures"]]
    unstable_files = {path_key(found.rel): found.rel for found in survey.scan.files if found.unstable and (
        material_kind(found.path.name) or is_note_name(found.path.name) or unstable_name(found.path.name))}
    unstable_files.update({path_key(found.rel): found.rel for found in survey.scan.notes if found.unstable})
    unstable_files.update(survey.unstable)
    entry = {
        "dir": course_dir, "name": name, "initialized": True, "mode": ledger["mode"], "materials": ledger["materials"],
        "progress": {"notes": sum(bool(lecture.get("note")) for lecture in ledger["lectures"]),
                     "done": sum((lecture.get("note") or {}).get("progress") == "done" for lecture in ledger["lectures"]),
                     "total": max(len(ledger["lectures"]), ledger.get("planned") or 0)},
        "notion": notion_json, "lectures": lectures,
        "unregistered": sorted(unregistered, key=lambda item: item["path"]),
        "unregistered_notes": sorted(unregistered_notes, key=lambda item: item["path"]),
        "moved": [{"from": source, "to": found.rel} for source, found in survey.moves.values()],
        "missing_files": survey.missing,
        "unstable_files": [{"path": rel, "kind": material_kind(Path(rel).name)} for rel in sorted(unstable_files.values())],
        "errors": errors,
    }

    ready: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = []
    unregistered_kinds = {item["kind"] for item in unregistered}
    for lecture, result in zip(ledger["lectures"], lectures):
        note = lecture.get("note") or {}
        if result["next"]:
            ready.append({"course": name, "dir": course_dir, "lecture": lecture["id"], "action": result["next"],
                          "mode": note.get("mode") or ledger["mode"], "note_source": note.get("source")})
        url = result["notion_url"] or notion_json["course_page_url"]
        counts = result["materials"]
        # 받기 할 일은 주차가 지난 강의만 낸다. 이번 주·다음 주 강의나 주차를 모르는 강의는 자료가 없어도 조용히 둔다.
        if result["materials_state"] == "missing" and overdue(lecture, week):
            if counts["handout"]["have"] < counts["handout"]["need"] and "handout" not in unregistered_kinds:
                todo.append(make_todo(name, course_dir, lecture, "fetch_materials",
                                      f"교안 받기 ({counts['handout']['have']}/{counts['handout']['need']})", url))
            have = counts["transcript"]["have"] + len(counts["untranscribed"])
            if have < counts["transcript"]["need"] and not unregistered_kinds & {"recording", "transcript"}:
                word = "녹음" if ledger["materials"] == "handout+recording" else "전사"
                todo.append(make_todo(name, course_dir, lecture, "fetch_materials",
                                      f"{word} 받기 ({have}/{counts['transcript']['need']})", url))
        if result["unresolved"]["relisten"]:
            todo.append(make_todo(name, course_dir, lecture, "relisten",
                                  f"알아듣기 어려운 곳 {result['unresolved']['relisten']}곳 다시 듣기", url))
        if result["notion_state"] == "edited_in_notion":
            todo.append(make_todo(name, course_dir, lecture, "merge_notion_edit",
                                  "노션에서 고친 내용을 원고에 옮길지 정해 주세요", url))
    by_id = {lecture["id"]: lecture for lecture in ledger["lectures"]}
    for item in survey.missing:
        lecture = by_id[item["lecture"]]
        todo.append(make_todo(name, course_dir, lecture, "missing_files",
                              f"등록된 {KIND_LABELS[item['kind']]} {Path(item['path']).name}가 없어졌어요",
                              notion_json["course_page_url"]))
    for action, items in (("register", [item["path"] for item in entry["unregistered"]]),
                          ("relink", [item["from"] for item in entry["moved"]]),
                          ("register_note_files", [item["path"] for item in entry["unregistered_notes"]])):
        if items:
            ready.append({"course": name, "dir": course_dir, "lecture": None, "action": action, "paths": items})
    return entry, ready, todo


def collect_status(*, semester: str | None = None, course_dirs: list[Path] | None = None,
                   settle_seconds: int = SETTLE_SECONDS, today: dt.date | None = None,
                   now: float | None = None) -> dict[str, Any]:
    """학기(또는 주어진 과목 폴더들)의 현황 문서(gongbu.status/1). 아무것도 쓰지 않고 네트워크·토큰을 쓰지 않는다."""
    registry = load_registry()
    if course_dirs is None:
        sem_id = semester or registry.get("current")
        if not sem_id:
            raise LedgerError("현재 학기가 없습니다. `gongbu semester init <ID> --title … --start … --weeks …`로 만드십시오.")
        if sem_id not in registry["semesters"]:
            raise LedgerError(f"학기 목록에 {sem_id} 학기가 없습니다.")
        dirs = [Path(course).expanduser().resolve() for course in registry["semesters"][sem_id].get("courses", [])]
    else:
        dirs = [Path(course).expanduser().resolve() for course in course_dirs]
        sem_id = semester or (semester_of(dirs[0], registry) if len(dirs) == 1 else None)
    today = today or dt.date.today()
    now = time.time() if now is None else now
    info = registry["semesters"].get(sem_id) if sem_id else None
    document: dict[str, Any] = {
        "schema": STATUS_SCHEMA, "generated_at": now_iso(now),
        "semester": None if info is None else {"id": sem_id, "title": info.get("title") or sem_id,
                                               "current_week": current_week(info, today), "weeks": info.get("weeks")},
        "courses": [], "ready": [], "todo": []}
    for course_dir in dirs:
        home = registry["semesters"].get(sem_id or semester_of(course_dir, registry) or "")
        week = current_week(home, today) if isinstance(home, dict) else None
        entry, ready, todo = course_status(course_dir, settle=settle_seconds, now=now, week=week)
        document["courses"].append(entry)
        document["ready"] += ready
        document["todo"] += todo
    return document


# ------------------------------------------------------------------ 사람이 읽는 현황

NOTE_TEXT = {"ready": "만들 차례", "needs_update": "갱신 차례(새 자료)", "edited": "원고 수정 중", "none": "재료 대기",
             "source_missing": "원고 없음", "done": "완료", "in_progress": "진행 중"}
NOTION_TEXT = {"up_to_date": "최신", "local_changed": "올릴 것 있음", "not_uploaded": "안 올림", "edited_in_notion": "노션에서 고침",
               "retry_pending": "다시 올리기 대기", "convert_error": "변환 오류", "legacy": "예전 방식"}
ACTION_TEXT = {"transcribe": "녹음 전사", "write_note": "노트 만들기", "update_note": "노트 갱신(새 자료)",
               "register_note": "고친 원고 확인·등록", "fix_note": "노션 변환 오류 고치기", "push": "노션 올리기"}
COURSE_ACTION_TEXT = {"register": "새 파일 {n}개 등록", "relink": "옮겨진 파일 {n}개 다시 연결",
                      "register_note_files": "등록 안 된 노트 원고 {n}개 등록"}


def text_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def table_lines(rows: list[list[str]], indent: str = "  ") -> list[str]:
    widths = [max(text_width(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [indent + "   ".join(cell + " " * (widths[index] - text_width(cell)) for index, cell in enumerate(row)).rstrip()
            for row in rows]


def recording_cell(materials: dict[str, Any]) -> str:
    have, need, waiting = materials["transcript"]["have"], materials["transcript"]["need"], len(materials["untranscribed"])
    if have == 0:
        return "녹음만(전사 대기)" if waiting else "필요 없음" if need == 0 else "없음"
    return f"전사 {have}/{need}" if have < need else f"전사 {have}"


def note_cell(lecture: dict[str, Any]) -> str:
    text = NOTE_TEXT.get(lecture["note_state"], lecture["note_state"])
    covers = (lecture["note"] or {}).get("covers")
    return f"{text} · {covers}" if lecture["note_state"] in PROGRESS and covers else text


def course_text(course: dict[str, Any], semester: dict[str, Any] | None, ready: list[dict[str, Any]],
                todo: list[dict[str, Any]]) -> list[str]:
    if not course["initialized"]:
        lines = [f"{course['name']} — 처음 설정이 필요해요(기본 모드·재료 조건)" if not course["errors"]
                 else f"{course['name']} — 원장을 읽지 못했습니다"]
        hints = course.get("hints") or {}
        if hints.get("note_suffixes"):
            lines.append("  노트로 보이는 파일: " + " · ".join(f"{suffix or '(확장자 없음)'} {count}"
                                                       for suffix, count in sorted(hints["note_suffixes"].items())))
        return lines + [f"  [주의] {error}" for error in course["errors"]]
    when = f"{semester['title']} {semester['current_week']}주차 · " if semester and semester.get("current_week") else ""
    progress = course["progress"]
    lines = [f"{course['name']} · {pn.MODES[course['mode']][0]} · {MATERIAL_LABELS[course['materials']]} — "
             f"{when}노트 {progress['notes']}/{progress['total']}"]
    if course["lectures"]:
        rows = [["강의", "교안", "녹음·전사", "노트", "노션"]]
        for lecture in course["lectures"]:
            materials = lecture["materials"]
            rows.append([lecture["title"], f"{materials['handout']['have']}/{materials['handout']['need']}",
                         recording_cell(materials), note_cell(lecture), NOTION_TEXT.get(lecture["notion_state"], "—")])
        lines += table_lines(rows)
    else:
        lines.append("  등록된 강의가 없습니다")
    footer = [f"확인 필요 {sum(lecture['unresolved']['total'] for lecture in course['lectures'])}곳",
              f"새 파일 {len(course['unregistered'])}", f"등록 안 된 노트 원고 {len(course['unregistered_notes'])}"]
    for key, label in (("moved", "옮겨진 파일"), ("missing_files", "없어진 파일"), ("unstable_files", "받는 중")):
        if course[key]:
            footer.append(f"{label} {len(course[key])}")
    lines.append("  " + " · ".join(footer))
    lines += [f"  [주의] {error}" for error in course["errors"]]
    labels = {lecture["id"]: short_label(lecture) for lecture in course["lectures"]}
    actions = [COURSE_ACTION_TEXT[item["action"]].format(n=len(item["paths"])) if item["lecture"] is None
               else f"{labels[item['lecture']]} {ACTION_TEXT[item['action']]}" for item in ready]
    chores = [item["text"].removeprefix(f"{course['name']} ").replace(": ", " ", 1) for item in todo]
    lines.append("다음 자동 작업: " + (", ".join(actions) or "없음"))
    lines.append("내가 할 일: " + (", ".join(chores) or "없음"))
    return lines


def status_text(document: dict[str, Any]) -> str:
    blocks = []
    for course in document["courses"]:
        mine = [item for item in document["ready"] if item["dir"] == course["dir"]]
        chores = [item for item in document["todo"] if item["dir"] == course["dir"]]
        blocks.append("\n".join(course_text(course, document["semester"], mine, chores)))
    return "\n\n".join(blocks) if blocks else "이 학기에 넣은 과목이 없습니다. `gongbu semester add-course`로 넣으십시오."


# ------------------------------------------------------------------ 명령

def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def announce(notices: list[str]) -> None:
    for notice in notices:
        print(notice, file=sys.stderr)


def course_dirs_of(args: argparse.Namespace, start_dir: Path | None = None) -> tuple[Path, Path]:
    """(명령을 시작한 폴더, 과목 폴더). 과목 폴더가 위쪽에서 찾아졌으면 알린다."""
    start = (start_dir or args.course_dir or Path.cwd()).expanduser().resolve()
    root = find_course_root(start)
    if root != start:
        print(f"과목 폴더: {root}", file=sys.stderr)
    return start, root


def command_status(args: argparse.Namespace) -> int:
    single = not (args.all or args.semester)
    if single:
        _, root = course_dirs_of(args)
        require_folder(root)  # 경로 오타는 처음 설정(3)이 아니라 오류(2)다
        document = collect_status(course_dirs=[root], settle_seconds=args.settle_seconds, today=args.today)
    else:
        document = collect_status(semester=args.semester, settle_seconds=args.settle_seconds, today=args.today)
    if args.json:
        emit(document)
    else:
        print(status_text(document))
    # 원장이 있는데 읽지 못한 과목(깨진 JSON, 새 형식)은 오류다. 원장이 아예 없는 과목은 처음 설정할 차례일 뿐이다.
    broken = [course for course in document["courses"]
              if not course["initialized"] and Path(course["dir"]).is_dir() and ledger_path(Path(course["dir"])).is_file()]
    for course in broken:
        print(f"[오류] {course['name']}: {'; '.join(course['errors'])}", file=sys.stderr)
    if broken:
        return 2
    if single and not document["courses"][0]["initialized"]:
        print("이 과목에는 아직 원장(.gongbu/course.json)이 없습니다. 처음 설정: gongbu course init --name <과목명> "
              "--mode faithful|deep --materials handout|handout+recording", file=sys.stderr)
        return 3
    return 0


def command_course_init(args: argparse.Namespace) -> int:
    start = (args.course_dir or Path.cwd()).expanduser().resolve()
    root = find_course_root(start)
    if root != start:
        raise LedgerError(f"위 폴더가 이미 과목 폴더입니다: {root}\n그 폴더에서 실행하십시오.")

    def change(ledger: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if ledger is not None:
            raise LedgerError(f"이미 과목 원장이 있습니다. 바꾸려면 `gongbu course set`을 쓰십시오: {ledger_path(start)}")
        created = new_ledger(args.name, args.mode, args.materials, args.alias, args.planned)
        return created, {"course": args.name, "dir": start.as_posix(), "ledger": str(ledger_path(start)), "created": True}

    emit(change_ledger(start, change, args.dry_run))
    return 0


def parse_expect(text: str) -> dict[str, int] | None:
    if text.strip().lower() == "default":
        return None
    values: dict[str, int] = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        if key.strip() not in ("handout", "transcript") or not value.strip().isdigit():
            raise LedgerError(f"--expect는 handout=N,transcript=N 또는 default입니다: {text}")
        values[key.strip()] = int(value)
    check_expect(values, "--expect")
    return values


def command_course_set(args: argparse.Namespace) -> int:
    _, root = course_dirs_of(args)

    def change(ledger: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        ledger = require(ledger)
        before = copy.deepcopy(ledger)
        for key in ("name", "mode", "materials"):
            if getattr(args, key) is not None:
                ledger[key] = getattr(args, key)
        if args.planned is not None:
            ledger["planned"] = None if args.planned == "none" else int(args.planned)
        if args.expect is not None:
            ledger["expect"] = parse_expect(args.expect)
        changed = {key: [before.get(key), ledger.get(key)] for key in ("name", "mode", "materials", "planned", "expect")
                   if before.get(key) != ledger.get(key)}
        return ledger, {"course": ledger["name"], "changed": changed}

    emit(change_ledger(root, change, args.dry_run))
    return 0


def plan_items(value: Any, kind: str, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(f"{where}은(는) 객체여야 합니다: {value}")
    unknown = sorted(set(value) - set(PLAN_KEYS[kind]))
    if unknown:
        raise LedgerError(f"{where}에 모르는 항목이 있습니다: {', '.join(unknown)}")
    return value


def apply_plan(course: Path, ledger: dict[str, Any] | None, plan: Any, force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """plan을 원장에 반영한다. 과목 항목은 바꾸고 강의는 ID로 덮어쓰거나 더한다. 틀린 항목이 있으면 아무것도 쓰지 않는다."""
    if not isinstance(plan, dict) or set(plan) - {"course", "lectures"}:
        raise LedgerError('plan은 {"course": {...}, "lectures": [...]} 형식이어야 합니다.')
    fields = plan_items(plan.get("course") or {}, "course", "plan의 course")
    if ledger is None:
        lacking = [key for key in ("name", "mode", "materials") if key not in fields]
        if lacking:
            raise LedgerError(f"원장이 아직 없어 plan의 course에 name·mode·materials가 모두 있어야 합니다(빠진 것: {', '.join(lacking)}).")
        ledger = new_ledger(fields["name"], fields["mode"], fields["materials"])
    ledger.update({key: copy.deepcopy(fields[key]) for key in PLAN_KEYS["course"] if key in fields})
    items = plan.get("lectures") or []
    if not isinstance(items, list):
        raise LedgerError("plan의 lectures는 목록이어야 합니다.")
    existing = {lecture["id"]: lecture for lecture in ledger["lectures"]}
    planned_ids: list[str] = []
    added, updated, notes, notices = [], [], [], []
    for index, item in enumerate(items):
        item = plan_items(item, "lecture", f"plan의 lectures[{index}]")
        lecture_id = item.get("id")
        if not isinstance(lecture_id, str) or not ID_RE.fullmatch(lecture_id):
            raise LedgerError(f"강의 ID는 영문·숫자로 시작하고 영문·숫자·_.-만 쓴 41자 이내여야 합니다: {lecture_id}")
        if lecture_id in planned_ids:
            raise LedgerError(f"plan에 같은 강의 ID가 두 번 있습니다: {lecture_id}")
        planned_ids.append(lecture_id)
        lecture = existing.get(lecture_id)
        if lecture is None:
            lecture = existing[lecture_id] = {"id": lecture_id, "title": None, "expect": None, "materials": [], "note": None}
            added.append(lecture_id)
        else:
            updated.append(lecture_id)
        for key in ("title", "weeks", "expect"):
            if key in item:
                lecture[key] = copy.deepcopy(item[key])
        if "materials" in item:
            if not isinstance(item["materials"], list):
                raise LedgerError(f"강의 {lecture_id}의 materials는 목록이어야 합니다.")
            lecture["materials"] = []
            for number, material in enumerate(item["materials"]):
                material = plan_items(material, "material", f"강의 {lecture_id}의 materials[{number}]")
                if "kind" not in material or "path" not in material:
                    raise LedgerError(f"강의 {lecture_id}의 materials[{number}]에 kind와 path가 있어야 합니다.")
                lecture["materials"].append(material_entry(course, material["kind"], material["path"], material.get("part"),
                                                           material.get("from"), force))
    old_ids = [lecture["id"] for lecture in ledger["lectures"]]
    if set(old_ids) <= set(planned_ids):
        order = planned_ids  # plan이 강의를 모두 적었으면 plan 순서가 표시 순서다
    else:
        order = old_ids + [lecture_id for lecture_id in planned_ids if lecture_id not in old_ids]
    ledger["lectures"] = [existing[lecture_id] for lecture_id in order]
    for item in items:
        if item.get("note") is None:
            continue
        note = plan_items(item["note"], "note", f"강의 {item['id']}의 note")
        if "source" not in note or "progress" not in note:
            raise LedgerError(f"강의 {item['id']}의 note에 source와 progress가 있어야 합니다.")
        notices += register_note(course, ledger, existing[item["id"]], note["source"], note["progress"],
                                 outputs=note.get("outputs"), covers=note.get("covers"), mode=note.get("mode"))
        notes.append(item["id"])
    return ledger, {"course": ledger["name"], "lectures": len(ledger["lectures"]), "added": added, "updated": updated,
                    "notes": notes, "ignore": ledger.get("ignore", []), "notices": notices}


def plan_preview(ledger: dict[str, Any], summary: dict[str, Any]) -> str:
    rows = [["강의", "제목", "주차", "교안", "녹음", "전사", "노트"]]
    for lecture in ledger["lectures"]:
        if lecture["id"] not in summary["added"] + summary["updated"]:
            continue
        counts = {kind: sum(m["kind"] == kind for m in lecture.get("materials", [])) for kind in KINDS}
        note = lecture.get("note")
        rows.append([lecture["id"] + (" (새)" if lecture["id"] in summary["added"] else ""), lecture.get("title") or "",
                     weeks_text(lecture.get("weeks")), str(counts["handout"]), str(counts["recording"]), str(counts["transcript"]),
                     f"{note['source']} · {NOTE_TEXT[note['progress']]}" if note else "—"])
    lines = [f"{ledger['name']} · {pn.MODES[ledger['mode']][0]} · {MATERIAL_LABELS[ledger['materials']]} "
             f"— 강의 {len(ledger['lectures'])}개 (새 {len(summary['added'])}, 바뀜 {len(summary['updated'])})"]
    lines += table_lines(rows) if len(rows) > 1 else ["  바뀌는 강의 없음"]
    lines.append("무시 패턴(정규식, 그대로):")
    lines += [f"  {pattern}" for pattern in ledger.get("ignore", [])] or ["  (없음)"]
    lines.append("--dry-run이라 아무것도 쓰지 않았습니다.")
    return "\n".join(lines)


def command_course_import(args: argparse.Namespace) -> int:
    _, root = course_dirs_of(args)
    try:
        # PowerShell 5.1의 Set-Content/Out-File은 UTF-8 앞에 BOM을 붙인다. utf-8-sig는 BOM이 있든 없든 읽는다.
        plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"plan JSON을 읽지 못했습니다({exc.lineno}행 {exc.colno}열, {exc.msg}): {args.plan}") from None
    except UnicodeDecodeError:
        raise LedgerError(f"plan은 UTF-8로 저장한 JSON이어야 합니다: {args.plan}") from None
    result: dict[str, Any] = {}

    def change(ledger: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        changed, summary = apply_plan(root, ledger, plan, args.force)
        result["ledger"] = changed
        return changed, summary

    summary = change_ledger(root, change, args.dry_run)
    announce(summary["notices"])
    if args.dry_run:
        print(plan_preview(result["ledger"], summary))
    else:
        emit(summary)
    return 0


def command_course_note(args: argparse.Namespace) -> int:
    _, root = course_dirs_of(args)

    def change(ledger: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        ledger = require(ledger)
        lecture = find_lecture(ledger, args.id)
        notices = register_note(root, ledger, lecture, args.source, args.progress, outputs=args.output,
                                covers=args.covers, mode=args.mode)
        return ledger, {"lecture": args.id, "note": lecture["note"], "notices": notices}

    summary = change_ledger(root, change, args.dry_run)
    announce(summary["notices"])
    emit(summary)
    return 0


def command_material_add(args: argparse.Namespace) -> int:
    _, root = course_dirs_of(args)

    def change(ledger: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        ledger = require(ledger)
        lecture = find_lecture(ledger, args.id)
        entry = material_entry(root, args.kind, args.path, args.part, args.from_, args.force)
        materials = lecture.setdefault("materials", [])
        index = next((number for number, material in enumerate(materials)
                      if path_key(material["path"]) == path_key(entry["path"])), None)
        if index is None:
            materials.append(entry)
        else:
            materials[index] = entry
        return ledger, {"lecture": args.id, "material": entry, "replaced": index is not None}

    emit(change_ledger(root, change, args.dry_run))
    return 0


def command_relink(args: argparse.Namespace) -> int:
    _, root = course_dirs_of(args)

    def change(ledger: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        ledger = require(ledger)
        check_ledger(ledger)
        survey = Survey(root, ledger, None, SETTLE_SECONDS, time.time())
        errors: list[str] = []
        survey.scan = scan_course(root, compile_ignore(ledger.get("ignore", []), errors), SETTLE_SECONDS, survey.now)
        find_moves(survey, registered_keys(survey))
        for lecture in ledger["lectures"]:
            for material in lecture.get("materials", []):
                move = survey.moves.get(path_key(material["path"]))
                if move:
                    material["path"] = move[1].rel
                    material["mtime_ns"] = move[1].path.stat().st_mtime_ns
                # 전사 자료가 가리키는 녹음도 옮겨졌으면 따라간다. 그러지 않으면 전사된 녹음이 다시 전사 대기로 보인다.
                source = survey.moves.get(path_key(material.get("from") or ""))
                if source:
                    material["from"] = source[1].rel
            inputs = (lecture.get("note") or {}).get("inputs") or {}
            for old in list(inputs):
                move = survey.moves.get(path_key(old))
                if move:
                    inputs[move[1].rel] = inputs.pop(old)
        missing = sorted({material["path"] for lecture in ledger["lectures"] for material in lecture.get("materials", [])
                          if locate(root, material["path"]) is None})
        return ledger, {"moved": [{"from": source, "to": found.rel} for source, found in survey.moves.values()],
                        "missing": missing}

    emit(change_ledger(root, change, args.dry_run))
    return 0


def command_course_show(args: argparse.Namespace) -> int:
    _, root = course_dirs_of(args)
    ledger = require(load_ledger(require_folder(root)))
    if args.json:
        emit(ledger)
        return 0
    aliases = f" (별칭: {', '.join(ledger['aliases'])})" if ledger.get("aliases") else ""
    planned = f" · 예정 강의 {ledger['planned']}" if ledger.get("planned") else ""
    print(f"{ledger['name']}{aliases} · {pn.MODES[ledger['mode']][0]} · {MATERIAL_LABELS[ledger['materials']]}{planned}")
    print(f"원장: {ledger_path(root)}")
    if ledger.get("expect"):
        print(f"강의마다 필요한 자료: 교안 {ledger['expect']['handout']} · 전사 {ledger['expect']['transcript']}")
    if ledger.get("ignore"):
        print("무시 패턴: " + ", ".join(ledger["ignore"]))
    for convention in ledger.get("conventions", []):
        print(f"약속: {convention}")
    rows = [["강의", "제목", "주차", "자료", "노트"]]
    for lecture in ledger["lectures"]:
        counts = " · ".join(f"{KIND_LABELS[kind]} {sum(m['kind'] == kind for m in lecture.get('materials', []))}" for kind in KINDS)
        note = lecture.get("note")
        rows.append([lecture["id"], lecture.get("title") or "", weeks_text(lecture.get("weeks")), counts,
                     f"{note['source']} ({NOTE_TEXT[note['progress']]}{' · ' + note['covers'] if note.get('covers') else ''})"
                     if note else "—"])
    print("\n".join(table_lines(rows)) if len(rows) > 1 else "  등록된 강의가 없습니다")
    return 0


def change_registry(mutate: Callable[[dict[str, Any]], dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"status": "ok", "dry_run": True, **mutate(load_registry())}
    summary: dict[str, Any] = {}
    update_registry(lambda registry: summary.update(mutate(registry)))
    return {"status": "ok", **summary}


def pick_semester(registry: dict[str, Any], requested: str | None) -> str:
    sem_id = requested or registry.get("current")
    if not sem_id:
        raise LedgerError("현재 학기가 없습니다. 먼저 `gongbu semester init <ID> --title … --start … --weeks …`를 실행하십시오.")
    if sem_id not in registry["semesters"]:
        raise LedgerError(f"학기 목록에 {sem_id} 학기가 없습니다. `gongbu semester show`로 확인하십시오.")
    return sem_id


def command_semester_init(args: argparse.Namespace) -> int:
    if not ID_RE.fullmatch(args.id):
        raise LedgerError(f"학기 ID는 영문·숫자로 시작하고 영문·숫자·_.-만 씁니다(예: 2026-2): {args.id}")

    def mutate(registry: dict[str, Any]) -> dict[str, Any]:
        if args.id in registry["semesters"]:
            raise LedgerError(f"이미 있는 학기입니다: {args.id}")
        registry["semesters"][args.id] = {"title": args.title, "start": args.start.isoformat(), "weeks": args.weeks,
                                          "courses": []}
        if not registry.get("current"):
            registry["current"] = args.id
        return {"semester": args.id, "current": registry["current"], "registry": str(registry_path())}

    emit(change_registry(mutate, args.dry_run))
    return 0


def command_semester_course(args: argparse.Namespace, add: bool) -> int:
    _, root = course_dirs_of(args, args.directory)
    if add and not root.is_dir():
        raise LedgerError(f"과목 폴더가 없습니다: {root}")

    def mutate(registry: dict[str, Any]) -> dict[str, Any]:
        sem_id = pick_semester(registry, args.semester)
        courses = registry["semesters"][sem_id].setdefault("courses", [])
        present = [course for course in courses if dir_key(course) == dir_key(root)]
        if add:
            if not present:
                courses.append(root.as_posix())
            elsewhere = [other for other, semester in registry["semesters"].items() if other != sem_id
                         and any(dir_key(course) == dir_key(root) for course in semester.get("courses", []))]
            return {"semester": sem_id, "course": root.as_posix(), "added": not present, "also_in": elsewhere,
                    "courses": list(courses)}
        if not present:
            raise LedgerError(f"{sem_id} 학기에 없는 과목입니다: {root.as_posix()}")
        courses[:] = [course for course in courses if dir_key(course) != dir_key(root)]
        return {"semester": sem_id, "course": root.as_posix(), "removed": True, "courses": list(courses)}

    emit(change_registry(mutate, args.dry_run))
    return 0


def command_semester_use(args: argparse.Namespace) -> int:
    def mutate(registry: dict[str, Any]) -> dict[str, Any]:
        registry["current"] = pick_semester(registry, args.id)
        return {"current": args.id}

    emit(change_registry(mutate, args.dry_run))
    return 0


def command_semester_show(args: argparse.Namespace) -> int:
    registry = load_registry()
    if args.json:
        emit({"path": str(registry_path()), **registry})
        return 0
    if not registry["semesters"]:
        print(f"학기가 없습니다. `gongbu semester init <ID> --title … --start … --weeks …`로 만드십시오. (학기 목록: {registry_path()})")
        return 0
    today = dt.date.today()
    for sem_id, semester in registry["semesters"].items():
        mark = "*" if sem_id == registry.get("current") else " "
        week = current_week(semester, today)
        page = " · 노션 학기 페이지 있음" if (semester.get("notion") or {}).get("page_id") else ""
        print(f"{mark} {sem_id}  {semester.get('title') or sem_id} · {semester.get('start')}부터 {semester.get('weeks')}주"
              f"{f' · 지금 {week}주차' if week else ''}{page}")
        for course in semester.get("courses", []):
            print(f"    {course}")
    print(f"학기 목록: {registry_path()}")
    return 0


# ------------------------------------------------------------------ 인자

def parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"YYYY-MM-DD 형식이 아닙니다: {text}") from None


def count_arg(text: str) -> int:
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"0 이상 정수여야 합니다: {text}")
    return int(text)


def weeks_arg(text: str) -> int:
    value = count_arg(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"1 이상이어야 합니다: {text}")
    return value


def planned_arg(text: str) -> str:
    if text.lower() != "none" and not text.isdigit():
        raise argparse.ArgumentTypeError(f"0 이상 정수 또는 none이어야 합니다: {text}")
    return text.lower()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gongbu", description="과목 원장·학기 목록을 관리하고 네트워크 없이 과목 현황을 봅니다.")
    commands = parser.add_subparsers(dest="command", required=True)
    leaves: list[argparse.ArgumentParser] = []
    writers: list[argparse.ArgumentParser] = []

    status = commands.add_parser("status", help="과목 현황(읽기 전용, 네트워크 없음)",
                                 description="원장과 과목 폴더를 훑어 강의별 교안·녹음·노트·노션 상태와 다음 할 일을 보여 줍니다.")
    status.add_argument("--json", action="store_true", help="JSON(gongbu.status/1)으로 출력")
    scope = status.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="현재 학기의 모든 과목")
    scope.add_argument("--semester", default=None, help="이 학기의 모든 과목")
    status.add_argument("--settle-seconds", type=count_arg, default=SETTLE_SECONDS,
                        help=f"이보다 최근에 바뀐 파일은 받는 중으로 본다(기본 {SETTLE_SECONDS}초)")
    status.add_argument("--today", type=parse_date, default=None, help="주차 계산에 쓸 오늘 날짜(YYYY-MM-DD)")
    leaves.append(status)

    course = commands.add_parser("course", help="과목 원장(.gongbu/course.json)")
    course_commands = course.add_subparsers(dest="action", required=True)
    init = course_commands.add_parser("init", help="과목 원장 만들기(처음 한 번)")
    init.add_argument("--name", required=True, help="과목 이름(노션 과목 페이지 이름과 같게)")
    init.add_argument("--mode", choices=MODES, required=True, help="기본 노트 모드")
    init.add_argument("--materials", choices=tuple(MATERIAL_DEFAULTS), required=True, help="강의마다 필요한 자료")
    init.add_argument("--alias", action="append", default=[], help="다른 이름(여러 번 가능)")
    init.add_argument("--planned", type=count_arg, default=None, help="이번 학기 예정 강의 수")
    changes = course_commands.add_parser("set", help="원장의 과목 항목 바꾸기")
    changes.add_argument("--name", default=None)
    changes.add_argument("--mode", choices=MODES, default=None)
    changes.add_argument("--materials", choices=tuple(MATERIAL_DEFAULTS), default=None)
    changes.add_argument("--planned", type=planned_arg, default=None, help="예정 강의 수 또는 none")
    changes.add_argument("--expect", default=None, help="강의마다 필요한 수: handout=N,transcript=N 또는 default")
    plan = course_commands.add_parser("import", help="plan.json으로 강의·자료·노트를 한 번에 등록")
    plan.add_argument("plan", type=Path, help='{"course": {...}, "lectures": [...]} 형식의 JSON 파일')
    plan.add_argument("--force", action="store_true", help="최근에 바뀐 파일도 등록")
    note = course_commands.add_parser("note", help="강의 하나의 노트 등록(만들거나 고친 뒤)")
    note.add_argument("id", help="원장 강의 ID(영문·숫자·_.-, 예: w01. 녹음·전사 폴더 이름과 다르다)")
    note.add_argument("--source", required=True, help="노트 원고(과목 폴더 기준 경로)")
    note.add_argument("--progress", choices=PROGRESS, required=True, help="done은 수업이 그 강의를 끝냈을 때만")
    note.add_argument("--output", action="append", default=None, help="노트 산출물(여러 번 가능, 생략하면 그대로)")
    note.add_argument("--covers", default=None, help="노트가 다룬 범위(예: p.1–19)")
    note.add_argument("--mode", choices=MODES, default=None, help="기본: .tex는 deep, 그 밖은 faithful")
    material = course_commands.add_parser("material", help="강의 자료")
    material_commands = material.add_subparsers(dest="material_action", required=True)
    add = material_commands.add_parser("add", help="강의에 자료 하나 등록")
    add.add_argument("id", help="원장 강의 ID(영문·숫자·_.-, 예: w01. 녹음·전사 폴더 이름과 다르다)")
    add.add_argument("path", help="자료 파일(과목 폴더 기준 경로)")
    add.add_argument("--kind", choices=KINDS, required=True)
    add.add_argument("--part", default=None, help="파일 안에서 이 강의에 해당하는 범위(예: 50:55~끝)")
    add.add_argument("--from", dest="from_", default=None, help="전사 파일이 나온 녹음 경로")
    add.add_argument("--force", action="store_true", help="최근에 바뀐 파일도 등록")
    relink = course_commands.add_parser("relink", help="옮겨진 등록 파일을 다시 연결")
    show = course_commands.add_parser("show", help="원장 보기")
    show.add_argument("--json", action="store_true")
    leaves += [init, changes, plan, note, add, relink, show]
    writers += [init, changes, plan, note, add, relink]

    semester = commands.add_parser("semester", help="학기 목록(사용자 설정 폴더의 semesters.json)")
    semester_commands = semester.add_subparsers(dest="action", required=True)
    create = semester_commands.add_parser("init", help="학기 만들기(현재 학기가 없으면 현재 학기가 된다)")
    create.add_argument("id", help="학기 ID(예: 2026-2)")
    create.add_argument("--title", required=True, help="학기 이름(예: 2026-2학기)")
    create.add_argument("--start", type=parse_date, required=True, help="1주차 월요일(YYYY-MM-DD)")
    create.add_argument("--weeks", type=weeks_arg, required=True, help="주 수")
    joins = []
    for name, text in (("add-course", "학기에 과목 넣기(기본: 이 과목, 현재 학기)"), ("remove-course", "학기에서 과목 빼기")):
        sub = semester_commands.add_parser(name, help=text)
        sub.add_argument("directory", nargs="?", type=Path, default=None, help="과목 폴더(기본: 현재 과목)")
        sub.add_argument("--semester", default=None, help="학기 ID(기본: 현재 학기)")
        joins.append(sub)
    use = semester_commands.add_parser("use", help="현재 학기 바꾸기")
    use.add_argument("id")
    listing = semester_commands.add_parser("show", help="학기 목록 보기")
    listing.add_argument("--json", action="store_true")
    leaves += [create, *joins, use, listing]
    writers += [create, *joins, use]

    for sub in writers:
        sub.add_argument("--dry-run", action="store_true", help="바뀔 내용만 보여 주고 쓰지 않는다")
    for sub in leaves:
        sub.add_argument("--course-dir", type=Path, default=None, help="과목 폴더(기본: 현재 폴더)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        if args.command == "status":
            return command_status(args)
        if args.command == "course":
            handlers = {"init": command_course_init, "set": command_course_set, "import": command_course_import,
                        "note": command_course_note, "material": command_material_add, "relink": command_relink,
                        "show": command_course_show}
            return handlers[args.action](args)
        if args.action in ("add-course", "remove-course"):
            return command_semester_course(args, args.action == "add-course")
        return {"init": command_semester_init, "use": command_semester_use, "show": command_semester_show}[args.action](args)
    except (LedgerError, pn.NotionError, OSError, ValueError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # gongbu는 이 파일을 `__main__`으로 실행한다. notion_dashboard 등이 `import course_ledger`로 두 번째 사본을 읽지 않게 한다.
    sys.modules.setdefault("course_ledger", sys.modules[__name__])
    sys.exit(main())
