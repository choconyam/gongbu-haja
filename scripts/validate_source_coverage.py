#!/usr/bin/env python3
"""source map과 coverage 보고서의 결정적 계약을 검증한다.

이 도구는 어떤 자료를 실제로 노트에 넣을지 의미 판단하지 않는다. 이미 작성된
source_unit_id와 coverage decision이 서로 완전하게 대응하고, 각 decision의
필수 근거 필드가 있는지만 검사한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SOURCE_MAP_KIND = "study_note_source_map"
COVERAGE_KIND = "study_note_source_coverage"
SCHEMA_VERSION = 1
NOTE_MODES = {"faithful", "deep"}
REVIEWER_PROFILES = {"faithful": "economy_max", "deep": "quality_xhigh"}
DECISIONS = {"included", "merged", "excluded", "unresolved"}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    location: str | None = None


class Report:
    def __init__(self) -> None:
        self.issues: list[Issue] = []
        self.summary: dict[str, Any] = {}
        self.source_ids: list[str] = []

    def add(self, code: str, message: str, location: str | None = None) -> None:
        self.issues.append(Issue(code, message, location))

    @property
    def errors(self) -> list[Issue]:
        return self.issues


class CoverageValidationError(ValueError):
    """검증 실패를 호출자가 상세 오류와 함께 처리할 수 있게 전달한다."""

    def __init__(self, report: Report) -> None:
        self.report = report
        details = "\n".join(
            f"{issue.code}: {issue.message}" for issue in report.errors
        )
        super().__init__(details or "source coverage validation failed")


def read_json(path: Path, report: Report, label: str) -> dict[str, Any] | None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        report.add("missing-file", f"{label} 파일이 없습니다: {resolved}", str(resolved))
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.add("invalid-json", f"{label} JSON을 읽을 수 없습니다: {exc}", str(resolved))
        return None
    if not isinstance(payload, dict):
        report.add("invalid-root", f"{label} 최상위 값은 객체여야 합니다.", str(resolved))
        return None
    return payload


def check_header(
    payload: dict[str, Any],
    expected_kind: str,
    label: str,
    report: Report,
) -> None:
    if payload.get("kind") != expected_kind:
        report.add(
            "invalid-kind",
            f"{label} kind가 {expected_kind!r}이어야 합니다: {payload.get('kind')!r}",
            label,
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        report.add(
            "invalid-schema-version",
            f"{label} schema_version이 {SCHEMA_VERSION}이어야 합니다: {payload.get('schema_version')!r}",
            label,
        )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_source_map(payload: dict[str, Any], report: Report) -> list[str]:
    check_header(payload, SOURCE_MAP_KIND, "source_map", report)
    units = payload.get("source_units")
    if not isinstance(units, list):
        report.add("invalid-source-units", "source_map source_units는 배열이어야 합니다.", "source_map.source_units")
        return []
    if not units:
        report.add("empty-source-units", "source_map source_units는 비어 있을 수 없습니다.", "source_map.source_units")
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for index, unit in enumerate(units):
        location = f"source_map.source_units[{index}]"
        if not isinstance(unit, dict):
            report.add("invalid-source-unit", "source unit은 객체여야 합니다.", location)
            continue
        source_unit_id = unit.get("source_unit_id")
        if not _nonempty_string(source_unit_id):
            report.add("invalid-source-unit-id", "source_unit_id는 비어 있지 않은 문자열이어야 합니다.", location)
            continue
        if source_unit_id in seen:
            report.add("duplicate-source-unit-id", f"source_unit_id가 중복됩니다: {source_unit_id}", location)
        else:
            seen.add(source_unit_id)
        ids.append(source_unit_id)
    return ids


def _validate_nonempty_string_array(
    value: Any, field: str, location: str, report: Report
) -> bool:
    if not isinstance(value, list) or not value or not all(_nonempty_string(item) for item in value):
        report.add("invalid-required-field", f"{field}는 비어 있지 않은 문자열 배열이어야 합니다.", location)
        return False
    return True


def _validate_coverage_payload(payload: dict[str, Any], report: Report) -> list[str]:
    check_header(payload, COVERAGE_KIND, "coverage", report)
    # 집계는 입력 payload의 임의 summary를 신뢰하지 않고 아래 item에서만 만든다.
    report.summary.setdefault("decision_counts", {})
    note_mode = payload.get("note_mode")
    if not isinstance(note_mode, str) or note_mode not in NOTE_MODES:
        report.add("invalid-note-mode", f"note_mode는 faithful 또는 deep이어야 합니다: {note_mode!r}", "coverage.note_mode")
    reviewer_profile = payload.get("reviewer_profile")
    expected_profile = REVIEWER_PROFILES.get(note_mode) if isinstance(note_mode, str) else None
    if not isinstance(reviewer_profile, str) or not reviewer_profile.strip():
        report.add("invalid-reviewer-profile", "reviewer_profile은 비어 있지 않은 문자열이어야 합니다.", "coverage.reviewer_profile")
    elif expected_profile is not None and reviewer_profile != expected_profile:
        report.add(
            "reviewer-profile-mismatch",
            f"{note_mode} 모드의 reviewer_profile은 {expected_profile!r}이어야 합니다: {reviewer_profile!r}",
            "coverage.reviewer_profile",
        )
    items = payload.get("items")
    if not isinstance(items, list):
        report.add("invalid-coverage-items", "coverage items는 배열이어야 합니다.", "coverage.items")
        return []
    if not items:
        report.add("empty-coverage-items", "coverage items는 비어 있을 수 없습니다.", "coverage.items")
        return []
    ids: list[str] = []
    seen: set[str] = set()
    decision_counts: Counter[str] = Counter()
    for index, item in enumerate(items):
        location = f"coverage.items[{index}]"
        if not isinstance(item, dict):
            report.add("invalid-coverage-item", "coverage item은 객체여야 합니다.", location)
            continue
        source_unit_id = item.get("source_unit_id")
        if not _nonempty_string(source_unit_id):
            report.add("invalid-coverage-source-unit-id", "source_unit_id는 비어 있지 않은 문자열이어야 합니다.", location)
        else:
            ids.append(source_unit_id)
            if source_unit_id in seen:
                report.add("duplicate-coverage-source-unit-id", f"coverage source_unit_id가 중복됩니다: {source_unit_id}", location)
            else:
                seen.add(source_unit_id)
        decision = item.get("decision")
        if not isinstance(decision, str) or decision not in DECISIONS:
            report.add("invalid-decision", f"decision은 {', '.join(sorted(DECISIONS))} 중 하나여야 합니다: {decision!r}", location)
            continue
        decision_counts[decision] += 1
        if decision in {"included", "merged"}:
            _validate_nonempty_string_array(item.get("note_refs"), "note_refs", location, report)
        elif decision == "excluded":
            if not _nonempty_string(item.get("reason")):
                report.add("invalid-required-field", "excluded decision에는 비어 있지 않은 reason이 필요합니다.", location)
        elif decision == "unresolved":
            if not _nonempty_string(item.get("reason")):
                report.add("invalid-required-field", "unresolved decision에는 비어 있지 않은 reason이 필요합니다.", location)
            _validate_nonempty_string_array(item.get("note_refs"), "note_refs", location, report)
    report.summary["decision_counts"] = dict(sorted(decision_counts.items()))
    return ids


def compare_ids(source_ids: list[str], coverage_ids: list[str], report: Report) -> None:
    source_counter = Counter(source_ids)
    coverage_counter = Counter(coverage_ids)
    source_set = set(source_ids)
    coverage_set = set(coverage_ids)
    for source_unit_id in sorted(source_set - coverage_set):
        report.add("missing-coverage", f"coverage에 source_unit_id가 없습니다: {source_unit_id}", "coverage.items")
    for source_unit_id in sorted(coverage_set - source_set):
        report.add("additional-coverage", f"source_map에 없는 source_unit_id입니다: {source_unit_id}", "coverage.items")
    for source_unit_id, count in sorted(source_counter.items()):
        if count > 1:
            # validate_source_map에서 위치가 있는 중복 오류를 이미 남겼지만,
            # 집계상 exact-once 계약도 명시한다.
            report.add("source-id-not-exactly-once", f"source_map ID가 정확히 한 번이 아닙니다: {source_unit_id} ({count})", "source_map.source_units")
    for source_unit_id, count in sorted(coverage_counter.items()):
        if count > 1:
            report.add("coverage-id-not-exactly-once", f"coverage ID가 정확히 한 번이 아닙니다: {source_unit_id} ({count})", "coverage.items")


def validate(source_map_path: Path, coverage_path: Path) -> Report:
    report = Report()
    source_payload = read_json(source_map_path, report, "source_map")
    coverage_payload = read_json(coverage_path, report, "coverage")
    source_ids = validate_source_map(source_payload, report) if source_payload is not None else []
    report.source_ids = source_ids
    coverage_ids = _validate_coverage_payload(coverage_payload, report) if coverage_payload is not None else []
    if source_payload is not None and coverage_payload is not None:
        compare_ids(source_ids, coverage_ids, report)
    report.summary.setdefault("source_unit_count", len(set(source_ids)))
    report.summary.setdefault("coverage_item_count", len(coverage_ids))
    return report


def validate_coverage(
    source_map_path: Path, coverage_path: Path
) -> tuple[list[str], dict[str, int]]:
    """파일을 쓰지 않고 source map과 coverage report를 검증한다.

    성공하면 source map의 source_unit_id 목록과 평탄화된 정수 집계를 반환한다.
    계약 위반이나 입력 오류는 :class:`CoverageValidationError`로 전달하며,
    예외의 ``report`` 속성에서 개별 오류를 확인할 수 있다.
    """

    report = validate(source_map_path, coverage_path)
    if report.errors:
        raise CoverageValidationError(report)
    decision_counts = report.summary.get("decision_counts", {})
    counts: dict[str, int] = {
        "source_unit_count": int(report.summary.get("source_unit_count", 0)),
        "coverage_item_count": int(report.summary.get("coverage_item_count", 0)),
    }
    for decision in sorted(DECISIONS):
        counts[f"decision_{decision}_count"] = int(decision_counts.get(decision, 0))
    return list(report.source_ids), counts


def report_payload(report: Report) -> dict[str, Any]:
    return {
        "status": "fail" if report.errors else "pass",
        "summary": report.summary,
        "errors": [asdict(issue) for issue in report.errors],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="학습노트 source map과 coverage report의 결정적 계약을 검증합니다.")
    parser.add_argument("source_map", type=Path)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON으로 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_ids, counts = validate_coverage(args.source_map, args.coverage)
    except CoverageValidationError as exc:
        report = exc.report
    else:
        report = Report()
        report.summary.update(counts)
        report.summary["decision_counts"] = {
            decision: counts[f"decision_{decision}_count"]
            for decision in sorted(DECISIONS)
            if counts[f"decision_{decision}_count"]
        }
    payload = report_payload(report)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for issue in report.errors:
            location = f" ({issue.location})" if issue.location else ""
            print(f"[ERROR] {issue.code}: {issue.message}{location}")
        if report.errors:
            print(f"검증 결과: FAIL | 오류 {len(report.errors)}개")
        else:
            summary = ", ".join(f"{key}={value}" for key, value in report.summary.items())
            print(f"검증 결과: PASS | {summary}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
