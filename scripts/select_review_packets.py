#!/usr/bin/env python3
"""검수 manifest의 메타데이터만 사용해 읽을 packet을 선택한다.

개별 packet 본문은 읽지 않는다. 이 모듈의 선택은 이유 우선순위, 명시적
필터, 파일 크기와 경로 검증처럼 결정적인 조건에만 근거한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


class SelectionError(RuntimeError):
    pass


# 실제 ASR 이상을 routine number/assessment-only보다 항상 먼저 선택한다.
REASON_PRIORITY = {
    "low_avg_logprob": 100,
    "high_no_speech_prob": 95,
    "high_compression_ratio": 90,
    "consecutive_duplicate": 85,
    "invalid_timing": 80,
    "raw_placeholder": 75,
    "timing_density": 65,
    "very_short_timing": 60,
    "number_sensitive": 20,
    "assessment_sensitive": 10,
}


def _priority(reasons: list[str]) -> int:
    return max((REASON_PRIORITY.get(reason, 1) for reason in reasons), default=0)


def _is_relative(path: Path) -> bool:
    return not path.is_absolute() and ".." not in path.parts


def load_manifest(path: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    manifest_path = path.expanduser().resolve()
    if not manifest_path.is_file():
        raise SelectionError(f"manifest가 없습니다: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectionError(f"manifest JSON을 읽을 수 없습니다: {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SelectionError("manifest 최상위 값은 객체여야 합니다.")
    # aggregate review JSON은 model_input=false라도 packet index가 아니므로
    # selector 입력으로 허용하지 않는다.
    if payload.get("kind") != "transcript_review_packet_manifest":
        raise SelectionError("aggregate 또는 알 수 없는 JSON입니다. review_packet_manifest를 지정하십시오.")
    if payload.get("model_input") is not False:
        raise SelectionError("manifest의 model_input은 false여야 합니다.")
    packet_dir_raw = payload.get("packet_dir")
    if not isinstance(packet_dir_raw, str) or not _is_relative(Path(packet_dir_raw)):
        raise SelectionError("manifest의 packet_dir가 안전한 상대 경로가 아닙니다.")
    packet_dir = (manifest_path.parent / packet_dir_raw).resolve()
    try:
        packet_dir.relative_to(manifest_path.parent.resolve())
    except ValueError as exc:
        raise SelectionError("manifest의 packet_dir가 manifest 폴더 밖을 가리킵니다.") from exc
    entries = payload.get("packets")
    if not isinstance(entries, list):
        raise SelectionError("manifest의 packets 배열이 없습니다.")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    checked: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SelectionError("manifest packet 항목이 객체가 아닙니다.")
        packet_id = entry.get("packet_id")
        raw_path = entry.get("path")
        reasons = entry.get("candidate_reasons")
        bytes_expected = entry.get("bytes")
        target_ids = entry.get("target_segment_ids")
        if not isinstance(packet_id, str) or not packet_id:
            raise SelectionError("packet_id가 없습니다.")
        if packet_id in seen_ids:
            raise SelectionError(f"중복 packet_id입니다: {packet_id}")
        if not isinstance(raw_path, str) or not _is_relative(Path(raw_path)):
            raise SelectionError(f"packet 경로가 안전한 상대 경로가 아닙니다: {raw_path}")
        relative_path = Path(raw_path)
        resolved = (manifest_path.parent / relative_path).resolve()
        try:
            resolved.relative_to(packet_dir)
        except ValueError as exc:
            raise SelectionError(f"packet 경로가 지정된 packet_dir 밖입니다: {raw_path}") from exc
        if str(relative_path).casefold() in seen_paths:
            raise SelectionError(f"중복 packet 경로입니다: {raw_path}")
        if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
            raise SelectionError(f"packet reasons가 올바르지 않습니다: {packet_id}")
        if not isinstance(target_ids, list):
            raise SelectionError(f"target_segment_ids가 올바르지 않습니다: {packet_id}")
        if not isinstance(bytes_expected, int) or isinstance(bytes_expected, bool) or bytes_expected < 1:
            raise SelectionError(f"packet bytes가 올바르지 않습니다: {packet_id}")
        if not resolved.is_file():
            raise SelectionError(f"packet 파일이 없습니다: {resolved}")
        actual_bytes = resolved.stat().st_size
        if actual_bytes != bytes_expected:
            raise SelectionError(
                f"packet 크기가 manifest와 다릅니다: {packet_id} ({actual_bytes} != {bytes_expected})"
            )
        seen_ids.add(packet_id)
        seen_paths.add(str(relative_path).casefold())
        checked.append(
            {
                "packet_id": packet_id,
                "path": relative_path.as_posix(),
                "target_segment_ids": target_ids,
                "candidate_reasons": reasons,
                "bytes": actual_bytes,
                "_priority": _priority(reasons),
            }
        )
    return manifest_path, payload, checked


def select_packets(
    manifest_path: Path,
    reasons: list[str] | None = None,
    segment_ids: list[str] | None = None,
    limit: int | None = None,
    max_total_bytes: int = 16_384,
) -> dict[str, Any]:
    if max_total_bytes < 1:
        raise SelectionError("max-total-bytes는 1 이상이어야 합니다.")
    if limit is not None and limit < 1:
        raise SelectionError("limit은 1 이상이어야 합니다.")
    manifest, _payload, entries = load_manifest(manifest_path)
    wanted_reasons = set(reasons or [])
    wanted_ids = {str(value) for value in (segment_ids or [])}
    filtered: list[tuple[int, dict[str, Any]]] = []
    for entry_index, entry in enumerate(entries):
        if wanted_reasons and not wanted_reasons.intersection(entry["candidate_reasons"]):
            continue
        if wanted_ids and not wanted_ids.intersection(str(value) for value in entry["target_segment_ids"]):
            continue
        filtered.append((entry_index, entry))
    # 필터가 있어도 위험 이유 우선순위를 유지하고, 동순위에서는 manifest
    # 원래 순서를 보존한다.
    filtered.sort(key=lambda item: (-item[1]["_priority"], item[0]))
    selected: list[dict[str, Any]] = []
    total_bytes = 0
    for _entry_index, entry in filtered:
        if limit is not None and len(selected) >= limit:
            break
        if total_bytes + entry["bytes"] > max_total_bytes:
            continue
        selected.append(
            {
                "packet_id": entry["packet_id"],
                "path": entry["path"],
                "candidate_reasons": entry["candidate_reasons"],
                "bytes": entry["bytes"],
            }
        )
        total_bytes += entry["bytes"]
    return {
        "manifest": manifest.name,
        "selected": selected,
        "selected_count": len(selected),
        "total_bytes": total_bytes,
        "max_total_bytes": max_total_bytes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="검수 packet manifest에서 읽을 packet만 결정적으로 선택합니다.")
    parser.add_argument("manifest", type=Path, help="review_packet_manifest.json 경로")
    parser.add_argument("--reason", action="append", default=[], help="포함할 anomaly reason(반복 가능)")
    parser.add_argument("--segment-id", action="append", default=[], help="포함할 target segment ID(반복 가능)")
    parser.add_argument("--limit", type=int, help="최대 packet 수")
    parser.add_argument("--max-total-bytes", type=int, default=16_384, help="선택 packet 총 바이트 상한")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = select_packets(args.manifest, args.reason, args.segment_id, args.limit, args.max_total_bytes)
    except SelectionError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
