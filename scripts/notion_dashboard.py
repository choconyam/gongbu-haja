"""학기 노션 페이지와 그 맨 위의 현황판을 다룬다(공부하자 › 학기 › 과목 › 차시).

    gongbu notion semester create [--root <링크>] [--semester ID] [--dry-run]   # 학기 페이지 만들기
    gongbu notion semester move [--semester ID] [--dry-run]                     # 과목 페이지를 학기 아래로 옮기기
    gongbu notion dashboard [--semester ID] [--preview]                         # 현황판 다시 그리기

명령은 `push_notion.py`가 받아 이 모듈로 넘긴다. 현황판은 과목 원장(`course_ledger.collect_status`)에서 그리는
기계 소유 영역이다. 학기 페이지의 기준 줄 바로 뒤부터 첫 하위 페이지(과목 페이지) 앞까지를 매번 지우고 다시 그리며,
블록 ID를 따로 기억하지 않는다. 과목 페이지와 그 아래, 첫 하위 페이지 뒤의 블록은 건드리지 않는다.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from . import push_notion as pn
except ImportError:  # gongbu가 scripts/를 sys.path에 넣고 스크립트를 직접 실행할 때
    import push_notion as pn

PAGE_TYPES = ("child_page", "child_database")
NO_PAGES_INSIDE = ("table", "table_row")  # 하위 페이지를 품을 수 없는 블록은 속을 읽지 않는다
TODO_LIMIT = 30
ANCHOR_TAIL = "이 줄부터 과목 페이지 위까지는 자동으로 다시 그려요"
SUMMARY_HEADER = ("과목", "모드", "노트 진도", "진행 중", "확인 필요", "노션")
LECTURE_HEADER = ("강의", "교안", "녹음·전사", "노트", "노션")
BUSY_NEXT = {"write_note", "update_note", "transcribe", "register_note"}
NOTE_CELLS = {"done": ("완료", "default"), "in_progress": ("진행 중", "default"), "ready": ("만들 차례", "blue"),
              "needs_update": ("갱신 차례(새 자료)", "blue"), "edited": ("원고 수정 중", "orange"),
              "none": ("재료 대기", "gray"), "source_missing": ("원고 없음", "red")}
NOTION_CELLS = {"up_to_date": ("최신", "default"), "local_changed": ("올릴 것 있음", "orange"),
                "not_uploaded": ("안 올림", "default"), "edited_in_notion": ("노션에서 고침", "red"),
                "retry_pending": ("다시 올리기 대기", "orange"), "convert_error": ("변환 오류", "red")}
NOTION_SUMMARY = (("올릴 것", ("not_uploaded", "local_changed", "retry_pending")), ("노션 수정", ("edited_in_notion",)),
                  ("변환 오류", ("convert_error",)))
NO_SEMESTER_PAGE = "학기 노션 페이지가 없습니다 — `gongbu notion semester create`로 먼저 만드십시오."
PUBLIC_SEMESTER = "학기 페이지가 웹에 공개된 페이지 아래라 현황판을 갱신하지 않습니다. 노션에서 웹 공개를 끈 뒤 다시 실행하십시오."
ANCHOR_BELOW_PAGES = ("현황판 기준 줄(회색 줄)이 과목 페이지보다 아래에 있어 현황판을 갱신하지 않습니다(그 아래 블록은 지우지 않았습니다). "
                      "노션에서 기준 줄과 그 아래 현황판을 과목 페이지들 위로 옮긴 뒤 `gongbu notion dashboard`로 다시 실행하십시오.")


def _ledger() -> Any:
    """과목 원장 모듈. 원장 쪽이 이 모듈을 읽지 않아도 되게 필요할 때만 읽는다."""
    try:
        from . import course_ledger
    except ImportError:  # gongbu가 scripts/를 sys.path에 넣고 스크립트를 직접 실행할 때
        import course_ledger
    return course_ledger


# ------------------------------------------------------------------ 그리기(네트워크 없음)

def cell(text: str, color: str = "default", link: str | None = None) -> list[dict[str, Any]]:
    """표 칸·목록 한 줄의 rich text. 링크는 http(s) 주소만 단다."""
    if not text:
        return []
    url = link if link and link.startswith(("http://", "https://")) else None
    return pn.merge_items([pn.text_item(text, color=color, link=url)])[:pn.ARRAY_LIMIT]


def table(header: tuple[str, ...], rows: list[list[list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """머리줄 있는 노션 표. 줄이 100개를 넘으면 `pn.table_blocks`처럼 머리줄을 반복해 나눈다."""
    head = {"type": "table_row", "table_row": {"cells": [cell(name) for name in header]}}
    groups = [rows[start:start + pn.ARRAY_LIMIT - 1] for start in range(0, len(rows), pn.ARRAY_LIMIT - 1)] or [[]]
    return [{"type": "table", "table": {
        "table_width": len(header), "has_column_header": True, "has_row_header": False,
        "children": [head, *({"type": "table_row", "table_row": {"cells": row}} for row in group)]}} for group in groups]


def gray_paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": cell(text), "color": "gray"}}


def anchor_text(status: dict[str, Any], now: dt.datetime) -> str:
    """학기 페이지 첫 줄(기준 줄). 예: `2026-2학기 · 6주차 · 자동 갱신 10월 10일 21:30 · 이 줄부터 …`"""
    semester = status.get("semester") or {}
    week = semester.get("current_week")
    parts = [semester.get("title") or semester.get("id") or "", f"{week}주차" if week else "",
             f"자동 갱신 {now.month}월 {now.day}일 {now:%H:%M}", ANCHOR_TAIL]
    return " · ".join(part for part in parts if part)


def lecture_title(lecture: dict[str, Any]) -> str:
    return lecture.get("title") or lecture.get("id") or "(이름 없음)"


def course_name(course: dict[str, Any]) -> str:
    return course.get("name") or Path(course.get("dir") or "과목").name


def course_url(course: dict[str, Any]) -> str | None:
    notion = course.get("notion") or {}
    return notion.get("course_page_url") if notion.get("connected") else None


def todo_items(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": cell(todo.get("text", ""), link=todo.get("url"))}}
             for todo in todos[:TODO_LIMIT]]
    if len(todos) > TODO_LIMIT:
        items.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": cell(f"외 {len(todos) - TODO_LIMIT}건")}})
    return items or [gray_paragraph("지금은 직접 할 일이 없어요")]


def summary_row(course: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """요약 표의 과목 한 줄: 과목 · 모드 · 노트 진도 · 진행 중 · 확인 필요 · 노션."""
    notion = course.get("notion") or {}
    name = cell(course_name(course), link=course_url(course))
    connected = "—" if notion.get("connected") else "연결 안 함"
    if not course.get("initialized"):
        return [name, cell("처음 설정 필요"), cell("—"), cell("—"), cell("—"), cell(connected)]
    lectures = course.get("lectures") or []
    mode = course.get("mode") or ""
    progress = course.get("progress") or {}
    busy = [lecture_title(lecture) for lecture in lectures
            if lecture.get("next") in BUSY_NEXT or (lecture.get("note") or {}).get("progress") == "in_progress"]
    busy_text = (f"{len(busy)} · {', '.join(busy[:2])}" + (f" 외 {len(busy) - 2}" if len(busy) > 2 else "")) if busy else "—"
    total = sum((lecture.get("unresolved") or {}).get("total", 0) for lecture in lectures)
    relisten = sum((lecture.get("unresolved") or {}).get("relisten", 0) for lecture in lectures)
    unresolved = f"{total}곳" + (f" (다시 듣기 {relisten})" if relisten else "") if total else "—"
    if not notion.get("connected"):
        notion_text = "연결 안 함"
    elif notion.get("legacy"):
        notion_text = "예전 표 방식(setup 다시)"
    else:
        counts = Counter(lecture.get("notion_state") for lecture in lectures)
        parts = [f"{label} {sum(counts[state] for state in states)}" for label, states in NOTION_SUMMARY
                 if sum(counts[state] for state in states)]
        notion_text = " · ".join(parts) or ("최신" if counts["up_to_date"] else "—")
    return [name, cell(pn.MODES[mode][0] if mode in pn.MODES else mode or "—"),
            cell(f"{progress.get('notes', 0)} / {progress.get('total', 0)}"), cell(busy_text), cell(unresolved),
            cell(notion_text)]


def materials_text(lecture: dict[str, Any]) -> str:
    """녹음·전사 칸: `전사 n` / `전사 1/3` / `녹음만(전사 대기)` / `없음` / `필요 없음`."""
    materials = lecture.get("materials") or {}
    transcript = materials.get("transcript") or {}
    have, need = transcript.get("have", 0), transcript.get("need", 0)
    recordings = (materials.get("recording") or {}).get("have", 0)
    if have:
        return f"전사 {have}" if have >= need else f"전사 {have}/{need}"
    if recordings or materials.get("untranscribed"):
        return "녹음만(전사 대기)"
    return "없음" if need else "필요 없음"


def lecture_row(lecture: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """강의 표의 한 줄: 강의 · 교안 · 녹음·전사 · 노트 · 노션."""
    handout = (lecture.get("materials") or {}).get("handout") or {}
    note_state = lecture.get("note_state")
    note_text, note_color = NOTE_CELLS.get(note_state, (note_state or "—", "default"))
    covers = (lecture.get("note") or {}).get("covers")
    if note_state in ("done", "in_progress") and covers:
        note_text = f"{note_text} · {covers}"
    notion_text, notion_color = NOTION_CELLS.get(lecture.get("notion_state"), ("—", "default"))
    return [cell(lecture_title(lecture), link=lecture.get("notion_url")),
            cell(f"{handout.get('have', 0)}/{handout.get('need', 0)}"), cell(materials_text(lecture)),
            cell(note_text, note_color), cell(notion_text, notion_color)]


def render(status: dict[str, Any], now: dt.datetime) -> tuple[str, list[dict[str, Any]]]:
    """`gongbu status` 문서(gongbu.status/1)를 현황판으로 그린다. (기준 줄 글자, 기준 줄 뒤에 붙일 블록)."""
    courses = status.get("courses") or []
    todos = status.get("todo") or []
    blocks: list[dict[str, Any]] = [{"type": "callout", "callout": {
        "rich_text": [pn.text_item("내가 할 일", bold=True)], "icon": {"type": "emoji", "emoji": "✋"},
        "color": "yellow_background" if todos else "gray_background", "children": todo_items(todos)}}]
    blocks += table(SUMMARY_HEADER, [summary_row(course) for course in courses])
    for course in courses:
        if course.get("initialized"):
            blocks.append({"type": "toggle", "toggle": {
                "rich_text": cell(f"{course_name(course)} · 강의별"),
                "children": table(LECTURE_HEADER, [lecture_row(lecture) for lecture in course.get("lectures") or []])}})
    return anchor_text(status, now), blocks


def anchor_block(text: str) -> dict[str, Any]:
    return gray_paragraph(text)


def plain(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("text", {}).get("content", "") for item in items)


def plain_text(anchor: str, blocks: list[dict[str, Any]]) -> str:
    """현황판을 터미널에서 읽을 글자로(`dashboard --preview`)."""
    lines = [anchor]

    def walk(items: list[dict[str, Any]], depth: int) -> None:
        for block in items:
            kind, pad = block["type"], "  " * depth
            body = block[kind]
            if kind == "table":
                lines.extend(pad + " | ".join(plain(cells) or "" for cells in row["table_row"]["cells"])
                             for row in body.get("children", []))
                continue
            prefix = {"callout": f"{body.get('icon', {}).get('emoji', '')} ", "bulleted_list_item": "- ",
                      "toggle": "▸ "}.get(kind, "")
            lines.append(pad + prefix + plain(body.get("rich_text", [])))
            walk(body.get("children", []), depth + 1)

    for block in blocks:
        lines.append("")
        walk([block], 0)
    return "\n".join(lines)


# ------------------------------------------------------------------ 학기와 등록부

def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def pick_semester(registry: dict[str, Any], explicit: str | None, course_dir: Path | None = None) -> str:
    """--semester, 아니면 이 과목 폴더가 속한 학기, 아니면 현재 학기."""
    sem_id = explicit or (_ledger().semester_of(course_dir, registry) if course_dir else None) or registry.get("current")
    if not sem_id:
        raise pn.NotionError("학기를 정하지 못했습니다. `--semester <ID>`를 주거나 `gongbu semester init`으로 학기를 먼저 만드십시오.")
    if sem_id not in (registry.get("semesters") or {}):
        raise pn.NotionError(f"학기 등록부에 '{sem_id}' 학기가 없습니다. `gongbu semester show`로 확인하십시오.")
    return sem_id


def semester_notion(registry: dict[str, Any], sem_id: str) -> dict[str, Any]:
    return ((registry.get("semesters") or {}).get(sem_id) or {}).get("notion") or {}


def semester_page_of(course_dir: Path) -> str | None:
    """이 과목 폴더가 속한 학기의 노션 페이지 ID. 학기에 속하지 않았거나 학기 페이지가 없으면 None."""
    ledger = _ledger()
    registry = ledger.load_registry()
    sem_id = ledger.semester_of(Path(course_dir), registry)
    return semester_notion(registry, sem_id).get("page_id") if sem_id else None


def page_title(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and isinstance(prop.get("title"), list):
            text = "".join(item.get("plain_text") or item.get("text", {}).get("content", "") for item in prop["title"])
            if text:
                return text
    return page.get("id", "(제목 없음)")


def alive_page(client: Any, target: str | None) -> dict[str, Any] | None:
    """페이지가 있고 휴지통에 없으면 그 페이지, 아니면 None."""
    if not target:
        return None
    try:
        page = client.request("GET", f"/pages/{target}")
    except pn.NotionError as exc:
        if exc.status in (400, 404):
            return None
        raise
    return None if page.get("in_trash") or page.get("archived") else page


# ------------------------------------------------------------------ 갱신

def trapped_pages(client: Any, block_id: str) -> list[str]:
    """블록 안(깊이 제한 없음, 표는 빼고)에 들어간 하위 페이지의 제목. 이런 블록은 지우면 페이지까지 지워지므로 남긴다."""
    titles: list[str] = []
    for kid in pn.children(client, block_id):
        if kid["type"] in PAGE_TYPES:
            titles.append(kid.get(kid["type"], {}).get("title") or "(제목 없음)")
        elif kid.get("has_children") and kid["type"] not in NO_PAGES_INSIDE:
            titles += trapped_pages(client, kid["id"])
    return titles


def refresh(client: Any, sem_id: str, now: dt.datetime | None = None) -> str:
    """학기 페이지의 현황판을 다시 그린다. 기준 줄 뒤에 새 현황판을 붙인 뒤 예전 영역을 지운다.

    중간에 실패하면 예외를 그대로 낸다. 다음 갱신이 기준 줄과 첫 하위 페이지 사이를 통째로 지우므로 현황판이 겹쳐 남지 않는다.
    기준 줄이 첫 하위 페이지보다 아래로 내려가 있으면 아무것도 쓰거나 지우지 않고 오류를 낸다.
    """
    ledger = _ledger()
    now = now or local_now()
    data = ledger.collect_status(semester=sem_id)  # 네트워크 없음
    notion = semester_notion(ledger.load_registry(), sem_id)
    page_id = notion.get("page_id")
    if not page_id:
        raise pn.NotionError(NO_SEMESTER_PAGE)
    page = client.request("GET", f"/pages/{page_id}")
    if page.get("in_trash") or page.get("archived"):
        raise pn.NotionError("학기 노션 페이지가 휴지통에 있습니다. 노션에서 복원하거나 `gongbu notion semester create`로 다시 만드십시오.")
    pn.ensure_private(client, page_id, page=page, levels=1, message=PUBLIC_SEMESTER)
    kids = pn.children(client, page_id)
    wanted = pn.id_key(notion.get("anchor_block_id"))
    index = next((number for number, kid in enumerate(kids)
                  if wanted and pn.id_key(kid["id"]) == wanted and kid["type"] == "paragraph"), None)
    first_page = next((number for number, kid in enumerate(kids) if kid["type"] in PAGE_TYPES), len(kids))
    if index is not None and index > first_page:  # 첫 하위 페이지 뒤는 사용자 영역이라 아무것도 쓰거나 지우지 않는다
        raise pn.NotionError(ANCHOR_BELOW_PAGES)
    if index is None:  # 기준 줄을 지웠거나 다른 블록으로 바꿨다: 맨 위에 다시 만들고, 첫 하위 페이지 앞은 모두 예전 현황판으로 본다
        response = client.request("PATCH", f"/blocks/{page_id}/children", {
            "children": [anchor_block(anchor_text(data, now))], "position": {"type": "start"}}, version=pn.DASHBOARD_VERSION)
        anchor = response["results"][0]

        def remember(registry: dict[str, Any]) -> None:
            registry["semesters"][sem_id].setdefault("notion", {})["anchor_block_id"] = anchor["id"]

        ledger.update_registry(remember)
        start = 0
    else:
        anchor, start = kids[index], index + 1
    region = kids[start:first_page]
    keep: list[str] = []
    trapped: list[dict[str, Any]] = []
    for block in region:
        if block.get("has_children") and block["type"] not in NO_PAGES_INSIDE:
            titles = trapped_pages(client, block["id"])
            if titles:
                keep.append(block["id"])
                trapped += [{"course": None, "dir": None, "lecture": None, "kind": "trapped_page", "url": None,
                             "text": f"현황판 안에 들어간 페이지를 밖으로 꺼내 주세요: {title}"} for title in titles]
    text, blocks = render({**data, "todo": [*(data.get("todo") or []), *trapped]}, now)
    previous = anchor["id"]
    for chunk in pn.request_chunks(blocks):
        response = client.request("PATCH", f"/blocks/{page_id}/children", {
            "children": chunk, "position": {"type": "after_block", "after_block": {"id": previous}}},
            version=pn.DASHBOARD_VERSION)
        previous = response["results"][-1]["id"]
    client.request("PATCH", f"/blocks/{anchor['id']}", {"paragraph": {"rich_text": cell(text), "color": "gray"}})
    for block in region:
        if block["id"] in keep or block["type"] in PAGE_TYPES:  # 하위 페이지와 그것을 품은 블록은 절대 지우지 않는다
            continue
        try:
            client.request("DELETE", f"/blocks/{block['id']}")
        except pn.NotionError as exc:
            if exc.status not in (400, 404):
                raise
    return "updated"


def refresh_for_course(client: Any, course_dir: Path, now: dt.datetime | None = None) -> str | None:
    """과목이 속한 학기의 현황판을 갱신한다. 학기에 속하지 않았거나 학기 페이지가 없으면 None."""
    ledger = _ledger()
    registry = ledger.load_registry()
    sem_id = ledger.semester_of(Path(course_dir), registry)
    if not sem_id or not semester_notion(registry, sem_id).get("page_id"):
        return None
    return refresh(client, sem_id, now)


def refresh_quietly(client: Any, sem_id: str, now: dt.datetime | None = None) -> None:
    """학기 페이지 만들기·과목 옮기기 뒤의 갱신. 실패해도 앞의 결과는 그대로 두고 경고만 한다."""
    try:
        if refresh(client, sem_id, now) == "updated":
            print("학기 현황판도 갱신했습니다.")
    except Exception as exc:  # 학기 페이지·이동 결과는 이미 기록됐다
        print(f"[경고] 학기 현황판을 갱신하지 못했습니다: {exc} (`gongbu notion dashboard`로 다시 시도하십시오)", file=sys.stderr)


# ------------------------------------------------------------------ 명령

def preview(course_dir: Path, semester: str | None = None, now: dt.datetime | None = None) -> str:
    """토큰·네트워크 없이 그릴 현황판을 글자로 돌려준다."""
    ledger = _ledger()
    sem_id = pick_semester(ledger.load_registry(), semester, course_dir)
    anchor, blocks = render(ledger.collect_status(semester=sem_id), now or local_now())
    return plain_text(anchor, blocks)


def command_dashboard(client: Any, course_dir: Path, semester: str | None = None, now: dt.datetime | None = None) -> int:
    registry = _ledger().load_registry()
    sem_id = pick_semester(registry, semester, course_dir)
    refresh(client, sem_id, now)
    print(f"학기 현황판을 갱신했습니다: {semester_notion(registry, sem_id).get('url', '')}")
    return 0


def common_parent(semester: dict[str, Any]) -> str:
    """학기 과목들의 노션 기록이 함께 가리키는 상위 페이지."""
    parents: dict[str, str] = {}
    for course in semester.get("courses") or []:
        parent = pn.load_state(Path(course), required=False).get("parent_page_id")
        if parent:
            parents.setdefault(pn.id_key(parent), parent)
    if len(parents) == 1:
        return next(iter(parents.values()))
    if parents:
        raise pn.NotionError("과목마다 노션 상위 페이지가 달라 학기 페이지를 둘 곳을 정하지 못했습니다. `--root <링크>`로 정하십시오.")
    raise pn.NotionError("학기 페이지를 둘 상위 페이지를 모릅니다. `--root <공부하자 페이지 링크>`를 주십시오.")


def create_semester(client: Any, semester: str | None = None, root: str | None = None, *, dry_run: bool = False,
                    now: dt.datetime | None = None) -> int:
    """상위 페이지(공부하자) 아래에 학기 페이지를 만들고 기준 줄을 첫 블록으로 둔다. 이미 있으면 그대로 쓴다."""
    ledger = _ledger()
    registry = ledger.load_registry()
    sem_id = pick_semester(registry, semester)
    info = registry["semesters"][sem_id]
    title = info.get("title") or sem_id
    now = now or local_now()
    existing = alive_page(client, semester_notion(registry, sem_id).get("page_id"))
    if existing:
        print(f"학기 페이지가 이미 있습니다: {existing.get('url') or semester_notion(registry, sem_id).get('url', '')}")
        if not dry_run:
            refresh_quietly(client, sem_id, now)
        return 0
    root_id = pn.page_id(root) if root else registry.get("root_page_id") or common_parent(info)
    root_page = client.request("GET", f"/pages/{root_id}")
    if root_page.get("in_trash") or root_page.get("archived"):
        raise pn.NotionError("학기 페이지를 둘 상위 페이지가 휴지통에 있습니다.")
    pn.ensure_private(client, root_id, page=root_page, levels=1,
                      message="상위 페이지가 웹에 공개돼 있어 학기 페이지를 만들지 않습니다. 노션에서 웹 공개를 끈 뒤 다시 실행하십시오.")
    status = ledger.collect_status(semester=sem_id)
    anchor, blocks = render(status, now)
    if dry_run:
        print(f"상위 페이지: {page_title(root_page)}\n만들 학기 페이지: {title}\n현황판 미리보기:\n{plain_text(anchor, blocks)}")
        return 0
    page = client.request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": root_id}, "icon": {"type": "emoji", "emoji": "🗓️"},
        "properties": {"title": {"title": [pn.text_item(title)]}}, "children": [anchor_block(anchor)]})
    kids = pn.children(client, page["id"])
    anchor_id = kids[0]["id"] if kids else None

    def save(data: dict[str, Any]) -> None:
        data["semesters"][sem_id]["notion"] = {"page_id": page["id"], "url": page.get("url", ""), "anchor_block_id": anchor_id}
        data["root_page_id"] = root_id

    ledger.update_registry(save)
    print(f"학기 페이지를 만들었습니다: {page.get('url', '')}")
    refresh_quietly(client, sem_id, now)
    print("기존 과목 페이지를 이 아래로 옮기려면 `gongbu notion semester move --dry-run`으로 먼저 확인하십시오.")
    return 0


def move_courses(client: Any, semester: str | None = None, *, dry_run: bool = False, now: dt.datetime | None = None) -> int:
    """등록부 순서대로 과목 페이지를 학기 페이지 아래로 옮긴다(링크·차시 페이지 그대로).

    남이 다른 곳에 놓은 페이지는 옮기지 않는다. 옮기기가 안 되면 사이드바에서 끌어 넣으라고 안내하고, 다시 실행하면 기록만 맞춘다.
    종료 코드: 0 모두 학기 아래, 1 직접 옮길 과목이 남음.
    """
    registry = _ledger().load_registry()
    sem_id = pick_semester(registry, semester)
    title = registry["semesters"][sem_id].get("title") or sem_id
    target = semester_notion(registry, sem_id).get("page_id")
    target_page = alive_page(client, target)
    if not target_page:
        raise pn.NotionError(NO_SEMESTER_PAGE)
    pn.ensure_private(client, target, page=target_page, levels=1,
                      message="학기 페이지가 웹에 공개된 페이지 아래라 과목 페이지를 옮기지 않습니다. 노션에서 웹 공개를 끈 뒤 다시 실행하십시오.")
    print(f"학기 페이지: {title}")
    remaining = 0
    for course in registry["semesters"][sem_id].get("courses") or []:
        course_dir = Path(course)
        state = pn.load_state(course_dir, required=False)
        name = state.get("course") or course_dir.name
        if not state.get("course_page_id"):
            print(f"- {name}: 연결 안 됨(건너뜀)")
            continue
        if "data_source_id" in state:
            print(f"- {name}: 예전 표 방식이라 건너뜀 — 과목 폴더에서 `gongbu notion setup`으로 새 방식으로 바꾼 뒤 이 명령을 "
                  f"다시 실행하십시오(링크 없이 안 되면 `gongbu notion setup <예전 상위 페이지 링크>`)")
            remaining += 1
            continue
        course_page = alive_page(client, state["course_page_id"])
        if course_page is None:
            print(f"- {name}: 과목 페이지를 찾지 못함(건너뜀) — 과목 폴더에서 `gongbu notion setup`으로 다시 만드십시오")
            continue
        actual = pn.parent_of(course_page)
        if pn.id_key(actual) == pn.id_key(target):
            set_parent(course_dir, state, target)
            print(f"- {name}: 이미 학기 아래")
            continue
        if pn.id_key(actual) != pn.id_key(state.get("parent_page_id")):
            print(f"- {name}: 예상과 다른 위치라 건너뜀(노션에서 직접 옮긴 페이지는 건드리지 않습니다)")
            remaining += 1
            continue
        if dry_run:
            print(f"- {name}: 옮길 예정")
            continue
        try:
            client.request("POST", f"/pages/{state['course_page_id']}/move",
                           {"parent": {"type": "page_id", "page_id": target}}, version=pn.DASHBOARD_VERSION)
            moved = client.request("GET", f"/pages/{state['course_page_id']}")
            if pn.id_key(pn.parent_of(moved)) != pn.id_key(target):
                raise pn.NotionError("옮긴 뒤 확인한 위치가 학기 페이지가 아닙니다.")
        except pn.NotionError as exc:
            print(f"- {name}: 옮기지 못함({exc})")
            print(f"  노션 사이드바에서 '{name}' 페이지를 '{title}' 페이지 안으로 끌어 넣은 뒤 같은 명령을 다시 실행하십시오.")
            remaining += 1
            continue
        set_parent(course_dir, state, target)
        print(f"- {name}: 옮김")
    if not dry_run:
        refresh_quietly(client, sem_id, now)
    return 1 if remaining and not dry_run else 0


def set_parent(course_dir: Path, state: dict[str, Any], parent: str) -> None:
    """과목 기록의 parent_page_id만 바꾼다. 노트 기록과 과목 페이지 ID는 그대로다."""
    if pn.id_key(state.get("parent_page_id")) == pn.id_key(parent):
        return
    course_page = state["course_page_id"]

    def mutate(disk: dict[str, Any]) -> None:
        if pn.id_key(disk.get("course_page_id")) == pn.id_key(course_page):
            disk["parent_page_id"] = parent

    pn.update_state(course_dir, mutate)
    state["parent_page_id"] = parent


def command_semester(client: Any, action: str, *, root: str | None = None, semester: str | None = None,
                     dry_run: bool = False, now: dt.datetime | None = None) -> int:
    if action == "create":
        return create_semester(client, semester, root, dry_run=dry_run, now=now)
    if root:
        raise pn.NotionError("--root는 semester create에서만 씁니다.")
    return move_courses(client, semester, dry_run=dry_run, now=now)
