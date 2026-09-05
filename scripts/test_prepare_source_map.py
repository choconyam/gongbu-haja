"""원문 보존·누락 거부·캐시 재사용을 실제 임시 입력으로 검사한다."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import manage_run, prepare_source_map as prep
from scripts import prepare_transcript_review as transcript_prep


class PrepareSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()

    def init_state(self):
        args = manage_run.build_parser().parse_args([
            "init", str(self.inputs), "--lecture-id", "lesson", "--note-mode", "faithful",
            "--runtime", "codex", "--root", str(self.root),
        ])
        manage_run.command_init(args)
        return self.root / "workspace" / "lesson" / "run_state.json"

    def test_keeps_all_words_examples_blanks_and_duplicates(self):
        original = "정의와 예시\n\n반례와 단서\n" * 350 + "시험 범위 42\n"
        (self.inputs / "lecture.txt").write_text(original, encoding="utf-8")
        state = self.init_state()
        source_map, screening = prep.prepare(state, [], [])
        units = source_map["source_units"]
        self.assertEqual(original, "".join(unit["evidence"] for unit in units))
        self.assertEqual(len(original.splitlines()), sum(unit["row_count"] for unit in units))
        self.assertEqual(1, units[0]["source_start"]["line"])
        for left, right in zip(units, units[1:]):
            self.assertEqual(left["source_end"]["line"] + 1, right["source_start"]["line"])
        self.assertFalse(screening["semantic_reviewed"])
        self.assertFalse(screening["reviewed_against_audio"])
        self.assertTrue(screening["flagged_units"])
        folder = self.root / "prepared"
        prep.write_outputs(folder, source_map, screening)
        mtime = (folder / "source_map.json").stat().st_mtime_ns
        again = prep.prepare(state, [], [])
        self.assertEqual(source_map, again[0])
        prep.write_outputs(folder, *again)
        self.assertEqual(mtime, (folder / "source_map.json").stat().st_mtime_ns)
        source_map["lecture_id"] = "different"
        with self.assertRaises(prep.PreparationError):
            prep.write_outputs(folder, source_map, screening)

    def test_two_recordings_keep_every_segment_and_unverified_state(self):
        (self.inputs / "part1.mp3").write_bytes(b"audio-1")
        (self.inputs / "part2.mp3").write_bytes(b"audio-2")
        state = self.init_state()
        segments = [{"id": i, "start": i, "end": i + 1, "text": f"예시 {i}",
                     "avg_logprob": -1.5 if i == 3 else -0.1} for i in range(100)]
        derivative = self.root / "segments.json"
        derivative.write_text(json.dumps({"segments": segments}), encoding="utf-8")
        source_map, screening = prep.prepare(state, [f"part1.mp3={derivative}", f"part2.mp3={derivative}"], [])
        self.assertEqual(2, len(source_map["source_files"]))
        self.assertEqual(200, sum(unit["row_count"] for unit in source_map["source_units"]))
        for source in source_map["source_files"]:
            units = [unit for unit in source_map["source_units"] if unit["source_id"] == source["source_id"]]
            self.assertEqual("\n".join(segment["text"] for segment in segments), "\n".join(unit["evidence"] for unit in units))
        ids = [unit["source_unit_id"] for unit in source_map["source_units"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({"audio_unverified"}, {source["verification"] for source in source_map["source_files"]})
        self.assertTrue(any("avg_logprob" in item["flags"] for item in screening["flagged_units"]))
        with self.assertRaisesRegex(prep.PreparationError, "녹음 전사"):
            prep.prepare(state, [f"part1.mp3={derivative}"], [])

    def test_changed_input_and_unmapped_sources_are_rejected(self):
        source = self.inputs / "lecture.txt"
        source.write_text("original", encoding="utf-8")
        state = self.init_state()
        with self.assertRaisesRegex(prep.PreparationError, "등록되지"):
            prep.prepare(state, [], [f"unknown.txt={source}"])
        source.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(prep.PreparationError, "refresh-inputs"):
            prep.prepare(state, [], [])

    def test_partial_pdf_extraction_is_not_silently_accepted(self):
        (self.inputs / "slides.pdf").write_bytes(b"fake-pdf")
        state = self.init_state()
        with patch.object(prep, "extract_pdf_pages", return_value=["page one", ""]):
            with self.assertRaisesRegex(prep.PreparationError, "빈 PDF 페이지"):
                prep.prepare(state, [], [])
        with patch.object(prep, "extract_pdf_pages", return_value=["page one", "page two"]):
            source_map, _ = prep.prepare(state, [], [])
            self.assertEqual(2, len(source_map["source_units"]))
            self.assertTrue(all("visual_review_required" in unit["flags"] for unit in source_map["source_units"]))

    def test_pdf_page_terminator_is_not_an_extra_blank_page(self):
        with patch.object(transcript_prep.shutil, "which", return_value="pdftotext"), \
                patch.object(transcript_prep.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="page one\f\fpage three\f", stderr="")):
            self.assertEqual(["page one", "", "page three"], transcript_prep._extract_pdf_pdftotext(Path("x.pdf")))

    def test_empty_and_unsupported_files_require_resolution(self):
        source = self.inputs / "slides.pptx"
        source.write_bytes(b"pptx")
        state = self.init_state()
        with self.assertRaisesRegex(prep.PreparationError, "텍스트 추출본"):
            prep.prepare(state, [], [])
        extracted = self.root / "extracted.txt"
        extracted.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(prep.PreparationError, "빈 자료"):
            prep.prepare(state, [], [f"slides.pptx={extracted}"])
        extracted.write_text("슬라이드 1의 정의", encoding="utf-8")
        source_map, _ = prep.prepare(state, [], [f"slides.pptx={extracted}"])
        self.assertEqual("slides.pptx", source_map["source_files"][0]["path"])
        self.assertIn("visual_review_required", source_map["source_units"][0]["flags"])

    def test_malformed_segment_is_rejected_and_long_utterance_not_truncated(self):
        (self.inputs / "audio.mp3").write_bytes(b"audio")
        state = self.init_state()
        derivative = self.root / "segments.json"
        derivative.write_text(json.dumps({"segments": [{"text": 42}]}), encoding="utf-8")
        with self.assertRaisesRegex(prep.PreparationError, "문자열 text"):
            prep.prepare(state, [f"audio.mp3={derivative}"], [])
        text = "긴 발언과 사례 " * 500
        derivative.write_text(json.dumps({"segments": [{"text": text}]}), encoding="utf-8")
        source_map, screening = prep.prepare(state, [f"audio.mp3={derivative}"], [])
        self.assertEqual(text, source_map["source_units"][0]["evidence"])
        self.assertIn("invalid_timing", screening["flagged_units"][0]["flags"])
        derivative.write_text(json.dumps({"source_audio": "wrong.mp3", "segments": [{"text": text}]}), encoding="utf-8")
        with self.assertRaisesRegex(prep.PreparationError, "원본 녹음이 다릅니다"):
            prep.prepare(state, [f"audio.mp3={derivative}"], [])


if __name__ == "__main__":
    unittest.main()
