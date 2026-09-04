"""`gongbu` 명령 — 과목 폴더에서 엔진 스크립트를 호출한다.

하위 명령은 엔진 `scripts/`의 파일 하나에 그대로 대응한다. 이 모듈이 하는 일은
둘뿐이다: 엔진 위치를 찾고, 사용자가 명시하지 않은 경로 인자에 과목 폴더
기준 기본값(`.gongbu/`)을 넣는다. 스크립트 본체의 인자·동작은 바꾸지 않는다.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .paths import (
    ENGINE_HOME_ENV,
    OUTPUT_DIR_NAME,
    STATE_DIR_NAME,
    EngineNotFoundError,
    course_root,
    describe,
    engine_root,
    output_root,
    state_root,
)

SCRIPT_COMMANDS: dict[str, str] = {
    "record": "record_lecture.py",
    "transcribe": "transcribe_lecture.py",
    "transcribe-batch": "transcribe_batch.py",
    "run": "manage_run.py",
    "review-prep": "prepare_transcript_review.py",
    "review-select": "select_review_packets.py",
    "review-apply": "apply_transcript_corrections.py",
}
VALIDATE_TARGETS: dict[str, str] = {
    "setup": "validate_agent_setup.py",
    "note": "validate_note_output.py",
    "transcript": "validate_transcript_package.py",
    "coverage": "validate_source_coverage.py",
}
LOCAL_COMMANDS = ("paths", "setup", "setup-agents", "version")

# 과목 폴더용 .gitignore. 녹음·상태·인증 산출물이 과목 폴더의 Git에 들어가지 않게 한다.
COURSE_GITIGNORE_PATTERNS = (
    f"/{STATE_DIR_NAME}/",
    "*.wav",
    "*.mp3",
    "*.m4a",
    "*.aac",
    "*.flac",
    "*.ogg",
    "*.webm",
    "*.mp4",
    ".env",
    ".env.*",
    "browser-profile/",
    ".auth/",
    "cookies*.json",
    "storage-state*.json",
)

USAGE = f"""gongbu {__version__} — 과목 폴더에서 쓰는 gongbu-haja 명령

사용법: gongbu <명령> [인자...]

과목 폴더 준비
  setup                  현재 폴더에 {STATE_DIR_NAME}/, {OUTPUT_DIR_NAME}/, .gitignore(녹음·상태 제외)를 만든다
  setup-agents           ~/.claude/agents, ~/.codex/agents 에 서브 에이전트 4개를 설치한다 (--home 경로)
  paths                  엔진·프롬프트·규칙·상태 폴더 위치를 JSON으로 출력한다

강의 자료
  record ...             온라인 강의 시스템 오디오 녹음 (기본 저장: <과목>/<강의ID>/)
  transcribe ...         녹음 1개 로컬 전사        (기본 출력: <과목>/{STATE_DIR_NAME}/<강의ID>/transcript/)
  transcribe-batch ...   녹음 여러 개 순차 전사

실행 상태 (manage_run.py 그대로)
  run init <입력폴더> --lecture-id <ID> --note-mode faithful|deep
                         상태 파일을 <과목>/{STATE_DIR_NAME}/<ID>/run_state.json 에 만든다
  run next|start|complete|fail|escalate|... <run_state.json> ...

검수 도구
  review-prep ...        전사 용어 후보·검수 패킷 생성
  review-select ...      16KiB 이하 검수 패킷 선택
  review-apply ...       승인된 교정 적용
  validate setup|note|transcript|coverage ...

  version                버전 출력

명령별 자세한 인자는 `gongbu <명령> --help`. 엔진 위치는 {ENGINE_HOME_ENV} 환경 변수로 바꿀 수 있다.
"""


def has_option(arguments: Sequence[str], *names: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in arguments for name in names)


def build_argv(command: str, rest: Sequence[str], course: Path) -> list[str]:
    """사용자가 경로를 명시하지 않았을 때만 과목 폴더 기준 기본값을 덧붙인다."""
    argv = list(rest)
    if command == "record":
        if "--list-devices" not in argv and not has_option(argv, "--output", "--input-root"):
            argv += ["--input-root", str(course)]
    elif command in ("transcribe", "transcribe-batch"):
        if not has_option(argv, "--output-root"):
            argv += ["--output-root", str(state_root(course))]
    elif command == "run":
        if argv and argv[0] == "init" and not has_option(argv, "--root", "--state-root"):
            argv += ["--state-root", str(state_root(course))]
    return argv


def resolve_script(command: str, rest: Sequence[str]) -> tuple[str, list[str]]:
    """명령 이름을 엔진 스크립트 파일명으로 바꾼다. validate는 첫 인자로 대상을 고른다."""
    if command in SCRIPT_COMMANDS:
        return SCRIPT_COMMANDS[command], list(rest)
    if command == "validate":
        if not rest or rest[0] not in VALIDATE_TARGETS:
            raise ValueError(
                "validate 대상을 지정하십시오: " + ", ".join(VALIDATE_TARGETS)
            )
        return VALIDATE_TARGETS[rest[0]], list(rest[1:])
    raise ValueError(f"알 수 없는 명령입니다: {command}")


def _exit_code(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def run_script(engine: Path, script_name: str, argv: Sequence[str]) -> int:
    """엔진 스크립트를 같은 프로세스에서 `__main__`으로 실행한다.

    스크립트들은 형제 모듈을 최상위 이름으로 import하므로 scripts/ 를 sys.path 앞에 둔다.
    """
    scripts_dir = engine / "scripts"
    script = scripts_dir / script_name
    if not script.is_file():
        print(f"[오류] 엔진 스크립트가 없습니다: {script}", file=sys.stderr)
        return 2
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    saved_argv = sys.argv
    sys.argv = [str(script), *argv]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return _exit_code(exc.code)
    finally:
        sys.argv = saved_argv
    return 0


def _ensure_gitignore(course: Path) -> list[str]:
    path = course / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    present = {line.strip() for line in existing}
    missing = [pattern for pattern in COURSE_GITIGNORE_PATTERNS if pattern not in present]
    if not missing:
        return []
    lines = list(existing)
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# gongbu-haja: 녹음·실행 상태·인증 산출물은 Git에 올리지 않는다")
    lines.extend(missing)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return missing


def command_setup(course: Path) -> int:
    created = []
    for directory in (state_root(course), output_root(course)):
        if not directory.exists():
            directory.mkdir(parents=True)
            created.append(str(directory))
    added = _ensure_gitignore(course)
    print(
        json.dumps(
            {
                "status": "ok",
                "course_root": str(course),
                "created": created,
                "gitignore_added": added,
                "next": "이 폴더 안에 강의별 하위폴더(예: 2026-03-10_과목A/)를 만들어 교안·녹음을 넣고, "
                "AI 코딩 도구에서 이 폴더를 연 뒤 '학습노트 만들어줘'라고 요청한다.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_setup_agents(engine: Path, rest: Sequence[str]) -> int:
    home = Path.home()
    arguments = list(rest)
    if has_option(arguments, "--home"):
        index = arguments.index("--home")
        if index + 1 >= len(arguments):
            print("[오류] --home 뒤에 경로가 필요합니다.", file=sys.stderr)
            return 2
        home = Path(arguments[index + 1]).expanduser().resolve()
    scripts_dir = engine / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import sync_runtime_agents  # noqa: PLC0415 — 엔진 위치를 정한 뒤에만 import 가능

    written, notices = sync_runtime_agents.sync_user(home)
    print(
        json.dumps(
            {
                "status": "ok",
                "home": str(home),
                "written": [str(path) for path in written],
                "notices": notices,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if arguments else 2
    command, rest = arguments[0], arguments[1:]
    if command in ("version", "--version", "-V"):
        print(__version__)
        return 0

    course = course_root()
    if command == "setup":
        return command_setup(course)

    try:
        engine = engine_root()
    except EngineNotFoundError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2

    if command == "paths":
        print(json.dumps(describe(course), ensure_ascii=False, indent=2))
        return 0
    if command == "setup-agents":
        return command_setup_agents(engine, rest)

    try:
        script_name, passthrough = resolve_script(command, rest)
    except ValueError as exc:
        print(f"[오류] {exc}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return run_script(engine, script_name, build_argv(command, passthrough, course))


if __name__ == "__main__":
    sys.exit(main())
