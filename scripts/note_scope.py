"""수업 진도 범위와 source map의 대응을 검증한다. 의미 판단은 하지 않는다."""

from __future__ import annotations

from pathlib import Path
from typing import Any


COORDINATES = {"pages": "page", "lines": "line", "segments": "segment_index"}


def validate_scope(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, dict) or not isinstance(scope.get("sources"), dict) or not scope["sources"]:
        raise ValueError("scope에는 비어 있지 않은 sources 객체가 필요합니다.")
    if not isinstance(scope.get("label"), str) or not scope["label"].strip():
        raise ValueError("scope.label에 이번 진도를 기록하십시오.")
    for name, selector in scope["sources"].items():
        if not isinstance(name, str) or not name or "\\" in name or ":" in name or name.startswith("/") or any(
            part in {"", ".", ".."} for part in name.split("/")
        ):
            raise ValueError("scope의 파일 경로는 입력 폴더 기준 / 구분 상대경로여야 합니다.")
        if selector == {"all": True} and type(selector.get("all")) is bool:
            continue
        if not isinstance(selector, dict) or len(selector) != 1:
            raise ValueError(f"범위를 하나 지정하십시오: {name}")
        kind, bounds = next(iter(selector.items()))
        if kind not in COORDINATES or not isinstance(bounds, list) or len(bounds) != 2 or not all(
            type(value) is int and value > 0 for value in bounds
        ) or bounds[0] > bounds[1]:
            raise ValueError(f"pages/lines/segments는 1부터 시작하는 [시작, 끝] 범위입니다: {name}")
    return scope


def scoped_inventory(items: list[dict[str, Any]], scope: dict[str, Any] | None) -> list[dict[str, Any]]:
    if scope is None:
        return items
    validate_scope(scope)
    requested = set(scope["sources"])
    missing = requested - {item["path"] for item in items}
    if missing:
        raise ValueError("진도 범위의 입력 파일이 없습니다: " + ", ".join(sorted(missing)))
    return [item for item in items if item["path"] in requested]


def select_rows(rows: list[dict[str, Any]], selector: dict[str, Any]) -> list[dict[str, Any]]:
    if "all" in selector:
        return rows
    kind, (start, end) = next(iter(selector.items()))
    coordinate = COORDINATES[kind]
    if any(type(row["location"].get(coordinate)) is not int for row in rows):
        raise ValueError(f"자료 위치와 지정 범위가 다릅니다: {kind}")
    selected = [row for row in rows if start <= row["location"][coordinate] <= end]
    if len(selected) != end - start + 1 or len({row["location"][coordinate] for row in selected}) != len(selected):
        raise ValueError(f"지정 범위가 실제 자료를 벗어나거나 누락됐습니다: {kind} {start}~{end}")
    return selected


def check_extension(previous: dict[str, Any], current: dict[str, Any]) -> None:
    """한 입력 파일의 같은 구간을 이어 쓰기로 중복 등록하지 않는다."""
    for path in previous["sources"].keys() & current["sources"].keys():
        old, new = previous["sources"][path], current["sources"][path]
        if "all" in old or "all" in new or old.keys() != new.keys():
            raise ValueError(f"완료 범위와 중복되거나 비교할 수 없는 범위입니다: {path}")
        kind = next(iter(old))
        if new[kind][0] <= old[kind][1]:
            raise ValueError(f"이어 쓰기는 완료 지점 뒤에서 시작해야 합니다: {path}")


def validate_map_scope(payload: dict[str, Any], scope: dict[str, Any] | None) -> None:
    if scope is None:
        return
    if payload.get("scope") != scope:
        raise ValueError("source map의 scope가 이번 진도와 일치하지 않습니다.")
    files = payload.get("source_files", [])
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError("진도 source map의 source_files가 잘못되었습니다.")
    if {item.get("path") for item in files} != set(scope["sources"]) or len(files) != len(scope["sources"]):
        raise ValueError("source map의 파일 목록이 진도 범위와 다릅니다.")
    ids = [item.get("source_id") for item in files]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("진도 source map의 source_id가 없거나 중복됩니다.")
    units = payload.get("source_units", [])
    if not isinstance(units, list) or any(not isinstance(unit, dict) or unit.get("source_id") not in ids for unit in units):
        raise ValueError("진도 밖 source unit이 있습니다.")
    for source in files:
        selector = scope["sources"][source["path"]]
        selected = [unit for unit in units if unit["source_id"] == source["source_id"]]
        if not selected:
            raise ValueError(f"진도 안의 자료가 누락됐습니다: {source['path']}")
        if "all" in selector:
            first_location = selected[0].get("source_start")
            keys = [key for key in COORDINATES.values() if isinstance(first_location, dict) and key in first_location]
            if len(keys) != 1 or type(source.get("row_count")) is not int or source["row_count"] < 1:
                raise ValueError(f"전체 자료의 위치·개수 기록이 없습니다: {source['path']}")
            coordinate, start, end = keys[0], 1, source["row_count"]
        else:
            kind, (start, end) = next(iter(selector.items()))
            coordinate = COORDINATES[kind]
        intervals = []
        for unit in selected:
            first, last = unit.get("source_start"), unit.get("source_end")
            if not isinstance(first, dict) or not isinstance(last, dict):
                raise ValueError(f"source unit의 시작·끝 위치가 없습니다: {source['path']}")
            low = first.get(coordinate)
            high = last.get(coordinate)
            if type(low) is not int or type(high) is not int or not start <= low <= high <= end:
                raise ValueError(f"source unit이 지정 진도를 벗어났습니다: {source['path']}")
            intervals.append((low, high))
        cursor = start
        for low, high in sorted(intervals):
            if low > cursor:
                raise ValueError(f"진도 안의 구간이 누락됐습니다: {source['path']} {cursor}")
            cursor = max(cursor, high + 1)
        if cursor != end + 1:
            raise ValueError(f"진도 끝까지 자료가 대응되지 않았습니다: {source['path']}")


def tex_part_input(path: Path) -> str:
    """기존 본문을 복사하지 않는 master TeX. 그림은 각 원고 폴더 기준으로 찾는다."""
    absolute = path.resolve().as_posix()
    if any(char in absolute for char in "%#{}\n\r"):
        raise ValueError(f"TeX 조합 경로에 지원하지 않는 문자가 있습니다: {path}")
    return ("\\begingroup\n"
            + "\\graphicspath{{" + path.parent.resolve().as_posix() + "/}}\n"
            + "\\input{" + absolute + "}\n\\clearpage\n\\endgroup\n")
