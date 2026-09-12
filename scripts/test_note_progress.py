"""진도별 상태·원고·검수 재사용을 실제 임시 파일로 검사한다. 모델 호출은 하지 않는다."""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import manage_run as run, prepare_source_map as prep
from scripts.note_scope import tex_part_input, validate_map_scope, validate_scope


def render_progress_fixture(directory):
    """서로 다른 폴더의 같은 그림 이름을 가진 부분 원고를 실제로 조판한다."""
    from reportlab.pdfgen.canvas import Canvas
    from scripts.build_deep_pdf import build_deep
    directory.mkdir(parents=True, exist_ok=True)
    bodies = []
    for number in (1, 2):
        folder = directory / f"부분 {number}"
        folder.mkdir(exist_ok=True)
        canvas = Canvas(str(folder / "slide.pdf"), pagesize=(500, 150))
        canvas.setFont("Helvetica", 22)
        canvas.drawString(30, 100, f"Part {number} original slide")
        canvas.save()
        body = folder / "body.tex"
        body.write_text(r"\sourceslide[140mm]{slide.pdf}{1}" + "\n"
                        + f"이번 부분 {number}의 쉬운 설명입니다.\n"
                        + r"\[A=\begin{bmatrix}1&2\\3&4\end{bmatrix},\quad a_{ij},\quad\frac{1}{2}\]"
                        + "\n", encoding="utf-8")
        bodies.append(body)
    hashes = [run.sha256_file(body) for body in bodies]
    baseline = build_deep(bodies[0], directory / "first.pdf", "수학", "01", "진도별 제작 시험")
    master = directory / "master.tex"
    master.write_text("\n".join(tex_part_input(body) for body in bodies), encoding="utf-8")
    combined = build_deep(master, directory / "combined.pdf", "수학", "01", "진도별 제작 시험")
    if hashes != [run.sha256_file(body) for body in bodies]:
        raise AssertionError("조합 중 기존 원고가 바뀌었습니다.")
    return baseline, combined


class ProgressRenderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("pdftoppm"),
                         "requires XeLaTeX, Korean fonts and Poppler")
    def test_actual_cumulative_pdf_preserves_previous_page(self):
        try:
            from pypdf import PdfReader
            import reportlab
        except ImportError:
            self.skipTest("requires pypdf and reportlab")
        with tempfile.TemporaryDirectory(prefix="progress 한글 space ") as temporary:
            baseline, combined = render_progress_fixture(Path(temporary))
            old, new = PdfReader(baseline), PdfReader(combined)
            self.assertEqual(2, len(old.pages))
            self.assertEqual(3, len(new.pages))
            self.assertIn("Part 1 original slide", new.pages[1].extract_text())
            self.assertNotIn("Part 2 original slide", new.pages[1].extract_text())
            self.assertIn("Part 2 original slide", new.pages[2].extract_text())
            command = [shutil.which("pdftoppm"), "-f", "2", "-l", "2", "-r", "96", "-singlefile", "-png"]
            self.assertEqual(subprocess.check_output([*command, str(baseline)], timeout=60),
                             subprocess.check_output([*command, str(combined)], timeout=60))


class NoteProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        (self.inputs / "handout.txt").write_text("첫 개념\n두 번째 개념\n세 번째 개념\n네 번째 개념\n", encoding="utf-8")

    def call(self, *arguments):
        args = run.build_parser().parse_args(list(arguments))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = args.func(args)
        self.assertEqual(0, result, output.getvalue())
        return output.getvalue()

    def init(self, name, bounds, previous=None, mode="deep", sources=None, classifications=()):
        scope = self.root / f"{name}-scope.json"
        scope.write_text(json.dumps({"label": name, "sources": sources or {"handout.txt": {"lines": bounds}}}), encoding="utf-8")
        arguments = ["init", str(self.inputs), "--root", str(self.root), "--lecture-id", name,
                     "--runtime", "codex", "--note-mode", mode, "--scope", str(scope)]
        if previous:
            arguments += ["--continue-from", str(previous)]
        for classification in classifications:
            arguments += ["--classify", classification]
        return Path(self.call(*arguments).strip())

    def complete(self, state, extracted=()):
        payload, screening = prep.prepare(state, [], list(extracted))
        paths = prep.write_outputs(state.parent / "sources", payload, screening)
        source_map = Path(paths["source_map"])
        mode = run.read_state(state)["note_mode"]
        body = state.parent / ("body.tex" if mode == "deep" else "body.md")
        body.write_text("\\[1+1=2\\]\n" if mode == "deep" else "본문\n", encoding="utf-8")
        # 각 역할의 실제 산출물 기록과 독립 검수 완료 게이트를 통과시키는 상태 테스트 fixture.
        for role in run.ROLE_ORDER:
            entry = run.read_state(state)["roles"][role]
            if not entry["active"]:
                continue
            artifact = source_map if role == "source_mapper" else body if role == "writer" else state.parent / f"{role}.txt"
            if role not in {"source_mapper", "writer"}:
                artifact.write_text("검사 결과", encoding="utf-8")
            self.call("start", str(state), "--role", role)
            arguments = ["complete", str(state), "--role", role, "--artifact", str(artifact)]
            if role == "final_reviewer":
                coverage = state.parent / "coverage.json"
                coverage.write_text(json.dumps({"kind": "study_note_source_coverage", "schema_version": 1,
                    "note_mode": mode, "reviewer_profile": "quality_xhigh" if mode == "deep" else "review_high",
                    "items": [{"source_unit_id": unit["source_unit_id"], "decision": "included", "note_refs": ["body"]}
                              for unit in payload["source_units"]]}), encoding="utf-8")
                arguments += ["--source-map", str(source_map), "--coverage-report", str(coverage)]
            self.call(*arguments)
        self.call("verify", str(state), "--check-inputs")
        return body

    def test_later_material_and_three_parts_preserve_previous_bodies_and_reviews(self):
        first = self.init("first", [1, 2])
        first_body = self.complete(first)
        saved_state, saved_body = first.read_bytes(), first_body.read_bytes()
        (self.inputs / "later_transcript.txt").write_text("새 강의 설명", encoding="utf-8")
        self.assertEqual([], run.changed_inputs(run.read_state(first)))
        second = self.init("second", [3, 3], first)
        second_map, _ = prep.prepare(second, [], [])
        self.assertEqual("세 번째 개념\n", second_map["source_units"][0]["evidence"])
        self.assertEqual(3, second_map["source_units"][0]["source_start"]["line"])
        next_step = json.loads(self.call("next", str(second), "--brief"))
        self.assertEqual(str(first_body), next_step["reused_parts"][0]["body"])
        second_body = self.complete(second)
        third = self.init("third", [4, 4], second)
        third_body = self.complete(third)
        combined = self.root / "combined.tex"
        result = json.loads(self.call("compose", str(third), "--output", str(combined)))
        self.assertEqual(3, result["parts"])
        text = combined.read_text(encoding="utf-8")
        self.assertLess(text.index(first_body.as_posix()), text.index(second_body.as_posix()))
        self.assertLess(text.index(second_body.as_posix()), text.index(third_body.as_posix()))
        self.assertNotIn("1+1=2", text)  # 원고 본문을 복사하지 않고 연결한다.
        self.assertEqual(saved_state, first.read_bytes())
        self.assertEqual(saved_body, first_body.read_bytes())
        self.assertEqual(1, len(run.read_state(first)["cost_usage"]["premium_final_reviews"]))
        self.assertEqual(1, len(run.read_state(third)["cost_usage"]["premium_final_reviews"]))

    def test_unfinished_previous_run_and_overlapping_range_are_rejected(self):
        first = self.init("first", [1, 2])
        with self.assertRaisesRegex(run.RunError, "검수 완료"):
            self.init("premature", [3, 4], first)
        self.assertFalse((self.root / "workspace" / "premature").exists())
        self.complete(first)
        with self.assertRaisesRegex(ValueError, "완료 지점 뒤"):
            self.init("duplicate", [2, 4], first)
        with self.assertRaisesRegex(run.RunError, "같은 과목"):
            self.init("wrong-mode", [3, 4], first, mode="faithful")

    def test_changed_approved_body_blocks_next_verify_and_compose(self):
        first = self.init("first", [1, 2])
        body = self.complete(first)
        second = self.init("second", [3, 4], first)
        self.complete(second)
        body.write_text("검수 이후 변경", encoding="utf-8")
        with self.assertRaises(run.RunError):
            self.call("next", str(second))
        self.assertTrue(run.verify_state(run.read_state(second), check_inputs=True))
        output = self.root / "combined.tex"
        output.write_text("기존 누적 원고", encoding="utf-8")
        with self.assertRaises(run.RunError):
            self.call("compose", str(second), "--output", str(output), "--force")
        self.assertEqual("기존 누적 원고", output.read_text(encoding="utf-8"))

    def test_changed_input_or_review_record_blocks_reuse(self):
        first = self.init("first", [1, 2])
        self.complete(first)
        second = self.init("second", [3, 4], first)
        self.call("refresh-inputs", str(first))
        with self.assertRaisesRegex(run.RunError, "실행 기록"):
            self.call("next", str(second))
        (self.inputs / "handout.txt").write_text("바뀐 원문", encoding="utf-8")
        with self.assertRaisesRegex(run.RunError, "검수 완료"):
            self.init("changed-input", [3, 4], first)

    def test_changed_extracted_evidence_blocks_reuse(self):
        first = self.init("derived", [1, 2])
        evidence = self.root / "extracted.txt"
        evidence.write_text("첫 개념\n두 번째 개념\n", encoding="utf-8")
        self.complete(first, extracted=(f"handout.txt={evidence}",))
        evidence.write_text("추출본이 바뀜\n", encoding="utf-8")
        with self.assertRaisesRegex(run.RunError, "근거 파일이 변경"):
            self.init("after-derived", [3, 4], first)

    def test_scope_guards_empty_future_pages_and_transcript_segments(self):
        (self.inputs / "slides.pdf").write_bytes(b"pdf fixture")
        segments = self.inputs / "transcript.json"
        segments.write_text(json.dumps({"segments": [
            {"id": i, "start": i, "end": i + 1, "text": f"발언 {i}"} for i in range(4)
        ]}), encoding="utf-8")
        state = self.init("limited", None, sources={"slides.pdf": {"pages": [1, 1]}, "transcript.json": {"segments": [2, 3]}},
                          classifications=("transcript.json=transcript",))
        with patch.object(prep, "extract_pdf_pages", return_value=["첫 페이지", "", "미진행 범위"]):
            source_map, _ = prep.prepare(state, [], [])
        evidence = " ".join(unit["evidence"] for unit in source_map["source_units"])
        self.assertIn("첫 페이지", evidence)
        self.assertIn("발언 1", evidence)
        self.assertNotIn("발언 0", evidence)
        self.assertNotIn("미진행", evidence)
        with patch.object(prep, "extract_pdf_pages", return_value=["", "뒷 페이지"]):
            with self.assertRaisesRegex(prep.PreparationError, "빈 PDF"):
                prep.prepare(state, [], [])

    def test_out_of_bounds_and_source_map_scope_mismatch_are_rejected(self):
        state = self.init("too-long", [1, 9])
        with self.assertRaisesRegex(ValueError, "실제 자료"):
            prep.prepare(state, [], [])
        state = self.init("valid", [2, 3])
        source_map, _ = prep.prepare(state, [], [])
        source_map["source_units"][0]["source_start"]["line"] = 1
        with self.assertRaisesRegex(ValueError, "진도를 벗어"):
            validate_map_scope(source_map, run.read_state(state)["scope"])
        source_map["source_units"][0]["source_start"]["line"] = 3
        with self.assertRaisesRegex(ValueError, "구간이 누락"):
            validate_map_scope(source_map, run.read_state(state)["scope"])

    def test_all_file_selector_still_requires_every_row(self):
        state = self.init("all-file", None, sources={"handout.txt": {"all": True}})
        source_map, _ = prep.prepare(state, [], [])
        source_map["source_units"][0]["source_end"]["line"] = 3
        with self.assertRaisesRegex(ValueError, "진도 끝"):
            validate_map_scope(source_map, run.read_state(state)["scope"])

    def test_force_cannot_replace_approved_body_and_markdown_parts_are_reused(self):
        first = self.init("md1", [1, 2], mode="faithful")
        first_body = self.complete(first)
        second = self.init("md2", [3, 4], first, mode="faithful")
        second_body = self.complete(second)
        with self.assertRaisesRegex(run.RunError, "승인 산출물"):
            self.call("compose", str(second), "--output", str(first_body), "--force")
        combined = self.root / "combined.md"
        self.call("compose", str(second), "--output", str(combined))
        self.assertEqual(first_body.read_text().rstrip() + "\n\n" + second_body.read_text().rstrip() + "\n",
                         combined.read_text())

    def test_invalid_scopes_fail_before_state_creation(self):
        for source, selector in (("../secret.txt", {"all": True}), ("a.txt", {"pages": [0, 1]}),
                                 ("a.txt", {"pages": [True, 4]}), ("a.txt", {"all": 1})):
            with self.subTest(source=source, selector=selector), self.assertRaises(ValueError):
                validate_scope({"label": "test", "sources": {source: selector}})


if __name__ == "__main__":
    unittest.main()
