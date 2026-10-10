"""학기 페이지·현황판을 가짜 노션과 가짜 과목 원장으로 확인한다. 네트워크·토큰·실제 등록부는 쓰지 않는다."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import notion_dashboard as nd
from scripts import push_notion as pn

NOW = dt.datetime(2026, 10, 10, 21, 30)
URL = "https://www.notion.so/"


def lecture(lecture_id: str, title: str, *, handout: tuple[int, int] = (1, 1), transcript: tuple[int, int] = (0, 0),
            recording: int = 0, untranscribed: tuple[str, ...] = (), note: dict | None = None, note_state: str = "none",
            notion_state: str | None = None, notion_url: str | None = None, unresolved: tuple[int, int] = (0, 0),
            next_action: str | None = None) -> dict[str, Any]:
    """명세 2.4 모양의 강의 한 줄."""
    return {"id": lecture_id, "title": title,
            "materials": {"handout": {"have": handout[0], "need": handout[1]},
                          "transcript": {"have": transcript[0], "need": transcript[1]},
                          "recording": {"have": recording}, "untranscribed": list(untranscribed), "missing": []},
            "materials_state": "complete", "note": note, "note_state": note_state, "notion_state": notion_state,
            "notion_url": notion_url, "unresolved": {"total": unresolved[0], "relisten": unresolved[1]},
            "unstable": False, "next": next_action, "blocked": None}


def note(progress: str, covers: str | None = None) -> dict[str, Any]:
    return {"source": "output/source/x.tex", "progress": progress, "covers": covers, "outputs_missing": [],
            "stale_inputs": [], "error": None}


STATUS: dict[str, Any] = {
    "schema": "gongbu.status/1",
    "generated_at": "2026-10-10T21:30:05+09:00",
    "semester": {"id": "2026-2", "title": "2026-2학기", "current_week": 6, "weeks": 15},
    "courses": [
        {"dir": "X:/과목/물리", "name": "일반물리학1", "initialized": True, "mode": "deep", "materials": "handout+recording",
         "progress": {"notes": 6, "done": 4, "total": 12},
         "notion": {"connected": True, "course_page_url": URL + "physics", "legacy": False},
         "lectures": [
             lecture("ch04", "4장 2·3차원 운동", transcript=(3, 1), note=note("done", "p.1–16"), note_state="done",
                     notion_state="up_to_date", notion_url=URL + "ch04", unresolved=(1, 1)),
             lecture("ch05", "5장 힘과 운동 I", transcript=(4, 1), note=note("in_progress", "p.1–19"),
                     note_state="in_progress", notion_state="local_changed", notion_url=URL + "ch05", unresolved=(2, 0),
                     next_action="push"),
             lecture("ch06", "6장 힘과 운동 II", transcript=(0, 1), recording=1, untranscribed=("6주차/6-1.wav",),
                     next_action="transcribe"),
             lecture("ch07", "7장 운동에너지와 일", transcript=(1, 3), note_state="ready", next_action="write_note"),
             lecture("ch08", "8장 퍼텐셜에너지", handout=(0, 1), transcript=(0, 1)),
             lecture("ch09", "9장 운동량", transcript=(1, 1), note=note("in_progress"), note_state="edited",
                     notion_state="edited_in_notion", notion_url=URL + "ch09", next_action="register_note"),
             lecture("ch10", "10장 회전", transcript=(1, 1), note=note("done"), note_state="needs_update",
                     notion_state="convert_error", next_action="update_note"),
             lecture("ch11", "11장 굴림", transcript=(1, 1), note=note("done"), note_state="source_missing",
                     notion_state="retry_pending", notion_url=URL + "ch11"),
             lecture("ch12", "12장 평형", transcript=(1, 1), note=note("done", "p.1–9"), note_state="done",
                     notion_state="not_uploaded", next_action="push"),
         ],
         "unregistered": [], "unregistered_notes": [], "moved": [], "missing_files": [], "unstable_files": [], "errors": []},
        {"dir": "X:/과목/media", "name": "미디어빅뱅과방송", "initialized": True, "mode": "faithful", "materials": "handout",
         "progress": {"notes": 0, "done": 0, "total": 1},
         "notion": {"connected": False, "course_page_url": None, "legacy": False},
         "lectures": [lecture("w03", "3주차 방송의 탄생", handout=(0, 1))],
         "unregistered": [], "unregistered_notes": [], "moved": [], "missing_files": [], "unstable_files": [], "errors": []},
        {"dir": "X:/과목/선형대수", "name": "선형대수", "initialized": False,
         "notion": {"connected": True, "course_page_url": URL + "linear", "legacy": False},
         "hints": {"note_suffixes": {".tex": 6}}},
    ],
    "ready": [],
    "todo": [
        {"course": "일반물리학1", "dir": "X:/과목/물리", "lecture": "ch04", "kind": "relisten",
         "text": "일반물리학1 4장: 알아듣기 어려운 곳 1곳 다시 듣기", "url": URL + "ch04"},
        {"course": "미디어빅뱅과방송", "dir": "X:/과목/media", "lecture": "w03", "kind": "fetch_materials",
         "text": "미디어빅뱅과방송 w03: 교안 받기 (0/1)", "url": None},
        {"course": "선형대수", "dir": "X:/과목/선형대수", "lecture": None, "kind": "setup_course",
         "text": "선형대수: 처음 설정이 필요해요(기본 모드·재료 조건)", "url": None},
    ],
}


class FakeLedger:
    """course_ledger 대신 쓰는 가짜(명세 2.1의 API). 등록부는 메모리에 두고 status는 미리 준 문서를 돌려준다."""

    def __init__(self, base: Path, registry: dict | None = None, status: dict | None = None) -> None:
        self.base = base
        self.registry = registry or {"version": 1, "current": None, "root_page_id": None, "semesters": {}}
        self.status = copy.deepcopy(STATUS if status is None else status)
        self.status_calls: list[str | None] = []

    def registry_dir(self) -> Path:
        return self.base

    def load_registry(self) -> dict:
        return copy.deepcopy(self.registry)

    def update_registry(self, mutate) -> dict:
        data = copy.deepcopy(self.registry)
        mutate(data)
        self.registry = data
        return copy.deepcopy(data)

    def semester_of(self, course_dir: Path, registry: dict | None = None) -> str | None:
        key = Path(course_dir).resolve().as_posix()
        for sem_id, info in (registry or self.registry)["semesters"].items():
            if key in info.get("courses", []):
                return sem_id
        return None

    def collect_status(self, *, semester: str | None = None, course_dirs: list[Path] | None = None,
                       settle_seconds: int = 300, today: dt.date | None = None, now: float | None = None) -> dict:
        self.status_calls.append(semester)
        return copy.deepcopy(self.status)


class DashFake:
    """노션처럼 블록 순서를 기억하는 가짜 클라이언트.

    `POST /pages`나 옮기기로 생긴 하위 페이지는 부모 블록 목록 끝의 child_page 블록이 된다. `PATCH …/children`은
    position(start·after_block·end)을 따르고 만든 블록을 ID와 함께 돌려준다. position과 move는 2026-03-11 버전에서만 받는다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.counter = 0
        self.fail_move = False
        self.fail_appends: set[int] = set()  # n번째(1부터) 블록 덧붙이기를 서버 오류로
        self.appends = 0
        self.root = self.add_page(None, "공부하자")

    def new_id(self) -> str:
        self.counter += 1
        return str(uuid.UUID(int=self.counter))

    def kids(self, parent: str) -> list[dict]:
        return self.blocks.setdefault(parent, [])

    def add_page(self, parent: str | None, title: str, *, in_block: bool = False) -> str:
        page_id = self.new_id()
        self.pages[page_id] = {
            "object": "page", "id": page_id, "url": URL + page_id.replace("-", ""), "in_trash": False, "public_url": None,
            "parent": ({"type": "block_id", "block_id": parent} if in_block else {"type": "page_id", "page_id": parent})
            if parent else {"type": "workspace", "workspace": True},
            "properties": {"title": {"id": "title", "type": "title",
                                     "title": [{"type": "text", "text": {"content": title}, "plain_text": title}]}}}
        if parent:
            self.kids(parent).append({"object": "block", "id": page_id, "type": "child_page",
                                      "child_page": {"title": title}, "has_children": False})
            if in_block:
                self.locate(parent)[1]["has_children"] = True
        return page_id

    def locate(self, block_id: str) -> tuple[list[dict], dict]:
        for items in self.blocks.values():
            for block in items:
                if block["id"] == block_id:
                    return items, block
        raise pn.NotionError("없음", 404, "object_not_found")

    def insert(self, parent: str, items: list[dict], index: int) -> list[dict]:
        created = []
        for block in items:
            block = copy.deepcopy(block)
            nested = block[block["type"]].pop("children", [])
            block.update(object="block", id=self.new_id(), has_children=bool(nested))
            created.append(block)
            if nested:
                self.insert(block["id"], nested, 0)
        self.kids(parent)[index:index] = created
        return created

    def drag(self, page_id: str, new_parent: str) -> None:
        """사용자가 노션 사이드바에서 페이지를 끌어 옮긴 것."""
        page = self.pages[page_id]
        items, block = self.locate(page_id)
        items.remove(block)
        self.kids(new_parent).append(block)
        page["parent"] = {"type": "page_id", "page_id": new_parent}

    def request(self, method: str, path: str, body: dict | None = None, *, version: str | None = None, **_: Any) -> dict:
        body = copy.deepcopy(body)
        self.calls.append((method, path, copy.deepcopy(body), version))
        route, _, query = path.partition("?")
        parts = route.strip("/").split("/")
        if method == "GET" and parts[0] == "pages":
            if parts[1] not in self.pages:
                raise pn.NotionError("없음", 404, "object_not_found")
            return copy.deepcopy(self.pages[parts[1]])
        if method == "POST" and route == "/pages":
            parent = body["parent"]["page_id"]
            if parent not in self.pages:
                raise pn.NotionError("없음", 404, "object_not_found")
            page_id = self.add_page(parent, body["properties"]["title"]["title"][0]["text"]["content"])
            self.pages[page_id]["icon"] = body.get("icon")
            self.insert(page_id, body.get("children", []), 0)
            return copy.deepcopy(self.pages[page_id])
        if method == "POST" and parts[0] == "pages" and parts[-1] == "move":
            if self.fail_move or version != pn.DASHBOARD_VERSION:
                raise pn.NotionError("노션 요청이 실패했습니다(400 validation_error): move", 400, "validation_error")
            self.drag(parts[1], body["parent"]["page_id"])
            return {"object": "page", "id": parts[1]}
        if method == "GET" and route.endswith("/children"):
            options = dict(item.split("=", 1) for item in query.split("&") if item)
            start, size = int(options.get("start_cursor", 0)), int(options.get("page_size", 100))
            items = self.kids(parts[1])
            more = start + size < len(items)
            return {"results": copy.deepcopy(items[start:start + size]), "has_more": more,
                    "next_cursor": str(start + size) if more else None}
        if method == "GET" and parts[0] == "blocks":  # 블록 하나: 노션처럼 parent를 같이 준다
            _, block = self.locate(parts[1])
            owner = next(key for key, items in self.blocks.items() if any(item["id"] == parts[1] for item in items))
            kind = "page_id" if owner in self.pages else "block_id"
            return {**copy.deepcopy(block), "parent": {"type": kind, kind: owner}}
        if method == "PATCH" and route.endswith("/children"):
            self.appends += 1
            if self.appends in self.fail_appends:
                raise pn.NotionError("서버 오류", 500, "internal_server_error")
            position = body.get("position") or {"type": "end"}
            if "position" in body and version != pn.DASHBOARD_VERSION:
                raise pn.NotionError("position은 2026-03-11 버전에서만 받습니다", 400, "validation_error")
            siblings = self.kids(parts[1])
            if position["type"] == "start":
                index = 0
            elif position["type"] == "after_block":
                ids = [block["id"] for block in siblings]
                if position["after_block"]["id"] not in ids:
                    raise pn.NotionError("기준 블록이 없습니다", 400, "validation_error")
                index = ids.index(position["after_block"]["id"]) + 1
            else:
                index = len(siblings)
            return {"results": copy.deepcopy(self.insert(parts[1], body["children"], index))}
        if method == "PATCH" and parts[0] == "blocks":
            _, block = self.locate(parts[1])
            if block["type"] not in body:
                raise pn.NotionError("블록 종류가 다릅니다", 400, "validation_error")
            block[block["type"]] = {**block[block["type"]], **body[block["type"]]}
            return copy.deepcopy(block)
        if method == "DELETE" and parts[0] == "blocks":
            items, block = self.locate(parts[1])
            items.remove(block)
            if block["type"] == "child_page":
                self.pages[block["id"]]["in_trash"] = True
            return copy.deepcopy(block)
        raise AssertionError(f"예상하지 못한 요청: {method} {path}")

    def count(self, method: str, suffix: str = "") -> int:
        return sum(1 for call in self.calls if call[0] == method and call[1].split("?")[0].endswith(suffix))

    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] != "GET"]


def text(items: list[dict]) -> str:
    return "".join(item["text"]["content"] for item in items)


def color(items: list[dict]) -> str:
    return items[0].get("annotations", {}).get("color", "default")


def link(items: list[dict]) -> str | None:
    return (items[0]["text"].get("link") or {}).get("url")


def rows(table_block: dict) -> list[list[list[dict]]]:
    return [row["table_row"]["cells"] for row in table_block["table"]["children"]]


def depth(block: dict) -> int:
    children = block[block["type"]].get("children", [])
    return 1 + max((depth(child) for child in children), default=0) if children else 0


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor, self.blocks = nd.render(copy.deepcopy(STATUS), NOW)

    def test_anchor_line_and_block_order(self) -> None:
        self.assertEqual("2026-2학기 · 6주차 · 자동 갱신 10월 10일 21:30 · 이 줄부터 과목 페이지 위까지는 자동으로 다시 그려요",
                         self.anchor)
        self.assertEqual(["callout", "table", "toggle", "toggle"], [block["type"] for block in self.blocks])
        self.assertEqual(["일반물리학1 · 강의별", "미디어빅뱅과방송 · 강의별"],
                         [text(block["toggle"]["rich_text"]) for block in self.blocks[2:]])  # 처음 설정 전 과목은 없다

    def test_todo_callout(self) -> None:
        callout = self.blocks[0]["callout"]
        self.assertEqual(("✋", "yellow_background"), (callout["icon"]["emoji"], callout["color"]))
        self.assertEqual("내가 할 일", text(callout["rich_text"]))
        self.assertEqual({"bold": True}, callout["rich_text"][0]["annotations"])
        items = callout["children"]
        self.assertEqual(["bulleted_list_item"] * 3, [item["type"] for item in items])
        self.assertEqual("일반물리학1 4장: 알아듣기 어려운 곳 1곳 다시 듣기", text(items[0]["bulleted_list_item"]["rich_text"]))
        self.assertEqual(URL + "ch04", link(items[0]["bulleted_list_item"]["rich_text"]))
        self.assertIsNone(link(items[1]["bulleted_list_item"]["rich_text"]))

    def test_todo_overflow_and_empty(self) -> None:
        many = {**STATUS, "todo": [{"text": f"할 일 {number}", "url": None} for number in range(35)]}
        items = nd.render(many, NOW)[1][0]["callout"]["children"]
        self.assertEqual(31, len(items))
        self.assertEqual("외 5건", text(items[-1]["bulleted_list_item"]["rich_text"]))
        callout = nd.render({**STATUS, "todo": []}, NOW)[1][0]["callout"]
        self.assertEqual("gray_background", callout["color"])
        self.assertEqual([{"type": "paragraph", "paragraph": {"rich_text": [pn.text_item("지금은 직접 할 일이 없어요")],
                                                             "color": "gray"}}], callout["children"])

    def test_summary_table(self) -> None:
        table = self.blocks[1]["table"]
        self.assertEqual((6, True, False), (table["table_width"], table["has_column_header"], table["has_row_header"]))
        header, physics, media, linear = rows(self.blocks[1])
        self.assertEqual(["과목", "모드", "노트 진도", "진행 중", "확인 필요", "노션"], [text(cell) for cell in header])
        self.assertEqual(["일반물리학1", "심화 이해형", "6 / 12", "5 · 5장 힘과 운동 I, 6장 힘과 운동 II 외 3", "3곳 (다시 듣기 1)",
                          "올릴 것 3 · 노션 수정 1 · 변환 오류 1"], [text(cell) for cell in physics])
        self.assertEqual(URL + "physics", link(physics[0]))
        self.assertEqual(["미디어빅뱅과방송", "자료 충실형", "0 / 1", "—", "—", "연결 안 함"], [text(cell) for cell in media])
        self.assertIsNone(link(media[0]))
        self.assertEqual(["선형대수", "처음 설정 필요", "—", "—", "—", "—"], [text(cell) for cell in linear])
        self.assertEqual(URL + "linear", link(linear[0]))

    def test_summary_notion_all_up_to_date(self) -> None:
        course = copy.deepcopy(STATUS["courses"][0])
        course["lectures"] = [course["lectures"][0], course["lectures"][2]]  # 올린 노트는 모두 최신, 노트 없는 강의는 —
        cells = rows(nd.render({**STATUS, "courses": [course]}, NOW)[1][1])[1]
        self.assertEqual("최신", text(cells[5]))

    def test_lecture_table_cells_and_colors(self) -> None:
        table = self.blocks[2]["toggle"]["children"][0]
        self.assertEqual((5, True), (table["table"]["table_width"], table["table"]["has_column_header"]))
        header, *body = rows(table)
        self.assertEqual(["강의", "교안", "녹음·전사", "노트", "노션"], [text(cell) for cell in header])
        expected = [
            ("4장 2·3차원 운동", "1/1", "전사 3", ("완료 · p.1–16", "default"), ("최신", "default")),
            ("5장 힘과 운동 I", "1/1", "전사 4", ("진행 중 · p.1–19", "default"), ("올릴 것 있음", "orange")),
            ("6장 힘과 운동 II", "1/1", "녹음만(전사 대기)", ("재료 대기", "gray"), ("—", "default")),
            ("7장 운동에너지와 일", "1/1", "전사 1/3", ("만들 차례", "blue"), ("—", "default")),
            ("8장 퍼텐셜에너지", "0/1", "없음", ("재료 대기", "gray"), ("—", "default")),
            ("9장 운동량", "1/1", "전사 1", ("원고 수정 중", "orange"), ("노션에서 고침", "red")),
            ("10장 회전", "1/1", "전사 1", ("갱신 차례(새 자료)", "blue"), ("변환 오류", "red")),
            ("11장 굴림", "1/1", "전사 1", ("원고 없음", "red"), ("다시 올리기 대기", "orange")),
            ("12장 평형", "1/1", "전사 1", ("완료 · p.1–9", "default"), ("안 올림", "default")),
        ]
        for cells, (title, handout, materials, note_cell, notion_cell) in zip(body, expected, strict=True):
            self.assertEqual([title, handout, materials, note_cell[0], notion_cell[0]], [text(cell) for cell in cells])
            self.assertEqual((note_cell[1], notion_cell[1]), (color(cells[3]), color(cells[4])))
        self.assertEqual(URL + "ch04", link(body[0][0]))  # 올린 노트는 강의 이름에 노트 페이지 링크
        self.assertIsNone(link(body[2][0]))
        media = rows(self.blocks[3]["toggle"]["children"][0])[1]
        self.assertEqual(["3주차 방송의 탄생", "0/1", "필요 없음", "재료 대기", "—"], [text(cell) for cell in media])

    def test_big_course_splits_tables_and_fits_request_limits(self) -> None:
        course = copy.deepcopy(STATUS["courses"][0])
        course["lectures"] = [lecture(f"L{number:03d}", f"{number}강") for number in range(150)]
        anchor, blocks = nd.render({**STATUS, "courses": [course] * 3}, NOW)
        tables = blocks[2]["toggle"]["children"]
        self.assertEqual(2, len(tables))
        self.assertTrue(all(len(table["table"]["children"]) <= pn.ARRAY_LIMIT for table in tables))
        self.assertEqual("강의", text(rows(tables[1])[0][0]))  # 머리줄을 반복한다
        self.assertEqual(150, sum(len(table["table"]["children"]) - 1 for table in tables))
        for chunk in pn.request_chunks(blocks):
            self.assertLessEqual(len(chunk), pn.ARRAY_LIMIT)
            self.assertLessEqual(pn.count_elements(chunk), pn.REQUEST_ELEMENT_LIMIT)
        self.assertLessEqual(max(depth(block) for block in blocks), 2)  # 노션이 한 요청에 받는 두 단계까지만 겹친다

    def test_plain_text_preview(self) -> None:
        preview = nd.plain_text(self.anchor, self.blocks)
        self.assertTrue(preview.startswith("2026-2학기 · 6주차"))
        self.assertIn("✋ 내가 할 일", preview)
        self.assertIn("  - 미디어빅뱅과방송 w03: 교안 받기 (0/1)", preview)
        self.assertIn("과목 | 모드 | 노트 진도 | 진행 중 | 확인 필요 | 노션", preview)
        self.assertIn("▸ 일반물리학1 · 강의별", preview)
        self.assertIn("  5장 힘과 운동 I | 1/1 | 전사 4 | 진행 중 · p.1–19 | 올릴 것 있음", preview)


class LedgerCase(unittest.TestCase):
    """임시 과목 폴더 두 개(공부하자 바로 아래 과목 페이지)와 학기 2026-2를 둔다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        environment = patch.dict(os.environ, {"GONGBU_HAJA_CONFIG": str(self.base / "config")})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = DashFake()
        self.courses: list[tuple[Path, str]] = []
        for name in ("과목A", "과목B"):
            course = self.base / name
            course.mkdir()
            page = self.client.add_page(self.client.root, name)
            pn.save_state(course, {"version": pn.STATE_VERSION, "course": name, "parent_page_id": self.client.root,
                                   "course_page_id": page, "course_page_url": URL + page, "slides": True,
                                   "notes": {"1주차/노트.md": {"page_id": f"note-of-{name}", "content_sha256": "x"}}})
            self.courses.append((course, page))
        self.ledger = FakeLedger(self.base, {
            "version": 1, "current": "2026-2", "root_page_id": None,
            "semesters": {"2026-2": {"title": "2026-2학기", "start": "2026-08-31", "weeks": 15,
                                     "courses": [course.resolve().as_posix() for course, _ in self.courses]}}})
        ledger = patch.object(nd, "_ledger", return_value=self.ledger)
        ledger.start()
        self.addCleanup(ledger.stop)

    def run_command(self, action: str, **options: Any) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nd.command_semester(self.client, action, now=NOW, **options)
        return code, out.getvalue(), err.getvalue()

    @property
    def semester(self) -> dict:
        return self.ledger.registry["semesters"]["2026-2"].get("notion") or {}

    def layout(self) -> list[str]:
        return [block["type"] for block in self.client.kids(self.semester["page_id"])]

    def created(self) -> None:
        self.assertEqual(0, self.run_command("create")[0])

    def moved(self) -> None:
        self.assertEqual(0, self.run_command("move")[0])


class SemesterCreateTests(LedgerCase):
    def test_create_puts_anchor_first_and_saves_registry(self) -> None:
        code, out, err = self.run_command("create")
        self.assertEqual((0, ""), (code, err))
        page_id = self.semester["page_id"]
        post = next(body for method, path, body, _ in self.client.calls if method == "POST" and path == "/pages")
        self.assertEqual(({"type": "page_id", "page_id": self.client.root}, "🗓️", "2026-2학기"),
                         (post["parent"], post["icon"]["emoji"], post["properties"]["title"]["title"][0]["text"]["content"]))
        kids = self.client.kids(page_id)
        self.assertEqual(kids[0]["id"], self.semester["anchor_block_id"])
        self.assertEqual("gray", kids[0]["paragraph"]["color"])
        self.assertTrue(text(kids[0]["paragraph"]["rich_text"]).startswith("2026-2학기 · 6주차 · 자동 갱신 10월 10일 21:30"))
        self.assertEqual(["paragraph", "callout", "table", "toggle", "toggle"], self.layout())
        self.assertEqual((self.client.pages[page_id]["url"], self.client.root),
                         (self.semester["url"], self.ledger.registry["root_page_id"]))
        self.assertIn("학기 현황판도 갱신했습니다.", out)
        self.assertEqual(["child_page"] * 3, [block["type"] for block in self.client.kids(self.client.root)])

    def test_create_is_idempotent(self) -> None:
        self.created()
        posts = self.client.count("POST", "/pages")
        code, out, _ = self.run_command("create")
        self.assertEqual((0, posts), (code, self.client.count("POST", "/pages")))
        self.assertIn("학기 페이지가 이미 있습니다", out)
        self.assertEqual(["paragraph", "callout", "table", "toggle", "toggle"], self.layout())

    def test_create_refuses_public_root_and_trashed_root(self) -> None:
        self.client.pages[self.client.root]["public_url"] = "https://someone.notion.site/x"
        with self.assertRaises(pn.NotionError):
            self.run_command("create")
        self.assertEqual([], self.client.writes())
        self.assertEqual({}, self.semester)
        self.client.pages[self.client.root].update(public_url=None, in_trash=True)
        with self.assertRaises(pn.NotionError):
            self.run_command("create")
        self.assertEqual([], self.client.writes())

    def test_create_dry_run_only_reads(self) -> None:
        code, out, _ = self.run_command("create", dry_run=True)
        self.assertEqual(0, code)
        self.assertEqual([], self.client.writes())
        self.assertEqual({}, self.semester)
        self.assertIn("상위 페이지: 공부하자\n만들 학기 페이지: 2026-2학기", out)
        self.assertIn("✋ 내가 할 일", out)

    def test_root_comes_from_link_or_needs_one_when_courses_differ(self) -> None:
        other = self.client.add_page(None, "다른 상위")
        state = pn.load_state(self.courses[1][0])
        pn.save_state(self.courses[1][0], {**state, "parent_page_id": other})
        with self.assertRaises(pn.NotionError) as raised:
            self.run_command("create")
        self.assertIn("--root", str(raised.exception))
        self.assertEqual(0, self.run_command("create", root=URL + "x-" + other.replace("-", ""))[0])
        self.assertEqual(other, self.client.pages[self.semester["page_id"]]["parent"]["page_id"])

    def test_no_semester_or_unknown_semester(self) -> None:
        self.ledger.registry["current"] = None
        with self.assertRaises(pn.NotionError):
            self.run_command("create")
        with self.assertRaises(pn.NotionError):
            self.run_command("create", semester="2027-1")
        self.assertEqual([], self.client.writes())


class RefreshTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.created()
        self.moved()

    def ids(self) -> list[str]:
        return [block["id"] for block in self.client.kids(self.semester["page_id"])]

    def test_dashboard_sits_between_anchor_and_course_pages(self) -> None:
        expected = ["paragraph", "callout", "table", "toggle", "toggle", "child_page", "child_page"]
        self.assertEqual(expected, self.layout())
        before = self.ids()
        calls = len(self.client.calls)
        self.assertEqual("updated", nd.refresh(self.client, "2026-2", NOW))
        self.assertEqual(expected, self.layout())
        after = self.ids()
        self.assertEqual((before[0], before[-2:]), (after[0], after[-2:]))  # 기준 줄과 과목 페이지는 그대로
        self.assertFalse(set(before[1:5]) & set(after[1:5]))  # 예전 현황판은 지우고 새로 그렸다
        deleted = [path.split("/")[-1] for method, path, _, _ in self.client.calls[calls:] if method == "DELETE"]
        self.assertEqual(sorted(before[1:5]), sorted(deleted))
        positioned = [call for call in self.client.calls[calls:] if call[2] and "position" in call[2]]
        self.assertTrue(positioned and all(version == pn.DASHBOARD_VERSION for *_, version in positioned))
        others = [call for call in self.client.calls[calls:] if not (call[2] and "position" in call[2])]
        self.assertTrue(all(version is None for *_, version in others))  # 나머지 요청은 기본 버전
        self.assertFalse(any(page["in_trash"] for page in self.client.pages.values()))

    def test_user_text_after_course_pages_is_never_touched(self) -> None:
        page_id = self.semester["page_id"]
        self.client.insert(page_id, [{"type": "paragraph", "paragraph": {"rich_text": [pn.text_item("내 메모")]}}],
                           len(self.client.kids(page_id)))
        memo = self.client.kids(page_id)[-1]["id"]
        nd.refresh(self.client, "2026-2", NOW)
        self.assertEqual(memo, self.ids()[-1])
        self.assertNotIn(("DELETE", f"/blocks/{memo}"), [(method, path) for method, path, _, _ in self.client.calls])

    def test_page_dragged_into_dashboard_is_kept_and_reported(self) -> None:
        toggle = self.client.kids(self.semester["page_id"])[3]
        hidden = self.client.add_page(toggle["id"], "숨은 페이지", in_block=True)
        calls = len(self.client.calls)
        nd.refresh(self.client, "2026-2", NOW)
        self.assertIn(toggle["id"], self.ids())
        deleted = [path for method, path, _, _ in self.client.calls[calls:] if method == "DELETE"]
        self.assertNotIn(f"/blocks/{toggle['id']}", deleted)
        self.assertFalse(self.client.pages[hidden]["in_trash"])
        callout = self.client.kids(self.semester["page_id"])[1]
        todo = [text(item["bulleted_list_item"]["rich_text"]) for item in self.client.kids(callout["id"])]
        self.assertIn("현황판 안에 들어간 페이지를 밖으로 꺼내 주세요: 숨은 페이지", todo)
        self.assertEqual(1, self.layout().count("callout"))

    def test_page_nested_deep_inside_dashboard_is_kept(self) -> None:
        page_id = self.semester["page_id"]
        callout = self.client.kids(page_id)[1]
        bullet = self.client.kids(callout["id"])[0]
        self.client.insert(bullet["id"], [{"type": "bulleted_list_item",
                                           "bulleted_list_item": {"rich_text": [pn.text_item("하위 항목")]}}], 0)
        bullet["has_children"] = True
        deep = self.client.add_page(self.client.kids(bullet["id"])[0]["id"], "깊은 페이지", in_block=True)  # 할 일 › 항목 › 하위 항목 › 페이지
        self.client.insert(page_id, [{"type": "column_list", "column_list": {"children": [{"type": "column", "column": {
            "children": [{"type": "toggle", "toggle": {"rich_text": [pn.text_item("접기")]}}]}}]}}], 2)
        columns = self.client.kids(page_id)[2]
        toggle = self.client.kids(self.client.kids(columns["id"])[0]["id"])[0]
        sided = self.client.add_page(toggle["id"], "단 속 페이지", in_block=True)  # 단 묶음 › 단 › 토글 › 페이지
        tables = [block["id"] for parent in [page_id, *self.client.blocks] for block in self.client.kids(parent)
                  if block["type"] == "table"]
        calls = len(self.client.calls)
        nd.refresh(self.client, "2026-2", NOW)
        deleted = [path for method, path, _, _ in self.client.calls[calls:] if method == "DELETE"]
        for kept in (callout["id"], columns["id"]):
            self.assertNotIn(f"/blocks/{kept}", deleted)
            self.assertIn(kept, self.ids())
        self.assertFalse(self.client.pages[deep]["in_trash"] or self.client.pages[sided]["in_trash"])
        todo = [text(item["bulleted_list_item"]["rich_text"]) for item in self.client.kids(self.client.kids(page_id)[1]["id"])]
        for title in ("깊은 페이지", "단 속 페이지"):
            self.assertIn(f"현황판 안에 들어간 페이지를 밖으로 꺼내 주세요: {title}", todo)
        read = {path.split("?")[0] for method, path, _, _ in self.client.calls[calls:] if method == "GET"}
        self.assertFalse({f"/blocks/{table}/children" for table in tables} & read)  # 표는 속을 읽지 않는다

    def test_anchor_below_course_pages_refuses_and_deletes_nothing(self) -> None:
        page_id = self.semester["page_id"]
        kids = self.client.kids(page_id)
        kids.sort(key=lambda block: block["type"] != "child_page")  # 과목 페이지를 기준 줄 위로 끌어 올림
        self.client.insert(page_id, [{"type": "paragraph", "paragraph": {"rich_text": [pn.text_item("내 메모: 시험 범위")]}}],
                           len(kids))
        before = copy.deepcopy(kids)
        calls = len(self.client.calls)
        with self.assertRaises(pn.NotionError) as raised:
            nd.refresh(self.client, "2026-2", NOW)
        self.assertIn("기준 줄", str(raised.exception))
        self.assertIn("과목 페이지들 위로 옮긴 뒤", str(raised.exception))
        self.assertEqual([], [call for call in self.client.calls[calls:] if call[0] != "GET"])
        self.assertEqual(before, self.client.kids(page_id))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            nd.refresh_quietly(self.client, "2026-2", NOW)  # 옮기기·만들기 뒤 갱신은 경고만 한다
        self.assertIn("[경고] 학기 현황판을 갱신하지 못했습니다: 현황판 기준 줄", err.getvalue())
        self.assertEqual(before, self.client.kids(page_id))

    def test_deleted_anchor_is_recreated_at_the_top(self) -> None:
        page_id = self.semester["page_id"]
        old_anchor = self.client.kids(page_id).pop(0)["id"]  # 사용자가 기준 줄을 지움
        calls = len(self.client.calls)
        nd.refresh(self.client, "2026-2", NOW)
        self.assertEqual(["paragraph", "callout", "table", "toggle", "toggle", "child_page", "child_page"], self.layout())
        new_anchor = self.ids()[0]
        self.assertNotEqual(old_anchor, new_anchor)
        self.assertEqual(new_anchor, self.semester["anchor_block_id"])
        start = next(call for call in self.client.calls[calls:] if call[2] and call[2].get("position") == {"type": "start"})
        self.assertEqual(pn.DASHBOARD_VERSION, start[3])
        self.assertFalse(any(page["in_trash"] for page in self.client.pages.values()))

    def test_failed_insert_heals_on_next_refresh(self) -> None:
        with patch.object(pn, "REQUEST_ELEMENT_LIMIT", 1):  # 블록 하나마다 요청 하나
            self.client.fail_appends = {self.client.appends + 3}
            with self.assertRaises(pn.NotionError):
                nd.refresh(self.client, "2026-2", NOW)
            self.assertEqual(2, self.layout().count("callout"))  # 새 현황판 일부와 예전 현황판이 겹쳐 있다
            nd.refresh(self.client, "2026-2", NOW)
        self.assertEqual(["paragraph", "callout", "table", "toggle", "toggle", "child_page", "child_page"], self.layout())

    def test_many_chunks_keep_order(self) -> None:
        calls = len(self.client.calls)
        with patch.object(pn, "REQUEST_ELEMENT_LIMIT", 1):
            nd.refresh(self.client, "2026-2", NOW)
        inserts = [body for method, path, body, _ in self.client.calls[calls:]
                   if method == "PATCH" and path.endswith("/children")]
        self.assertEqual(4, len(inserts))
        self.assertEqual(self.semester["anchor_block_id"], inserts[0]["position"]["after_block"]["id"])
        self.assertEqual(["paragraph", "callout", "table", "toggle", "toggle", "child_page", "child_page"], self.layout())

    def test_refresh_refuses_public_or_trashed_semester_page(self) -> None:
        calls = len(self.client.calls)
        self.client.pages[self.semester["page_id"]]["public_url"] = "https://someone.notion.site/s"
        with self.assertRaises(pn.NotionError) as raised:
            nd.refresh(self.client, "2026-2", NOW)
        self.assertIn("웹에 공개", str(raised.exception))
        self.client.pages[self.semester["page_id"]]["public_url"] = None
        pn._PRIVATE_SEEN.clear()  # 새 프로세스처럼: 확인해 둔 상위 페이지를 다시 읽는다
        self.client.pages[self.client.root]["public_url"] = "https://someone.notion.site/x"
        with self.assertRaises(pn.NotionError):
            nd.refresh(self.client, "2026-2", NOW)
        self.client.pages[self.client.root]["public_url"] = None
        self.client.pages[self.semester["page_id"]]["in_trash"] = True
        with self.assertRaises(pn.NotionError):
            nd.refresh(self.client, "2026-2", NOW)
        self.assertEqual([], [call for call in self.client.calls[calls:] if call[0] != "GET"])

    def test_refresh_refuses_semester_page_in_a_column_of_a_public_page(self) -> None:
        sem, root = self.semester["page_id"], self.client.root
        self.client.insert(root, [{"type": "column_list", "column_list": {"children": [{"type": "column", "column": {}}]}}], 0)
        column = self.client.kids(self.client.kids(root)[0]["id"])[0]
        items, block = self.client.locate(sem)  # 사용자가 학기 페이지를 공부하자의 단 안으로 끌어 넣음
        items.remove(block)
        self.client.kids(column["id"]).append(block)
        column["has_children"] = True
        self.client.pages[sem]["parent"] = {"type": "block_id", "block_id": column["id"]}
        self.client.pages[root]["public_url"] = "https://someone.notion.site/x"
        pn._PRIVATE_SEEN.clear()  # 새 프로세스처럼
        calls = len(self.client.calls)
        with self.assertRaises(pn.NotionError) as raised:
            nd.refresh(self.client, "2026-2", NOW)
        self.assertIn("웹에 공개", str(raised.exception))
        self.assertEqual([], [call for call in self.client.calls[calls:] if call[0] != "GET"])
        read = [path for method, path, _, _ in self.client.calls[calls:] if method == "GET"]
        self.assertIn(f"/blocks/{column['id']}", read)
        self.assertIn(f"/pages/{root}", read)

    def test_refresh_needs_semester_page(self) -> None:
        self.ledger.registry["semesters"]["2026-2"].pop("notion")
        with self.assertRaises(pn.NotionError) as raised:
            nd.refresh(self.client, "2026-2", NOW)
        self.assertIn("semester create", str(raised.exception))
        self.assertIsNone(nd.refresh_for_course(self.client, self.courses[0][0]))

    def test_refresh_for_course_and_dashboard_command(self) -> None:
        outside = self.base / "다른 과목"
        outside.mkdir()
        self.assertIsNone(nd.refresh_for_course(self.client, outside))
        self.assertEqual("updated", nd.refresh_for_course(self.client, self.courses[0][0], NOW))
        self.assertEqual("2026-2", self.ledger.status_calls[-1])
        out = io.StringIO()
        with patch.object(pn, "load_token", return_value="ntn_test"), \
                patch.object(pn, "NotionClient", return_value=self.client), contextlib.redirect_stdout(out):
            code = pn.main(["dashboard", "--course-dir", str(self.courses[0][0])])
        self.assertEqual(0, code)
        self.assertIn("학기 현황판을 갱신했습니다", out.getvalue())
        self.assertEqual(1, self.layout().count("callout"))


class SemesterMoveTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.created()

    def state(self, index: int) -> dict:
        return pn.load_state(self.courses[index][0])

    def test_move_reparents_and_keeps_records(self) -> None:
        before = [self.state(index) for index in range(2)]
        code, out, _ = self.run_command("move")
        self.assertEqual(0, code)
        self.assertEqual(2, out.count(": 옮김"))
        sem = self.semester["page_id"]
        for index, (course, page) in enumerate(self.courses):
            self.assertEqual(sem, self.client.pages[page]["parent"]["page_id"])
            state = self.state(index)
            self.assertEqual(sem, state["parent_page_id"])
            self.assertEqual({**before[index], "parent_page_id": sem}, state)  # 노트 기록과 과목 페이지 ID는 그대로
        moves = [call for call in self.client.calls if call[1].endswith("/move")]
        self.assertEqual(2, len(moves))
        self.assertTrue(all(version == pn.DASHBOARD_VERSION for *_, version in moves))
        self.assertEqual(["paragraph", "callout", "table", "toggle", "toggle", "child_page", "child_page"], self.layout())

    def test_move_again_only_reports(self) -> None:
        self.moved()
        code, out, _ = self.run_command("move")
        self.assertEqual(0, code)
        self.assertEqual(2, out.count("이미 학기 아래"))
        self.assertEqual(2, len([call for call in self.client.calls if call[1].endswith("/move")]))

    def test_unexpected_parent_is_left_alone(self) -> None:
        elsewhere = self.client.add_page(self.client.root, "다른 곳")
        self.client.drag(self.courses[0][1], elsewhere)  # 사용자가 다른 곳에 둔 과목 페이지
        code, out, _ = self.run_command("move")
        self.assertEqual(1, code)
        self.assertIn("과목A: 예상과 다른 위치", out)
        self.assertEqual(elsewhere, self.client.pages[self.courses[0][1]]["parent"]["page_id"])
        self.assertEqual(self.client.root, self.state(0)["parent_page_id"])
        self.assertEqual(self.semester["page_id"], self.state(1)["parent_page_id"])

    def test_unsupported_move_asks_for_a_drag_then_reconciles(self) -> None:
        self.client.fail_move = True
        code, out, _ = self.run_command("move")
        self.assertEqual(1, code)
        self.assertIn("노션 사이드바에서 '과목A' 페이지를 '2026-2학기' 페이지 안으로 끌어 넣은 뒤 같은 명령을 다시 실행하십시오.", out)
        self.assertEqual(self.client.root, self.state(0)["parent_page_id"])
        for _, page in self.courses:
            self.client.drag(page, self.semester["page_id"])
        code, out, _ = self.run_command("move")
        self.assertEqual(0, code)
        self.assertEqual(2, out.count("이미 학기 아래"))
        self.assertEqual([self.semester["page_id"]] * 2, [self.state(index)["parent_page_id"] for index in range(2)])

    def test_dry_run_changes_nothing(self) -> None:
        before = [self.state(index) for index in range(2)]
        writes = len(self.client.writes())
        code, out, _ = self.run_command("move", dry_run=True)
        self.assertEqual(0, code)
        self.assertEqual(2, out.count("옮길 예정"))
        self.assertEqual(writes, len(self.client.writes()))
        self.assertEqual(before, [self.state(index) for index in range(2)])

    def test_unconnected_and_missing_course_pages_are_reported(self) -> None:
        bare = self.base / "과목C"
        bare.mkdir()
        self.ledger.registry["semesters"]["2026-2"]["courses"].append(bare.resolve().as_posix())
        del self.client.pages[self.courses[1][1]]
        code, out, _ = self.run_command("move")
        self.assertEqual(0, code)
        self.assertIn("과목C: 연결 안 됨", out)
        self.assertIn("과목B: 과목 페이지를 찾지 못함", out)
        self.assertIn("과목A: 옮김", out)
        self.assertFalse((bare / ".gongbu").exists())

    def test_legacy_course_hint_leads_to_setup_then_move(self) -> None:
        course, page = self.courses[0]
        pn.save_state(course, {**self.state(0), "version": 1, "database_id": "db-1", "data_source_id": "ds-1"})
        code, out, _ = self.run_command("move")
        self.assertEqual(1, code)
        self.assertIn("과목A: 예전 표 방식이라 건너뜀", out)
        self.assertIn("`gongbu notion setup`으로 새 방식으로 바꾼 뒤 이 명령을 다시 실행하십시오", out)
        self.assertIn("`gongbu notion setup <예전 상위 페이지 링크>`", out)
        self.assertEqual(self.client.root, self.client.pages[page]["parent"]["page_id"])
        posts = self.client.count("POST", "/pages")
        upgraded = pn.setup(self.client, course, None, "과목A")  # 안내대로 링크 없이: 원래 자리에서 새 방식으로 바꾼다
        self.assertEqual((page, self.client.root, {}), (upgraded["course_page_id"], upgraded["parent_page_id"], upgraded["notes"]))
        self.assertEqual(posts, self.client.count("POST", "/pages"))
        self.assertNotIn("data_source_id", self.state(0))
        code, out, _ = self.run_command("move")
        self.assertEqual(0, code)
        self.assertIn("과목A: 옮김", out)
        self.assertEqual((self.semester["page_id"],) * 2, (self.client.pages[page]["parent"]["page_id"],
                                                           self.state(0)["parent_page_id"]))

    def test_move_needs_semester_page(self) -> None:
        self.ledger.registry["semesters"]["2026-2"].pop("notion")
        with self.assertRaises(pn.NotionError):
            self.run_command("move")
        self.assertFalse(any(call[1].endswith("/move") for call in self.client.calls))


class PreviewTests(unittest.TestCase):
    def test_preview_runs_without_token_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary)
            ledger = FakeLedger(course, {"version": 1, "current": "2026-2", "root_page_id": None,
                                         "semesters": {"2026-2": {"title": "2026-2학기", "courses": []}}})
            out = io.StringIO()
            with patch.dict(os.environ, {"GONGBU_HAJA_CONFIG": str(course / "config")}), \
                    patch.object(nd, "_ledger", return_value=ledger), \
                    patch.object(pn, "load_token", side_effect=AssertionError("토큰을 읽으면 안 된다")), \
                    patch.object(pn, "NotionClient", side_effect=AssertionError("네트워크를 쓰면 안 된다")), \
                    contextlib.redirect_stdout(out):
                code = pn.main(["dashboard", "--preview", "--course-dir", str(course)])
            self.assertEqual(0, code)
            self.assertIn("2026-2학기 · 6주차", out.getvalue())
            self.assertIn("▸ 일반물리학1 · 강의별", out.getvalue())
            self.assertEqual(["2026-2"], ledger.status_calls)
            self.assertFalse((course / ".gongbu").exists())

    def test_preview_without_any_semester_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            err = io.StringIO()
            with patch.object(nd, "_ledger", return_value=FakeLedger(Path(temporary))), contextlib.redirect_stderr(err):
                code = pn.main(["dashboard", "--preview", "--course-dir", temporary])
            self.assertEqual(2, code)
            self.assertIn("학기를 정하지 못했습니다", err.getvalue())


if __name__ == "__main__":
    unittest.main()
