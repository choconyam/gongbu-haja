#!/usr/bin/env python3
"""외부 패키지 없이 배치 전사 큐의 수집·전달·요약 동작을 검증한다."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcribe_batch as tb  # noqa: E402

SCRIPT = Path(__file__).with_name("transcribe_batch.py")

TRANSCRIPT_SUFFIX_PARTS = (
    "_transcript_raw.srt",
    "_transcript_raw.txt",
    "_transcript_draft.md",
    "_segments.json",
    "_transcript_manifest.json",
)


def make_args(**overrides: object) -> Namespace:
    defaults: dict[str, object] = {
        "model": None,
        "language": None,
        "device": None,
        "beam_size": None,
        "min_silence_ms": None,
        "glossary": None,
        "output_root": None,
        "no_vad": False,
        "interactive": False,
        "force": False,
        "dry_run": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def make_transcript_package(output_root: Path, lecture_id: str, parts: tuple[str, ...]) -> None:
    transcript_dir = output_root / lecture_id / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    for part in parts:
        (transcript_dir / f"{lecture_id}{part}").write_text("기존 산출물", encoding="utf-8")


class CollectAudioFilesTests(unittest.TestCase):
    def test_directory_collects_only_audio_and_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a_lecture.m4a").write_bytes(b"a")
            (root / "b_lecture.mp4").write_bytes(b"b")
            (root / "handout.pdf").write_bytes(b"pdf")
            (root / "notes.txt").write_text("메모", encoding="utf-8")
            files, missing, no_audio = tb.collect_audio_files([str(root), str(root / "없는파일.wav")])
            self.assertEqual(["a_lecture.m4a", "b_lecture.mp4"], [item.name for item in files])
            self.assertEqual(["없는파일.wav"], [item.name for item in missing])
            self.assertEqual([], no_audio)

    def test_directory_without_audio_is_reported(self) -> None:
        # 엉뚱한 폴더를 지정한 사실이 요약에서 조용히 사라지면 안 된다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good"
            bad = root / "bad"
            good.mkdir()
            bad.mkdir()
            (good / "lecture.m4a").write_bytes(b"a")
            (bad / "handout.pdf").write_bytes(b"pdf")
            files, missing, no_audio = tb.collect_audio_files([str(good), str(bad)])
            self.assertEqual(["lecture.m4a"], [item.name for item in files])
            self.assertEqual([], missing)
            self.assertEqual([bad.resolve()], [item.resolve() for item in no_audio])

    def test_explicit_file_included_even_with_unusual_suffix(self) -> None:
        # 사용자가 직접 지정한 파일은 확장자 판정보다 사용자 의도를 우선한다.
        with tempfile.TemporaryDirectory() as temporary:
            oddball = Path(temporary) / "recording.amr"
            oddball.write_bytes(b"amr")
            files, missing, no_audio = tb.collect_audio_files([str(oddball)])
            self.assertEqual([oddball.resolve()], files)
            self.assertEqual([], missing)
            self.assertEqual([], no_audio)

    def test_duplicate_inputs_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "lecture.m4a"
            audio.write_bytes(b"a")
            files, _, _ = tb.collect_audio_files([str(audio), str(root)])
            self.assertEqual([audio.resolve()], files)


class PassthroughTests(unittest.TestCase):
    def test_only_user_supplied_options_are_forwarded(self) -> None:
        args = make_args(model="auto", beam_size=5, force=True)
        passthrough = tb.build_passthrough(args)
        self.assertEqual(["--model", "auto", "--beam-size", "5", "--force"], passthrough)

    def test_interactive_is_never_forwarded_to_children(self) -> None:
        # 자식 프로세스의 입력 대기(식별자·CPU 확인)로 무인 큐가 멈추면 안 된다.
        args = make_args(interactive=True, dry_run=True)
        self.assertEqual([], tb.build_passthrough(args))

    def test_empty_when_nothing_supplied(self) -> None:
        self.assertEqual([], tb.build_passthrough(make_args()))


class BatchCliTests(unittest.TestCase):
    # 실제 하위 프로세스로 실행해 대기열·요약·종료 코드를 함께 확인한다.
    def run_cli(self, *arguments: str, expected: int) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(expected, result.returncode, msg=result.stdout + result.stderr)
        return result

    def test_dry_run_batch_processes_files_serially(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "260310_테스트과목.wav").write_bytes(b"a")
            (root / "260317_테스트과목.wav").write_bytes(b"b")
            output_root = root / "workspace"
            result = self.run_cli(
                str(root), "--dry-run", "--output-root", str(output_root), expected=0
            )
            self.assertIn("전사 대기열: 2개", result.stdout)
            self.assertEqual(2, result.stdout.count("[성공]"))

    def test_identity_failure_is_reported_but_queue_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "음성 001.wav").write_bytes(b"a")  # 날짜·과목 추정 불가
            (root / "260310_테스트과목.wav").write_bytes(b"b")
            result = self.run_cli(
                str(root), "--dry-run", "--output-root", str(root / "workspace"), expected=1
            )
            self.assertIn("[강의 식별·인자 확인 필요]", result.stdout)
            self.assertIn("[성공]", result.stdout)
            self.assertIn("--interactive", result.stdout)

    def test_complete_existing_package_is_skipped_as_success(self) -> None:
        # 이미 전사가 끝난 강의를 다시 드래그해도 배치는 성공으로 끝나야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "260310_테스트과목.wav").write_bytes(b"a")
            output_root = root / "workspace"
            make_transcript_package(output_root, "2026-03-10_테스트과목_본강의", TRANSCRIPT_SUFFIX_PARTS)
            result = self.run_cli(str(root), "--output-root", str(output_root), expected=0)
            self.assertIn("[기존 산출물 있음(건너뜀)]", result.stdout)

    def test_partial_existing_package_is_an_error_not_a_skip(self) -> None:
        # 중단 흔적(SRT만 존재)을 '이미 전사됨'으로 오판하면 안 된다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "260310_테스트과목.wav").write_bytes(b"a")
            output_root = root / "workspace"
            make_transcript_package(
                output_root, "2026-03-10_테스트과목_본강의", TRANSCRIPT_SUFFIX_PARTS[:1]
            )
            result = self.run_cli(str(root), "--output-root", str(output_root), expected=1)
            self.assertIn("[오류]", result.stdout)
            self.assertNotIn("[기존 산출물 있음(건너뜀)]", result.stdout)

    def test_missing_input_returns_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_cli(str(Path(temporary) / "없는폴더"), "--dry-run", expected=2)
            self.assertIn("찾지 못했습니다", result.stderr)


if __name__ == "__main__":
    unittest.main()
