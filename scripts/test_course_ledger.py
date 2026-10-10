"""과목 원장·학기 목록·현황 계산을 임시 폴더에서 확인한다. 네트워크·토큰·실제 설정 폴더는 쓰지 않는다."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import course_ledger as cl
from scripts import push_notion as pn

NOTE_TEX = r"""\documentclass{article}
\begin{document}
\gongbucover{과목A}{1장 벡터}{벡터 기초}
\section{벡터}
벡터 $\vec A$를 쓴다. [확인 필요: 부호] [전사 불명확] 그리고 [청취 불가 12:30].
\end{document}
"""
BROKEN_TEX = NOTE_TEX.replace(r"$\vec A$", r"\(\vec A")
SOURCE = "output/source/01 학습노트.tex"
HANDOUT = "[제공]과목A-01.pdf"
TRANSCRIPT = "과목A 1-1.txt"


def write(path: Path, content: str | bytes, age: float = 3600) -> Path:
    """파일을 쓰고 수정 시각을 age초 전으로 돌린다(기본: 이미 다 받은 파일)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def snapshot(*roots: Path) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                stat = path.stat()
                found[str(path)] = (stat.st_size if path.is_file() else -1, stat.st_mtime_ns)
    return found


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.course = self.base / "과목A"
        self.course.mkdir()
        self.config = self.base / "config"
        environment = patch.dict(os.environ, {cl.CONFIG_ENV: str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)

    # ---- 도우미
    def run_cli(self, *argv: str, expected: int = 0, course: Path | None = None) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cl.main([*argv, "--course-dir", str(course or self.course)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(expected, code, out.getvalue() + err.getvalue())
        return out.getvalue(), err.getvalue()

    def json_cli(self, *argv: str, **options: Any) -> dict[str, Any]:
        return json.loads(self.run_cli(*argv, **options)[0])

    def init(self, mode: str = "deep", materials: str = "handout+recording") -> dict[str, Any]:
        return self.json_cli("course", "init", "--name", "과목A", "--mode", mode, "--materials", materials)

    def import_plan(self, plan: dict[str, Any], *extra: str, expected: int = 0) -> tuple[str, str]:
        path = self.base / "plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return self.run_cli("course", "import", str(path), *extra, expected=expected)

    def ledger(self) -> dict[str, Any]:
        return json.loads(cl.ledger_path(self.course).read_text(encoding="utf-8"))

    def status(self, **options: Any) -> dict[str, Any]:
        return cl.collect_status(course_dirs=[self.course], **options)

    @staticmethod
    def lecture(document: dict[str, Any], lecture_id: str = "L01") -> dict[str, Any]:
        return next(item for item in document["courses"][0]["lectures"] if item["id"] == lecture_id)

    def standard(self, note: str | None = NOTE_TEX, progress: str = "in_progress") -> None:
        """교안 1개·전사 1개·노트 1개가 등록된 과목."""
        write(self.course / HANDOUT, b"%PDF-1.4 handout")
        write(self.course / TRANSCRIPT, "전사 내용\n")
        lecture: dict[str, Any] = {"id": "L01", "title": "1장 벡터", "materials": [
            {"kind": "handout", "path": HANDOUT}, {"kind": "transcript", "path": TRANSCRIPT}]}
        if note is not None:
            write(self.course / SOURCE, note)
            lecture["note"] = {"source": SOURCE, "progress": progress, "covers": "p.1–5", "outputs": ["01 학습노트.pdf"]}
        self.import_plan({"course": {"name": "과목A", "mode": "deep", "materials": "handout+recording"},
                          "lectures": [lecture]})

    def connect(self, **record: Any) -> dict[str, Any]:
        """notion.json을 만든다. record를 주면 노트 원고 기록 하나를 넣는다."""
        state = {"version": 2, "course": "과목A", "parent_page_id": "parent", "course_page_id": "course",
                 "course_page_url": "https://www.notion.so/course", "slides": False, "notes": {}}
        if record:
            state["notes"][SOURCE] = {"page_id": "page", "url": "https://www.notion.so/page", **record}
        pn.save_state(self.course, state)
        return state

    def content_hash(self) -> str:
        return pn.prepare_note(self.course, self.course / SOURCE, pn.load_state(self.course)).content_hash

    def semester(self) -> None:
        """2026-2학기(2026-08-31 시작, 15주)를 만들고 이 과목을 넣는다. 2026-10-10은 6주차다."""
        self.run_cli("semester", "init", "2026-2", "--title", "2026-2학기", "--start", "2026-08-31", "--weeks", "15")
        self.run_cli("semester", "add-course")

    @staticmethod
    def fetch_todo(document: dict[str, Any]) -> list[str]:
        return [item["text"] for item in document["todo"] if item["kind"] == "fetch_materials"]


class CourseCommandTests(LedgerCase):
    def test_init_writes_ledger_once(self) -> None:
        summary = self.init()
        self.assertEqual("ok", summary["status"])
        ledger = self.ledger()
        self.assertEqual({"version": 1, "name": "과목A", "aliases": [], "mode": "deep", "materials": "handout+recording",
                          "expect": None, "planned": None, "ignore": [], "conventions": [], "lectures": []}, ledger)
        _, err = self.run_cli("course", "init", "--name", "X", "--mode", "deep", "--materials", "handout", expected=2)
        self.assertIn("course set", err)

    def test_init_refuses_inside_an_existing_course(self) -> None:
        write(pn.state_path(self.course), "{}")
        inner = self.course / "3주차"
        inner.mkdir()
        _, err = self.run_cli("course", "init", "--name", "X", "--mode", "deep", "--materials", "handout",
                              course=inner, expected=2)
        self.assertIn(str(self.course), err)
        self.assertFalse((inner / ".gongbu").exists())
        self.init()  # notion.json만 있던 과목 폴더 자체에서는 만든다

    def test_commands_from_a_subfolder_use_the_course_root(self) -> None:
        self.init()
        inner = self.course / "3주차" / "실습"
        inner.mkdir(parents=True)
        out, err = self.run_cli("course", "show", "--json", course=inner)
        self.assertIn(f"과목 폴더: {self.course}", err)
        self.assertEqual("과목A", json.loads(out)["name"])
        self.assertFalse((inner / ".gongbu").exists())

    def test_set_changes_defaults(self) -> None:
        self.init()
        summary = self.json_cli("course", "set", "--mode", "faithful", "--planned", "12", "--expect",
                                "handout=2,transcript=0", "--name", "과목A2")
        self.assertEqual(["deep", "faithful"], summary["changed"]["mode"])
        ledger = self.ledger()
        self.assertEqual(("과목A2", "faithful", 12, {"handout": 2, "transcript": 0}),
                         (ledger["name"], ledger["mode"], ledger["planned"], ledger["expect"]))
        self.json_cli("course", "set", "--planned", "none", "--expect", "default")
        self.assertEqual((None, None), (self.ledger()["planned"], self.ledger()["expect"]))
        self.run_cli("course", "set", "--expect", "handout=x", expected=2)
        self.run_cli("course", "set", "--planned", "-1", expected=2)

    def test_dry_runs_write_nothing(self) -> None:
        summary = self.json_cli("course", "init", "--name", "과목A", "--mode", "deep", "--materials", "handout", "--dry-run")
        self.assertTrue(summary["dry_run"])
        self.assertFalse((self.course / ".gongbu").exists())
        self.standard()
        write(self.course / "b02.pdf", b"%PDF-1.4 two")
        before = snapshot(self.course)
        self.json_cli("course", "set", "--mode", "faithful", "--dry-run")
        self.json_cli("course", "material", "add", "L01", "b02.pdf", "--kind", "handout", "--dry-run")
        self.json_cli("course", "note", "L01", "--source", SOURCE, "--progress", "done", "--dry-run")
        self.json_cli("course", "relink", "--dry-run")
        out, _ = self.import_plan({"course": {"ignore": ["^KakaoTalk_", r"^\[제공\]"]},
                                   "lectures": [{"id": "L02", "title": "2장", "materials": [{"kind": "handout", "path": "b02.pdf"}]}]},
                                  "--dry-run")
        self.assertIn("L02 (새)", out)
        self.assertIn("  ^KakaoTalk_\n", out)
        self.assertIn(r"  ^\[제공\]", out)
        self.json_cli("semester", "init", "2026-2", "--title", "2026-2학기", "--start", "2026-08-31", "--weeks", "15",
                      "--dry-run")
        self.assertEqual(before, snapshot(self.course))
        self.assertFalse(self.config.exists())

    def test_help_and_bad_options_create_nothing(self) -> None:
        cases = [(["status", "--help"], 0), (["course", "init", "--help"], 0), (["course", "init", "--bogus"], 2),
                 (["course", "init", "--name", "A", "--mode", "fast", "--materials", "handout"], 2),
                 (["semester", "init", "2026-2"], 2), (["semester", "init", "--help"], 0), (["course"], 2),
                 (["status", "--today", "어제"], 2), (["course", "material", "add", "--help"], 0)]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.run_cli(*argv, expected=expected)
                self.assertEqual([], list(self.course.iterdir()))
                self.assertFalse(self.config.exists())

    def test_show_prints_ledger(self) -> None:
        self.standard()
        self.assertEqual(self.ledger(), self.json_cli("course", "show", "--json"))
        out, _ = self.run_cli("course", "show")
        self.assertIn("과목A · 심화 이해형 · 교안+녹음", out)
        self.assertIn("L01", out)
        self.run_cli("course", "show", expected=2, course=self.base)  # 원장이 없는 폴더


class ImportTests(LedgerCase):
    def test_import_creates_the_ledger_and_fingerprints_everything(self) -> None:
        self.standard()
        ledger = self.ledger()
        lecture = ledger["lectures"][0]
        handout = lecture["materials"][0]
        self.assertEqual(HANDOUT, handout["path"])
        self.assertEqual(pn.file_sha256(self.course / HANDOUT), handout["sha256"])
        self.assertEqual((self.course / HANDOUT).stat().st_size, handout["size"])
        self.assertEqual((self.course / HANDOUT).stat().st_mtime_ns, handout["mtime_ns"])
        note = lecture["note"]
        self.assertEqual({HANDOUT: handout["sha256"], TRANSCRIPT: lecture["materials"][1]["sha256"]}, note["inputs"])
        state = {"course": "과목A", "slides": False}
        self.assertEqual(pn.prepare_note(self.course, self.course / SOURCE, state).content_hash, note["digest"])
        self.assertEqual(cl.source_digest([self.course / SOURCE]), note["source_sha256"])
        self.assertEqual(("deep", "in_progress", "p.1–5", ["01 학습노트.pdf"]),
                         (note["mode"], note["progress"], note["covers"], note["outputs"]))
        self.assertRegex(note["registered_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_import_without_ledger_needs_name_mode_materials(self) -> None:
        _, err = self.import_plan({"course": {"name": "과목A"}, "lectures": []}, expected=2)
        self.assertIn("mode", err)
        self.run_cli("course", "set", "--mode", "deep", expected=2)
        self.run_cli("course", "relink", expected=2)
        self.assertEqual([], list(self.course.iterdir()))  # 실패한 첫 쓰기는 빈 .gongbu도 남기지 않는다

    def test_import_is_all_or_nothing(self) -> None:
        self.standard()
        before = self.ledger()
        write(self.course / "b02.pdf", b"%PDF two")
        bad_plans = [
            {"lectures": [{"id": "L02", "materials": [{"kind": "handout", "path": "b02.pdf"}]},
                          {"id": "L03", "materials": [{"kind": "handout", "path": "없는 파일.pdf"}]}]},
            {"lectures": [{"id": "L02", "materials": [{"kind": "slides", "path": "b02.pdf"}]}]},
            {"lectures": [{"id": "-bad"}]},
            {"lectures": [{"id": "L02"}, {"id": "L02"}]},
            {"lectures": [{"id": "L02", "note": {"source": SOURCE, "progress": "done"}}]},  # 이미 L01의 원고
            {"course": {"ignore": ["("]}},
            {"course": {"mode": "fast"}},
            {"course": {"colour": "red"}},
            {"lectures": [{"id": "L02", "expect": {"handout": -1, "transcript": 0}}]},
            {"lectures": [{"id": "L01", "note": {"source": SOURCE, "progress": "done", "outputs": "01.pdf"}}]},
            {"lectures": [{"id": "L01", "note": {"source": SOURCE}}]},
            {"lectures": [{"id": "L02", "materials": "b02.pdf"}]},
            ["not", "an", "object"],
        ]
        for plan in bad_plans:
            with self.subTest(plan=plan):
                self.import_plan(plan, expected=2)
                self.assertEqual(before, self.ledger())

    def test_plan_with_utf8_bom_is_read(self) -> None:
        # Windows PowerShell 5.1의 Set-Content -Encoding utf8은 BOM을 붙인다.
        plan = {"course": {"name": "과목A", "mode": "faithful", "materials": "handout"}, "lectures": [{"id": "L01"}]}
        path = self.base / "plan_bom.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(plan, ensure_ascii=False).encode("utf-8"))
        self.run_cli("course", "import", str(path))
        self.assertEqual(["L01"], [lecture["id"] for lecture in self.ledger()["lectures"]])
        path.write_text('{"lectures": [}', encoding="utf-8")
        _, err = self.run_cli("course", "import", str(path), expected=2)
        self.assertIn("1행 15열", err)
        path.write_text(json.dumps(plan), encoding="utf-16")
        _, err = self.run_cli("course", "import", str(path), expected=2)
        self.assertIn("UTF-8", err)

    def test_lecture_weeks_are_validated_stored_and_shown(self) -> None:
        self.init()
        self.import_plan({"lectures": [{"id": "L01", "title": "1장", "weeks": [4, 3]}, {"id": "L02"}]})
        self.assertEqual([[4, 3], None], [lecture.get("weeks") for lecture in self.ledger()["lectures"]])
        out, _ = self.run_cli("course", "show")
        self.assertIn("3·4주", out)
        self.assertEqual([4, 3], self.lecture(self.status())["weeks"])
        before = self.ledger()
        for weeks in (3, [], [0], [-1], ["3"], [True], [1.5]):
            with self.subTest(weeks=weeks):
                _, err = self.import_plan({"lectures": [{"id": "L01", "weeks": weeks}]}, expected=2)
                self.assertIn("weeks", err)
                self.assertEqual(before, self.ledger())
        out, _ = self.import_plan({"lectures": [{"id": "L02", "weeks": [5]}]}, "--dry-run")
        self.assertIn("5주", out)
        self.import_plan({"lectures": [{"id": "L01", "weeks": None}]})
        self.assertIsNone(self.ledger()["lectures"][0]["weeks"])

    def test_lecture_order_and_upsert(self) -> None:
        self.init()
        self.import_plan({"lectures": [{"id": "a", "title": "A"}, {"id": "b"}]})
        self.import_plan({"lectures": [{"id": "c"}, {"id": "a", "title": "A2"}]})
        self.assertEqual(["a", "b", "c"], [lecture["id"] for lecture in self.ledger()["lectures"]])
        self.assertEqual("A2", self.ledger()["lectures"][0]["title"])
        self.import_plan({"lectures": [{"id": "c"}, {"id": "b"}, {"id": "a"}]})
        self.assertEqual(["c", "b", "a"], [lecture["id"] for lecture in self.ledger()["lectures"]])
        self.assertEqual("A2", self.ledger()["lectures"][2]["title"])  # 적지 않은 항목은 그대로

    def test_import_replaces_course_fields_and_lecture_materials(self) -> None:
        self.standard()
        write(self.course / "b02.pdf", b"%PDF two")
        summary = json.loads(self.import_plan({"course": {"conventions": ["전사 'N-M'은 N주차"], "planned": 10},
                                               "lectures": [{"id": "L01", "materials": [{"kind": "handout", "path": "b02.pdf",
                                                                                         "part": "p.1–3"}]}]})[0])
        self.assertEqual(["L01"], summary["updated"])
        ledger = self.ledger()
        self.assertEqual((["전사 'N-M'은 N주차"], 10), (ledger["conventions"], ledger["planned"]))
        self.assertEqual([("b02.pdf", "p.1–3")], [(m["path"], m.get("part")) for m in ledger["lectures"][0]["materials"]])
        self.assertEqual(SOURCE, ledger["lectures"][0]["note"]["source"])


class PathRuleTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.import_plan({"lectures": [{"id": "L01", "title": "1장"}]})

    def add(self, path: str, kind: str = "handout", expected: int = 0, *extra: str) -> dict[str, Any] | None:
        out, _ = self.run_cli("course", "material", "add", "L01", path, "--kind", kind, *extra, expected=expected)
        return json.loads(out) if expected == 0 else None

    def test_outside_and_parent_paths_are_refused(self) -> None:
        outside = write(self.base / "밖.pdf", b"%PDF")
        write(self.course / "sub" / "x.pdf", b"%PDF")
        self.add(str(outside), expected=2)
        self.add("sub/../sub/x.pdf", expected=2)
        self.add("없음.pdf", expected=2)
        self.assertEqual("sub/x.pdf", self.add(str(self.course / "sub" / "x.pdf"))["material"]["path"])

    @unittest.skipUnless(os.name == "nt", "대소문자를 가리지 않는 파일 시스템에서만")
    def test_case_follows_the_disk(self) -> None:
        write(self.course / "b01.pdf", b"%PDF")
        self.assertEqual("b01.pdf", self.add("B01.PDF")["material"]["path"])

    def test_nfd_names_are_stored_as_nfc_and_still_found(self) -> None:
        name = unicodedata.normalize("NFD", "녹음 전사.txt")
        write(self.course / name, "전사")
        stored = self.add(name, "transcript")["material"]["path"]
        self.assertEqual(unicodedata.normalize("NFC", name), stored)
        lecture = self.lecture(self.status())
        self.assertEqual([], lecture["materials"]["missing"])
        self.assertEqual(1, lecture["materials"]["transcript"]["have"])
        self.assertEqual([], self.status()["courses"][0]["unregistered"])

    def test_bracketed_names_and_ignore_patterns(self) -> None:
        write(self.course / HANDOUT, b"%PDF")
        write(self.course / "[제공]과목A-02.pdf", b"%PDF 2")
        self.add(HANDOUT)
        self.assertEqual([{"path": "[제공]과목A-02.pdf", "kind": "handout"}], self.status()["courses"][0]["unregistered"])
        self.import_plan({"course": {"ignore": [r"^\[제공\]과목A-02"]}})
        self.assertEqual([], self.status()["courses"][0]["unregistered"])
        self.assertEqual(1, self.lecture(self.status())["materials"]["handout"]["have"])


class NoteAndMaterialTests(LedgerCase):
    def test_note_registration_rules(self) -> None:
        self.standard(note=None)
        self.run_cli("course", "note", "L01", "--source", SOURCE, "--progress", "done", expected=2)  # 원고 없음
        write(self.course / SOURCE, NOTE_TEX)
        self.run_cli("course", "note", "L99", "--source", SOURCE, "--progress", "done", expected=2)
        self.run_cli("course", "note", "L01", "--source", SOURCE, "--progress", "finished", expected=2)
        summary = self.json_cli("course", "note", "L01", "--source", SOURCE, "--progress", "done", "--output", "01.pdf",
                                "--covers", "p.1–9")
        self.assertEqual(("done", ["01.pdf"], "p.1–9"), (summary["note"]["progress"], summary["note"]["outputs"],
                                                          summary["note"]["covers"]))
        # 다시 등록할 때 적지 않은 산출물·범위는 그대로 둔다.
        again = self.json_cli("course", "note", "L01", "--source", SOURCE, "--progress", "in_progress")["note"]
        self.assertEqual((["01.pdf"], "p.1–9"), (again["outputs"], again["covers"]))
        self.import_plan({"lectures": [{"id": "L02"}]})
        _, err = self.run_cli("course", "note", "L02", "--source", SOURCE, "--progress", "done", expected=2)
        self.assertIn("L01", err)

    def test_changed_source_suggests_rekey_when_notion_has_the_old_page(self) -> None:
        self.standard()
        self.connect(content_sha256="x")
        new_source = "output/source/01 학습노트 v2.tex"
        write(self.course / new_source, NOTE_TEX)
        _, err = self.run_cli("course", "note", "L01", "--source", new_source, "--progress", "in_progress")
        self.assertIn(f'gongbu notion rekey "{SOURCE}" "{new_source}"', err)

    def test_unconvertible_note_is_registered_with_null_digest(self) -> None:
        self.standard(note=None)
        write(self.course / SOURCE, BROKEN_TEX)
        out, err = self.run_cli("course", "note", "L01", "--source", SOURCE, "--progress", "in_progress")
        self.assertIsNone(json.loads(out)["note"]["digest"])
        self.assertIn("[경고]", err)

    def test_material_add_refuses_unstable_files_without_force(self) -> None:
        self.standard(note=None)
        write(self.course / "녹음.part.wav", b"RIFF", age=3600)
        write(self.course / "방금.wav", b"RIFF now", age=5)
        self.run_cli("course", "material", "add", "L01", "녹음.part.wav", "--kind", "recording", expected=2)
        _, err = self.run_cli("course", "material", "add", "L01", "방금.wav", "--kind", "recording", expected=2)
        self.assertIn("--force", err)
        summary = self.json_cli("course", "material", "add", "L01", "방금.wav", "--kind", "recording", "--force")
        self.assertFalse(summary["replaced"])
        summary = self.json_cli("course", "material", "add", "L01", "방금.wav", "--kind", "recording", "--force",
                                "--part", "0:00~50:00")
        self.assertTrue(summary["replaced"])
        self.assertEqual(3, len(self.ledger()["lectures"][0]["materials"]))
        write(self.course / "방금.txt", "전사")
        entry = self.json_cli("course", "material", "add", "L01", "방금.txt", "--kind", "transcript", "--from",
                              "방금.wav")["material"]
        self.assertEqual("방금.wav", entry["from"])

    def test_ledger_write_rereads_under_the_lock(self) -> None:
        self.init()
        original = pn.file_lock

        @contextlib.contextmanager
        def racing_lock(lock: Path, *args: Any, **kwargs: Any):
            # 잠금을 얻기 직전에 다른 프로세스가 원장을 바꿔 두었다.
            ledger = json.loads(cl.ledger_path(self.course).read_text(encoding="utf-8"))
            ledger["conventions"] = ["다른 작업이 적은 약속"]
            cl.write_json(cl.ledger_path(self.course), ledger)
            with original(lock, *args, **kwargs):
                yield

        with patch.object(cl.pn, "file_lock", racing_lock):
            self.json_cli("course", "set", "--mode", "faithful")
        ledger = self.ledger()
        self.assertEqual(("faithful", ["다른 작업이 적은 약속"]), (ledger["mode"], ledger["conventions"]))
        self.assertFalse((self.course / ".gongbu" / "course.lock").exists())

    def test_newer_ledger_version_is_refused(self) -> None:
        self.init()
        ledger = self.ledger()
        ledger["version"] = 99
        cl.write_json(cl.ledger_path(self.course), ledger)
        _, err = self.run_cli("course", "set", "--mode", "faithful", expected=2)
        self.assertIn("엔진을 업데이트하십시오", err)
        _, err = self.run_cli("status", expected=2)
        self.assertIn("엔진을 업데이트하십시오", err)


class RelinkTests(LedgerCase):
    def test_moved_file_is_detected_and_relinked(self) -> None:
        self.standard()
        (self.course / "1주차").mkdir()
        os.replace(self.course / HANDOUT, self.course / "1주차" / "교안.pdf")
        course = self.status()["courses"][0]
        self.assertEqual([{"from": HANDOUT, "to": "1주차/교안.pdf"}], course["moved"])
        self.assertEqual(([], []), (course["missing_files"], course["unregistered"]))
        self.assertEqual(1, self.lecture(self.status())["materials"]["handout"]["have"])
        ready = self.status()["ready"]
        self.assertIn({"course": "과목A", "dir": self.course.as_posix(), "lecture": None, "action": "relink",
                       "paths": [HANDOUT]}, ready)
        summary = self.json_cli("course", "relink")
        self.assertEqual([{"from": HANDOUT, "to": "1주차/교안.pdf"}], summary["moved"])
        lecture = self.ledger()["lectures"][0]
        self.assertEqual("1주차/교안.pdf", lecture["materials"][0]["path"])
        self.assertIn("1주차/교안.pdf", lecture["note"]["inputs"])
        self.assertNotIn(HANDOUT, lecture["note"]["inputs"])
        self.assertEqual([], self.status()["courses"][0]["moved"])
        self.assertEqual("in_progress", self.lecture(self.status())["note_state"])

    def test_relink_moves_the_transcript_from_link_with_the_recording(self) -> None:
        self.init()
        write(self.course / "b01.pdf", b"%PDF")
        write(self.course / "w01" / "a.mp3", b"ID3" + b"0" * 100)
        write(self.course / "w01" / "a_전사본.md", "전사")
        self.import_plan({"lectures": [{"id": "L01", "materials": [
            {"kind": "handout", "path": "b01.pdf"}, {"kind": "recording", "path": "w01/a.mp3"},
            {"kind": "transcript", "path": "w01/a_전사본.md", "from": "w01/a.mp3"}]}]})
        os.replace(self.course / "w01" / "a.mp3", self.course / "w01" / "1차시 녹음.mp3")
        summary = self.json_cli("course", "relink")
        self.assertEqual([{"from": "w01/a.mp3", "to": "w01/1차시 녹음.mp3"}], summary["moved"])
        materials = self.ledger()["lectures"][0]["materials"]
        self.assertEqual(["w01/1차시 녹음.mp3", "w01/1차시 녹음.mp3"], [materials[1]["path"], materials[2]["from"]])
        lecture = self.lecture(self.status())
        self.assertEqual(([], {"have": 1, "need": 1}, "complete", "write_note"),
                         (lecture["materials"]["untranscribed"], lecture["materials"]["transcript"],
                          lecture["materials_state"], lecture["next"]))

    def test_relink_keeps_manifest_evidence_by_sha256(self) -> None:
        self.init()
        write(self.course / "b01.pdf", b"%PDF")
        recording = write(self.course / "2026-09-05_123453.wav", b"RIFF" + b"1" * 200)
        write(self.course / ".gongbu" / "rec" / "transcript" / "rec_transcript_manifest.json",
              json.dumps({"source_audio": recording.name, "source_audio_bytes": recording.stat().st_size,
                          "source_audio_sha256": pn.file_sha256(recording)}))
        self.import_plan({"lectures": [{"id": "L01", "materials": [
            {"kind": "handout", "path": "b01.pdf"}, {"kind": "recording", "path": recording.name}]}]})
        self.assertEqual({"have": 1, "need": 1}, self.lecture(self.status())["materials"]["transcript"])
        (self.course / "w01").mkdir()
        os.replace(recording, self.course / "w01" / "1차시.wav")
        self.json_cli("course", "relink")
        self.assertEqual("w01/1차시.wav", self.ledger()["lectures"][0]["materials"][1]["path"])
        lecture = self.lecture(self.status())
        self.assertEqual(([], {"have": 1, "need": 1}, "complete", "write_note"),
                         (lecture["materials"]["untranscribed"], lecture["materials"]["transcript"],
                          lecture["materials_state"], lecture["next"]))

    def test_ambiguous_or_missing_file_stays_missing(self) -> None:
        self.standard()
        data = (self.course / HANDOUT).read_bytes()
        (self.course / HANDOUT).unlink()
        write(self.course / "a.pdf", data)
        write(self.course / "b.pdf", data)
        course = self.status()["courses"][0]
        self.assertEqual([], course["moved"])
        self.assertEqual([{"lecture": "L01", "kind": "handout", "path": HANDOUT}], course["missing_files"])
        texts = [item["text"] for item in self.status()["todo"] if item["kind"] == "missing_files"]
        self.assertEqual([f"과목A 1장: 등록된 교안 {HANDOUT}가 없어졌어요"], texts)
        summary = self.json_cli("course", "relink")
        self.assertEqual(([], [HANDOUT]), (summary["moved"], summary["missing"]))


class ScanTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()

    def course_entry(self, **options: Any) -> dict[str, Any]:
        return self.status(**options)["courses"][0]

    def test_pruned_folders_and_skipped_files(self) -> None:
        for rel in ("output/x.pdf", "tmp/x.pdf", "temp/x.pdf", "scratch/x.pdf", ".hidden/x.pdf", "node_modules/x.pdf",
                    "venv/x.pdf", "HW/과제.pdf", "AGENTS.md", "README.md", "CLAUDE.md", "a.recording.json",
                    "w1/w1_segments.json", "w1/w1_transcript_raw.srt", "KakaoTalk_1.pdf", "01 학습노트.pdf"):
            write(self.course / rel, "x")
        write(self.course / "venv" / "pyvenv.cfg", "home = x")
        write(self.course / "w1" / "w1_transcript_manifest.json", json.dumps({"source_audio": "a.wav", "source_audio_bytes": 1}))
        write(self.course / "따로.srt", "1\n")
        write(self.course / "교안.pdf", "x")
        self.import_plan({"course": {"ignore": ["^HW/", "^KakaoTalk_"]}})
        entry = self.course_entry()
        self.assertEqual([{"path": "교안.pdf", "kind": "handout"}, {"path": "따로.srt", "kind": "transcript"}],
                         entry["unregistered"])
        self.assertEqual([], entry["errors"])

    def test_unstable_files_by_name_and_by_age(self) -> None:
        write(self.course / "강의.part.wav", b"RIFF")
        write(self.course / "교안.pdf.crdownload", b"%PDF")
        write(self.course / "~$발표.pptx", b"x")
        fresh = write(self.course / "방금.wav", b"RIFF", age=10)
        entry = self.course_entry()
        self.assertEqual([], entry["unregistered"])
        self.assertEqual({"강의.part.wav", "교안.pdf.crdownload", "~$발표.pptx", "방금.wav"},
                         {item["path"] for item in entry["unstable_files"]})
        later = fresh.stat().st_mtime + 1000
        entry = self.course_entry(now=later)
        self.assertEqual([{"path": "방금.wav", "kind": "recording"}], entry["unregistered"])
        self.assertEqual([{"path": "방금.wav", "kind": "recording"}],
                         self.course_entry(settle_seconds=0)["unregistered"])

    def test_note_like_files(self) -> None:
        write(self.course / "1주차" / "01 학습노트.md", "# 노트")
        write(self.course / "output" / "source" / "02 학습노트.tex", NOTE_TEX)
        write(self.course / "output" / "source" / "part.tex", r"\section{조각}")
        entry = self.course_entry()
        self.assertEqual([{"path": "1주차/01 학습노트.md"}, {"path": "output/source/02 학습노트.tex"}],
                         entry["unregistered_notes"])
        self.assertEqual([], entry["unregistered"])

    def test_faithful_note_in_output_folder_blocks_a_new_note(self) -> None:
        write(self.course / "b01.pdf", b"%PDF")
        self.import_plan({"course": {"materials": "handout"},
                          "lectures": [{"id": "L01", "materials": [{"kind": "handout", "path": "b01.pdf"}]}]})
        self.assertEqual("write_note", self.lecture(self.status())["next"])
        write(self.course / "output" / "과목A_01_학습노트.md", "# 노트")
        write(self.course / "output" / "메모.md", "# 노트 아님")
        write(self.course / "output" / "old" / "예전 학습노트.md", "# 한 단계 아래는 보지 않는다")
        entry = self.course_entry()
        self.assertEqual([{"path": "output/과목A_01_학습노트.md"}], entry["unregistered_notes"])
        self.assertEqual([], entry["unregistered"])
        lecture = self.lecture(self.status())
        self.assertEqual((None, "unregistered_notes"), (lecture["next"], lecture["blocked"]))
        self.json_cli("course", "note", "L01", "--source", "output/과목A_01_학습노트.md", "--progress", "in_progress")
        self.assertEqual([], self.course_entry()["unregistered_notes"])
        write(self.course / "output" / "과목A_02_학습노트.md", "# 노트")
        self.assertEqual([{"path": "output/과목A_02_학습노트.md"}], self.course_entry()["unregistered_notes"])
        self.import_plan({"course": {"ignore": ["^output/과목A_02"]}})
        self.assertEqual([], self.course_entry()["unregistered_notes"])


class TranscriptionTests(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        write(self.course / "b01.pdf", b"%PDF")
        self.recording = write(self.course / "1주차" / "강의.wav", b"RIFF" + b"0" * 100)
        self.import_plan({"lectures": [{"id": "L01", "title": "1장", "materials": [
            {"kind": "handout", "path": "b01.pdf"}, {"kind": "recording", "path": "1주차/강의.wav"}]}]})

    def manifest(self, size: int) -> None:
        write(self.course / ".gongbu" / "L01" / "transcript" / "L01_transcript_manifest.json",
              json.dumps({"source_audio": "강의.wav", "source_audio_bytes": size, "source_audio_sha256": "x"}))

    def test_untranscribed_recording(self) -> None:
        self.manifest(size=1)  # 다른 녹음의 전사 기록
        lecture = self.lecture(self.status())
        self.assertEqual(["1주차/강의.wav"], lecture["materials"]["untranscribed"])
        self.assertEqual(("needs_transcription", "transcribe"), (lecture["materials_state"], lecture["next"]))
        self.assertEqual(1, lecture["materials"]["recording"]["have"])

    def test_manifest_marks_recording_transcribed(self) -> None:
        self.manifest(size=self.recording.stat().st_size)
        lecture = self.lecture(self.status())
        self.assertEqual([], lecture["materials"]["untranscribed"])
        self.assertEqual({"have": 1, "need": 1}, lecture["materials"]["transcript"])
        self.assertEqual(("complete", "ready", "write_note"), (lecture["materials_state"], lecture["note_state"], lecture["next"]))

    def test_transcript_from_recording_counts_once(self) -> None:
        self.manifest(size=self.recording.stat().st_size)
        write(self.course / "1주차" / "강의.txt", "전사")
        self.import_plan({"lectures": [{"id": "L01", "materials": [
            {"kind": "handout", "path": "b01.pdf"}, {"kind": "recording", "path": "1주차/강의.wav"},
            {"kind": "transcript", "path": "1주차/강의.txt", "from": "1주차/강의.wav"}]}]})
        self.assertEqual({"have": 1, "need": 1}, self.lecture(self.status())["materials"]["transcript"])
        (self.course / ".gongbu" / "L01" / "transcript" / "L01_transcript_manifest.json").unlink()
        self.assertEqual([], self.lecture(self.status())["materials"]["untranscribed"])

    def test_transcript_without_from_pairs_with_the_transcribed_recording(self) -> None:
        # 1차시 녹음만 왔고 gongbu로 전사됐다. 전사본은 `from` 없이 등록됐다. 2차시 녹음은 아직 없다.
        self.manifest(size=self.recording.stat().st_size)
        write(self.course / "1주차" / "강의_전사본.md", "전사")
        self.semester()
        self.import_plan({"lectures": [{"id": "L01", "weeks": [1], "expect": {"handout": 1, "transcript": 2}, "materials": [
            {"kind": "handout", "path": "b01.pdf"}, {"kind": "recording", "path": "1주차/강의.wav"},
            {"kind": "transcript", "path": "1주차/강의_전사본.md"}]}]})
        document = self.status(today=dt.date(2026, 10, 10))
        lecture = self.lecture(document)
        self.assertEqual(({"have": 1, "need": 2}, [], "missing", None),
                         (lecture["materials"]["transcript"], lecture["materials"]["untranscribed"],
                          lecture["materials_state"], lecture["next"]))
        self.assertEqual(["과목A 1장: 녹음 받기 (1/2)"], self.fetch_todo(document))
        self.import_plan({"lectures": [{"id": "L01", "expect": None}]})
        self.assertEqual({"have": 1, "need": 1}, self.lecture(self.status())["materials"]["transcript"])

    def test_recording_alone_still_needs_transcription_when_transcripts_are_short(self) -> None:
        write(self.course / "1주차" / "2차시.wav", b"RIFF" + b"2" * 50)
        self.import_plan({"lectures": [{"id": "L02", "materials": [{"kind": "recording", "path": "1주차/2차시.wav"}]}]})
        lecture = self.lecture(self.status(), "L02")
        self.assertEqual(("missing", "transcribe"), (lecture["materials_state"], lecture["next"]))


class StateTests(LedgerCase):
    def test_missing_materials_and_fetch_todo(self) -> None:
        self.init()
        self.semester()
        self.import_plan({"lectures": [{"id": "w03", "title": "미디어의 이해", "weeks": [3]}]})
        document = self.status(today=dt.date(2026, 10, 10))
        lecture = self.lecture(document, "w03")
        self.assertEqual(("missing", "none", None), (lecture["materials_state"], lecture["note_state"], lecture["next"]))
        self.assertEqual(["과목A w03: 교안 받기 (0/1)", "과목A w03: 녹음 받기 (0/1)"],
                         [item["text"] for item in document["todo"]])
        write(self.course / "새 녹음.m4a", b"audio")
        document = self.status(today=dt.date(2026, 10, 10))
        self.assertEqual(["과목A w03: 교안 받기 (0/1)"], [item["text"] for item in document["todo"]])
        self.assertIn({"course": "과목A", "dir": self.course.as_posix(), "lecture": None, "action": "register",
                       "paths": ["새 녹음.m4a"]}, document["ready"])

    def test_fetch_todo_only_for_lectures_whose_week_has_passed(self) -> None:
        self.init()
        self.import_plan({"lectures": [{"id": "w01"}, {"id": "w05", "weeks": [5]}, {"id": "w06", "weeks": [6]},
                                       {"id": "w07", "weeks": [7, 6]}]})
        today = dt.date(2026, 10, 10)  # 6주차
        self.assertEqual([], self.fetch_todo(self.status(today=today)))  # 학기에 넣지 않아 주차를 모른다
        self.semester()
        document = self.status(today=today)
        self.assertEqual(["과목A w05: 교안 받기 (0/1)", "과목A w05: 녹음 받기 (0/1)"], self.fetch_todo(document))
        self.assertEqual(["missing"] * 4, [lecture["materials_state"] for lecture in document["courses"][0]["lectures"]])
        self.assertEqual([None, [5], [6], [7, 6]], [lecture["weeks"] for lecture in document["courses"][0]["lectures"]])
        later = self.fetch_todo(self.status(today=dt.date(2026, 10, 19)))  # 8주차: 주차가 없는 w01은 여전히 조용하다
        self.assertEqual(["w05", "w06", "w07"], sorted({text.split()[1].rstrip(":") for text in later}))
        all_courses = cl.collect_status(semester="2026-2", today=today)
        self.assertEqual(self.fetch_todo(document), self.fetch_todo(all_courses))
        cl.update_registry(lambda registry: registry["semesters"]["2026-2"].pop("start"))
        self.assertEqual([], self.fetch_todo(self.status(today=today)))  # 학기 시작일을 모른다

    def backfill_with_recording(self, note: bool = True) -> None:
        """처음 정리: 교안·전사본(`from` 없음)·전사 기록 없는 예전 녹음과 (있으면) 완료 노트를 한 번에 등록한다."""
        write(self.course / HANDOUT, b"%PDF-1.4 handout")
        write(self.course / TRANSCRIPT, "전사 내용\n")
        write(self.course / "1주차" / "강의.wav", b"RIFF" + b"0" * 100, age=7 * 86400)
        lecture: dict[str, Any] = {"id": "L01", "title": "1장 벡터", "materials": [
            {"kind": "handout", "path": HANDOUT}, {"kind": "transcript", "path": TRANSCRIPT},
            {"kind": "recording", "path": "1주차/강의.wav"}]}
        if note:
            write(self.course / SOURCE, NOTE_TEX)
            lecture["note"] = {"source": SOURCE, "progress": "done"}
        self.import_plan({"course": {"name": "과목A", "mode": "deep", "materials": "handout+recording"},
                          "lectures": [lecture]})

    def test_old_untranscribed_recording_does_not_reopen_a_finished_lecture(self) -> None:
        self.backfill_with_recording()
        self.connect()
        self.connect(content_sha256=self.content_hash())
        document = self.status()
        lecture = self.lecture(document)
        self.assertEqual(("complete", "done", "up_to_date", None),
                         (lecture["materials_state"], lecture["note_state"], lecture["notion_state"], lecture["next"]))
        self.assertEqual(["1주차/강의.wav"], lecture["materials"]["untranscribed"])  # 표시는 그대로
        self.assertEqual([], [item for item in document["ready"] if item["lecture"]])
        self.connect(content_sha256="old")
        lecture = self.lecture(self.status())
        self.assertEqual(("local_changed", "push"), (lecture["notion_state"], lecture["next"]))

    def test_old_untranscribed_recording_does_not_delay_a_new_note(self) -> None:
        self.backfill_with_recording(note=False)
        lecture = self.lecture(self.status())
        self.assertEqual(("complete", "ready", "write_note"), (lecture["materials_state"], lecture["note_state"], lecture["next"]))

    def test_recording_added_after_the_note_is_transcribed(self) -> None:
        self.backfill_with_recording()
        self.connect()
        self.connect(content_sha256=self.content_hash())
        write(self.course / "1주차" / "보강.wav", b"RIFF" + b"9" * 80, age=0)
        self.json_cli("course", "material", "add", "L01", "1주차/보강.wav", "--kind", "recording", "--force")
        lecture = self.lecture(self.status(now=time.time() + 1000))  # 녹음이 다 끝난 뒤 본 현황
        self.assertEqual(("complete", "transcribe"), (lecture["materials_state"], lecture["next"]))
        self.assertEqual(["1주차/강의.wav", "1주차/보강.wav"], lecture["materials"]["untranscribed"])

    def test_late_second_recording_is_transcribed_even_after_the_note_is_registered_again(self) -> None:
        self.backfill_with_recording(note=False)
        write(self.course / "1주차" / "2차시.wav", b"RIFF" + b"7" * 90, age=0)  # 전사 자료보다 늦게 들어온 녹음
        self.json_cli("course", "material", "add", "L01", "1주차/2차시.wav", "--kind", "recording", "--force")
        later = time.time() + 1000
        self.assertEqual("transcribe", self.lecture(self.status(now=later))["next"])
        write(self.course / SOURCE, NOTE_TEX)
        self.json_cli("course", "note", "L01", "--source", SOURCE, "--progress", "done")  # 전사 전에 노트를 다시 등록해도
        self.assertEqual("transcribe", self.lecture(self.status(now=later))["next"])

    def test_lecture_expect_overrides_course_expect(self) -> None:
        self.init(materials="handout")
        self.semester()
        write(self.course / "b01.pdf", b"%PDF")
        self.import_plan({"lectures": [{"id": "L01", "materials": [{"kind": "handout", "path": "b01.pdf"}]},
                                       {"id": "L02", "weeks": [1], "expect": {"handout": 2, "transcript": 0},
                                        "materials": [{"kind": "handout", "path": "b01.pdf"}]}]})
        document = self.status(today=dt.date(2026, 10, 10))
        self.assertEqual("complete", self.lecture(document, "L01")["materials_state"])
        self.assertEqual("missing", self.lecture(document, "L02")["materials_state"])
        self.assertIn("과목A L02: 교안 받기 (1/2)", [item["text"] for item in document["todo"]])

    def test_ready_note_is_blocked_by_unregistered_manuscripts(self) -> None:
        self.standard(note=None)
        self.assertEqual("write_note", self.lecture(self.status())["next"])
        write(self.course / "output" / "source" / "07 학습노트.tex", NOTE_TEX)
        lecture = self.lecture(self.status())
        self.assertEqual((None, "unregistered_notes"), (lecture["next"], lecture["blocked"]))
        self.assertIn("register_note_files", [item["action"] for item in self.status()["ready"]])

    def test_note_states_without_notion(self) -> None:
        self.standard()
        lecture = self.lecture(self.status())
        self.assertEqual(("in_progress", "not_connected", None), (lecture["note_state"], lecture["notion_state"], lecture["next"]))
        self.assertEqual({"source": SOURCE, "progress": "in_progress", "covers": "p.1–5", "outputs_missing": ["01 학습노트.pdf"],
                          "stale_inputs": [], "error": None}, lecture["note"])
        self.assertEqual({"total": 3, "relisten": 2}, lecture["unresolved"])
        self.assertIn("과목A 1장: 알아듣기 어려운 곳 2곳 다시 듣기", [item["text"] for item in self.status()["todo"]])

    def test_edited_source_needs_registration(self) -> None:
        self.standard()
        write(self.course / SOURCE, NOTE_TEX.replace("쓴다", "쓴다. 더 썼다"))
        lecture = self.lecture(self.status())
        self.assertEqual(("edited", "register_note"), (lecture["note_state"], lecture["next"]))
        self.json_cli("course", "note", "L01", "--source", SOURCE, "--progress", "done")
        self.assertEqual("done", self.lecture(self.status())["note_state"])

    def test_edited_input_part_counts_as_edit(self) -> None:
        self.standard(note=NOTE_TEX.replace(r"\section{벡터}", r"\section{벡터}" + "\n" + r"\input{part01}"))
        part = write(self.course / "output" / "source" / "part01.tex", "조각 내용\n")
        self.json_cli("course", "note", "L01", "--source", SOURCE, "--progress", "in_progress")
        self.assertEqual("in_progress", self.lecture(self.status())["note_state"])
        self.assertEqual([], self.status()["courses"][0]["unregistered_notes"])
        write(part, "조각 내용을 고쳤다\n")
        self.assertEqual("edited", self.lecture(self.status())["note_state"])

    def test_changed_material_needs_note_update(self) -> None:
        self.standard()
        write(self.course / TRANSCRIPT, "전사 내용이 늘었다\n")
        lecture = self.lecture(self.status())
        self.assertEqual(("needs_update", "update_note", [TRANSCRIPT]),
                         (lecture["note_state"], lecture["next"], lecture["note"]["stale_inputs"]))

    def test_notion_states(self) -> None:
        self.standard()
        cases = [({}, "not_uploaded", "push"), ({"content_sha256": "old"}, "local_changed", "push"),
                 ({"content_sha256": None}, "retry_pending", "push"),
                 ({"content_sha256": "old", "notion_edited_at": "2026-10-10T00:00:00Z"}, "edited_in_notion", None)]
        for record, expected, action in cases:
            with self.subTest(expected=expected):
                self.connect(**record)
                lecture = self.lecture(self.status())
                self.assertEqual((expected, action), (lecture["notion_state"], lecture["next"]))
                self.assertEqual("https://www.notion.so/page" if record else None, lecture["notion_url"])
        todo = [item for item in self.status()["todo"] if item["kind"] == "merge_notion_edit"]
        self.assertEqual("과목A 1장: 노션에서 고친 내용을 원고에 옮길지 정해 주세요", todo[0]["text"])
        self.assertEqual("https://www.notion.so/page", todo[0]["url"])
        self.connect(content_sha256="x")
        self.connect(content_sha256=self.content_hash())
        lecture = self.lecture(self.status())
        self.assertEqual(("up_to_date", None), (lecture["notion_state"], lecture["next"]))
        legacy = pn.load_state(self.course)
        legacy["data_source_id"] = "old-table"
        pn.save_state(self.course, legacy)
        document = self.status()
        self.assertEqual("legacy", self.lecture(document)["notion_state"])
        self.assertTrue(document["courses"][0]["notion"]["legacy"])

    def test_convert_error(self) -> None:
        self.standard(note=BROKEN_TEX)
        self.connect()
        lecture = self.lecture(self.status())
        self.assertEqual(("convert_error", "fix_note"), (lecture["notion_state"], lecture["next"]))
        self.assertIn("닫히지 않은", lecture["note"]["error"])
        self.assertNotIn("\n", lecture["note"]["error"])

    def test_source_missing(self) -> None:
        self.standard()
        (self.course / SOURCE).unlink()
        document = self.status()
        lecture = self.lecture(document)
        self.assertEqual(("source_missing", None), (lecture["note_state"], lecture["next"]))
        self.assertIn({"lecture": "L01", "kind": "note", "path": SOURCE}, document["courses"][0]["missing_files"])

    def test_unstable_material_blocks_next(self) -> None:
        self.standard(note=None)
        write(self.course / TRANSCRIPT, "아직 쓰는 중", age=1)
        lecture = self.lecture(self.status())
        self.assertEqual((True, None, "unstable"), (lecture["unstable"], lecture["next"], lecture["blocked"]))
        self.assertIn({"path": TRANSCRIPT, "kind": "transcript"}, self.status()["courses"][0]["unstable_files"])

    def test_progress_counts(self) -> None:
        self.standard(progress="done")
        self.import_plan({"course": {"planned": 15}, "lectures": [{"id": "L02"}]})
        self.assertEqual({"notes": 1, "done": 1, "total": 15}, self.status()["courses"][0]["progress"])
        self.import_plan({"course": {"planned": None}})
        self.assertEqual({"notes": 1, "done": 1, "total": 2}, self.status()["courses"][0]["progress"])


class SemesterTests(LedgerCase):
    def semester_init(self, sem_id: str = "2026-2", *extra: str, expected: int = 0) -> tuple[str, str]:
        return self.run_cli("semester", "init", sem_id, "--title", f"{sem_id}학기", "--start", "2026-08-31",
                            "--weeks", "15", *extra, expected=expected)

    def test_registry_location_and_skeleton(self) -> None:
        self.assertEqual(self.config, cl.registry_dir())
        self.assertEqual({"version": 1, "current": None, "root_page_id": None, "semesters": {}}, cl.load_registry())
        self.assertFalse(self.config.exists())

    def test_semester_commands(self) -> None:
        self.run_cli("semester", "add-course", expected=2)  # 학기가 없다
        self.assertFalse(self.config.exists())
        self.semester_init()
        self.semester_init("2027-1")
        self.semester_init(expected=2)
        registry = cl.load_registry()
        self.assertEqual("2026-2", registry["current"])
        self.assertEqual({"title": "2026-2학기", "start": "2026-08-31", "weeks": 15, "courses": []},
                         registry["semesters"]["2026-2"])
        inner = self.course / "1주차"
        inner.mkdir()
        self.init()
        added = json.loads(self.run_cli("semester", "add-course", course=inner)[0])
        self.assertEqual((True, self.course.as_posix()), (added["added"], added["course"]))
        self.assertFalse(json.loads(self.run_cli("semester", "add-course")[0])["added"])
        other = self.base / "과목B"
        other.mkdir()
        self.run_cli("semester", "add-course", str(other))
        self.assertEqual([self.course.as_posix(), other.as_posix()], cl.load_registry()["semesters"]["2026-2"]["courses"])
        self.assertEqual("2026-2", cl.semester_of(self.course))
        self.assertIsNone(cl.semester_of(self.base))
        self.run_cli("semester", "remove-course", str(other))
        self.run_cli("semester", "remove-course", str(other), expected=2)
        self.run_cli("semester", "use", "2027-1")
        self.run_cli("semester", "use", "2030-1", expected=2)
        shown = json.loads(self.run_cli("semester", "show", "--json")[0])
        self.assertEqual("2027-1", shown["current"])
        self.assertIn("2026-2", self.run_cli("semester", "show")[0])

    def test_registry_update_rereads_under_the_lock(self) -> None:
        self.semester_init()
        original = pn.file_lock

        @contextlib.contextmanager
        def racing_lock(lock: Path, *args: Any, **kwargs: Any):
            registry = cl.load_registry()
            registry["semesters"]["2026-2"]["notion"] = {"page_id": "sem"}
            cl.write_json(cl.registry_path(), registry)
            with original(lock, *args, **kwargs):
                yield

        with patch.object(cl.pn, "file_lock", racing_lock):
            self.semester_init("2027-1")
        registry = cl.load_registry()
        self.assertEqual({"page_id": "sem"}, registry["semesters"]["2026-2"]["notion"])
        self.assertIn("2027-1", registry["semesters"])
        cl.update_registry(lambda data: data.__setitem__("root_page_id", "root"))
        self.assertEqual("root", cl.load_registry()["root_page_id"])


class StatusTests(LedgerCase):
    def test_status_document_shape(self) -> None:
        self.standard()
        self.connect(content_sha256="old")
        self.semester()
        document = cl.collect_status(semester="2026-2", today=dt.date(2026, 10, 10))
        self.assertEqual("gongbu.status/1", document["schema"])
        self.assertEqual({"id": "2026-2", "title": "2026-2학기", "current_week": 6, "weeks": 15}, document["semester"])
        course = document["courses"][0]
        self.assertEqual({"dir", "name", "initialized", "mode", "materials", "progress", "notion", "lectures", "unregistered",
                          "unregistered_notes", "moved", "missing_files", "unstable_files", "errors"}, set(course))
        self.assertEqual({"connected": True, "course_page_url": "https://www.notion.so/course", "legacy": False},
                         course["notion"])
        lecture = course["lectures"][0]
        self.assertEqual({"id", "title", "weeks", "materials", "materials_state", "note", "note_state", "notion_state",
                          "notion_url", "unresolved", "unstable", "next", "blocked"}, set(lecture))
        self.assertEqual({"course": "과목A", "dir": self.course.as_posix(), "lecture": "L01", "action": "push",
                          "mode": "deep", "note_source": SOURCE}, document["ready"][0])
        self.assertEqual({"course", "dir", "lecture", "kind", "text", "url"}, set(document["todo"][0]))
        self.assertEqual(document, json.loads(json.dumps(document, ensure_ascii=False)))
        self.assertEqual(cl.collect_status(today=dt.date(2026, 10, 10))["courses"], document["courses"])
        self.assertEqual(1, cl.current_week({"start": "2026-08-31", "weeks": 15}, dt.date(2026, 8, 1)))
        self.assertEqual(15, cl.current_week({"start": "2026-08-31", "weeks": 15}, dt.date(2027, 8, 1)))

    def test_cli_exit_codes_and_text(self) -> None:
        empty = self.base / "새 과목"
        empty.mkdir()
        out, err = self.run_cli("status", course=empty, expected=3)
        self.assertIn("course init", err)
        self.assertEqual([], list(empty.iterdir()))
        document = json.loads(self.run_cli("status", "--json", course=empty, expected=3)[0])
        self.assertFalse(document["courses"][0]["initialized"])
        self.run_cli("status", "--all", expected=2)  # 학기가 없다
        self.standard()
        self.semester()
        out, _ = self.run_cli("status", "--today", "2026-10-10")
        self.assertIn("과목A · 심화 이해형 · 교안+녹음 — 2026-2학기 6주차 · 노트 1/1", out)
        self.assertIn("1장 벡터", out)
        self.assertIn("진행 중 · p.1–5", out)
        self.assertIn("확인 필요 3곳 · 새 파일 0 · 등록 안 된 노트 원고 0", out)
        self.assertIn("다음 자동 작업: 없음", out)
        self.assertIn("내가 할 일: 1장 알아듣기 어려운 곳 2곳 다시 듣기", out)
        all_courses = json.loads(self.run_cli("status", "--all", "--json")[0])
        self.assertEqual(1, len(all_courses["courses"]))
        self.run_cli("status", "--semester", "2030-1", expected=2)
        cl.ledger_path(self.course).write_text("{broken", encoding="utf-8")
        _, err = self.run_cli("status", expected=2)
        self.assertIn("JSON", err)
        self.run_cli("status", "--all", expected=2)
        cl.registry_path().write_text("[", encoding="utf-8")
        self.run_cli("status", "--all", expected=2)

    def test_missing_course_folder_is_an_error_not_first_setup(self) -> None:
        missing = self.base / "오타폴더"
        afile = write(self.base / "afile.txt", "x")
        plan = write(self.base / "plan.json", json.dumps({"course": {"name": "A", "mode": "deep", "materials": "handout"}}))
        for folder in (missing, afile):
            with self.subTest(folder=folder.name):
                for argv in (["status"], ["status", "--json"], ["course", "show"], ["course", "set", "--mode", "deep"],
                             ["course", "init", "--name", "A", "--mode", "deep", "--materials", "handout"],
                             ["course", "init", "--name", "A", "--mode", "deep", "--materials", "handout", "--dry-run"],
                             ["course", "import", str(plan)], ["course", "relink"]):
                    _, err = self.run_cli(*argv, course=folder, expected=2)
                    self.assertIn("과목 폴더가 없습니다", err)
                    self.assertNotIn("course init --name <과목명>", err)
        self.assertFalse(missing.exists())
        self.assertEqual("x", afile.read_text(encoding="utf-8"))

    def test_status_from_a_subfolder_finds_the_course(self) -> None:
        self.standard()
        inner = self.course / "1주차"
        inner.mkdir()
        out, err = self.run_cli("status", "--json", course=inner)
        self.assertIn(f"과목 폴더: {self.course}", err)
        self.assertEqual(self.course.as_posix(), json.loads(out)["courses"][0]["dir"])

    def test_uninitialized_semester_course(self) -> None:
        self.semester()
        write(self.course / "output" / "source" / "01 학습노트.tex", NOTE_TEX)
        write(self.course / "02 학습노트.pdf", b"%PDF")
        write(self.course / "b01.pdf", b"%PDF")
        document = cl.collect_status()
        course = document["courses"][0]
        self.assertEqual({"dir": self.course.as_posix(), "name": "과목A", "initialized": False,
                          "notion": {"connected": False, "course_page_url": None, "legacy": False},
                          "hints": {"note_suffixes": {".tex": 1, ".pdf": 1}, "materials": {"handout": 1}}, "errors": []},
                         course)
        self.assertEqual([{"course": "과목A", "dir": self.course.as_posix(), "lecture": None, "kind": "setup_course",
                           "text": "과목A: 처음 설정이 필요해요(기본 모드·재료 조건)", "url": None}], document["todo"])
        self.assertEqual([], document["ready"])
        out, _ = self.run_cli("status", "--all")
        self.assertIn("처음 설정이 필요해요", out)

    def test_status_is_read_only_and_offline(self) -> None:
        self.standard()
        self.connect(content_sha256="old")
        write(self.course / ".gongbu" / "L01" / "transcript" / "L01_transcript_manifest.json",
              json.dumps({"source_audio": "a.wav", "source_audio_bytes": 3}))
        self.semester()
        before = snapshot(self.course, self.config)
        refuse = AssertionError("현황 계산은 노션·토큰을 쓰지 않는다")
        with patch.object(pn, "load_token", side_effect=refuse), patch.object(pn, "NotionClient", side_effect=refuse), \
                patch.object(pn, "keyring_module", side_effect=refuse), \
                patch("urllib.request.urlopen", side_effect=refuse):
            cl.collect_status()
            self.run_cli("status", "--json")
            self.run_cli("status", "--all")
            self.run_cli("course", "show")
            self.run_cli("semester", "show")
        self.assertEqual(before, snapshot(self.course, self.config))


if __name__ == "__main__":
    unittest.main()
