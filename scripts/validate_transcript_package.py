#!/usr/bin/env python3
"""외부 패키지 없이 강의 전사 패키지의 결정적 조건을 검사한다."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from .project_types import AUDIO_SUFFIXES
except ImportError:  # `python scripts/validate_transcript_package.py`로 직접 실행할 때
    from project_types import AUDIO_SUFFIXES

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# -----------------------------------------------------------------------------
# 1. 지원 형식, 필수 메타데이터, 시간표시 규칙
# 음성 인식 정확도가 아니라 추적 가능한 전사 패키지의 최소 계약을 정의한다.
# -----------------------------------------------------------------------------

TRANSCRIPT_SUFFIXES = {".txt", ".md", ".markdown", ".srt", ".vtt"}
MANIFEST_FIELDS = {
    "source_audio",
    "transcription_method",
    "language",
    "status",
    "reviewed_against_audio",
    "unresolved_spans",
}
VALID_STATUSES = {
    "raw",
    "reviewed",
    "partially_audio_verified",
    "audio_verified",
    "transcript_only",
}

TIME_TOKEN = r"(?:\d{1,3}:)?\d{2}:\d{2}(?:[,.]\d{1,3})?"
RANGE_RE = re.compile(
    rf"(?P<start>{TIME_TOKEN})\s*(?:-->|[–—])\s*(?P<end>{TIME_TOKEN})"
)
PREFIX_TIME_RE = re.compile(
    rf"^\s*(?:[-*]\s*)?\[?(?P<time>{TIME_TOKEN})\]?(?:\s|$)"
)
UNCERTAINTY_RE = re.compile(
    r"\[(?:전사 불명확|청취 불가|화자 불명|확인 필요)(?:\s+[^\]]+)?\]"
    r"|\[(?:inaudible|unintelligible)(?:\s+[^\]]+)?\]",
    re.IGNORECASE,
)
RAW_PLACEHOLDER_RE = re.compile(r"<unk>|\?{3,}|\[(?:inaudible|unintelligible)\]", re.IGNORECASE)
SPEAKER_RE = re.compile(r"^(?:교수|강사|학생|화자\s*\d+|speaker\s*\d+)\s*[:：]\s*", re.IGNORECASE)


# -----------------------------------------------------------------------------
# 2. 오류·경고와 전사 품질 지표 수집
# 자동화는 결과를 JSON으로 읽을 수 있고 사람은 같은 내용을 텍스트로 본다.
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
        self.metrics: dict[str, int | str | bool] = {}

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
# 3. 타임스탬프와 전사 본문 검사
# 시간 역전, 비정상 구간, 반복, 무음 환각 후보, 불확실성 표지를 찾는다.
# -----------------------------------------------------------------------------

def parse_timestamp(token: str) -> float | None:
    normalized = token.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        if len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds = float(parts[1])
        else:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
    except ValueError:
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def read_transcript(path: Path, report: Report) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        report.add("error", "encoding", f"전사본이 UTF-8이 아닙니다: {exc}", path)
    except OSError as exc:
        report.add("error", "read", f"전사본을 읽을 수 없습니다: {exc}", path)
    return None


def extract_timestamps(text: str, path: Path, report: Report) -> list[tuple[int, float, float | None]]:
    entries: list[tuple[int, float, float | None]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        range_match = RANGE_RE.search(line)
        if range_match:
            start = parse_timestamp(range_match.group("start"))
            end = parse_timestamp(range_match.group("end"))
            if start is None or end is None:
                report.add("error", "invalid-timestamp", "해석할 수 없는 시간 범위입니다.", f"{path}:{line_number}")
                continue
            if end < start:
                report.add("error", "reversed-range", "종료 시간이 시작 시간보다 빠릅니다.", f"{path}:{line_number}")
            entries.append((line_number, start, end))
            continue

        prefix_match = PREFIX_TIME_RE.search(line)
        if prefix_match:
            value = parse_timestamp(prefix_match.group("time"))
            if value is None:
                report.add("error", "invalid-timestamp", "해석할 수 없는 시간표시입니다.", f"{path}:{line_number}")
                continue
            entries.append((line_number, value, None))

    previous = -1.0
    for line_number, start, _ in entries:
        if start < previous:
            report.add("error", "timestamp-regression", "시간표시가 앞 구간보다 뒤로 이동합니다.", f"{path}:{line_number}")
        previous = max(previous, start)
    return entries


def normalized_content_line(line: str) -> str:
    value = RANGE_RE.sub("", line)
    value = PREFIX_TIME_RE.sub("", value)
    value = SPEAKER_RE.sub("", value.strip())
    value = re.sub(r"\s+", " ", value).strip(" -\t")
    if value.isdigit():
        return ""
    return value


def check_repetition_and_segmentation(text: str, path: Path, report: Report) -> None:
    previous = ""
    duplicate_lines = 0
    long_lines = 0
    for line in text.splitlines():
        normalized = normalized_content_line(line)
        if len(normalized) >= 15 and normalized == previous:
            duplicate_lines += 1
        if len(normalized) > 1000:
            long_lines += 1
        if normalized:
            previous = normalized
    report.metrics["consecutive_duplicate_lines"] = duplicate_lines
    report.metrics["lines_over_1000_characters"] = long_lines
    if duplicate_lines:
        report.add("warning", "repeated-segment", f"연속 중복 문장이 {duplicate_lines}개 있습니다. ASR 반복 여부를 확인하십시오.", path)
    if long_lines:
        report.add("warning", "poor-segmentation", f"1,000자를 넘는 줄이 {long_lines}개 있습니다. 시간축 분할을 확인하십시오.", path)


# -----------------------------------------------------------------------------
# 4. 원본 녹음과 manifest 교차 검사
# 파일명만 믿지 않고 원본 해시·상태·음성 검수 범위가 일치하는지 확인한다.
# -----------------------------------------------------------------------------

def validate_audio(path: Path | None, report: Report) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    report.metrics["audio_path"] = str(resolved)
    if not resolved.is_file():
        report.add("error", "missing-audio", "연결된 녹음 파일이 없습니다.", resolved)
        return None
    report.metrics["audio_bytes"] = resolved.stat().st_size
    if resolved.stat().st_size == 0:
        report.add("error", "empty-audio", "녹음 파일이 비어 있습니다.", resolved)
    if resolved.suffix.lower() not in AUDIO_SUFFIXES:
        report.add("warning", "unrecognized-audio-format", f"일반적인 녹음 형식이 아닙니다: {resolved.suffix}", resolved)
    return resolved


def load_manifest(path: Path | None, audio: Path | None, uncertainty_count: int, report: Report) -> None:
    if path is None:
        report.add("warning", "missing-manifest", "전사 메타데이터 JSON이 지정되지 않았습니다.")
        return
    resolved = path.expanduser().resolve()
    report.metrics["manifest_path"] = str(resolved)
    if not resolved.is_file():
        report.add("error", "missing-manifest", "전사 메타데이터 JSON이 없습니다.", resolved)
        return
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.add("error", "invalid-manifest", f"메타데이터 JSON을 읽을 수 없습니다: {exc}", resolved)
        return
    if not isinstance(payload, dict):
        report.add("error", "invalid-manifest", "메타데이터 최상위 값은 객체여야 합니다.", resolved)
        return

    missing = sorted(MANIFEST_FIELDS - payload.keys())
    for field in missing:
        report.add("error", "missing-manifest-field", f"필수 필드가 없습니다: {field}", resolved)

    method = payload.get("transcription_method")
    if method is not None and (not isinstance(method, str) or not method.strip()):
        report.add("error", "invalid-method", "transcription_method는 비어 있지 않은 문자열이어야 합니다.", resolved)
    language = payload.get("language")
    if language is not None and (not isinstance(language, str) or not language.strip()):
        report.add("error", "invalid-language", "language는 비어 있지 않은 문자열이어야 합니다.", resolved)

    status = payload.get("status")
    if status is not None and status not in VALID_STATUSES:
        report.add("error", "invalid-status", f"허용되지 않은 전사 상태입니다: {status}", resolved)
    elif isinstance(status, str):
        report.metrics["transcript_status"] = status

    reviewed = payload.get("reviewed_against_audio")
    if reviewed is not None and not isinstance(reviewed, bool):
        report.add("error", "invalid-review-flag", "reviewed_against_audio는 true 또는 false여야 합니다.", resolved)
    if status in {"partially_audio_verified", "audio_verified"} and reviewed is not True:
        report.add("error", "verification-conflict", f"{status} 상태는 reviewed_against_audio=true여야 합니다.", resolved)
    if status == "transcript_only" and reviewed is True:
        report.add("error", "verification-conflict", "transcript_only 상태는 음성 검수 완료로 표시할 수 없습니다.", resolved)

    unresolved = payload.get("unresolved_spans")
    if unresolved is not None and not isinstance(unresolved, list):
        report.add("error", "invalid-unresolved-spans", "unresolved_spans는 배열이어야 합니다.", resolved)
    elif isinstance(unresolved, list):
        report.metrics["manifest_unresolved_spans"] = len(unresolved)
        if uncertainty_count and not unresolved:
            report.add("warning", "untracked-uncertainty", "전사본에 불확실성 표지가 있지만 manifest의 unresolved_spans가 비어 있습니다.", resolved)

    source_audio = payload.get("source_audio")
    if audio is not None:
        if not isinstance(source_audio, str) or not source_audio.strip():
            report.add("error", "missing-audio-reference", "녹음이 지정됐지만 manifest의 source_audio가 비어 있습니다.", resolved)
        elif Path(source_audio).name.casefold() != audio.name.casefold():
            report.add("warning", "audio-name-mismatch", f"manifest의 녹음명({source_audio})과 검사 대상({audio.name})이 다릅니다.", resolved)
        if reviewed is False:
            report.add("warning", "audio-not-reviewed", "녹음은 연결됐지만 음성 대조 검수가 완료되지 않았습니다.", resolved)
        if status == "transcript_only":
            report.add("warning", "verification-conflict", "녹음이 연결됐지만 상태가 transcript_only입니다. 검수 전이면 reviewed로 표시하십시오.", resolved)
    elif source_audio is not None and source_audio != "":
        report.add("warning", "audio-not-supplied", "manifest에는 녹음이 있으나 --audio로 검사하지 않았습니다.", resolved)


# -----------------------------------------------------------------------------
# 5. 전체 검사 실행과 명령행 인터페이스
# strict 모드에서는 경고도 실패로 처리해 다음 에이전트 단계 진입을 막을 수 있다.
# -----------------------------------------------------------------------------

def validate(args: argparse.Namespace) -> Report:
    report = Report()
    transcript = args.transcript.expanduser().resolve()
    report.metrics["transcript_path"] = str(transcript)
    if not transcript.is_file():
        report.add("error", "missing-transcript", "전사본 파일이 없습니다.", transcript)
        return report
    report.metrics["transcript_bytes"] = transcript.stat().st_size
    suffix = transcript.suffix.lower()
    if suffix not in TRANSCRIPT_SUFFIXES:
        report.add("error", "unsupported-transcript-format", f"지원하지 않는 전사 형식입니다: {suffix}", transcript)
        return report

    text = read_transcript(transcript, report)
    if text is None:
        return report
    visible = re.sub(r"\s+", " ", text).strip()
    report.metrics["characters"] = len(visible)
    if not visible:
        report.add("error", "empty-transcript", "전사본에 내용이 없습니다.", transcript)
    elif len(visible) < args.min_characters:
        report.add("warning", "short-transcript", f"전사본이 권장 최소 길이보다 짧습니다: {len(visible)} < {args.min_characters}", transcript)

    timestamps = extract_timestamps(text, transcript, report)
    report.metrics["timestamp_entries"] = len(timestamps)
    require_timestamps = args.require_timestamps or suffix in {".srt", ".vtt"}
    if not timestamps:
        severity = "error" if require_timestamps else "warning"
        report.add(severity, "missing-timestamps", "전사본에서 시간표시를 찾지 못했습니다.", transcript)

    uncertainty_count = len(UNCERTAINTY_RE.findall(text))
    placeholder_count = len(RAW_PLACEHOLDER_RE.findall(text))
    report.metrics["uncertainty_markers"] = uncertainty_count
    report.metrics["raw_placeholders"] = placeholder_count
    if placeholder_count:
        report.add("warning", "raw-placeholder", f"정리되지 않은 ASR 표지가 {placeholder_count}개 있습니다.", transcript)

    check_repetition_and_segmentation(text, transcript, report)
    audio = validate_audio(args.audio, report)
    load_manifest(args.manifest, audio, uncertainty_count, report)
    return report


def print_report(report: Report, as_json: bool) -> None:
    payload = {
        "status": "fail" if report.errors else "pass",
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "metrics": report.metrics,
        "issues": [asdict(issue) for issue in report.issues],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for issue in report.issues:
        location = f" ({issue.location})" if issue.location else ""
        print(f"[{issue.severity.upper()}] {issue.code}: {issue.message}{location}")
    metrics = ", ".join(f"{key}={value}" for key, value in report.metrics.items())
    print(f"검증 결과: {payload['status'].upper()} | 오류 {payload['errors']}개 | 경고 {payload['warnings']}개")
    if metrics:
        print(f"측정값: {metrics}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="강의 전사본·녹음·메타데이터의 기본 무결성을 검증합니다.")
    parser.add_argument("transcript", type=Path, help="검증할 TXT, Markdown, SRT 또는 VTT 전사본")
    parser.add_argument("--audio", type=Path, help="전사에 연결된 원본 녹음")
    parser.add_argument("--manifest", type=Path, help="전사 메타데이터 JSON")
    parser.add_argument("--min-characters", type=int, default=200, help="권장 최소 전사 글자 수")
    parser.add_argument("--require-timestamps", action="store_true", help="시간표시가 없으면 오류로 처리")
    parser.add_argument("--strict", action="store_true", help="경고도 실패로 처리")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_characters < 0:
        print("min-characters는 0 이상이어야 합니다.", file=sys.stderr)
        return 2
    report = validate(args)
    print_report(report, args.json)
    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
