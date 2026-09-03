#!/usr/bin/env python3
"""강의 전사 검수용 결정적 근거·문맥 패키지를 준비한다.

이 도구는 용어를 확정하거나 전사 문장을 고치지 않는다. PDF/텍스트 자료에서
재현 가능한 후보와 근거 위치를 뽑고, segments.json의 기계적 품질 지표를
검수 후보 packet으로 묶는 역할만 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SUPPORTED_HANDOUT_SUFFIXES = {".pdf", ".txt", ".md", ".srt", ".vtt"}
EVIDENCE_EXCERPT_CAP = 140
MAX_RELATED_TERMS = 6
TOKEN_RE = re.compile(
    r"[\uac00-\ud7a3]+(?:[-·][\uac00-\ud7a3]+)*|"
    r"[A-Za-z][A-Za-z0-9]*(?:[-_/][A-Za-z0-9]+)*|"
    r"\d+(?:[./-]\d+)*"
)
TIMESTAMP_LINE_RE = re.compile(
    r"^\s*(?:\d+\s*)?(?:\d{1,3}:)?\d{2}:\d{2}(?:[,.]\d{1,3})?\s*"
    r"(?:-->|-|–|—)"
)
WEBVTT_RE = re.compile(r"^\s*WEBVTT(?:\s|$)", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(
    r"<unk>|\?{3,}|\[(?:inaudible|unintelligible|청취 불가|전사 불명확)(?:\s[^\]]+)?\]",
    re.IGNORECASE,
)
ASSESSMENT_RE = re.compile(
    r"시험|출제|중요|과제|퀴즈|중간|기말|암기|답안|점수|평가|문제|채점|범위"
)
NUMBER_RE = re.compile(
    r"\d+(?:[.,/]\d+)*\s*(?:%|퍼센트|Hz|kHz|MHz|GHz|nm|μm|um|mm|cm|m|kg|g|년|월|일|배|도|개|명)?",
    re.IGNORECASE,
)
KOREAN_STOPWORDS = {
    "강의", "수업", "내용", "설명", "부분", "경우", "정도", "것", "수", "때", "등",
    "대한", "대해", "그리고", "또는", "이것", "저것", "여기", "저기", "있는", "한다",
    "합니다", "입니다", "있다", "없다", "위해", "통해", "관련", "사용", "방법", "이후",
}


class PreparationError(RuntimeError):
    """입력에서 결정적 검수 산출물을 만들 수 없을 때 발생한다."""


@dataclass(frozen=True)
class HandoutLine:
    source: str
    source_hash: str
    source_id: str
    page: int | None
    line: int
    text: str

    def evidence(self) -> dict[str, Any]:
        location: dict[str, Any] = {"line": self.line}
        if self.page is not None:
            location["page"] = self.page
        return {
            "source_id": self.source_id,
            **location,
            "excerpt": self.text[:EVIDENCE_EXCERPT_CAP],
        }


@dataclass(frozen=True)
class Segment:
    index: int
    segment_id: Any
    start: float | None
    end: float | None
    text: str
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None
    raw: dict[str, Any]

    @property
    def duration(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        return self.end - self.start


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_id_for_hash(source_hash: str) -> str:
    """전체 경로/해시를 packet마다 반복하지 않는 안정적인 compact ID."""

    return f"src_{source_hash[:12]}"


def normalize_term(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\s+", " ", value).strip(" -–—,.;:()[]{}")
    return value


KOREAN_PARTICLES = (
    "으로부터", "에서부터", "으로", "에게", "에서", "부터", "까지", "처럼", "보다",
    "라고", "이라", "란", "은", "는", "이", "가", "을", "를", "의", "에", "로",
    "와", "과", "도", "만",
)


def _normalize_token(token: str) -> str:
    value = normalize_term(token)
    if re.search(r"[\uac00-\ud7a3]", value):
        for particle in KOREAN_PARTICLES:
            if value.endswith(particle) and len(value) > len(particle) + 1:
                return value[: -len(particle)]
    return value


def tokens(value: str) -> list[str]:
    return [_normalize_token(match.group(0)) for match in TOKEN_RE.finditer(value)]


def _nonempty_text(value: str | None) -> str:
    return (value or "").replace("\r", "").strip()


def _extract_pdf_pypdf(path: Path) -> list[str] | None:
    """pypdf로 페이지를 추출한다. 설치되지 않았거나 실패하면 None."""

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path), strict=False)
        pages: list[str] = []
        for page in reader.pages:
            pages.append(_nonempty_text(page.extract_text()))
        if any(pages):
            return pages
    except Exception:
        return None
    return None


def _extract_pdf_pdftotext(path: Path) -> list[str] | None:
    executable = shutil.which("pdftotext")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return [_nonempty_text(page) for page in result.stdout.split("\f")]


def extract_pdf_pages(path: Path) -> list[str]:
    pages = _extract_pdf_pypdf(path)
    if pages is None:
        pages = _extract_pdf_pdftotext(path)
    if pages is None or not any(page.strip() for page in pages):
        raise PreparationError(
            f"PDF에서 추출 가능한 텍스트가 없습니다(이미지형 PDF이거나 읽을 수 없음): {path}. "
            "네트워크/OCR은 사용하지 않으므로 텍스트 PDF 또는 별도 전사를 제공하십시오."
        )
    return pages


def extract_handout_lines(path: Path) -> list[HandoutLine]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PreparationError(f"교안 파일이 없습니다: {resolved}")
    suffix = resolved.suffix.casefold()
    if suffix not in SUPPORTED_HANDOUT_SUFFIXES:
        raise PreparationError(f"지원하지 않는 교안 형식입니다: {resolved.suffix}")
    source_hash = sha256_file(resolved)
    source_id = source_id_for_hash(source_hash)
    if suffix == ".pdf":
        pages = extract_pdf_pages(resolved)
        lines: list[HandoutLine] = []
        for page_number, page_text in enumerate(pages, start=1):
            for line_number, line in enumerate(page_text.splitlines(), start=1):
                lines.append(HandoutLine(str(resolved), source_hash, source_id, page_number, line_number, line.strip()))
        return lines

    try:
        text = resolved.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise PreparationError(f"교안 텍스트를 읽을 수 없습니다: {resolved}: {exc}") from exc
    lines = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        value = line.strip()
        # 자막의 시간표시·WEBVTT 헤더는 용어 후보와 근거 excerpt에서 제외한다.
        if WEBVTT_RE.match(value) or TIMESTAMP_LINE_RE.match(value) or value.isdigit():
            continue
        lines.append(HandoutLine(str(resolved), source_hash, source_id, None, line_number, value))
    return lines


def _candidate_pattern_reasons(term: str, line: str) -> list[str]:
    reasons: list[str] = []
    has_hangul = bool(re.search(r"[\uac00-\ud7a3]", term))
    has_latin = bool(re.search(r"[A-Za-z]", term))
    if has_hangul and has_latin:
        reasons.append("mixed_script")
    if re.search(r"[A-Z]{2,}", term):
        reasons.append("uppercase")
    if re.search(r"[-_/·]", term):
        reasons.append("compound_token")
    if re.search(r"\d", term):
        reasons.append("numbered_term")
    normalized_line = normalize_term(line)
    if re.search(rf"[\(\[\{{].*{re.escape(term)}.*[\)\]\}}]", normalized_line):
        reasons.append("parenthetical_or_bracketed")
    if re.search(r"(란|은|는|뜻|정의|의미|라고 한다|라고 부른다)", line):
        reasons.append("definition_cue")
    return reasons


def _candidate_terms_from_line(line: str) -> Iterable[tuple[str, list[str]]]:
    if not line or TIMESTAMP_LINE_RE.match(line):
        return
    line_tokens = tokens(line)
    for width in (1, 2, 3):
        for start in range(0, len(line_tokens) - width + 1):
            term = " ".join(line_tokens[start : start + width])
            if not term:
                continue
            parts = term.split()
            content_parts = [part for part in parts if part not in KOREAN_STOPWORDS]
            if not content_parts:
                continue
            if width == 1:
                if re.search(r"[\uac00-\ud7a3]", term) and len(term) < 2:
                    continue
                if re.fullmatch(r"[a-z]+", term) and len(term) < 3:
                    continue
            yield term, _candidate_pattern_reasons(term, line)


MAX_TERM_EVIDENCE = 2


def extract_term_candidates(lines: list[HandoutLine], max_candidates: int = 80) -> list[dict[str, Any]]:
    occurrences: dict[str, list[HandoutLine]] = defaultdict(list)
    frequencies: Counter[str] = Counter()
    pattern_reasons: dict[str, set[str]] = defaultdict(set)
    display_terms: dict[str, str] = {}
    for line in lines:
        seen_on_line: set[str] = set()
        for term, reasons in _candidate_terms_from_line(line.text):
            normalized = normalize_term(term)
            if not normalized:
                continue
            # 같은 줄에서 반복된 용어도 빈도에는 반영하되 근거 줄은 한 번만 남긴다.
            frequencies[normalized] += 1
            if normalized in seen_on_line:
                continue
            seen_on_line.add(normalized)
            occurrences[normalized].append(line)
            pattern_reasons[normalized].update(reasons)
            display_terms.setdefault(normalized, term)

    ranked: list[dict[str, Any]] = []
    for normalized, evidence_lines in occurrences.items():
        frequency = frequencies[normalized]
        reasons = set(pattern_reasons[normalized])
        if frequency >= 2:
            reasons.add("repeated")
        if not reasons:
            continue
        # 단일 일반 단어는 반복 또는 명시적 표기 패턴이 있을 때만 남긴다.
        if len(normalized.split()) == 1 and frequency < 2 and not (reasons - {"definition_cue"}):
            continue
        evidence = [line.evidence() for line in evidence_lines[:MAX_TERM_EVIDENCE]]
        ranked.append(
            {
                "term": display_terms[normalized],
                "normalized_term": normalized,
                "frequency": frequency,
                "reasons": sorted(reasons),
                "evidence": evidence,
                "evidence_truncated": max(0, len(evidence_lines) - len(evidence)),
            }
        )
    ranked.sort(key=lambda item: (-item["frequency"], -len(item["reasons"]), item["normalized_term"]))
    return ranked[:max_candidates]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_segments(path: Path) -> tuple[str, list[Segment]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PreparationError(f"segments JSON이 없습니다: {resolved}")
    source_hash = sha256_file(resolved)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"segments JSON을 읽을 수 없습니다: {resolved}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise PreparationError("segments JSON의 최상위 값에 segments 배열이 필요합니다.")
    segments: list[Segment] = []
    for index, raw in enumerate(payload["segments"]):
        if not isinstance(raw, dict):
            raise PreparationError(f"segments[{index}]가 객체가 아닙니다.")
        segments.append(
            Segment(
                index=index,
                segment_id=raw.get("id", index + 1),
                start=_float_or_none(raw.get("start")),
                end=_float_or_none(raw.get("end")),
                text=_nonempty_text(str(raw.get("text", ""))),
                avg_logprob=_float_or_none(raw.get("avg_logprob")),
                no_speech_prob=_float_or_none(raw.get("no_speech_prob")),
                compression_ratio=_float_or_none(raw.get("compression_ratio")),
                raw=raw,
            )
        )
    return source_hash, segments


def reasons_for_segment(
    segment: Segment,
    previous: Segment | None,
    short_duration: float,
    avg_logprob_threshold: float,
    no_speech_threshold: float,
    compression_ratio_threshold: float,
    timing_density_count: int,
) -> list[str]:
    reasons: list[str] = []
    duration = segment.duration
    if segment.start is None or segment.end is None or segment.start < 0 or segment.end < 0 or duration is None or duration <= 0:
        reasons.append("invalid_timing")
    elif duration < short_duration:
        reasons.append("very_short_timing")
    if timing_density_count >= 3:
        reasons.append("timing_density")
    if segment.avg_logprob is not None and segment.avg_logprob < avg_logprob_threshold:
        reasons.append("low_avg_logprob")
    if segment.no_speech_prob is not None and segment.no_speech_prob >= no_speech_threshold:
        reasons.append("high_no_speech_prob")
    if segment.compression_ratio is not None and segment.compression_ratio >= compression_ratio_threshold:
        reasons.append("high_compression_ratio")
    normalized = normalize_term(segment.text)
    if previous is not None and normalized and normalized == normalize_term(previous.text):
        reasons.append("consecutive_duplicate")
    if PLACEHOLDER_RE.search(segment.text):
        reasons.append("raw_placeholder")
    if ASSESSMENT_RE.search(segment.text):
        reasons.append("assessment_sensitive")
    if NUMBER_RE.search(segment.text):
        reasons.append("number_sensitive")
    return sorted(set(reasons))


def _timing_density(segments: list[Segment], index: int, short_duration: float) -> int:
    start = max(0, index - 2)
    end = min(len(segments), index + 3)
    return sum(
        1
        for candidate in segments[start:end]
        if candidate.duration is not None and 0 < candidate.duration < short_duration
    )


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _segment_view(segment: Segment, char_cap: int) -> dict[str, Any]:
    return {
        "index": segment.index,
        "id": segment.segment_id,
        "start": segment.start,
        "end": segment.end,
        "text": _clip(segment.text, char_cap),
    }


def _handout_matches_text(text: str, lines: list[HandoutLine], char_cap: int) -> list[dict[str, Any]]:
    segment_tokens = set(tokens(text))
    if not segment_tokens:
        return []
    scored: list[tuple[int, int, HandoutLine]] = []
    for line_index, line in enumerate(lines):
        line_tokens = set(tokens(line.text))
        overlap = len(segment_tokens & line_tokens)
        if overlap:
            scored.append((overlap, line_index, line))
    # 동일한 overlap이면 원자료에서 먼저 나온 줄을 우선한다.
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = scored[:2]
    if not selected:
        return []
    each_cap = max(1, char_cap // len(selected))
    return [
        {
            "source_id": line.source_id,
            "page": line.page,
            "line": line.line,
            "overlap_tokens": score,
            "excerpt": _clip(line.text, each_cap),
        }
        for score, _line_index, line in selected
    ]


def _handout_matches(segment: Segment, lines: list[HandoutLine], char_cap: int) -> list[dict[str, Any]]:
    """단일 segment 호환용 래퍼."""

    return _handout_matches_text(segment.text, lines, char_cap)


def _group_flagged_indices(flagged: list[int], max_targets: int) -> list[list[int]]:
    """문맥이 겹치는 인접 위험 구간을 묶고, packet당 target 수를 제한한다."""

    if not flagged:
        return []
    groups: list[list[int]] = []
    current: list[int] = [flagged[0]]
    for index in flagged[1:]:
        # 각 단일 packet이 앞뒤 segment를 문맥으로 볼 수 있으므로 간격 2까지는
        # 같은 bounded context로 묶어 중복된 이웃 문맥을 줄인다.
        if index - current[-1] <= 2:
            current.append(index)
        else:
            groups.append(current)
            current = [index]
    groups.append(current)
    chunks: list[list[int]] = []
    for group in groups:
        for start in range(0, len(group), max_targets):
            chunks.append(group[start : start + max_targets])
    return chunks


def _context_indices(
    segments: list[Segment], target_indices: list[int], full_group: list[int], all_flagged: set[int]
) -> list[int]:
    """target에 포함되지 않은, 가장 가까운 최대 두 문맥 segment를 고른다."""

    target_set = set(target_indices)
    candidates: list[int] = []
    span_start, span_end = full_group[0], full_group[-1]
    # group 내부의 unflagged gap을 먼저 보존한 뒤, 바깥 이웃을 추가한다.
    for index in range(span_start, span_end + 1):
        if index not in target_set and index not in all_flagged:
            candidates.append(index)
    for index in (span_start - 1, span_end + 1):
        if (
            0 <= index < len(segments)
            and index not in target_set
            and index not in all_flagged
            and index not in candidates
        ):
            candidates.append(index)
    return candidates[:2]


def build_review_packets(
    segments: list[Segment],
    handout_lines: list[HandoutLine],
    char_cap: int = 1200,
    short_duration: float = 0.25,
    avg_logprob_threshold: float = -1.0,
    no_speech_threshold: float = 0.6,
    compression_ratio_threshold: float = 2.4,
    max_targets_per_packet: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], Counter[str]]:
    if char_cap < 1 or max_targets_per_packet < 1:
        raise PreparationError("char_cap과 max_targets_per_packet은 1 이상이어야 합니다.")
    packets: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    reasons_by_index: dict[int, list[str]] = {}
    flagged_indices: list[int] = []
    for index, segment in enumerate(segments):
        density = _timing_density(segments, index, short_duration)
        reasons = reasons_for_segment(
            segment,
            segments[index - 1] if index else None,
            short_duration,
            avg_logprob_threshold,
            no_speech_threshold,
            compression_ratio_threshold,
            density,
        )
        reasons_by_index[index] = reasons
        reason_counts.update(reasons)
        if not reasons:
            continue
        flagged_indices.append(index)

    groups = _group_flagged_indices(flagged_indices, max_targets_per_packet)
    all_flagged = set(flagged_indices)
    # 그룹별 target을 만들 때 원래의 인접 위험 segment를 다시 문맥으로 복제하지
    # 않는다. 그룹이 max_targets_per_packet으로 나뉘는 경우에도 flagged segment는
    # 다른 packet의 target으로만 등장한다.
    for target_indices in groups:
        full_group = [target_indices[0]]
        for index in flagged_indices:
            if target_indices[0] < index <= target_indices[-1]:
                full_group.append(index)
        context_indices = _context_indices(segments, target_indices, full_group, all_flagged)
        target_text = " ".join(segments[index].text for index in target_indices)
        handout_excerpts = _handout_matches_text(target_text, handout_lines, char_cap)
        context_parts = len(target_indices) + len(context_indices) + len(handout_excerpts)
        part_cap = max(1, char_cap // max(1, context_parts))
        for item in handout_excerpts:
            item["excerpt"] = _clip(item["excerpt"], part_cap)
        target_views = [
            {
                **_segment_view(segments[index], part_cap),
                "candidate_reasons": reasons_by_index[index],
            }
            for index in target_indices
        ]
        context_views = [_segment_view(segments[index], part_cap) for index in context_indices]
        aggregate_reasons = sorted(
            {reason for index in target_indices for reason in reasons_by_index[index]}
        )
        packets.append(
            {
                "target_segment_ids": [segments[index].segment_id for index in target_indices],
                "target_segment_indices": target_indices,
                "candidate_reasons": aggregate_reasons,
                "target_segments": target_views,
                "context_segments": context_views,
                "handout_excerpts": handout_excerpts,
            }
        )

    samples: dict[str, list[dict[str, Any]]] = {"start": [], "middle": [], "end": []}
    if segments:
        selected = {"start": 0, "middle": len(segments) // 2, "end": len(segments) - 1}
        for label, index in selected.items():
            samples[label] = [
                {
                    **_segment_view(segments[index], char_cap),
                    "candidate_reasons": reasons_by_index[index],
                }
            ]
    return packets, samples, reason_counts


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _related_term_candidates(
    text: str, candidates: list[dict[str, Any]], max_terms: int = MAX_RELATED_TERMS
) -> list[dict[str, Any]]:
    """packet 문맥과 lexical overlap이 있는 후보만 작게 복사한다."""

    text_tokens = set(tokens(text))
    normalized_text = normalize_term(text)
    scored: list[tuple[int, int, str, dict[str, Any]]] = []
    for candidate in candidates:
        term = candidate["normalized_term"]
        term_tokens = set(tokens(term))
        overlap = len(text_tokens & term_tokens)
        exact = 2 if term and term in normalized_text else 0
        if overlap or exact:
            scored.append((exact + overlap, int(candidate.get("frequency", 0)), term, candidate))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    result: list[dict[str, Any]] = []
    for _score, _frequency, _term, candidate in scored[:max_terms]:
        result.append(
            {
                "term": candidate["term"],
                "normalized_term": candidate["normalized_term"],
                "frequency": candidate["frequency"],
                "reasons": candidate["reasons"],
                "evidence": candidate["evidence"],
            }
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="전사 검수용 용어 후보와 결정적 문맥 packet을 준비합니다.")
    parser.add_argument("--segments", type=Path, help="top-level segments 배열을 가진 JSON")
    parser.add_argument("--handout", type=Path, action="append", required=True, help="PDF/TXT/MD/SRT/VTT 교안(반복 지정 가능)")
    parser.add_argument("--output-dir", type=Path, required=True, help="JSON 산출물 폴더")
    parser.add_argument("--prefix", default="review", help="산출물 파일명 접두사")
    parser.add_argument("--context-char-cap", type=int, default=1200)
    parser.add_argument("--short-duration", type=float, default=0.25)
    parser.add_argument("--avg-logprob-threshold", type=float, default=-1.0)
    parser.add_argument("--no-speech-threshold", type=float, default=0.6)
    parser.add_argument("--compression-ratio-threshold", type=float, default=2.4)
    parser.add_argument("--max-candidates", type=int, default=80)
    parser.add_argument("--max-targets-per-packet", type=int, default=8)
    parser.add_argument("--max-related-terms", type=int, default=MAX_RELATED_TERMS)
    parser.add_argument("--json", action="store_true", help="산출물 경로 대신 요약 JSON을 출력")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if (
        args.context_char_cap < 1
        or args.short_duration <= 0
        or args.max_candidates < 1
        or args.max_targets_per_packet < 1
        or args.max_related_terms < 1
    ):
        raise PreparationError(
            "context-char-cap/max-candidates/max-targets-per-packet은 1 이상, "
            "short-duration은 0보다 커야 합니다."
        )
    prefix = re.sub(r"[^A-Za-z0-9가-힣_.-]+", "_", args.prefix.strip()) or "review"
    handout_lines: list[HandoutLine] = []
    source_map: dict[str, dict[str, str]] = {}
    seen_sources: set[str] = set()
    for raw_path in args.handout:
        path = raw_path.expanduser().resolve()
        if str(path) not in seen_sources:
            seen_sources.add(str(path))
            handout_lines.extend(extract_handout_lines(path))
            source_hash = sha256_file(path)
            source_id = source_id_for_hash(source_hash)
            source_map[source_id] = {"path": str(path), "sha256": source_hash}
    # global 후보는 로컬/Python cache로 남기고, packet에는 이 pool에서 현재
    # target과 겹치는 후보만 소수 복사한다.
    candidate_pool = extract_term_candidates(handout_lines, max(args.max_candidates, 10_000))
    candidates = candidate_pool[: args.max_candidates]
    term_payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "term_candidates",
        "model_input": False,
        "semantic_status": "candidates_only",
        "replacement_applied": False,
        "sources": source_map,
        "summary": {"source_count": len(source_map), "line_count": len(handout_lines), "candidate_count": len(candidates)},
        "candidates": candidates,
    }
    output_dir = args.output_dir.expanduser().resolve()
    term_path = output_dir / f"{prefix}_term_candidates.json"
    _json_write(term_path, term_payload)
    result: dict[str, Any] = {"term_candidates": str(term_path)}

    if args.segments is None:
        return result
    segments_hash, segments = load_segments(args.segments)
    packets, samples, reason_counts = build_review_packets(
        segments,
        handout_lines,
        args.context_char_cap,
        args.short_duration,
        args.avg_logprob_threshold,
        args.no_speech_threshold,
        args.compression_ratio_threshold,
        args.max_targets_per_packet,
    )
    packet_dir = output_dir / f"{prefix}_packets"
    packet_index: list[dict[str, Any]] = []
    for packet_number, packet in enumerate(packets, start=1):
        packet_id = f"packet_{packet_number:04d}"
        target_text = " ".join(
            segments[index].text for index in packet["target_segment_indices"]
        )
        related_terms = _related_term_candidates(target_text, candidate_pool, args.max_related_terms)
        source_ids = {
            item["source_id"]
            for candidate in related_terms
            for item in candidate.get("evidence", [])
            if "source_id" in item
        }
        source_ids.update(item["source_id"] for item in packet["handout_excerpts"])
        packet_document = {
            "schema_version": 1,
            "kind": "transcript_review_packet",
            "model_input": True,
            "packet_id": packet_id,
            "source_ids": sorted(source_ids),
            "target_segment_ids": packet["target_segment_ids"],
            "target_segment_indices": packet["target_segment_indices"],
            "candidate_reasons": packet["candidate_reasons"],
            "target_segments": packet["target_segments"],
            "context_segments": packet["context_segments"],
            "handout_excerpts": packet["handout_excerpts"],
            "related_term_candidates": related_terms,
        }
        packet_path = packet_dir / f"{packet_id}.json"
        _json_write(packet_path, packet_document)
        relative_path = packet_path.relative_to(output_dir).as_posix()
        packet_index.append(
            {
                "packet_id": packet_id,
                "path": relative_path,
                "target_segment_ids": packet["target_segment_ids"],
                "target_segment_indices": packet["target_segment_indices"],
                "target_reasons": [
                    target["candidate_reasons"] for target in packet["target_segments"]
                ],
                "candidate_reasons": packet["candidate_reasons"],
                "target_count": len(packet["target_segment_ids"]),
                "related_term_count": len(related_terms),
                "bytes": packet_path.stat().st_size,
            }
        )

    packet_manifest_path = output_dir / f"{prefix}_review_packet_manifest.json"
    packet_manifest = {
        "schema_version": 1,
        "kind": "transcript_review_packet_manifest",
        "model_input": False,
        "semantic_status": "anomaly_candidates_only",
        "sources": source_map,
        "source_hashes": {"segments_json": segments_hash, "handout_source_ids": sorted(source_map)},
        "summary": {
            "segment_count": len(segments),
            "packet_count": len(packet_index),
            "flagged_segment_count": len(
                {index for packet in packets for index in packet["target_segment_indices"]}
            ),
            "reason_counts": dict(sorted(reason_counts.items())),
            "quality_sample_count": sum(len(value) for value in samples.values()),
        },
        "quality_samples": samples,
        "packet_dir": packet_dir.relative_to(output_dir).as_posix(),
        "packets": packet_index,
    }
    _json_write(packet_manifest_path, packet_manifest)

    # 전체 packet body는 여기서 반복하지 않는다. 이 파일은 cache/index이며,
    # model_input=false를 명시해 agent 입력으로 직접 전달하지 않게 한다.
    packet_payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "transcript_review_packets",
        "model_input": False,
        "semantic_status": "anomaly_candidates_only",
        "replacement_applied": False,
        "source_hashes": {"segments_json": segments_hash, "handout_source_ids": sorted(source_map)},
        "thresholds": {
            "context_char_cap": args.context_char_cap,
            "short_duration": args.short_duration,
            "avg_logprob": args.avg_logprob_threshold,
            "no_speech_prob": args.no_speech_threshold,
            "compression_ratio": args.compression_ratio_threshold,
        },
        "summary": {
            "segment_count": len(segments),
            "packet_count": len(packet_index),
            "flagged_segment_count": len(
                {index for packet in packets for index in packet["target_segment_indices"]}
            ),
            "reason_counts": dict(sorted(reason_counts.items())),
            "quality_sample_count": sum(len(value) for value in samples.values()),
        },
        "packet_manifest": packet_manifest_path.relative_to(output_dir).as_posix(),
        "packet_dir": packet_dir.relative_to(output_dir).as_posix(),
    }
    packet_path = output_dir / f"{prefix}_review_packets.json"
    _json_write(packet_path, packet_payload)
    result["review_packets"] = str(packet_path)
    result["review_packet_manifest"] = str(packet_manifest_path)
    result["summary"] = packet_payload["summary"]
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except PreparationError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for name, path in result.items():
            if isinstance(path, str):
                print(f"{name}: {path}")
        if "summary" in result:
            print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
