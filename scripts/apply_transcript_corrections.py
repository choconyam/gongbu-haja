#!/usr/bin/env python3
"""승인된 구간별 전사 교정 결정을 원본 보존 방식으로 적용한다.

이 도구는 교정어를 추론하지 않는다. 검수 에이전트가 지정한 구간 ID와
정확한 현재 원문이 실제 segments JSON과 일치할 때만 대체문을 적용한다.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


VALID_ACTIONS = {"replace", "keep", "unresolved"}
VALID_VERIFICATION = {"audio", "handout", "context", "multiple", "unverified"}


class CorrectionError(RuntimeError):
    """교정 결정이 오래됐거나 안전하게 적용할 수 없을 때 발생한다."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CorrectionError(f"{label} 파일이 없습니다: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorrectionError(f"{label} JSON을 읽을 수 없습니다: {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorrectionError(f"{label} JSON의 최상위 값은 객체여야 합니다.")
    return payload


def segment_key(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        raise CorrectionError(f"잘못된 구간 ID입니다: {value!r}")
    return f"{type(value).__name__}:{value}"


def validate_segments(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise CorrectionError("segments JSON의 최상위 값에 segments 배열이 필요합니다.")
    by_id: dict[str, dict[str, Any]] = {}
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise CorrectionError(f"segments[{index}]가 객체가 아닙니다.")
        identifier = segment.get("id", index + 1)
        key = segment_key(identifier)
        if key in by_id:
            raise CorrectionError(f"중복 구간 ID가 있습니다: {identifier!r}")
        if not isinstance(segment.get("text"), str):
            raise CorrectionError(f"구간 {identifier!r}의 text가 문자열이 아닙니다.")
        by_id[key] = segment
    return segments, by_id


def validate_decisions(
    payload: dict[str, Any],
    source_hash: str,
    segments_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    recorded_hash = payload.get("source_segments_sha256")
    if recorded_hash != source_hash:
        raise CorrectionError(
            "교정 결정의 source_segments_sha256가 현재 segments JSON과 일치하지 않습니다. "
            "오래된 결정을 적용하지 않았습니다."
        )
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise CorrectionError("교정 결정 JSON에 decisions 배열이 필요합니다.")

    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            raise CorrectionError(f"decisions[{index}]가 객체가 아닙니다.")
        identifier = raw.get("segment_id")
        key = segment_key(identifier)
        if key in seen:
            raise CorrectionError(f"같은 구간에 결정이 두 개 있습니다: {identifier!r}")
        seen.add(key)
        segment = segments_by_id.get(key)
        if segment is None:
            raise CorrectionError(f"교정 대상 구간 ID가 없습니다: {identifier!r}")

        action = raw.get("action")
        if action not in VALID_ACTIONS:
            raise CorrectionError(
                f"구간 {identifier!r}의 action은 replace, keep, unresolved 중 하나여야 합니다."
            )
        original = raw.get("original")
        if not isinstance(original, str) or original != segment["text"]:
            raise CorrectionError(
                f"구간 {identifier!r}의 original이 현재 전사 원문과 정확히 일치하지 않습니다."
            )
        verification = raw.get("verification", "unverified")
        if verification not in VALID_VERIFICATION:
            raise CorrectionError(f"구간 {identifier!r}의 verification 값이 잘못됐습니다: {verification}")
        rationale = raw.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise CorrectionError(f"구간 {identifier!r}에 rationale이 필요합니다.")

        normalized = dict(raw)
        normalized["rationale"] = rationale.strip()
        if action == "replace":
            replacement = raw.get("replacement")
            if not isinstance(replacement, str) or not replacement.strip():
                raise CorrectionError(f"구간 {identifier!r}의 replace 결정에 replacement가 필요합니다.")
            replacement = replacement.strip()
            if replacement == original:
                raise CorrectionError(f"구간 {identifier!r}의 대체문이 원문과 같습니다.")
            normalized["replacement"] = replacement
        elif "replacement" in raw and raw.get("replacement") not in {None, ""}:
            raise CorrectionError(f"구간 {identifier!r}의 {action} 결정에는 replacement를 넣지 않습니다.")
        checked.append(normalized)
    return checked


def format_clock(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "??:??:??"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_reviewed_markdown(lecture_id: str, segments: list[dict[str, Any]]) -> str:
    lines = [
        f"# {lecture_id} 강의 전사 검수본",
        "",
        "> 승인된 구간별 교정만 적용한 작업본입니다. 음성 검증 범위는 별도 manifest를 따릅니다.",
        "",
    ]
    for segment in segments:
        lines.append(
            f"[{format_clock(segment.get('start'))}–{format_clock(segment.get('end'))}] "
            f"[화자 불명] {segment['text']}"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def atomic_write_text(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise CorrectionError(f"기존 산출물을 덮어쓰지 않았습니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent,
        prefix=path.name + ".", suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def apply_corrections(
    segments_path: Path,
    decisions_path: Path,
    output_dir: Path,
    prefix: str,
    force: bool = False,
) -> dict[str, Any]:
    resolved_segments = segments_path.expanduser().resolve()
    source_hash = sha256_file(resolved_segments) if resolved_segments.is_file() else ""
    source_payload = read_json_object(resolved_segments, "segments")
    _source_segments, source_by_id = validate_segments(source_payload)
    decision_payload = read_json_object(decisions_path, "교정 결정")
    decisions = validate_decisions(decision_payload, source_hash, source_by_id)

    reviewed_payload = copy.deepcopy(source_payload)
    reviewed_segments, reviewed_by_id = validate_segments(reviewed_payload)
    applied: list[dict[str, Any]] = []
    unresolved: list[Any] = []
    kept: list[Any] = []
    for decision in decisions:
        identifier = decision["segment_id"]
        action = decision["action"]
        if action == "replace":
            reviewed_by_id[segment_key(identifier)]["text"] = decision["replacement"]
            applied.append(decision)
        elif action == "unresolved":
            unresolved.append(identifier)
        else:
            kept.append(identifier)

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    decision_hash = sha256_file(decisions_path.expanduser().resolve())
    review_metadata = {
        "source_segments": str(resolved_segments),
        "source_segments_sha256": source_hash,
        "decisions_sha256": decision_hash,
        "replacement_count": len(applied),
        "kept_segment_ids": kept,
        "unresolved_segment_ids": unresolved,
        "created_at": timestamp,
    }
    reviewed_payload["review"] = review_metadata
    audit_payload = {
        "schema_version": 1,
        "kind": "transcript_correction_audit",
        **review_metadata,
        "decisions": decisions,
    }

    safe_prefix = re.sub(r"[^A-Za-z0-9가-힣_.-]+", "_", prefix.strip()) or "review"
    resolved_output = output_dir.expanduser().resolve()
    json_path = resolved_output / f"{safe_prefix}_segments_reviewed.json"
    markdown_path = resolved_output / f"{safe_prefix}_transcript_reviewed.md"
    audit_path = resolved_output / f"{safe_prefix}_correction_audit.json"
    outputs = (json_path, markdown_path, audit_path)
    existing = [path for path in outputs if path.exists()]
    if existing and not force:
        raise CorrectionError(
            "기존 산출물을 덮어쓰지 않았습니다: " + ", ".join(str(path) for path in existing)
        )

    lecture_id = str(reviewed_payload.get("lecture_id") or safe_prefix)
    atomic_write_text(
        json_path,
        json.dumps(reviewed_payload, ensure_ascii=False, indent=2) + "\n",
        force,
    )
    atomic_write_text(markdown_path, render_reviewed_markdown(lecture_id, reviewed_segments), force)
    atomic_write_text(
        audit_path,
        json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n",
        force,
    )
    return {
        "segments_reviewed": str(json_path),
        "transcript_reviewed": str(markdown_path),
        "correction_audit": str(audit_path),
        "summary": {
            "decisions": len(decisions),
            "replacements": len(applied),
            "kept": len(kept),
            "unresolved": len(unresolved),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="승인된 구간별 전사 교정을 원본 보존 방식으로 적용합니다.")
    parser.add_argument("segments", type=Path, help="원본 segments JSON")
    parser.add_argument("decisions", type=Path, help="검수 에이전트의 구조화된 교정 결정 JSON")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--force", action="store_true", help="기존 파생 산출물을 의도적으로 교체")
    parser.add_argument("--json", action="store_true", help="결과 요약을 JSON으로 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = apply_corrections(
            args.segments,
            args.decisions,
            args.output_dir,
            args.prefix,
            args.force,
        )
    except CorrectionError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key in ("segments_reviewed", "transcript_reviewed", "correction_audit"):
            print(f"{key}: {result[key]}")
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
