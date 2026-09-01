#!/usr/bin/env python3
"""외부 패키지 없이 manage_run.py의 핵심 상태 전이를 검증한다."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# 실제 사용자 폴더를 건드리지 않도록 모든 시나리오는 임시 폴더에서 실행한다.
SCRIPT = Path(__file__).with_name("manage_run.py")


class ManageRunTests(unittest.TestCase):
    # CLI를 실제 하위 프로세스로 실행해 출력과 종료 코드까지 함께 확인한다.
    def run_cli(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(expected, result.returncode, msg=result.stdout + result.stderr)
        return result

    def test_routes_audio_without_transcript_and_enforces_order(self) -> None:
        # 녹음만 있으면 전사부터 시작하고 작성 담당의 선행 실행은 차단해야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "lecture.m4a").write_bytes(b"test-audio")
            (inputs / "handout.pdf").write_bytes(b"test-pdf")

            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "2026-09-01_test", "--root", str(root)
            )
            state_file = Path(result.stdout.strip())
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("ready", state["roles"]["transcriber"]["status"])
            self.assertEqual("blocked", state["roles"]["transcript_auditor"]["status"])
            self.run_cli("start", str(state_file), "--role", "writer", expected=2)

            artifact = state_file.parent / "transcript.json"
            artifact.write_text("{}", encoding="utf-8")
            self.run_cli("start", str(state_file), "--role", "transcriber")
            self.run_cli("complete", str(state_file), "--role", "transcriber", "--artifact", str(artifact))
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("ready", state["roles"]["transcript_auditor"]["status"])

    def test_routes_video_recordings_as_audio(self) -> None:
        # Zoom 녹화에 흔한 MP4/WebM도 전사 파이프라인에서 빠지면 안 된다.
        for suffix in (".mp4", ".webm"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                inputs = root / "input"
                inputs.mkdir()
                (inputs / f"lecture{suffix}").write_bytes(b"test-video")
                result = self.run_cli(
                    "init", str(inputs), "--lecture-id", f"video-{suffix[1:]}", "--root", str(root)
                )
                state = json.loads(Path(result.stdout.strip()).read_text(encoding="utf-8"))
                self.assertEqual(1, state["routing_summary"]["audio_files"])
                self.assertEqual("ready", state["roles"]["transcriber"]["status"])

    def test_transcript_avoids_unnecessary_transcription(self) -> None:
        # 기존 전사본이 있으면 토큰·시간 낭비가 되는 재전사를 생략해야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "lecture.m4a").write_bytes(b"test-audio")
            (inputs / "lecture_transcript.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\ntest\n", encoding="utf-8")

            result = self.run_cli("init", str(inputs), "--lecture-id", "lecture", "--root", str(root))
            state = json.loads(Path(result.stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual("skipped", state["roles"]["transcriber"]["status"])
            self.assertEqual("ready", state["roles"]["transcript_auditor"]["status"])
            self.assertEqual("skipped", state["roles"]["instructor_integrator"]["status"])

    def test_manual_transcript_classification_avoids_retranscription(self) -> None:
        # 이름만으로 알기 어려운 TXT 전사는 관리자가 명시적으로 교정할 수 있어야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "lecture.m4a").write_bytes(b"test-audio")
            (inputs / "강의내용메모.txt").write_text("실제 전사 내용", encoding="utf-8")
            result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "manual-transcript",
                "--root",
                str(root),
                "--classify",
                "강의내용메모.txt=transcript",
            )
            state = json.loads(Path(result.stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual(1, state["routing_summary"]["transcript_files"])
            self.assertEqual("skipped", state["roles"]["transcriber"]["status"])

    def test_activated_role_restores_required_dependencies(self) -> None:
        # 작성 초안이 없을 때 설명 보강 역할을 시작할 수 없어야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli("init", str(inputs), "--lecture-id", "deps", "--root", str(root))
            state_file = Path(result.stdout.strip())
            self.run_cli(
                "activate", str(state_file), "--role", "pedagogy_editor", "--reason", "설명 부족"
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(["writer"], state["roles"]["pedagogy_editor"]["dependencies"])
            self.assertEqual("blocked", state["roles"]["pedagogy_editor"]["status"])
            self.run_cli("start", str(state_file), "--role", "pedagogy_editor", expected=2)

    def test_deactivate_transcriber_unblocks_auditor_for_provided_transcript(self) -> None:
        # 관리자가 기존 전사를 확인한 경우 불필요한 자동 전사를 명시적으로 끌 수 있어야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "lecture.m4a").write_bytes(b"test-audio")
            result = self.run_cli("init", str(inputs), "--lecture-id", "deactivate", "--root", str(root))
            state_file = Path(result.stdout.strip())
            self.run_cli(
                "deactivate",
                str(state_file),
                "--role",
                "transcriber",
                "--reason",
                "사용자 제공 전사본을 별도로 확인함",
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("skipped", state["roles"]["transcriber"]["status"])
            self.assertEqual([], state["roles"]["transcript_auditor"]["dependencies"])
            self.assertEqual("ready", state["roles"]["transcript_auditor"]["status"])

    def test_full_minimal_route_and_hash_guards(self) -> None:
        # 최소 경로가 완주하며 입력이나 통과 산출물이 바뀌면 검증이 실패해야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            source = inputs / "handout.pdf"
            source.write_bytes(b"test-pdf")
            result = self.run_cli("init", str(inputs), "--lecture-id", "minimal", "--root", str(root))
            state_file = Path(result.stdout.strip())

            for role in ("source_mapper", "writer", "layout_builder", "final_reviewer", "maintainer"):
                artifact = state_file.parent / f"{role}.txt"
                artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            self.run_cli("verify", str(state_file), "--check-inputs")
            source.write_bytes(b"changed-pdf")
            self.run_cli("verify", str(state_file), "--check-inputs", expected=1)

            source.write_bytes(b"test-pdf")
            artifact = state_file.parent / "writer.txt"
            artifact.write_text("changed-writer", encoding="utf-8")
            result = self.run_cli("verify", str(state_file), expected=1)
            self.assertIn("변경되었거나 누락된 산출물", result.stdout)

    def test_refresh_inputs_invalidates_affected_pipeline(self) -> None:
        # 새 교안이 들어오면 수동 JSON 삭제 없이 자료 매핑부터 다시 실행해야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"first")
            result = self.run_cli("init", str(inputs), "--lecture-id", "refresh", "--root", str(root))
            state_file = Path(result.stdout.strip())
            self.run_cli("refresh-inputs", str(state_file))

            (inputs / "supplement.pdf").write_bytes(b"second")
            self.run_cli("refresh-inputs", str(state_file))
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(2, len(state["inputs"]))
            self.assertEqual("ready", state["roles"]["source_mapper"]["status"])
            self.assertEqual("blocked", state["roles"]["writer"]["status"])

    def test_state_lock_blocks_concurrent_mutation(self) -> None:
        # 같은 강의 상태에 두 세션이 붙는 사고는 락으로 막고, 락은 명령 후 정리돼야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli("init", str(inputs), "--lecture-id", "lock", "--root", str(root))
            state_file = Path(result.stdout.strip())
            lock_file = state_file.with_name(state_file.name + ".lock")
            self.assertFalse(lock_file.exists())

            lock_file.write_text("pid=테스트 at=수동생성\n", encoding="utf-8")
            result = self.run_cli("start", str(state_file), "--role", "source_mapper", expected=2)
            self.assertIn("다른 프로세스가 같은 강의 상태를 수정 중", result.stderr)

            lock_file.unlink()
            self.run_cli("start", str(state_file), "--role", "source_mapper")
            self.assertFalse(lock_file.exists())

    def test_failed_init_leaves_no_workspace_folder(self) -> None:
        # 잘못된 입력으로 실패한 init이 유령 강의 폴더를 남기면 안 된다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty_inputs = root / "input"
            empty_inputs.mkdir()
            self.run_cli("init", str(empty_inputs), "--lecture-id", "ghost", "--root", str(root), expected=2)
            self.assertFalse((root / "workspace" / "ghost").exists())

    def test_mutation_on_missing_state_path_creates_no_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing_state = Path(temporary) / "no_such" / "deep" / "run_state.json"
            self.run_cli("start", str(missing_state), "--role", "writer", expected=2)
            self.assertFalse(missing_state.parent.exists())

    def test_lecture_id_with_trailing_dot_or_space_is_rejected(self) -> None:
        # Windows 경로 정규화로 'week3.'와 'week3'가 같은 폴더로 합쳐지는 사고 방지.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"pdf")
            for bad_id in ("week3.", "week3 "):
                with self.subTest(lecture_id=bad_id):
                    self.run_cli(
                        "init", str(inputs), "--lecture-id", bad_id, "--root", str(root), expected=2
                    )

    def test_refresh_preserves_manual_role_and_its_dependency(self) -> None:
        # 입력 갱신 뒤에도 수동 활성화한 기술 검수가 조판 선행조건에서 빠지면 안 된다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"first")
            result = self.run_cli("init", str(inputs), "--lecture-id", "manual-role", "--root", str(root))
            state_file = Path(result.stdout.strip())
            self.run_cli(
                "activate",
                str(state_file),
                "--role",
                "formula_code_checker",
                "--reason",
                "교안에서 수식 발견",
            )
            (inputs / "supplement.pdf").write_bytes(b"second")
            self.run_cli("refresh-inputs", str(state_file))
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(state["roles"]["formula_code_checker"]["active"])
            self.assertEqual(["writer"], state["roles"]["formula_code_checker"]["dependencies"])
            self.assertIn("formula_code_checker", state["roles"]["layout_builder"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
