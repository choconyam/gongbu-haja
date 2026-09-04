"""엔진(규칙·프롬프트·스크립트)과 과목 폴더(.gongbu 상태) 위치를 정한다.

엔진 위치는 세 후보를 순서대로 본다.
1. 환경 변수 `GONGBU_HAJA_HOME` — 개발자가 다른 사본을 쓰고 싶을 때
2. 이 패키지 안의 `engine/` — pipx/pip로 설치된 배포본
3. 이 패키지의 상위 폴더 — 저장소를 그대로 열었거나 `pip install -e .`한 경우

과목 폴더는 명령을 친 현재 폴더다. 그 안의 `.gongbu/<강의ID>/`에 실행 상태와
중간 산출물이, `output/`에 최종 노트가 남는다. 과목 자료를 엔진 폴더로 옮기게
하지 않는다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from . import __version__

PACKAGE_DIR = Path(__file__).resolve().parent
ENGINE_HOME_ENV = "GONGBU_HAJA_HOME"
STATE_DIR_NAME = ".gongbu"
OUTPUT_DIR_NAME = "output"
# 엔진 사본이라고 인정하려면 이 셋이 모두 있어야 한다.
ENGINE_MARKERS = (
    Path("scripts") / "manage_run.py",
    Path("agent_prompts"),
    Path("AGENTS.md"),
)


class EngineNotFoundError(RuntimeError):
    """엔진 사본을 어디에서도 찾지 못했을 때."""


def is_engine_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in ENGINE_MARKERS)


def engine_root(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(ENGINE_HOME_ENV)
    if override:
        candidate = Path(override).expanduser().resolve()
        if not is_engine_root(candidate):
            raise EngineNotFoundError(
                f"{ENGINE_HOME_ENV}={override} 는 gongbu-haja 엔진 폴더가 아닙니다 "
                f"(scripts/manage_run.py, agent_prompts/, AGENTS.md 필요)."
            )
        return candidate
    for candidate in (PACKAGE_DIR / "engine", PACKAGE_DIR.parent):
        if is_engine_root(candidate):
            return candidate
    raise EngineNotFoundError(
        "gongbu-haja 엔진을 찾지 못했습니다. `pipx install gongbu-haja`로 다시 설치하거나 "
        f"{ENGINE_HOME_ENV}에 저장소 사본 경로를 지정하십시오."
    )


def install_kind(root: Path, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if env.get(ENGINE_HOME_ENV):
        return "env"
    if root == PACKAGE_DIR / "engine":
        return "bundled"
    return "repo"


def course_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def state_root(course: Path) -> Path:
    return course / STATE_DIR_NAME


def output_root(course: Path) -> Path:
    return course / OUTPUT_DIR_NAME


def describe(course: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`gongbu paths`가 출력하는 위치 요약. 관리자 에이전트가 프롬프트·규칙을 찾는 근거다."""
    course_dir = course_root(course)
    engine = engine_root(environ)
    return {
        "version": __version__,
        "install": install_kind(engine, environ),
        "engine_root": str(engine),
        "scripts_dir": str(engine / "scripts"),
        "prompts_dir": str(engine / "agent_prompts"),
        "rules_dir": str(engine / "rules"),
        "agents_md": str(engine / "AGENTS.md"),
        "note_final_rules": str(engine / "note_final_rules.md"),
        "course_root": str(course_dir),
        "state_root": str(state_root(course_dir)),
        "output_root": str(output_root(course_dir)),
    }
