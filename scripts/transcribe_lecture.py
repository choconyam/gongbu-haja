#!/usr/bin/env python3
"""강의 녹음을 원본과 연결되는 안전한 전사 패키지로 변환한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import site
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .project_types import AUDIO_SUFFIXES
except ImportError:  # `python scripts/transcribe_lecture.py`로 직접 실행할 때
    from project_types import AUDIO_SUFFIXES

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# -----------------------------------------------------------------------------
# 1. 프로젝트 위치와 지원 파일 형식
# 전사 결과는 원본 옆이 아니라 프로젝트 workspace 아래에만 만든다.
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "workspace"
GENERIC_NAME_TOKENS = {
    "음성",
    "녹음",
    "강의",
    "수업",
    "recording",
    "audio",
    "lecture",
}
TYPE_PATTERNS = (
    ("질답", re.compile(r"질\s*답|q\s*&?\s*a", re.IGNORECASE)),
    ("보충", re.compile(r"보충|추가", re.IGNORECASE)),
    ("실습", re.compile(r"실습|practice|lab", re.IGNORECASE)),
)


# -----------------------------------------------------------------------------
# 2. 강의 식별 정보와 표준 산출물 경로
# 날짜·과목·강의 유형을 안전하게 확정할 수 없으면 추정으로 덮지 않는다.
# -----------------------------------------------------------------------------

class IdentityError(ValueError):
    """안정적인 강의 식별자를 안전하게 만들 수 없을 때 발생한다."""


@dataclass(frozen=True)
class LectureIdentity:
    lecture_id: str
    subject: str | None
    lecture_date: str | None
    lecture_type: str
    inferred: bool


@dataclass(frozen=True)
class OutputPaths:
    output_dir: str
    raw_text: str
    raw_srt: str
    draft_markdown: str
    segments_json: str
    manifest_json: str


_NVIDIA_DLL_DIR_HANDLES: list[Any] = []


def discover_nvidia_dll_dirs() -> tuple[Path, ...]:
    """현재 Python 환경에 설치된 NVIDIA DLL 디렉터리를 찾는다."""
    if os.name != "nt":
        return ()

    site_roots: list[str] = []
    try:
        site_roots.extend(site.getsitepackages())
    except (AttributeError, OSError):
        pass
    try:
        user_site = site.getusersitepackages()
    except (AttributeError, OSError):
        user_site = ""
    if user_site:
        site_roots.append(user_site)

    discovered: list[Path] = []
    seen: set[str] = set()
    for site_root in site_roots:
        nvidia_root = Path(site_root) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for dll_dir in sorted(nvidia_root.glob("*/bin")):
            if not dll_dir.is_dir() or not any(dll_dir.glob("*.dll")):
                continue
            resolved = dll_dir.resolve()
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                discovered.append(resolved)
    return tuple(discovered)


def add_nvidia_dll_dirs() -> tuple[Path, ...]:
    """NVIDIA DLL 폴더를 현재 전사 프로세스에만 등록한다."""
    dll_dirs = discover_nvidia_dll_dirs()
    if not dll_dirs:
        return ()

    existing_path = os.environ.get("PATH", "")
    existing_parts = [part for part in existing_path.split(os.pathsep) if part]
    existing_keys = {os.path.normcase(part) for part in existing_parts}
    prepend: list[str] = []

    for dll_dir in dll_dirs:
        dll_path = str(dll_dir)
        try:
            handle = os.add_dll_directory(dll_path)
        except (AttributeError, OSError):
            handle = None
        if handle is not None:
            # 핸들이 해제되면 Windows DLL 검색 경로 등록도 사라진다.
            _NVIDIA_DLL_DIR_HANDLES.append(handle)
        if os.path.normcase(dll_path) not in existing_keys:
            existing_keys.add(os.path.normcase(dll_path))
            prepend.append(dll_path)

    if prepend:
        os.environ["PATH"] = os.pathsep.join([*prepend, *existing_parts])
    return dll_dirs


# -----------------------------------------------------------------------------
# 3. 강의명·날짜·수업 유형 판별
# 파일명으로 명확한 경우만 자동 확정하고, 애매하면 대화형 확인으로 넘긴다.
# -----------------------------------------------------------------------------

def sanitize_component(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value.strip())
    cleaned = re.sub(r"[\s_]+", "_", cleaned).strip(" ._")
    return cleaned[:120]


def parse_date(value: str) -> str:
    normalized = value.strip().replace(".", "-").replace("_", "-").replace("/", "-")
    if re.fullmatch(r"\d{6}", normalized):
        normalized = "20" + normalized
    if re.fullmatch(r"\d{8}", normalized):
        normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise IdentityError(f"강의 날짜를 해석할 수 없습니다: {value}") from exc


def infer_date(stem: str) -> str | None:
    patterns = (
        r"(?<!\d)(20\d{2})[-_.년 ]?(0?[1-9]|1[0-2])[-_.월 ]?(0?[1-9]|[12]\d|3[01])(?:일)?(?!\d)",
        r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, stem)
        if not match:
            continue
        year, month, day = match.groups()
        if index == 1:
            year = "20" + year
        try:
            return datetime(int(year), int(month), int(day)).date().isoformat()
        except ValueError:
            continue
    return None


def detect_lecture_type(stem: str) -> str | None:
    if "본강의" in stem:
        return "본강의"
    for label, pattern in TYPE_PATTERNS:
        if pattern.search(stem):
            return label
    return None


def infer_lecture_type(stem: str) -> str:
    detected = detect_lecture_type(stem)
    if detected:
        return detected
    return "본강의"


def infer_subject(stem: str) -> str | None:
    candidate = stem
    candidate = re.sub(
        r"(?<!\d)20\d{2}[-_.년 ]?(?:0?[1-9]|1[0-2])[-_.월 ]?(?:0?[1-9]|[12]\d|3[01])(?:일)?(?!\d)",
        " ",
        candidate,
    )
    candidate = re.sub(r"(?<!\d)\d{6}(?!\d)", " ", candidate)
    # detect_lecture_type이 인식하는 기본 유형 '본강의'도 과목명 후보에서 제거해,
    # 권장 명명 규칙(날짜_과목_본강의)에서 lecture_type이 중복되지 않게 한다.
    candidate = candidate.replace("본강의", " ")
    for _, pattern in TYPE_PATTERNS:
        candidate = pattern.sub(" ", candidate)
    for token in GENERIC_NAME_TOKENS:
        candidate = re.sub(rf"(?<![A-Za-z가-힣]){re.escape(token)}(?![A-Za-z가-힣])", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", candidate)
    candidate = re.sub(r"[-_.]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate or candidate.isdigit():
        return None
    return sanitize_component(candidate)


def resolve_identity(args: argparse.Namespace, audio: Path) -> LectureIdentity:
    inferred_date = infer_date(audio.stem)
    inferred_subject = infer_subject(audio.stem)

    if args.lecture_id:
        lecture_id = sanitize_component(args.lecture_id)
        if not lecture_id:
            raise IdentityError("lecture_id가 비어 있습니다.")
        id_date = infer_date(lecture_id)
        id_type = detect_lecture_type(lecture_id)
        id_subject_source = lecture_id
        if id_type:
            id_subject_source = id_subject_source.replace(id_type, " ")
        id_subject = infer_subject(id_subject_source)
        lecture_date = parse_date(args.lecture_date) if args.lecture_date else (id_date or inferred_date)
        subject = sanitize_component(args.subject) if args.subject else (id_subject or inferred_subject)
        lecture_type = (
            sanitize_component(args.lecture_type)
            if args.lecture_type
            else (id_type or detect_lecture_type(audio.stem) or "본강의")
        )
        return LectureIdentity(lecture_id, subject, lecture_date, lecture_type, False)

    lecture_date = parse_date(args.lecture_date) if args.lecture_date else inferred_date
    subject = sanitize_component(args.subject) if args.subject else inferred_subject
    lecture_type = sanitize_component(args.lecture_type) if args.lecture_type else infer_lecture_type(audio.stem)
    missing: list[str] = []
    if not lecture_date:
        missing.append("수업 날짜")
    if not subject:
        missing.append("과목명")
    if missing:
        raise IdentityError(
            f"파일명에서 {', '.join(missing)}을(를) 확정할 수 없습니다. "
            "관리자가 사용자에게 확인한 뒤 --lecture-id 또는 --subject/--lecture-date를 지정하십시오."
        )
    lecture_id = sanitize_component(f"{lecture_date}_{subject}_{lecture_type}")
    return LectureIdentity(lecture_id, subject, lecture_date, lecture_type, True)


def resolve_identity_interactively(args: argparse.Namespace, audio: Path) -> LectureIdentity:
    try:
        return resolve_identity(args, audio)
    except IdentityError as exc:
        if not args.interactive:
            raise
        print(f"[확인 필요] {exc}")
        print("예: 2026-03-10_과목A_본강의")
        try:
            value = input("강의 식별자를 입력하세요: ").strip()
        except EOFError:
            raise IdentityError("강의 식별자가 입력되지 않았습니다(입력 스트림 없음).") from None
        if not value:
            raise IdentityError("강의 식별자가 입력되지 않았습니다.")
        args.lecture_id = value
        return resolve_identity(args, audio)


def build_output_paths(output_root: Path, identity: LectureIdentity) -> tuple[Path, OutputPaths]:
    output_dir = output_root / identity.lecture_id / "transcript"
    base = output_dir / identity.lecture_id
    paths = OutputPaths(
        output_dir=str(output_dir),
        raw_text=str(base.with_name(base.name + "_transcript_raw.txt")),
        raw_srt=str(base.with_name(base.name + "_transcript_raw.srt")),
        draft_markdown=str(base.with_name(base.name + "_transcript_draft.md")),
        segments_json=str(base.with_name(base.name + "_segments.json")),
        manifest_json=str(base.with_name(base.name + "_transcript_manifest.json")),
    )
    return output_dir, paths


def ensure_outputs_available(paths: OutputPaths, force: bool) -> None:
    output_paths = [
        Path(paths.raw_text),
        Path(paths.raw_srt),
        Path(paths.draft_markdown),
        Path(paths.segments_json),
        Path(paths.manifest_json),
    ]
    existing = [path for path in output_paths if path.exists()]
    if not existing or force:
        return
    joined = "\n  - ".join(str(path) for path in existing)
    if len(existing) == len(output_paths):
        raise FileExistsError(
            "기존 전사 산출물이 있어 중단했습니다. 원본 보호를 위해 자동으로 덮어쓰지 않습니다.\n"
            f"  - {joined}\n다른 lecture_id를 쓰거나 의도적으로 교체할 때만 --force를 사용하십시오."
        )
    # 5개 산출물 중 일부만 있으면 중단된 실행의 흔적이다. '이미 전사됨'으로
    # 건너뛰면 불완전 패키지가 완료로 오판되므로 별도 오류로 구분한다.
    raise RuntimeError(
        "불완전한 전사 산출물이 남아 있습니다(중단된 실행 흔적일 수 있음).\n"
        f"  - {joined}\n남은 파일을 정리하거나 --force로 전체를 다시 만드십시오."
    )


def read_glossary(path: Path | None) -> str | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"전문용어 파일이 없습니다: {resolved}")
    text = resolved.read_text(encoding="utf-8-sig")
    terms = []
    for line in text.splitlines():
        value = line.strip().lstrip("-* ").strip()
        if value and not value.startswith("#"):
            terms.append(value)
    if not terms:
        return None
    return "강의 전문용어: " + ", ".join(terms)[:3500]


def segment_to_record(segment: Any) -> dict[str, Any]:
    return {
        "id": int(segment.id),
        "start": round(float(segment.start), 3),
        "end": round(float(segment.end), 3),
        "text": str(segment.text).strip(),
        "avg_logprob": round(float(getattr(segment, "avg_logprob", 0.0)), 6),
        "no_speech_prob": round(float(getattr(segment, "no_speech_prob", 0.0)), 6),
        "compression_ratio": round(float(getattr(segment, "compression_ratio", 0.0)), 6),
    }


# -----------------------------------------------------------------------------
# 4. 하드웨어 감지와 faster-whisper 실행
# 기본 모델 auto는 GPU 메모리에 맞는 가장 정확한 모델을 고르고 선택 근거를 남긴다.
# 실행에 실패하면 다음 후보 조합으로 전체 전사를 처음부터 재시도한다.
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class TranscriptionPlan:
    """모델·장치 인자와 감지된 하드웨어로 확정한 실행 계획."""

    model: str
    attempts: tuple[tuple[str, str, str], ...]  # (모델, 장치, 연산 형식)
    tier: str
    selection: str  # "auto" 또는 "manual"
    detected_vram_mb: int | None
    realtime_factor: float  # 실시간 대비 처리 배속의 대략 추정


# (최소 VRAM MiB, 모델, 연산 형식, 티어 이름, 실시간 대비 배속 추정)
GPU_AUTO_TIERS = (
    (10_000, "large-v3", "float16", "gpu_large-v3_float16", 10.0),
    (6_000, "large-v3", "int8_float16", "gpu_large-v3_int8", 8.0),
    (3_000, "medium", "int8_float16", "gpu_medium_int8", 12.0),
    (2_000, "small", "int8_float16", "gpu_small_int8", 15.0),
)
CPU_AUTO_TIER = ("small", "int8", "cpu_small_int8", 2.0)
MANUAL_REALTIME_FACTORS = {"cuda": 8.0, "cpu": 1.0}


def detect_gpu_vram_mb() -> int | None:
    """nvidia-smi로 첫 GPU의 전체 메모리(MiB)를 읽는다. 실패하면 None."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output:
        return None
    try:
        return int(float(output.splitlines()[0].strip()))
    except ValueError:
        return None


def build_transcription_plan(model: str, device: str, vram_mb: int | None) -> TranscriptionPlan:
    """결정적으로 실행 계획을 만든다. 감지 값은 인자로 받아 테스트 가능하게 유지한다."""
    if model != "auto":
        if device == "cuda":
            attempts: tuple[tuple[str, str, str], ...] = ((model, "cuda", "float16"),)
        elif device == "cpu":
            attempts = ((model, "cpu", "int8"),)
        else:
            attempts = ((model, "cuda", "float16"), (model, "cpu", "int8"))
        factor = MANUAL_REALTIME_FACTORS["cpu" if device == "cpu" else "cuda"]
        return TranscriptionPlan(model, attempts, "manual", "manual", vram_mb, factor)

    if device != "cpu" and vram_mb is not None:
        for minimum, tier_model, compute_type, tier_name, factor in GPU_AUTO_TIERS:
            if vram_mb >= minimum:
                gpu_attempts = [(tier_model, "cuda", compute_type)]
                if device == "auto":
                    gpu_attempts.append((CPU_AUTO_TIER[0], "cpu", CPU_AUTO_TIER[1]))
                return TranscriptionPlan(
                    tier_model, tuple(gpu_attempts), tier_name, "auto", vram_mb, factor
                )
    if device == "cuda":
        if vram_mb is not None:
            # 최소 티어에도 못 미치는 GPU를 강제한 경우: 가장 작은 GPU 티어로 시도한다.
            _, tier_model, compute_type, tier_name, factor = GPU_AUTO_TIERS[-1]
            return TranscriptionPlan(
                tier_model, ((tier_model, "cuda", compute_type),), tier_name, "auto", vram_mb, factor
            )
        # GPU를 강제했지만 메모리를 읽지 못한 경우: 기존 수동 CUDA 동작과 같게 시도한다.
        return TranscriptionPlan(
            "large-v3", (("large-v3", "cuda", "float16"),), "gpu_unknown", "auto", vram_mb, 8.0
        )
    cpu_model, cpu_compute, cpu_tier, cpu_factor = CPU_AUTO_TIER
    return TranscriptionPlan(
        cpu_model, ((cpu_model, "cpu", cpu_compute),), cpu_tier, "auto", vram_mb, cpu_factor
    )


def probe_audio_duration_seconds(audio: Path) -> float | None:
    """faster-whisper가 의존하는 av로 녹음 길이를 읽는다. 실패하면 None."""
    try:
        import av  # type: ignore
    except ImportError:
        return None
    try:
        with av.open(str(audio)) as container:
            if container.duration is None:
                return None
            # container.duration은 PyAV 버전과 무관하게 FFmpeg AV_TIME_BASE(마이크로초) 단위다.
            # av.time_base는 버전에 따라 Fraction 또는 정수라 직접 계산하지 않는다.
            return float(container.duration) / 1_000_000.0
    except Exception:
        return None


def estimate_minutes(duration_seconds: float, realtime_factor: float) -> int:
    return max(1, round(duration_seconds / realtime_factor / 60))


def describe_plan(plan: TranscriptionPlan, duration_seconds: float | None) -> list[str]:
    lines: list[str] = []
    if plan.selection == "auto":
        if plan.detected_vram_mb is not None and plan.attempts[0][1] == "cpu":
            lines.append(
                f"[자동 감지] GPU 메모리 {plan.detected_vram_mb / 1024:.1f}GB는 전사에 부족 → "
                f"CPU에서 {plan.model} 모델 사용 ({plan.tier})"
            )
        elif plan.detected_vram_mb is not None:
            lines.append(
                f"[자동 감지] GPU 메모리 {plan.detected_vram_mb / 1024:.1f}GB → "
                f"모델 {plan.model} 선택 ({plan.tier})"
            )
        elif plan.tier == "gpu_unknown":
            lines.append("[자동 감지] GPU 메모리를 읽지 못해 기본 CUDA 설정으로 시도합니다.")
        else:
            lines.append(
                f"[자동 감지] GPU 미사용(미탐지 또는 CPU 지정) → CPU에서 {plan.model} 모델 사용 ({plan.tier})"
            )
    else:
        lines.append(f"[수동 설정] 모델 {plan.model} / 우선 장치 {plan.attempts[0][1]}")
    if duration_seconds and duration_seconds > 0:
        minutes = duration_seconds / 60
        lines.append(
            f"[예상] 녹음 {minutes:.0f}분 → 전사 약 {estimate_minutes(duration_seconds, plan.realtime_factor)}분 (대략 추정)"
        )
    return lines


def transcribe_once(
    audio: Path,
    model_size: str,
    language: str,
    device: str,
    compute_type: str,
    beam_size: int,
    vad_filter: bool,
    min_silence_ms: int,
    initial_prompt: str | None,
) -> tuple[list[dict[str, Any]], Any]:
    from faster_whisper import WhisperModel  # type: ignore

    print(f"[정보] 모델 로딩: {model_size} / {device} / {compute_type}")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(audio),
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": min_silence_ms} if vad_filter else None,
        initial_prompt=initial_prompt,
    )
    records: list[dict[str, Any]] = []
    for segment in segments:
        record = segment_to_record(segment)
        if record["text"]:
            records.append(record)
            print(f"[{format_clock(record['start'])}–{format_clock(record['end'])}] {record['text']}")
    if not records:
        raise RuntimeError("전사 구간이 하나도 생성되지 않았습니다.")
    return records, info


def perform_transcription(
    args: argparse.Namespace,
    audio: Path,
    initial_prompt: str | None,
    plan: TranscriptionPlan,
) -> tuple[list[dict[str, Any]], Any, str, str, tuple[str, str, str]]:
    # CTranslate2를 불러오기 전에 현재 Python 환경의 CUDA DLL을 등록한다.
    add_nvidia_dll_dirs()
    try:
        import faster_whisper  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper가 설치되어 있지 않습니다. "
            "requirements-transcription.txt를 사용해 설치하십시오."
        ) from exc

    last_error: Exception | None = None
    for index, (model_size, device, compute_type) in enumerate(plan.attempts):
        try:
            records, info = transcribe_once(
                audio,
                model_size,
                args.language,
                device,
                compute_type,
                args.beam_size,
                not args.no_vad,
                args.min_silence_ms,
                initial_prompt,
            )
            version = getattr(faster_whisper, "__version__", "unknown")
            return (
                records,
                info,
                f"{device}({compute_type})",
                str(version),
                (model_size, device, compute_type),
            )
        except Exception as exc:
            last_error = exc
            if index + 1 < len(plan.attempts):
                next_model, next_device, next_compute = plan.attempts[index + 1]
                hint = ""
                if device != "cpu" and any(token in str(exc).lower() for token in ("cublas", "cudnn", "cuda", "nvrtc")):
                    hint = (
                        " | CUDA 런타임 휠이 없는 환경으로 보입니다. 전사 환경에 "
                        "nvidia-cublas-cu12·nvidia-cudnn-cu12·nvidia-cuda-nvrtc-cu12 를 설치하면 GPU를 씁니다"
                        " (pipx 설치본: pipx inject gongbu-haja nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12)."
                    )
                print(
                    f"[경고] {device} 전사 실패. {next_device} {next_model}({next_compute})로 "
                    f"전체 전사를 다시 시도합니다: {exc}{hint}",
                    file=sys.stderr,
                )
                continue
            break
    raise RuntimeError(f"전사에 실패했습니다: {last_error}")


# -----------------------------------------------------------------------------
# 5. 전사 구간을 SRT·TXT·검수용 Markdown으로 변환
# 요약하지 않고 원본 시간축과 불확실 구간을 추적할 수 있게 유지한다.
# -----------------------------------------------------------------------------

def fmt_srt(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_srt(records: list[dict[str, Any]]) -> str:
    blocks = []
    for index, record in enumerate(records, start=1):
        blocks.append(
            f"{index}\n{fmt_srt(record['start'])} --> {fmt_srt(record['end'])}\n{record['text']}"
        )
    return "\n\n".join(blocks) + "\n"


def render_raw_text(records: list[dict[str, Any]]) -> str:
    return "\n".join(record["text"] for record in records).strip() + "\n"


def render_draft_markdown(identity: LectureIdentity, records: list[dict[str, Any]]) -> str:
    lines = [
        f"# {identity.lecture_id} 강의 전사 초안",
        "",
        "> 자동 전사 초안입니다. 음성 대조 전에는 교수의 확정 발언이나 직접 인용으로 사용하지 않습니다.",
        "",
    ]
    for record in records:
        lines.append(
            f"[{format_clock(record['start'])}–{format_clock(record['end'])}] "
            f"[화자 불명] {record['text']}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -----------------------------------------------------------------------------
# 6. 원본 해시·메타데이터 기록과 원자적 파일 저장
# 임시 파일을 완성한 뒤 교체하여 중간 상태의 결과물이 남지 않게 한다.
# -----------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def write_outputs(
    audio: Path,
    identity: LectureIdentity,
    paths: OutputPaths,
    records: list[dict[str, Any]],
    info: Any,
    device_used: str,
    faster_whisper_version: str,
    used_attempt: tuple[str, str, str],
    plan: TranscriptionPlan,
    args: argparse.Namespace,
) -> None:
    ensure_outputs_available(paths, args.force)

    # manifest에는 계획이 아니라 실제로 성공한 시도의 모델·티어를 기록한다.
    model_used, device_used_name, compute_used = used_attempt
    model_fallback = used_attempt != plan.attempts[0]
    if not model_fallback:
        tier_used = plan.tier
    elif (model_used, device_used_name, compute_used) == (CPU_AUTO_TIER[0], "cpu", CPU_AUTO_TIER[1]):
        tier_used = CPU_AUTO_TIER[2]
    else:
        tier_used = f"{device_used_name}_{model_used}_{compute_used}"

    segment_payload = {
        "lecture_id": identity.lecture_id,
        "source_audio": audio.name,
        "segments": records,
    }
    recording_meta = read_recording_sidecar(audio)
    manifest = {
        "source_audio": audio.name,
        "source_audio_path": str(audio),
        "source_audio_sha256": sha256_file(audio),
        "source_audio_bytes": audio.stat().st_size,
        "lecture_id": identity.lecture_id,
        "subject": identity.subject,
        "lecture_date": identity.lecture_date,
        "lecture_type": identity.lecture_type,
        "identity_inferred": identity.inferred,
        "transcription_method": (
            f"faster-whisper {faster_whisper_version}; model={model_used}; device={device_used}; "
            f"beam_size={args.beam_size}; vad={not args.no_vad}"
        ),
        "model": model_used,
        "model_selection": plan.selection,
        "model_tier": tier_used,
        "model_fallback": model_fallback,
        "language": str(getattr(info, "language", getattr(args, "language", "ko"))),
        "language_probability": float(getattr(info, "language_probability", 0.0)),
        "duration_seconds": float(getattr(info, "duration", 0.0)),
        # 녹음기가 남긴 sidecar(<stem>.recording.json)의 재생 배속. 타임스탬프는 녹음 시간 기준이고
        # 강의 시간은 타임스탬프 × playback_rate 다.
        "playback_rate": recording_meta.get("playback_rate", 1.0),
        "lecture_seconds_estimate": round(float(getattr(info, "duration", 0.0)) * float(recording_meta.get("playback_rate", 1.0)), 3),
        "recording_sidecar": recording_meta.get("_sidecar"),
        "status": "raw",
        "reviewed_against_audio": False,
        "unresolved_spans": [],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "outputs": {
            "raw_text": Path(paths.raw_text).name,
            "raw_srt": Path(paths.raw_srt).name,
            "draft_markdown": Path(paths.draft_markdown).name,
            "segments_json": Path(paths.segments_json).name,
        },
    }

    atomic_write_text(Path(paths.raw_srt), render_srt(records))
    atomic_write_text(Path(paths.raw_text), render_raw_text(records))
    atomic_write_text(Path(paths.draft_markdown), render_draft_markdown(identity, records))
    atomic_write_text(Path(paths.segments_json), json.dumps(segment_payload, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(Path(paths.manifest_json), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def read_recording_sidecar(audio: Path) -> dict:
    """record_lecture.py가 녹음 옆에 남긴 배속·장치 메타데이터를 읽는다. 없으면 빈 dict."""
    sidecar = audio.with_name(audio.stem + ".recording.json")
    if not sidecar.is_file():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("kind") != "lecture_recording_sidecar":
        return {}
    try:
        rate = float(payload.get("playback_rate", 1.0))
    except (TypeError, ValueError):
        rate = 1.0
    return {"playback_rate": rate if rate > 0 else 1.0, "_sidecar": sidecar.name}


def print_plan(
    audio: Path,
    identity: LectureIdentity,
    paths: OutputPaths,
    args: argparse.Namespace,
    plan: TranscriptionPlan,
) -> None:
    payload = {
        "audio": str(audio),
        "identity": asdict(identity),
        "model": args.model,
        "resolved_model": plan.model,
        "model_tier": plan.tier,
        "language": args.language,
        "device": args.device,
        "outputs": asdict(paths),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# -----------------------------------------------------------------------------
# 7. 명령행 진입점
# dry-run으로 이름과 출력 위치를 먼저 확인할 수 있고 기존 결과는 덮지 않는다.
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="강의 녹음을 안전한 작업 폴더에 전사하고 추적 메타데이터를 생성합니다."
    )
    parser.add_argument("audio", type=Path, help="전사할 강의 녹음 파일")
    parser.add_argument("--lecture-id", help="확정된 강의 식별자. 예: 2026-03-10_과목A_본강의")
    parser.add_argument("--subject", help="과목명. lecture-id가 없을 때 자동 이름 생성에 사용")
    parser.add_argument("--lecture-date", help="수업 날짜: YYYY-MM-DD 또는 YYMMDD")
    parser.add_argument("--lecture-type", help="본강의, 질답, 보충, 실습 등의 구분")
    parser.add_argument(
        "--model",
        default="auto",
        help="faster-whisper 모델 이름 또는 auto(GPU 메모리에 맞는 모델 자동 선택)",
    )
    parser.add_argument("--language", default="ko", help="전사 언어 코드")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true", help="무음 필터를 끔")
    parser.add_argument("--min-silence-ms", type=int, default=500)
    parser.add_argument("--glossary", type=Path, help="교안에서 추출한 전문용어 UTF-8 텍스트")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--interactive", action="store_true", help="이름 자동 추정 실패 시 강의 식별자를 질문")
    parser.add_argument("--force", action="store_true", help="기존 동일 산출물을 의도적으로 교체")
    parser.add_argument("--dry-run", action="store_true", help="전사하지 않고 식별자와 출력 경로만 표시")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.beam_size < 1 or args.min_silence_ms < 0:
        print("beam-size는 1 이상, min-silence-ms는 0 이상이어야 합니다.", file=sys.stderr)
        return 2

    audio = args.audio.expanduser().resolve()
    if not audio.is_file():
        print(f"녹음 파일을 찾을 수 없습니다: {audio}", file=sys.stderr)
        return 2
    if audio.suffix.lower() not in AUDIO_SUFFIXES:
        print(f"[경고] 일반적인 오디오 확장자가 아닙니다: {audio.suffix}", file=sys.stderr)

    try:
        identity = resolve_identity_interactively(args, audio)
        output_root = args.output_root.expanduser().resolve()
        _, paths = build_output_paths(output_root, identity)
        detected_vram = (
            detect_gpu_vram_mb() if args.model == "auto" and args.device != "cpu" else None
        )
        plan = build_transcription_plan(args.model, args.device, detected_vram)
        duration_seconds = probe_audio_duration_seconds(audio)
        for line in describe_plan(plan, duration_seconds):
            print(line)
        if args.dry_run:
            print_plan(audio, identity, paths, args, plan)
            return 0

        if (
            args.interactive
            and plan.attempts[0][1] == "cpu"
            and duration_seconds
            and duration_seconds / plan.realtime_factor > 20 * 60
        ):
            estimated = estimate_minutes(duration_seconds, plan.realtime_factor)
            try:
                answer = input(
                    f"CPU 전사는 약 {estimated}분이 걸릴 수 있습니다. 계속할까요? [Enter=계속 / n=취소]: "
                ).strip().lower()
            except EOFError:
                print("[정보] 입력 스트림이 없어 확인 없이 계속합니다.")
                answer = ""
            if answer in {"n", "no"}:
                print("[중단] 사용자가 전사를 취소했습니다.")
                return 4

        ensure_outputs_available(paths, args.force)
        initial_prompt = read_glossary(args.glossary)
        print_plan(audio, identity, paths, args, plan)
        records, info, device_used, version, used_attempt = perform_transcription(
            args, audio, initial_prompt, plan
        )
        write_outputs(
            audio, identity, paths, records, info, device_used, version, used_attempt, plan, args
        )
    except IdentityError as exc:
        print(f"[확인 필요] {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 3
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1

    print("[완료] 강의 전사 초안과 메타데이터를 생성했습니다.")
    print(f"  - 출력 폴더: {paths.output_dir}")
    if used_attempt != plan.attempts[0]:
        print(
            f"  - [주의] 계획한 {plan.attempts[0][0]}({plan.attempts[0][1]}) 대신 "
            f"{used_attempt[0]}({used_attempt[1]})로 전사됐습니다. 정확도가 낮을 수 있으니 "
            "manifest의 model_fallback 을 확인하고, GPU 환경을 고친 뒤 --force 로 다시 전사하는 것을 권합니다."
        )
    print("  - 다음 단계: 전사 검수 담당이 녹음·전사·교안을 대조해야 합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
