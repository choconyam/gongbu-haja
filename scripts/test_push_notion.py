"""노션 올리기를 가짜 노션 응답으로 확인한다. 네트워크와 실제 토큰은 쓰지 않는다."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import push_notion as pn

NOTE = """# 과목A 1주차 — 미디어의 이해
<!-- units: handout-p01 -->

## 1차시 — 미디어의 개념

### 정의

미디어는 **메시지를 전달하는 매개체**다. 공식은 $E=mc^2$이고 `코드`도 있다 [확인 필요].

| 구분 | 설명 |
|---|---|
| **신문** | 인쇄 매체 |
| 방송 | 전파 \\| 케이블 |

1. 첫째
   - 안쪽 항목
2. 둘째

- [x] 끝냄

> [!WARNING]
> **주의할 점**
> 순서가 중요하다.

$$
a^2+b^2=c^2
$$

---

## 2차시 — 방송의 탄생

[로컬 노트](../2주차/노트.md)와 [사이트](https://example.com/a).

## 후속 역할 인계 메모

학생용에는 들어가면 안 된다.
"""

PARENT = "https://www.notion.so/workspace/공부하자-0123456789abcdef0123456789abcdef?pvs=4"


class FakeClient:
    """요청을 기록하고 노션처럼 답하는 가짜 클라이언트. 페이지마다 붙은 블록도 기억한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.pages: dict[str, dict] = {"01234567-89ab-cdef-0123-456789abcdef": {"id": "parent", "public_url": None}}
        self.blocks: dict[str, list[dict]] = {}
        self.clock = 0
        self.created = 0
        self.fail_on_append = False

    def stamp(self) -> str:
        self.clock += 1
        return f"2026-10-05T00:{self.clock:02d}:00.000Z"

    def owner(self, block_id: str) -> tuple[str, dict]:
        for page_id, items in self.blocks.items():
            for block in items:
                if block["id"] == block_id:
                    return page_id, block
        raise pn.NotionError("없음", 404, "object_not_found")

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        body = copy.deepcopy(body)
        self.calls.append((method, path, copy.deepcopy(body)))
        route = path.split("?")[0]
        target = route.split("/")[2] if route.count("/") >= 2 else ""
        if method == "GET" and route.startswith("/pages/"):
            page = self.pages.get(target)
            if page is None:
                raise pn.NotionError("없음", 404, "object_not_found")
            return page
        if method == "GET" and route.endswith("/children"):
            limit = int(path.split("page_size=")[1].split("&")[0]) if "page_size=" in path else 100
            return {"results": copy.deepcopy(self.blocks.get(target, [])[:limit]), "has_more": False}
        if method == "POST" and route == "/pages":
            self.created += 1
            page_id = f"page-{self.created}"
            page = {"id": page_id, "url": f"https://www.notion.so/{page_id}", "in_trash": False,
                    "last_edited_time": self.stamp(), "properties": body["properties"], "parent": body["parent"],
                    "icon": body.get("icon")}
            self.pages[page_id] = page
            return page
        if method == "POST" and route == "/file_uploads":
            self.created += 1
            return {"id": f"upload-{self.created}", "status": "pending"}
        if method == "PATCH" and route.endswith("/children"):
            if self.fail_on_append:
                raise pn.NotionError("서버 오류", 500, "internal_server_error")
            self.store(target, body["children"])
            self.pages[target]["last_edited_time"] = self.stamp()
            return {"results": body["children"]}
        if method in ("PATCH", "DELETE") and route.startswith("/blocks/"):
            parent, block = self.owner(target)
            if method == "DELETE":
                self.blocks[parent].remove(block)
            else:
                block.update(body)
            if parent in self.pages:
                self.pages[parent]["last_edited_time"] = self.stamp()
            return block
        if method == "PATCH" and route.startswith("/pages/"):
            page = self.pages[target]
            if "in_trash" in body:
                page["in_trash"] = body["in_trash"]
            if "icon" in body:
                page["icon"] = body["icon"]
            page.setdefault("properties", {}).update(body.get("properties", {}))
            page["last_edited_time"] = self.stamp()
            return page
        raise AssertionError(f"예상하지 못한 요청: {method} {path}")

    def send_file(self, upload_id: str, filename: str, data: bytes, content_type: str) -> dict:
        self.calls.append(("SEND", f"/file_uploads/{upload_id}/send", {"filename": filename, "bytes": len(data)}))
        return {"id": upload_id, "status": "uploaded"}

    def store(self, parent: str, items: list[dict]) -> None:
        """노션처럼 하위 블록은 따로 두고 has_children으로 알린다."""
        for block in items:
            self.created += 1
            block = copy.deepcopy(block)
            nested = block[block["type"]].pop("children", [])
            block.update(id=f"block-{self.created}", has_children=bool(nested))
            self.blocks.setdefault(parent, []).append(block)
            if nested:
                self.store(block["id"], nested)

    def count(self, method: str, prefix: str) -> int:
        return sum(1 for call in self.calls if call[0] == method and call[1].startswith(prefix))

    def title(self, page_id: str) -> str:
        return self.pages[page_id]["properties"]["title"]["title"][0]["text"]["content"]


def texts(block: dict) -> str:
    body = block[block["type"]]
    return "".join(item.get("text", {}).get("content", "") or item.get("equation", {}).get("expression", "")
                   for item in body.get("rich_text", []))


class ConvertTests(unittest.TestCase):
    def test_title_summary_and_heading_levels(self) -> None:
        note = pn.convert(NOTE, "과목A")
        self.assertEqual("1주차 · 미디어의 이해", note.title)
        self.assertEqual("미디어의 개념 · 방송의 탄생", note.summary)
        kinds = [block["type"] for block in note.blocks]
        self.assertEqual("heading_1", kinds[0])
        self.assertEqual("1차시 — 미디어의 개념", texts(note.blocks[0]))
        self.assertIn("heading_2", kinds)
        # 추적 주석과 인계 메모는 올리지 않는다.
        dumped = json.dumps(note.blocks, ensure_ascii=False)
        self.assertNotIn("units:", dumped)
        self.assertNotIn("학생용에는", dumped)

    def test_rich_text_marks_bold_code_math_links_and_uncertainty(self) -> None:
        problems: list[str] = []
        items = pn.rich_text("**굵게** `코드` $x^2$ [사이트](https://example.com) [확인 필요] [노트](a.md)", problems)
        self.assertEqual({"bold": True}, items[0]["annotations"])
        self.assertIn({"type": "text", "text": {"content": "코드"}, "annotations": {"code": True}}, items)
        self.assertIn({"type": "equation", "equation": {"expression": "x^2"}}, items)
        self.assertIn({"type": "text", "text": {"content": "사이트", "link": {"url": "https://example.com"}}}, items)
        self.assertIn({"type": "text", "text": {"content": "[확인 필요]"}, "annotations": {"color": "yellow_background"}}, items)
        self.assertTrue(any("로컬 경로 링크" in problem for problem in problems))
        # 가격 표기의 $는 수식이 아니다.
        self.assertEqual([{"type": "text", "text": {"content": "가격은 $5와 $10"}}], pn.rich_text("가격은 $5와 $10"))

    def test_blocks_for_table_lists_callout_equation_and_divider(self) -> None:
        note = pn.convert(NOTE, "과목A")
        table = next(block for block in note.blocks if block["type"] == "table")["table"]
        self.assertEqual((2, True, 3), (table["table_width"], table["has_column_header"], len(table["children"])))
        self.assertEqual("전파 | 케이블", "".join(item["text"]["content"] for item in table["children"][2]["table_row"]["cells"][1]))
        numbered = [block for block in note.blocks if block["type"] == "numbered_list_item"]
        self.assertEqual("안쪽 항목", texts(numbered[0]["numbered_list_item"]["children"][0]))
        todo = next(block for block in note.blocks if block["type"] == "to_do")
        self.assertTrue(todo["to_do"]["checked"])
        callout = next(block for block in note.blocks if block["type"] == "callout")["callout"]
        self.assertEqual(("⚠️", "orange_background"), (callout["icon"]["emoji"], callout["color"]))
        self.assertEqual({"bold": True}, callout["rich_text"][0]["annotations"])
        self.assertEqual("주의할 점\n순서가 중요하다.", "".join(item["text"]["content"] for item in callout["rich_text"]))
        self.assertIn({"type": "equation", "equation": {"expression": "a^2+b^2=c^2"}}, note.blocks)
        self.assertIn({"type": "divider", "divider": {}}, note.blocks)

    def test_limits_long_text_long_equation_and_request_chunks(self) -> None:
        long_text = "가" * 4500
        items = pn.rich_text(long_text)
        self.assertTrue(all(len(item["text"]["content"]) <= pn.TEXT_LIMIT for item in items))
        self.assertEqual(long_text, "".join(item["text"]["content"] for item in items))
        note = pn.convert("# 과목 1주차 — 주제\n\n$$\n" + "x+" * 600 + "x\n$$\n")
        self.assertTrue(note.errors)
        many = [{"type": "paragraph", "paragraph": {"rich_text": []}}] * 250
        self.assertEqual([100, 100, 50], [len(chunk) for chunk in pn.request_chunks(many)])

    def test_big_table_is_split_with_repeated_header(self) -> None:
        rows = [["머리", "값"]] + [[str(number), "x"] for number in range(150)]
        tables = pn.table_blocks(rows, [])
        self.assertEqual(2, len(tables))
        self.assertTrue(all(len(table["table"]["children"]) <= pn.ARRAY_LIMIT for table in tables))
        self.assertEqual("머리", tables[1]["table"]["children"][0]["table_row"]["cells"][0][0]["text"]["content"])

    def test_page_id_from_link(self) -> None:
        self.assertEqual("01234567-89ab-cdef-0123-456789abcdef", pn.page_id(PARENT))
        with self.assertRaises(pn.NotionError):
            pn.page_id("https://www.notion.so/없는링크")


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.course = Path(self.temp.name)
        self.note = self.course / "1주차" / "노트.md"
        self.note.parent.mkdir()
        self.note.write_text(NOTE, encoding="utf-8")
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def push(self, **options: Any) -> dict:
        state = pn.load_state(self.course)
        plan = pn.plan_push(self.client, self.course, self.note, state, **options)
        self.last_action = plan.action
        return pn.push(self.client, self.course, plan, state, "2026-10-05")

    def test_setup_creates_course_page_once(self) -> None:
        state = pn.setup(self.client, self.course, PARENT, "과목A")
        course = next(body for method, path, body in self.client.calls if method == "POST" and path == "/pages")
        self.assertEqual({"type": "page_id", "page_id": "01234567-89ab-cdef-0123-456789abcdef"}, course["parent"])
        self.assertEqual("과목A", course["properties"]["title"]["title"][0]["text"]["content"])
        self.assertEqual(pn.STATE_VERSION, state["version"])
        self.assertNotIn("data_source_id", state)
        self.assertTrue(state["slides"])  # 새 과목은 묻지 않고 슬라이드를 올린다(비공개 페이지에만)
        self.assertFalse(pn.setup(self.client, self.course, PARENT, "과목A", False)["slides"])  # 빼라고 할 때만 끈다
        self.assertTrue(pn.state_path(self.course).is_file())
        calls = len(self.client.calls)
        pn.setup(self.client, self.course, PARENT, "과목A")  # 다시 해도 새로 만들지 않는다
        self.assertEqual(0, sum(1 for method, path, _ in self.client.calls[calls:] if method == "POST"))

    def test_setup_moves_old_table_course_to_child_pages(self) -> None:
        state = pn.setup(self.client, self.course, PARENT, "과목A")
        old = {**state, "version": 1, "database_id": "db-1", "data_source_id": "ds-1",
               "notes": {"1주차/노트.md": {"page_id": "row-1", "content_sha256": "x"}}}
        pn.save_state(self.course, old)
        with self.assertRaises(pn.NotionError):  # 표의 줄을 차시 페이지로 고치지 않는다
            pn.plan_push(self.client, self.course, self.note, pn.load_state(self.course))
        calls = len(self.client.calls)
        upgraded = pn.setup(self.client, self.course, PARENT, "과목A")
        self.assertEqual(0, sum(1 for method, _, _ in self.client.calls[calls:] if method == "POST"))
        self.assertEqual(state["course_page_id"], upgraded["course_page_id"])
        self.assertEqual({}, upgraded["notes"])
        self.assertFalse({"database_id", "data_source_id"} & set(pn.load_state(self.course)))
        self.push()
        self.assertEqual("create", self.last_action)

    def test_setup_refuses_public_parent_page(self) -> None:
        self.client.pages["01234567-89ab-cdef-0123-456789abcdef"]["public_url"] = "https://notion.site/x"
        with self.assertRaises(pn.NotionError):
            pn.setup(self.client, self.course, PARENT, "과목A")
        self.assertFalse(pn.state_path(self.course).exists())

    def test_first_push_creates_child_page_of_course_page(self) -> None:
        state = pn.setup(self.client, self.course, PARENT, "과목A")
        record = self.push()
        page = [body for method, path, body in self.client.calls if method == "POST" and path == "/pages"][-1]
        self.assertEqual({"type": "page_id", "page_id": state["course_page_id"]}, page["parent"])
        self.assertEqual("업로드 중 · 1주차 · 미디어의 이해", page["properties"]["title"]["title"][0]["text"]["content"])
        self.assertEqual("📗", page["icon"]["emoji"])
        self.assertEqual("1주차 · 미디어의 이해", self.client.title(record["page_id"]))
        header = self.client.blocks[record["page_id"]][0]
        self.assertEqual("gray", header["paragraph"]["color"])
        self.assertEqual("자료 충실형 · 미디어의 개념 · 방송의 탄생", texts(header))
        saved = pn.load_state(self.course)["notes"]["1주차/노트.md"]
        self.assertEqual(record["last_edited_time"], saved["last_edited_time"])

    def test_unchanged_note_is_skipped_and_header_change_only_patches(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        record = self.push()
        posts = self.client.count("POST", "/pages")
        self.push()
        self.assertEqual("skip", self.last_action)
        self.push(handout="교안 01·02")
        self.assertEqual("properties", self.last_action)
        self.assertEqual(posts, self.client.count("POST", "/pages"))
        self.assertEqual(0, self.client.count("DELETE", "/blocks/"))
        self.assertEqual("자료 충실형 · 교안 01·02 · 미디어의 개념 · 방송의 탄생", texts(self.client.blocks[record["page_id"]][0]))
        self.push(handout="교안 01·02")
        self.assertEqual("skip", self.last_action)

    def test_changed_note_replaces_content_of_the_same_page(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        first = self.push()
        old_ids = [block["id"] for block in self.client.blocks[first["page_id"]]]
        posts = self.client.count("POST", "/pages")
        self.note.write_text(NOTE.replace("첫째", "첫째 항목"), encoding="utf-8")
        second = self.push()
        self.assertEqual("replace", self.last_action)
        self.assertEqual(first["page_id"], second["page_id"])  # 과목 페이지 안의 자리와 링크가 그대로다
        self.assertEqual(posts, self.client.count("POST", "/pages"))
        self.assertEqual(len(old_ids), self.client.count("DELETE", "/blocks/"))
        current = self.client.blocks[first["page_id"]]
        self.assertFalse({block["id"] for block in current} & set(old_ids))
        self.assertIn("첫째 항목", json.dumps(current, ensure_ascii=False))
        self.assertEqual("1주차 · 미디어의 이해", self.client.title(first["page_id"]))
        self.push()
        self.assertEqual("skip", self.last_action)

    def test_page_edited_in_notion_stops_until_the_edit_is_in_the_note(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        first = self.push()
        edited = next(block for block in self.client.blocks[first["page_id"]][1:] if block["type"] == "paragraph")
        edited["paragraph"]["rich_text"] = [{"type": "text", "text": {"content": "노션에서 고친 문장"}}]  # 사용자가 노션에서 고침
        self.client.pages[first["page_id"]]["last_edited_time"] = "2026-10-06T09:00:00.000Z"
        plan = pn.plan_push(self.client, self.course, self.note, pn.load_state(self.course))
        self.assertEqual(("edited", False), (plan.action, plan.local_changed))
        self.note.write_text(NOTE.replace("첫째", "첫째 항목"), encoding="utf-8")  # 노션의 수정을 원고에 옮김
        posts, calls = self.client.count("POST", "/pages"), len(self.client.calls)
        with self.assertRaises(pn.NotionError):  # 수정이 두 페이지로 갈라지지 않게 새 페이지를 만들지 않는다
            self.push()
        self.assertEqual(("edited", True), (self.last_action, pn.plan_push(
            self.client, self.course, self.note, pn.load_state(self.course)).local_changed))
        self.assertEqual(0, sum(1 for method, *_ in self.client.calls[calls:] if method != "GET"))
        second = self.push(overwrite=True)
        self.assertEqual("replace", self.last_action)
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertEqual(posts, self.client.count("POST", "/pages"))
        self.assertEqual("1주차 · 미디어의 이해", self.client.title(first["page_id"]))
        self.assertIn("첫째 항목", json.dumps(self.client.blocks[first["page_id"]], ensure_ascii=False))
        self.push()
        self.assertEqual("skip", self.last_action)

    def test_empty_paragraph_or_bold_change_in_notion(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        first = self.push()
        page = self.client.pages[first["page_id"]]
        self.client.store(first["page_id"], [{"type": "paragraph", "paragraph": {"rich_text": []}}])  # 빈 곳을 누름
        page["last_edited_time"] = "2026-10-06T09:00:00.000Z"
        self.push()
        self.assertEqual("skip", self.last_action)  # 빈 문단은 고친 것으로 보지 않는다
        self.assertEqual("2026-10-06T09:00:00.000Z", pn.load_state(self.course)["notes"]["1주차/노트.md"]["last_edited_time"])
        reads = self.client.count("GET", "/blocks/")
        self.push()
        self.assertEqual(("skip", reads), (self.last_action, self.client.count("GET", "/blocks/")))  # 다시 읽지 않는다
        target = next(block for block in self.client.blocks[first["page_id"]] if block["type"] == "heading_1")
        target["heading_1"]["rich_text"][0]["annotations"] = {"bold": True}  # 서식만 바꿔도 고친 것이다
        page["last_edited_time"] = "2026-10-06T10:00:00.000Z"
        self.assertEqual("edited", pn.plan_push(self.client, self.course, self.note, pn.load_state(self.course)).action)

    def test_deleted_page_is_recreated(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        first = self.push()
        del self.client.pages[first["page_id"]]
        second = self.push()
        self.assertEqual("create", self.last_action)
        self.assertNotEqual(first["page_id"], second["page_id"])

    def test_failed_upload_trashes_partial_page_and_keeps_state(self) -> None:
        state = pn.setup(self.client, self.course, PARENT, "과목A")
        self.client.fail_on_append = True
        with self.assertRaises(pn.NotionError):
            self.push()
        partial = [page for page in self.client.pages.values()
                   if page.get("parent", {}).get("page_id") == state["course_page_id"]]
        self.assertTrue(partial and all(page["in_trash"] for page in partial))
        self.assertEqual({}, pn.load_state(self.course).get("notes"))

    def test_failed_replace_keeps_page_and_retries_without_mistaking_it_for_an_edit(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        first = self.push()
        self.note.write_text(NOTE.replace("첫째", "첫째 항목"), encoding="utf-8")
        self.client.fail_on_append = True
        with self.assertRaises(pn.NotionError):
            self.push()
        self.assertFalse(self.client.pages[first["page_id"]]["in_trash"])
        self.client.fail_on_append = False
        second = self.push()
        self.assertEqual("replace", self.last_action)
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertEqual("1주차 · 미디어의 이해", self.client.title(first["page_id"]))

    def test_conversion_errors_stop_before_any_request(self) -> None:
        pn.setup(self.client, self.course, PARENT, "과목A")
        self.note.write_text("# 과목A 1주차 — 주제\n\n$$\n" + "x+" * 600 + "x\n$$\n", encoding="utf-8")
        calls = len(self.client.calls)
        with self.assertRaises(pn.NotionError):
            self.push()
        self.assertEqual(calls, len(self.client.calls))


DEEP_TEX = r"""\documentclass{article}
\newcommand{\sourcepdf}{\detokenize{slides.pdf}}
\begin{document}
\gongbucover{일반물리학1}{3장 벡터}{벡터와 그 연산}
\section{벡터}
\sourceslide{2}
벡터 $\vec A$를 쓴다.
\begin{equation}
\vec C = \vec A + \vec B
\end{equation}
\sourceslide{5}
\sourceslide{2}
\end{document}
"""


class DeepNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.course = Path(self.temp.name)
        self.note = self.course / "03 학습노트.tex"
        self.note.write_text(DEEP_TEX, encoding="utf-8")
        (self.course / "slides.pdf").write_bytes(b"%PDF-1.4 fake")
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def push(self, slides: bool) -> dict:
        pn.setup(self.client, self.course, PARENT, "일반물리학1", slides)
        state = pn.load_state(self.course)
        plan = pn.plan_push(self.client, self.course, self.note, state)
        with patch.object(pn, "render_slide", return_value=(b"\x89PNG fake", "image/png")) as render:
            record = pn.push(self.client, self.course, plan, state, "2026-10-05")
        self.render_calls = render.call_args_list
        self.plan = plan
        return record

    def appended_blocks(self) -> list[dict]:
        return [block for method, path, body in self.client.calls if method == "PATCH" and path.endswith("/children")
                for block in body["children"]]

    def test_slides_are_uploaded_once_per_page_and_attached(self) -> None:
        self.push(slides=True)
        self.assertEqual([2, 5], [call.args[1] for call in self.render_calls])
        self.assertEqual(2, self.client.count("POST", "/file_uploads"))
        images = [block["image"] for block in self.appended_blocks() if block["type"] == "image"]
        self.assertEqual(3, len(images))
        self.assertTrue(all(image["file_upload"]["id"].startswith("upload-") for image in images))
        page = [body for method, path, body in self.client.calls if method == "POST" and path == "/pages"][-1]
        self.assertEqual("📘", page["icon"]["emoji"])
        self.assertEqual("업로드 중 · 3장 벡터", page["properties"]["title"]["title"][0]["text"]["content"])
        self.assertEqual("심화 이해형 · 교안 p.2–5 · 벡터와 그 연산", texts(self.appended_blocks()[0]))

    def test_slides_off_leaves_only_captions(self) -> None:
        self.push(slides=False)
        self.assertEqual(0, self.client.count("POST", "/file_uploads"))
        blocks = self.appended_blocks()
        self.assertFalse(any(block["type"] == "image" for block in blocks))
        self.assertIn("원본 PDF p.5", json.dumps(blocks, ensure_ascii=False))

    def test_missing_slide_pdf_stops_before_requests(self) -> None:
        (self.course / "slides.pdf").unlink()
        pn.setup(self.client, self.course, PARENT, "일반물리학1", True)
        calls = len(self.client.calls)
        with self.assertRaises(pn.NotionError):
            pn.plan_push(self.client, self.course, self.note, pn.load_state(self.course))
        self.assertEqual(calls, len(self.client.calls))

    def test_render_slide_draws_png_with_gray_border(self) -> None:
        try:
            import pypdfium2  # noqa: F401
            from PIL import Image
            from reportlab.pdfgen.canvas import Canvas
        except ImportError:
            self.skipTest("pypdfium2·Pillow·reportlab이 필요하다")
        pdf = self.course / "real.pdf"
        canvas = Canvas(str(pdf), pagesize=(453.5, 283.5))  # 작은 Beamer 판형도 같은 폭으로 그린다
        canvas.drawString(100, 140, "slide")
        canvas.showPage()
        canvas.save()
        data, content_type = pn.render_slide(pdf, 1)
        self.assertEqual("image/png", content_type)
        image = Image.open(io.BytesIO(data))
        self.assertAlmostEqual(pn.SLIDE_WIDTH_PX + 2, image.width, delta=1)
        self.assertEqual((208, 208, 208), image.getpixel((0, 0))[:3])
        with self.assertRaises(pn.NotionError):
            pn.render_slide(pdf, 2)


class TokenTests(unittest.TestCase):
    def test_login_refuses_without_a_real_terminal(self) -> None:
        with patch("sys.stdin", io.StringIO("ignored")), self.assertRaises(pn.NotionError) as raised:
            pn.command_login()
        self.assertIn("자기 터미널", str(raised.exception))

    def test_pasted_token_is_cleaned_of_terminal_paste_markers(self) -> None:
        token = "ntn" + "_" + "a1B2c3D4e5" * 5
        self.assertEqual(token, pn.clean_token(f"\x1b[200~{token}\x1b[201~\r\n"))
        self.assertEqual(token, pn.clean_token(f"  '{token}' "))
        self.assertEqual("", pn.clean_token("\x16"))  # 붙여 넣기 대신 Ctrl+V 글자만 들어온 경우

    def test_missing_keyring_explains_how_to_install(self) -> None:
        with patch.dict("sys.modules", {"keyring": None}), self.assertRaises(pn.NotionError) as raised:
            pn.load_token()
        self.assertIn("keyring", str(raised.exception))

    def test_client_never_puts_token_in_errors_or_repr(self) -> None:
        token = "ntn" + "_" + "a1B2c3D4e5" * 5

        def fail(request, timeout):
            raise pn.urllib.error.URLError("offline")

        client = pn.NotionClient(token, sleep=lambda seconds: None, opener=fail)
        with self.assertRaises(pn.NotionError) as raised:
            client.request("GET", "/pages/x")
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, repr(client))

    def test_check_command_runs_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            note = Path(temporary) / "노트.md"
            note.write_text(NOTE, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = pn.main(["check", str(note), "--course-dir", temporary])
            self.assertEqual(0, code)
            self.assertIn("블록:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
