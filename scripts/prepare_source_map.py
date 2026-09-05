#!/usr/bin/env python3
"""자료 충실형용 무손실 근거 묶음과 기계적 전사 검사 보고서를 만든다.

원문을 요약하거나 교정하지 않는다. 모든 입력을 포함하며 의미·음성 검증은
작성자와 독립 검수자의 책임으로 남긴다. 동일 출력은 재사용하고 변경본은 거부한다.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

try:
    from .manage_run import RunError, changed_inputs, read_state, sha256_file
    from .prepare_transcript_review import (
        ASSESSMENT_RE, NUMBER_RE, PLACEHOLDER_RE, PreparationError, extract_pdf_pages,
    )
except ImportError:
    from manage_run import RunError, changed_inputs, read_state, sha256_file
    from prepare_transcript_review import (
        ASSESSMENT_RE, NUMBER_RE, PLACEHOLDER_RE, PreparationError, extract_pdf_pages,
    )

TEXT_SUFFIXES = {".txt", ".md", ".srt", ".vtt", ".rst", ".tex", ".csv", ".tsv"}
CHUNK_CHARS = 2400


def parse_derivatives(values: list[str], input_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        source, sep, derivative = value.partition("=")
        if not sep or not source or not derivative:
            raise PreparationError("파생 자료는 원본=파생파일 형식이어야 합니다.")
        original = (input_root / source).resolve()
        try:
            relative = original.relative_to(input_root).as_posix()
        except ValueError as exc:
            raise PreparationError(f"원본이 입력 폴더 밖에 있습니다: {source}") from exc
        if relative in result:
            raise PreparationError(f"중복 원본 매핑: {relative}")
        result[relative] = Path(derivative).expanduser().resolve()
    return result


def text_flags(text: str) -> list[str]:
    return [name for name, pattern in (
        ("raw_placeholder", PLACEHOLDER_RE),
        ("assessment_sensitive", ASSESSMENT_RE),
        ("number_sensitive", NUMBER_RE),
    ) if pattern.search(text)]


def segment_rows(path: Path, expected_audio: str | None = None) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise PreparationError(f"segments 배열이 필요합니다: {path}")
    if expected_audio and payload.get("source_audio") not in (None, expected_audio):
        raise PreparationError(f"전사에 기록된 원본 녹음이 다릅니다: {path}")
    rows = []
    previous = None
    for index, segment in enumerate(payload["segments"], 1):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise PreparationError(f"문자열 text가 없는 전사 구간: {path}, {index}")
        text = segment["text"]
        flags = text_flags(text)
        start, end = segment.get("start"), segment.get("end")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (start, end)) or start < 0 or end <= start:
            flags.append("invalid_timing")
        elif previous is not None and isinstance(previous.get("end"), (int, float)):
            if start < previous["end"]:
                flags.append("overlapping_timing")
        for key, threshold, below in (
            ("avg_logprob", -1.0, True), ("no_speech_prob", 0.6, False),
            ("compression_ratio", 2.4, False),
        ):
            value = segment.get(key)
            if isinstance(value, (int, float)) and (
                value < threshold if below else value >= threshold
            ):
                flags.append(key)
        if previous is not None and text.strip() and text.strip() == previous["text"].strip():
            flags.append("consecutive_duplicate")
        rows.append({"location": {"segment_index": index, "segment_id": segment.get("id", index),
                                  "start": start, "end": end},
                     "text": text, "flags": flags})
        previous = segment
    return rows


def source_rows(path: Path, transcript: bool, expected_audio: str | None = None) -> list[dict[str, Any]]:
    if transcript and path.suffix.lower() == ".json":
        return segment_rows(path, expected_audio)
    if path.suffix.lower() == ".pdf":
        pages = extract_pdf_pages(path)
        if any(not page.strip() for page in pages):
            raise PreparationError(f"빈 PDF 페이지가 있습니다. 원본 확인·OCR이 필요합니다: {path}")
        return [{"location": {"page": index}, "text": page, "flags": ["visual_review_required"]}
                for index, page in enumerate(pages, 1)]
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise PreparationError(f"텍스트 추출본을 --extracted 원본=파일로 지정하십시오: {path}")
    # 빈 줄·숫자·시간표시를 포함해 그대로 보존한다. 학생 본문에서만 추적 정보를 숨긴다.
    return [{"location": {"line": index}, "text": line, "flags": text_flags(line)}
            for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(keepends=True), 1)]


def chunk_rows(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks, current, size = [], [], 0
    for row in rows:
        # PDF는 페이지가 의미 경계다. 한 페이지나 발언을 예산 때문에 잘라 버리지 않는다.
        if current and ("page" in row["location"] or size + len(row["text"]) > CHUNK_CHARS):
            chunks.append(current)
            current, size = [], 0
        current.append(row)
        size += len(row["text"])
    if current:
        chunks.append(current)
    return chunks


def prepare(
    state_path: Path, transcripts: list[str], extracted: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = read_state(state_path)
    if state.get("preprocessing") != "deterministic":
        raise PreparationError("이 실행은 deterministic 전처리 계약이 아닙니다. 기존 상태를 직접 변경하지 마십시오.")
    changes = changed_inputs(state)
    if changes:
        raise PreparationError("입력이 변경됐습니다. 먼저 refresh-inputs: " + "; ".join(changes))
    input_root = Path(state["input_root"])
    audio_map = parse_derivatives(transcripts, input_root)
    text_map = parse_derivatives(extracted, input_root)
    known = {item["path"] for item in state["inputs"]}
    if (set(audio_map) | set(text_map)) - known or set(audio_map) & set(text_map):
        raise PreparationError("등록되지 않았거나 중복된 파생 자료 매핑입니다.")
    sources, units = [], []
    for number, item in enumerate(state["inputs"], 1):
        relative = item["path"]
        original = input_root / relative
        is_transcript = item["kind"] in {"audio", "transcript"}
        if item["kind"] == "audio" and relative not in audio_map:
            raise PreparationError(f"녹음 전사를 --transcript 원본=segments.json으로 지정하십시오: {relative}")
        if relative in audio_map and not is_transcript:
            raise PreparationError(f"전사 매핑의 원본은 녹음 또는 전사여야 합니다: {relative}")
        derivative = audio_map.get(relative, text_map.get(relative, original))
        rows = source_rows(derivative, is_transcript, original.name if item["kind"] == "audio" else None)
        if relative in text_map:
            for row in rows:
                row["flags"].append("extraction_alignment_required")
                if item["kind"] in {"document", "image"}:
                    row["flags"].append("visual_review_required")
        if not rows or not any(row["text"].strip() for row in rows):
            raise PreparationError(f"빈 자료는 자동 제외하지 않습니다: {relative}")
        # 입력 순서와 원본 해시가 같으면 같은 ID. 파생본 변경은 전체 JSON 해시로 감지한다.
        source_id = f"src_{number:03d}_{item['sha256'][:12]}"
        sources.append({**item, "source_id": source_id, "evidence_path": str(derivative),
                        "evidence_sha256": sha256_file(derivative),
                        "verification": "audio_unverified" if is_transcript else "extracted_unreviewed",
                        "semantic_reviewed": False, "row_count": len(rows)})
        for index, chunk in enumerate(chunk_rows(rows), 1):
            # 발언마다 JSON 메타데이터를 반복하지 않는다. 원문은 한 번, 위치는 구간 양 끝만 저장한다.
            separator = "\n" if derivative.suffix.lower() == ".json" else ""
            units.append({"source_unit_id": f"{source_id}_u{index:04d}", "source_id": source_id,
                          "source_start": chunk[0]["location"], "source_end": chunk[-1]["location"],
                          "row_count": len(chunk),
                          "evidence": separator.join(row["text"] for row in chunk),
                          "flags": sorted({flag for row in chunk for flag in row["flags"]}),
                          "semantic_reviewed": False})
    source_map = {"kind": "study_note_source_map", "schema_version": 1,
                  "preparation": "lossless_deterministic", "lecture_id": state["lecture_id"],
                  "source_files": sources, "source_units": units}
    screening = {"kind": "study_note_source_screening", "schema_version": 1,
                 "lecture_id": state["lecture_id"], "semantic_reviewed": False,
                 "reviewed_against_audio": False,
                 "source_files": [{key: source[key] for key in
                                   ("path", "sha256", "evidence_sha256", "verification", "row_count")}
                                  for source in sources],
                 "flagged_units": [{"source_unit_id": unit["source_unit_id"], "flags": unit["flags"]}
                                   for unit in units if unit["flags"]],
                 "meaning_check": "writer_and_independent_final_reviewer_required"}
    return source_map, screening


def write_outputs(directory: Path, source_map: dict[str, Any], screening: dict[str, Any]) -> dict[str, Any]:
    outputs = {directory / "source_map.json": source_map, directory / "screening.json": screening}
    encoded = {path: (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
               for path, value in outputs.items()}
    for path, data in encoded.items():
        if path.exists() and path.read_bytes() != data:
            raise PreparationError(f"다른 산출물을 덮어쓰지 않습니다. 새 출력 폴더를 지정하십시오: {path}")
    directory.mkdir(parents=True, exist_ok=True)
    for path, data in encoded.items():
        if not path.exists():
            path.write_bytes(data)
    return {"source_map": str(directory / "source_map.json"),
            "screening": str(directory / "screening.json"),
            "source_files": len(source_map["source_files"]),
            "source_units": len(source_map["source_units"]),
            "flagged_units": len(screening["flagged_units"]), "semantic_reviewed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--transcript", action="append", default=[], metavar="원본=전사_JSON")
    parser.add_argument("--extracted", action="append", default=[], metavar="원본=텍스트_추출본")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source_map, screening = prepare(args.state.resolve(), args.transcript, args.extracted)
        print(json.dumps(write_outputs(args.output_dir.resolve(), source_map, screening), ensure_ascii=False))
    except (OSError, ValueError, PreparationError, RunError) as exc:
        print(f"[ERROR] {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
