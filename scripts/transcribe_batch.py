#!/usr/bin/env python3
"""여러 강의 녹음을 한 번에 하나씩 순서대로 전사하는 배치 큐.

전사 모델 하나가 GPU 메모리를 사실상 독점하므로 동시 전사는 지원하지 않는다.
강의 식별 확인은 대기열 시작 시점에 모두 끝내(사전 점검), 무인 실행 중에
입력 대기로 큐가 멈추는 일이 없게 한다. 한 파일이 실패해도 다음 파일을
계속 처리한 뒤 마지막에 결과를 요약한다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from .project_types import AUDIO_SUFFIXES
except ImportError:  # `python scripts/transcribe_batch.py`로 직접 실행할 때
    from project_types import AUDIO_SUFFIXES

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


TRANSCRIBE_SCRIPT = Path(__file__).with_name("transcribe_lecture.py")

# transcribe_lecture.py의 종료 코드를 사람이 읽는 결과로 옮긴다.
# 2는 강의 식별 실패 외에 인자 검증 실패·녹음 파일 없음에도 쓰이므로 단정하지 않는다.
OUTCOME_LABELS = {
    0: "성공",
    1: "오류",
    2: "강의 식별·인자 확인 필요",
    3: "기존 산출물 있음(건너뜀)",
    4: "사용자 취소",
}
SKIPPED_LABEL = "미실행(중단됨)"
IDENTITY_MARKER = "[확인 필요]"


def collect_audio_files(raw_inputs: list[str]) -> tuple[list[Path], list[Path], list[Path]]:
    """파일·폴더 인자 목록에서 전사 대상을 모은다.

    명시적으로 지정한 파일은 확장자와 무관하게 포함하고, 폴더는 알려진
    녹음·녹화 확장자만 재귀 수집한다. 오디오가 하나도 없는 폴더는 사용자가
    엉뚱한 폴더를 지정한 신호일 수 있어 별도로 보고한다.
    """
    files: list[Path] = []
    missing: list[Path] = []
    no_audio_dirs: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_inputs:
        path = Path(raw).expanduser()
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(
                (item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES),
                key=lambda item: item.as_posix().lower(),
            )
            if not candidates:
                no_audio_dirs.append(path)
                continue
        else:
            missing.append(path)
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    return files, missing, no_audio_dirs


def build_passthrough(args: argparse.Namespace) -> list[str]:
    """사용자가 실제로 지정한 옵션만 transcribe_lecture.py로 전달한다.

    --interactive는 전달하지 않는다. 식별 확인은 배치가 시작 시점에 직접
    처리하며, 자식 프로세스의 입력 대기(식별자·CPU 장시간 확인)로 무인
    대기열이 멈추는 일을 막는다.
    """
    passthrough: list[str] = []
    for option, value in (
        ("--model", args.model),
        ("--language", args.language),
        ("--device", args.device),
        ("--beam-size", args.beam_size),
        ("--min-silence-ms", args.min_silence_ms),
        ("--glossary", args.glossary),
        ("--output-root", args.output_root),
    ):
        if value is not None:
            passthrough.extend([option, str(value)])
    for option, enabled in (
        ("--no-vad", args.no_vad),
        ("--force", args.force),
    ):
        if enabled:
            passthrough.append(option)
    return passthrough


def prompt_lecture_id(audio: Path) -> str:
    print(f"{IDENTITY_MARKER} {audio.name}: 파일명에서 강의를 식별하지 못했습니다.")
    print("예: 2026-03-10_과목A_본강의 (이 파일을 건너뛰려면 빈 입력)")
    try:
        return input("강의 식별자: ").strip()
    except EOFError:
        return ""


def preflight(
    files: list[Path],
    passthrough: list[str],
    interactive: bool,
) -> tuple[list[tuple[Path, str | None]], list[tuple[Path, int | None]]]:
    """대기열 시작 전에 각 파일의 식별 가능 여부를 확인한다.

    반환: (실행할 파일과 확정 식별자 목록, 사전 점검에서 탈락한 결과 목록)
    """
    runnable: list[tuple[Path, str | None]] = []
    outcomes: list[tuple[Path, int | None]] = []
    for audio in files:
        command = [sys.executable, str(TRANSCRIBE_SCRIPT), str(audio), *passthrough, "--dry-run"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
        except KeyboardInterrupt:
            print("\n[중단] 사용자가 사전 점검을 중단했습니다.", file=sys.stderr)
            outcomes.append((audio, None))
            outcomes.extend((remaining, None) for remaining in files[files.index(audio) + 1 :])
            return runnable, outcomes
        if result.returncode == 0:
            runnable.append((audio, None))
            continue
        stderr_text = (result.stderr or "").strip()
        if result.returncode == 2 and IDENTITY_MARKER in stderr_text and interactive:
            lecture_id = prompt_lecture_id(audio)
            if lecture_id:
                runnable.append((audio, lecture_id))
                continue
        if stderr_text:
            print(f"[사전 점검] {audio.name}:", file=sys.stderr)
            print(stderr_text, file=sys.stderr)
        outcomes.append((audio, result.returncode))
    return runnable, outcomes


def run_batch(
    runnable: list[tuple[Path, str | None]],
    passthrough: list[str],
) -> list[tuple[Path, int | None]]:
    outcomes: list[tuple[Path, int | None]] = []
    total = len(runnable)
    for index, (audio, lecture_id) in enumerate(runnable, start=1):
        print()
        print("=" * 72)
        print(f"[{index}/{total}] {audio}")
        print("=" * 72)
        command = [sys.executable, str(TRANSCRIBE_SCRIPT), str(audio), *passthrough]
        if lecture_id:
            command.extend(["--lecture-id", lecture_id])
        try:
            result = subprocess.run(command, check=False)
        except KeyboardInterrupt:
            print("\n[중단] 사용자가 배치 전사를 중단했습니다.", file=sys.stderr)
            outcomes.append((audio, None))
            outcomes.extend((remaining, None) for remaining, _ in runnable[index:])
            return outcomes
        outcomes.append((audio, result.returncode))
    return outcomes


def print_summary(
    outcomes: list[tuple[Path, int | None]],
    missing: list[Path],
    no_audio_dirs: list[Path],
) -> int:
    print()
    print("=" * 72)
    print("배치 전사 요약")
    print("=" * 72)
    for audio, code in outcomes:
        label = SKIPPED_LABEL if code is None else OUTCOME_LABELS.get(code, f"오류(코드 {code})")
        print(f"  [{label}] {audio}")
    for path in missing:
        print(f"  [입력 없음] {path}")
    for path in no_audio_dirs:
        print(f"  [오디오 없음 폴더] {path}")

    needs_attention = [audio for audio, code in outcomes if code == 2]
    if needs_attention:
        print()
        print("확인이 필요한 파일은 위 사전 점검·실행 출력의 오류 내용을 먼저 확인하십시오.")
        print("강의 식별 실패라면 --lecture-id를 지정해 개별 실행하거나,")
        print("배치에 --interactive를 붙이면 대기열 시작 시점에 식별자를 물어봅니다.")

    acceptable = {0, 3}
    failed = bool(missing or no_audio_dirs) or any(
        code is None or code not in acceptable for _, code in outcomes
    )
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="여러 강의 녹음 파일 또는 폴더를 한 번에 하나씩 순서대로 전사합니다."
    )
    parser.add_argument("inputs", nargs="+", help="전사할 녹음 파일들 또는 녹음이 든 폴더")
    parser.add_argument("--model", help="faster-whisper 모델 이름 또는 auto(기본값)")
    parser.add_argument("--language", help="전사 언어 코드")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), help="전사 장치")
    parser.add_argument("--beam-size", type=int, help="빔 서치 크기")
    parser.add_argument("--min-silence-ms", type=int, help="무음 분할 기준(밀리초)")
    parser.add_argument("--glossary", type=Path, help="교안에서 추출한 전문용어 UTF-8 텍스트")
    parser.add_argument("--output-root", type=Path, help="전사 산출물 최상위 폴더")
    parser.add_argument("--no-vad", action="store_true", help="무음 필터를 끔")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="식별 실패 파일의 강의 식별자를 대기열 시작 시점에 한꺼번에 질문",
    )
    parser.add_argument("--force", action="store_true", help="기존 동일 산출물을 의도적으로 교체")
    parser.add_argument("--dry-run", action="store_true", help="전사 없이 각 파일의 식별자와 출력 경로만 표시")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files, missing, no_audio_dirs = collect_audio_files(args.inputs)
    if not files:
        for path in missing:
            print(f"[오류] 입력 경로가 없습니다: {path}", file=sys.stderr)
        for path in no_audio_dirs:
            print(f"[오류] 폴더에 지원하는 녹음 파일이 없습니다: {path}", file=sys.stderr)
        print("전사할 녹음 파일을 찾지 못했습니다.", file=sys.stderr)
        return 2

    print(f"전사 대기열: {len(files)}개 파일 (GPU 충돌 방지를 위해 한 번에 하나씩 처리)")
    for audio in files:
        print(f"  - {audio}")

    passthrough = build_passthrough(args)
    runnable, outcomes = preflight(files, passthrough, args.interactive)
    if any(code is None for _, code in outcomes):  # 사전 점검 단계에서 중단됨
        return print_summary(outcomes, missing, no_audio_dirs)

    if args.dry_run:
        for audio, lecture_id in runnable:
            command = [sys.executable, str(TRANSCRIBE_SCRIPT), str(audio), *passthrough, "--dry-run"]
            if lecture_id:
                command.extend(["--lecture-id", lecture_id])
            result = subprocess.run(command, check=False)
            outcomes.append((audio, result.returncode))
    else:
        outcomes.extend(run_batch(runnable, passthrough))

    ordered = {audio: code for audio, code in outcomes}
    outcomes = [(audio, ordered[audio]) for audio in files if audio in ordered]
    return print_summary(outcomes, missing, no_audio_dirs)


if __name__ == "__main__":
    sys.exit(main())
