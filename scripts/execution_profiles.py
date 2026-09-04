#!/usr/bin/env python3
"""공통 실행 프로필과 런타임별 모델표.

프로필(`economy_high` 등)은 "어떤 비용·품질 등급으로 일을 맡길지"를 정한 계약이고
모델명을 담지 않는다. 그 계약을 실제로 어느 모델이 수행하는지는 런타임(Codex,
Claude Code)별 표가 정한다. manage_run.py는 실행 시점에 `resolve()`로 해석한 실제
모델·effort를 상태 파일에 기록하고, 검증은 그 실행이 사용한 표 스냅샷과 대조한다.

두 런타임의 등급은 *대응*이지 *등가*가 아니다. Codex의 `max`와 Claude의 `max`가
같은 비용·품질을 뜻하지 않으므로, 표는 "같은 역할에 같은 등급을 쓴다"는 의도만
고정한다. 모델을 바꾸려면 이 파일의 표만 고치고 `sync_runtime_agents.py`를 실행한다.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

RUNTIMES = ("codex", "claude")
PROFILE_ORDER = ("local_python", "economy_high", "review_high", "quality_high", "quality_xhigh")
SUBAGENT_PROFILES = ("economy_high", "review_high", "quality_high", "quality_xhigh")
SUBAGENT_CONCURRENCY_LIMIT = 2

# 실행 책임 계약. executor가 python이면 모델을 호출하지 않는다.
EXECUTION_PROFILES: dict[str, dict[str, Any]] = {
    "local_python": {
        "executor": "python",
        "agent": None,
        "description": "로컬 Python·CLI로 결정적 처리; 모델 호출 없음",
    },
    "economy_high": {
        "executor": "subagent",
        "agent": "study_note_worker",
        "description": "반복적·범위 제한 의미 작업(전사 검수·자료 대응·조판 표본); 집필에는 쓰지 않음",
    },
    "review_high": {
        "executor": "subagent",
        "agent": "faithful_note_reviewer",
        "description": "자료 충실형의 독립 누락·왜곡·약화 최종 대조 (상위 모델 — 교수 설명 축약을 잡는 마지노선)",
    },
    "quality_high": {
        "executor": "subagent",
        "agent": "quality_note_worker",
        "description": "심화 집필·통합 또는 자료 충실형의 국소 의미 충돌 해결",
    },
    "quality_xhigh": {
        "executor": "subagent",
        "agent": "deep_note_reviewer",
        "description": "심화 이해형 완성본의 독립 논리·유도 최종 검수",
    },
}

# 런타임별 모델표. 모델은 별칭(`sonnet`, `opus`)이 아니라 전체 ID로 고정한다.
# 실전 A/B(2026-09-04, 미디어빅뱅과방송 1주2차시)에서 경량 모델 집필이 교수 설명을
# 축약해 "수정 필요" 판정을 받았으므로, 집필·최종 판정은 상위 모델이 맡고
# 경량 모델은 전사 검수·대응표·조판 같은 절차·대조 작업에만 쓴다.
# 별칭은 새 세대가 나오면 조용히 따라가서 사용감이 바뀌므로, 새 모델은 검증을
# 거친 뒤 이 표를 갱신해야만 적용된다. Claude의 `effort`는 Claude Code 서브
# 에이전트 frontmatter 등급(low/medium/high/xhigh/max)이다. Haiku는 의미 작업
# 품질을 보장하기 어려워 표에 두지 않는다.
RUNTIME_MODEL_TABLES: dict[str, dict[str, dict[str, str]]] = {
    "codex": {
        "economy_high": {"model": "gpt-5.6-luna", "effort": "high"},
        "review_high": {"model": "gpt-5.6-sol", "effort": "high"},
        "quality_high": {"model": "gpt-5.6-sol", "effort": "high"},
        "quality_xhigh": {"model": "gpt-5.6-sol", "effort": "xhigh"},
    },
    "claude": {
        "economy_high": {"model": "claude-sonnet-5", "effort": "high"},
        "review_high": {"model": "claude-opus-5", "effort": "high"},
        "quality_high": {"model": "claude-opus-5", "effort": "high"},
        "quality_xhigh": {"model": "claude-opus-5", "effort": "xhigh"},
    },
}

# 기본 경로에서 쓰지 않는 모델. 선언 파일이나 상태에 나타나면 검증 오류다.
FORBIDDEN_MODELS: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-5.6-terra",),
    "claude": ("haiku",),
}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# 서브 에이전트 선언 파일에 들어가는 역할 설명과 지시문. Codex TOML과 Claude md가
# 같은 원문을 공유하도록 여기에만 둔다.
AGENT_DESCRIPTIONS: dict[str, str] = {
    "study_note_worker": (
        "Runs bounded routine semantic work and faithful-mode drafting after deterministic Python preprocessing."
    ),
    "quality_note_worker": "Authors deep-mode sections and resolves bounded high-value semantic conflicts.",
    "faithful_note_reviewer": (
        "Independently checks faithful-mode notes for source coverage, omission, distortion, and duplication."
    ),
    "deep_note_reviewer": "Performs one independent whole-note logic and derivation audit for deep mode.",
}

AGENT_INSTRUCTIONS: dict[str, str] = {
    "study_note_worker": """Read only the assigned role prompt, evidence packet, predecessor artifacts, and output contract.
Do not scan the full repository, full conversation, full transcript, or every source unless the manager supplies an exact unresolved range that requires it.
Reuse Python-produced extraction, hashes, indexes, validation results, and context packets instead of recreating them.
Make semantic judgments only within the assigned role. Write only the assigned artifact, preserve source pointers, and return unresolved items explicitly.
For faithful drafting, use only the supplied course sources and reviewed instructor material. Do not add external background, new examples, or new derivations.
Do not rerun the whole role after a local failure; request the exact missing page, section, or timestamp.
""",
    "quality_note_worker": """Read only the assigned role prompt, deterministic source packet, predecessor artifact, mode contract, and output path.
For deep-mode authoring, restore prerequisite context, causal links, intermediate reasoning, derivations, examples, and application conditions only where they improve understanding.
Keep course-source content, reviewed instructor explanations, and supplemental knowledge distinguishable. Verify supplemental claims and preserve uncertainty.
For a faithful-mode escalation, resolve only the supplied ambiguity or source conflict; do not broaden the note or reread unrelated material.
Return the assigned artifact and a compact structured list of uncertainty, source conflict, derivation gap, and instructor-distortion risks. Never rewrite an already valid full note during a local repair.
""",
    "faithful_note_reviewer": """Act independently from the writer. Compare every source_unit_id in the supplied source map with its mapped final-note location.
Do not add outside knowledge or rewrite the whole note. Classify each source unit exactly once as included, merged, excluded, or unresolved.
Use excluded only with a specific reason. Use unresolved only when the uncertainty is visibly marked at a concrete note location.
Return a study_note_source_coverage JSON report with reviewer_profile=review_high. Report missing, distorted, weakened, or duplicated course content as failures.
Apply any necessary localized source-faithful patches and recheck only their affected locations within this one final-review call; never request a second whole-note pass.
If a meaning conflict cannot be settled from the bounded evidence, request one exact evidence packet for the quality_high profile instead of guessing.
""",
    "deep_note_reviewer": """Act independently from the author. Read the completed deep-mode note exactly once together with the compact source map, coverage ledger, and Python validation results.
Check global logical order, missing prerequisite links, derivation continuity, assumptions, application conditions, helpfulness of supplemental context, and distortion of instructor explanations.
Do not reread the full raw transcript, recording, or every source. Open raw evidence only for an exact flagged source_unit_id supplied by the manager.
Do not regenerate the whole note. Apply necessary localized patches, recheck only their affected locations, and finish within this single whole-note call. Return a concise verdict and a study_note_source_coverage JSON report with reviewer_profile=quality_xhigh.
Pass only when every source unit is accounted for and no major logical, derivation, or attribution defect remains.
""",
}

# 런타임 감지 신호. Claude Code는 CLAUDECODE를, Codex CLI는 CODEX_ 접두 변수를 남긴다.
# codex-companion 플러그인이 Claude 세션 안에 CODEX_COMPANION_*를 두므로 그 접두는 제외한다.
CLAUDE_ENV_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
CODEX_ENV_PREFIX = "CODEX_"
CODEX_ENV_IGNORED_PREFIXES = ("CODEX_COMPANION_",)


class ProfileError(ValueError):
    pass


def runtime_table(runtime: str) -> dict[str, dict[str, Any]]:
    """상태 파일에 스냅샷으로 남길 런타임 모델표(서브 에이전트 프로필만)."""
    if runtime not in RUNTIME_MODEL_TABLES:
        raise ProfileError(f"지원하지 않는 런타임입니다: {runtime} (허용: {', '.join(RUNTIMES)})")
    table = RUNTIME_MODEL_TABLES[runtime]
    return {
        profile: {
            "agent": EXECUTION_PROFILES[profile]["agent"],
            "model": table[profile]["model"],
            "effort": table[profile]["effort"],
        }
        for profile in SUBAGENT_PROFILES
    }


def resolve(
    profile: str,
    runtime: str,
    table: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """프로필을 런타임의 실제 모델·effort로 해석한다.

    `table`을 주면 (상태 파일의 스냅샷처럼) 그 표를 우선 사용해, 프로젝트 표가
    나중에 바뀌어도 과거 실행의 기록을 같은 기준으로 검증할 수 있다.
    """
    if profile not in EXECUTION_PROFILES:
        raise ProfileError(f"알 수 없는 실행 프로필입니다: {profile}")
    if runtime not in RUNTIME_MODEL_TABLES:
        raise ProfileError(f"지원하지 않는 런타임입니다: {runtime} (허용: {', '.join(RUNTIMES)})")
    contract = EXECUTION_PROFILES[profile]
    resolved: dict[str, Any] = {
        "profile": profile,
        "runtime": runtime,
        "executor": contract["executor"],
        "agent": contract["agent"],
        "model": None,
        "effort": None,
    }
    if contract["executor"] != "subagent":
        return resolved
    source = table if table is not None else runtime_table(runtime)
    row = source.get(profile)
    if not isinstance(row, Mapping) or not row.get("model") or not row.get("effort"):
        raise ProfileError(f"{runtime} 모델표에 {profile} 항목이 없습니다.")
    resolved["model"] = str(row["model"])
    resolved["effort"] = str(row["effort"])
    return resolved


def detect_runtime(environ: Mapping[str, str] | None = None) -> str | None:
    """실행 환경 변수로 런타임을 추정한다. 판정할 수 없거나 신호가 겹치면 None."""
    env = os.environ if environ is None else environ
    claude = any(marker in env for marker in CLAUDE_ENV_MARKERS)
    codex = any(
        key.startswith(CODEX_ENV_PREFIX) and not key.startswith(CODEX_ENV_IGNORED_PREFIXES)
        for key in env
    )
    if claude and not codex:
        return "claude"
    if codex and not claude:
        return "codex"
    return None
