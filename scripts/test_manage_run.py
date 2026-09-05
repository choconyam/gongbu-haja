#!/usr/bin/env python3
"""외부 패키지 없이 manage_run.py의 핵심 상태 전이를 검증한다."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# 실제 사용자 폴더를 건드리지 않도록 모든 시나리오는 임시 폴더에서 실행한다.
SCRIPT = Path(__file__).with_name("manage_run.py")


def runtime_neutral_env(**extra: str) -> dict[str, str]:
    """런타임 감지 신호(CLAUDE*, CODEX_*)를 걷어낸 하위 프로세스 환경."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE", "CODEX_"))}
    env.update(extra)
    return env


class ManageRunTests(unittest.TestCase):
    # CLI를 실제 하위 프로세스로 실행해 출력과 종료 코드까지 함께 확인한다.
    # init에는 런타임과 제작 모드가 필수이므로, 기존 시나리오는 Codex·faithful을 기본으로 넣는다.
    def run_cli(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        if command and command[0] == "init":
            if "--runtime" not in command:
                command += ["--runtime", "codex"]
            if "--note-mode" not in command:
                command += ["--note-mode", "faithful"]
        return self.run_raw(*command, expected=expected)

    def run_raw(
        self,
        *arguments: str,
        expected: int = 0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
        )
        self.assertEqual(expected, result.returncode, msg=result.stdout + result.stderr)
        return result

    def write_source_map(self, state_file: Path) -> Path:
        path = state_file.parent / "source_map.json"
        path.write_text(
            json.dumps(
                {
                    "kind": "study_note_source_map",
                    "schema_version": 1,
                    "source_units": [{"source_unit_id": "handout-page-1"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_coverage(self, state_file: Path, mode: str) -> Path:
        path = state_file.parent / f"coverage-{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "kind": "study_note_source_coverage",
                    "schema_version": 1,
                    "note_mode": mode,
                    "reviewer_profile": "review_high" if mode == "faithful" else "quality_xhigh",
                    "items": [
                        {
                            "source_unit_id": "handout-page-1",
                            "decision": "included",
                            "note_refs": ["section-1"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def complete_final_review(
        self,
        state_file: Path,
        artifact: Path,
        source_map: Path,
        mode: str,
    ) -> None:
        coverage = self.write_coverage(state_file, mode)
        self.run_cli(
            "complete",
            str(state_file),
            "--role",
            "final_reviewer",
            "--artifact",
            str(artifact),
            "--source-map",
            str(source_map),
            "--coverage-report",
            str(coverage),
        )

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

    def test_note_mode_is_recorded_and_changes_default_enrichment_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")

            faithful_result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "faithful-mode",
                "--root",
                str(root),
                "--note-mode",
                "faithful",
            )
            faithful = json.loads(Path(faithful_result.stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual("faithful", faithful["note_mode"])
            self.assertEqual("자료 충실형", faithful["mode_contract"]["label"])
            self.assertEqual("skipped", faithful["roles"]["pedagogy_editor"]["status"])

            deep_result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "deep-mode",
                "--root",
                str(root),
                "--note-mode",
                "deep",
            )
            deep_state_file = Path(deep_result.stdout.strip())
            deep = json.loads(deep_state_file.read_text(encoding="utf-8"))
            self.assertEqual("deep", deep["note_mode"])
            self.assertEqual("심화 이해형", deep["mode_contract"]["label"])
            self.assertTrue(deep["roles"]["pedagogy_editor"]["active"])
            self.assertEqual("blocked", deep["roles"]["pedagogy_editor"]["status"])
            next_payload = json.loads(self.run_cli("next", str(deep_state_file)).stdout)
            self.assertEqual("deep", next_payload["note_mode"])

    def test_schema_v1_state_is_migrated_without_restarting_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "legacy-state", "--root", str(root)
            )
            state_file = Path(result.stdout.strip())
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state.pop("review_cycle")
            state["cost_usage"] = {
                "critical_review_limit": 1,
                "critical_reviews": [],
            }
            for role, entry in state["roles"].items():
                entry.pop("active_profile", None)
                entry.pop("premium_call_id", None)
                if role == "final_reviewer":
                    entry["max_attempts"] = 2
            state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            self.run_cli("status", str(state_file))
            self.run_cli("refresh-inputs", str(state_file))
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(3, migrated["schema_version"])
            self.assertEqual("codex", migrated["runtime"])
            self.assertEqual("gpt-5.6-luna", migrated["runtime_model_table"]["economy_high"]["model"])
            self.assertEqual(1, migrated["review_cycle"])
            self.assertEqual(1, migrated["roles"]["final_reviewer"]["max_attempts"])
            self.assertEqual([], migrated["cost_usage"]["premium_final_reviews"])

    def test_execution_policy_is_mode_aware_and_prefers_python_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "lecture.wav").write_bytes(b"test-audio")
            (inputs / "handout.pdf").write_bytes(b"test-pdf")

            result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "cost-routing",
                "--root",
                str(root),
            )
            state_file = Path(result.stdout.strip())
            state = json.loads(state_file.read_text(encoding="utf-8"))

            self.assertTrue(state["cost_policy"]["deterministic_first"])
            self.assertEqual("economy_high", state["cost_policy"]["default_subagent_profile"])
            self.assertEqual(0, state["cost_policy"]["full_role_retries"])
            self.assertFalse(state["cost_policy"]["automatic_flagship_escalation"])
            # 프로필에는 모델명이 없고, 런타임 표 스냅샷이 실제 모델·effort를 든다.
            self.assertEqual("codex", state["runtime"])
            self.assertNotIn("codex_model", state["execution_profiles"]["economy_high"])
            table = state["runtime_model_table"]
            self.assertEqual({"agent": "study_note_worker", "model": "gpt-5.6-luna", "effort": "high"}, table["economy_high"])
            self.assertEqual({"agent": "faithful_note_reviewer", "model": "gpt-5.6-sol", "effort": "high"}, table["review_high"])
            self.assertEqual({"agent": "quality_note_worker", "model": "gpt-5.6-sol", "effort": "high"}, table["quality_high"])
            self.assertEqual({"agent": "deep_note_reviewer", "model": "gpt-5.6-sol", "effort": "xhigh"}, table["quality_xhigh"])
            self.assertTrue(state["cost_policy"]["terra_enabled"] is False)

            self.assertEqual("python", state["roles"]["transcriber"]["execution"]["executor"])
            self.assertEqual("hybrid", state["roles"]["transcript_auditor"]["execution"]["executor"])
            self.assertEqual("subagent", state["roles"]["writer"]["execution"]["executor"])
            self.assertEqual("quality_high", state["roles"]["writer"]["execution"]["agent_profile"])
            self.assertEqual("review_high", state["roles"]["final_reviewer"]["execution"]["agent_profile"])
            self.assertEqual("python", state["roles"]["maintainer"]["execution"]["executor"])

            deep_result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "cost-routing-deep",
                "--root",
                str(root),
                "--note-mode",
                "deep",
            )
            deep = json.loads(Path(deep_result.stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual("economy_high", deep["roles"]["source_mapper"]["execution"]["agent_profile"])
            self.assertEqual("quality_high", deep["roles"]["writer"]["execution"]["agent_profile"])
            self.assertEqual("quality_high", deep["roles"]["pedagogy_editor"]["execution"]["agent_profile"])
            self.assertEqual("quality_xhigh", deep["roles"]["final_reviewer"]["execution"]["agent_profile"])
            self.assertIsNone(deep["roles"]["final_reviewer"]["execution"]["escalation_profile"])

            next_payload = json.loads(self.run_cli("next", str(state_file)).stdout)
            self.assertEqual("economy_high", next_payload["cost_policy"]["default_subagent_profile"])
            self.assertEqual("python", next_payload["ready"][0]["execution"]["executor"])

            state["roles"]["writer"]["execution"]["agent_profile"] = "unbounded-expensive-model"
            state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            drift = self.run_cli("status", str(state_file), expected=2)
            self.assertIn("실행 정책이 프로젝트 기준과 일치하지 않습니다", drift.stderr)

    def test_failed_role_allows_only_one_scoped_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "retry-budget",
                "--root",
                str(root),
            )
            state_file = Path(result.stdout.strip())

            self.run_cli("start", str(state_file), "--role", "source_mapper")
            self.run_cli(
                "fail",
                str(state_file),
                "--role",
                "source_mapper",
                "--reason",
                "교안 2쪽 대응 불명확",
            )
            self.run_cli("start", str(state_file), "--role", "source_mapper", expected=2)
            repair_packet = state_file.parent / "repair_packet.json"
            repair_packet.write_text(
                json.dumps(
                    {
                        "model_input": True,
                        "kind": "repair_packet",
                        "target": "교안 2쪽과 전사 구간 4",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.run_cli(
                "start",
                str(state_file),
                "--role",
                "source_mapper",
                "--repair-scope",
                "전체 강의 재검수",
                "--repair-packet",
                str(repair_packet),
                expected=2,
            )
            self.run_cli(
                "start",
                str(state_file),
                "--role",
                "source_mapper",
                "--repair-scope",
                "교안 2쪽과 전사 구간 4만 재검수",
                "--repair-packet",
                str(repair_packet),
            )
            self.run_cli(
                "fail",
                str(state_file),
                "--role",
                "source_mapper",
                "--reason",
                "근거 부족",
            )
            third = self.run_cli(
                "start",
                str(state_file),
                "--role",
                "source_mapper",
                "--repair-scope",
                "교안 2쪽",
                "--repair-packet",
                str(repair_packet),
                expected=2,
            )
            self.assertIn("재검수 한도를 초과", third.stderr)

    def test_targeted_sol_review_requires_one_small_explicit_model_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "critical-budget",
                "--root",
                str(root),
            )
            state_file = Path(result.stdout.strip())
            packet = state_file.parent / "packet.json"
            packet.write_text(
                json.dumps(
                    {
                        "model_input": True,
                        "kind": "review_packet",
                        "target": {"claim": "숫자 10인지 100인지 불명확"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(packet),
                "--category",
                "number",
                "--reason",
                "핵심 수치 충돌",
                expected=2,
            )
            self.run_cli("start", str(state_file), "--role", "source_mapper")

            aggregate = state_file.parent / "aggregate.json"
            aggregate.write_text('{"model_input": false}', encoding="utf-8")
            self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(aggregate),
                "--category",
                "number",
                "--reason",
                "핵심 수치 충돌",
                expected=2,
            )
            oversized = state_file.parent / "oversized.json"
            oversized.write_text(
                json.dumps({"model_input": True, "text": "x" * (17 * 1024)}),
                encoding="utf-8",
            )
            self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(oversized),
                "--category",
                "number",
                "--reason",
                "핵심 수치 충돌",
                expected=2,
            )

            dispatch = self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(packet),
                "--category",
                "number",
                "--reason",
                "핵심 수치 충돌",
            )
            payload = json.loads(dispatch.stdout)
            self.assertEqual("critical-review-1", payload["call_id"])
            self.assertEqual("codex", payload["execution"]["runtime"])
            self.assertEqual("gpt-5.6-sol", payload["execution"]["model"])
            self.assertEqual("high", payload["execution"]["reasoning_effort"])
            self.assertEqual(0, payload["remaining_critical_reviews"])
            self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(packet),
                "--category",
                "number",
                "--reason",
                "다시 검수",
                expected=2,
            )
            self.run_cli(
                "fail",
                str(state_file),
                "--role",
                "source_mapper",
                "--reason",
                "핵심 수치 충돌이 남음",
            )
            self.run_cli(
                "start",
                str(state_file),
                "--role",
                "source_mapper",
                "--repair-scope",
                "교안 2쪽 수치만 재검수",
                "--repair-packet",
                str(packet),
                expected=2,
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(1, len(state["cost_usage"]["critical_reviews"]))
            self.assertEqual("failed", state["cost_usage"]["critical_reviews"][0]["status"])

    def test_failed_role_escalation_becomes_a_tracked_running_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "failed-escalation", "--root", str(root)
            )
            state_file = Path(result.stdout.strip())
            packet = state_file.parent / "review_packet.json"
            packet.write_text(
                json.dumps(
                    {
                        "model_input": True,
                        "kind": "review_packet",
                        "target": {"source_unit_id": "handout-page-1"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.run_cli("start", str(state_file), "--role", "source_mapper")
            self.run_cli(
                "fail", str(state_file), "--role", "source_mapper", "--reason", "수치 충돌"
            )
            self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(packet),
                "--category",
                "number",
                "--reason",
                "교안 핵심 수치 확인",
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("running", state["roles"]["source_mapper"]["status"])
            self.assertEqual("quality_high", state["roles"]["source_mapper"]["active_profile"])
            self.assertEqual(
                "critical-review-1", state["roles"]["source_mapper"]["critical_review_call_id"]
            )
            self.assertEqual("running", state["cost_usage"]["critical_reviews"][0]["status"])

            source_map = self.write_source_map(state_file)
            self.run_cli(
                "complete",
                str(state_file),
                "--role",
                "source_mapper",
                "--artifact",
                str(source_map),
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("passed", state["cost_usage"]["critical_reviews"][0]["status"])

    def test_set_mode_reuses_mapping_and_invalidates_only_writer_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "mode-change",
                "--root",
                str(root),
                "--note-mode",
                "faithful",
            )
            state_file = Path(result.stdout.strip())

            source_map = self.write_source_map(state_file)
            for role in ("source_mapper", "writer", "layout_builder", "final_reviewer"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                if role == "final_reviewer":
                    self.complete_final_review(state_file, artifact, source_map, "faithful")
                else:
                    self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            self.run_cli(
                "set-mode",
                str(state_file),
                "--note-mode",
                "deep",
                "--reason",
                "수식 유도와 배경지식 보강 필요",
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("deep", state["note_mode"])
            self.assertEqual("passed", state["roles"]["source_mapper"]["status"])
            self.assertEqual("ready", state["roles"]["writer"]["status"])
            self.assertEqual("blocked", state["roles"]["pedagogy_editor"]["status"])
            self.assertEqual("blocked", state["roles"]["layout_builder"]["status"])
            self.assertEqual("blocked", state["roles"]["final_reviewer"]["status"])
            self.assertEqual([], state["roles"]["writer"]["artifacts"])

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

            source_map = self.write_source_map(state_file)
            for role in ("source_mapper", "writer", "layout_builder", "final_reviewer"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                if role == "final_reviewer":
                    self.complete_final_review(state_file, artifact, source_map, "faithful")
                else:
                    self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("skipped", state["roles"]["maintainer"]["status"])
            self.assertEqual(3, state["schema_version"])
            self.assertEqual(1, state["roles"]["final_reviewer"]["max_attempts"])
            premium = state["cost_usage"]["premium_final_reviews"]
            self.assertEqual(1, len(premium))
            self.assertEqual("faithful_final_review_high", premium[0]["route"])
            self.assertEqual("review_high", premium[0]["profile"])
            self.assertEqual("gpt-5.6-sol", premium[0]["model"])
            self.assertEqual("high", premium[0]["reasoning_effort"])
            self.assertEqual("passed", premium[0]["status"])

            self.run_cli("verify", str(state_file), "--check-inputs")
            source.write_bytes(b"changed-pdf")
            self.run_cli("verify", str(state_file), "--check-inputs", expected=1)

            source.write_bytes(b"test-pdf")
            artifact = state_file.parent / "writer.txt"
            artifact.write_text("changed-writer", encoding="utf-8")
            result = self.run_cli("verify", str(state_file), expected=1)
            self.assertIn("변경되었거나 누락된 산출물", result.stdout)

    def test_final_review_requires_complete_mode_matched_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "coverage-gate", "--root", str(root)
            )
            state_file = Path(result.stdout.strip())
            source_map = self.write_source_map(state_file)
            for role in ("source_mapper", "writer", "layout_builder"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            review = state_file.parent / "review.txt"
            review.write_text("pass", encoding="utf-8")
            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            self.run_cli(
                "complete",
                str(state_file),
                "--role",
                "final_reviewer",
                "--artifact",
                str(review),
                expected=2,
            )

            bad_coverage = self.write_coverage(state_file, "faithful")
            payload = json.loads(bad_coverage.read_text(encoding="utf-8"))
            payload["items"] = []
            bad_coverage.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.run_cli(
                "complete",
                str(state_file),
                "--role",
                "final_reviewer",
                "--artifact",
                str(review),
                "--source-map",
                str(source_map),
                "--coverage-report",
                str(bad_coverage),
                expected=2,
            )

            coverage = self.write_coverage(state_file, "faithful")
            self.run_cli(
                "complete",
                str(state_file),
                "--role",
                "final_reviewer",
                "--artifact",
                str(review),
                "--source-map",
                str(source_map),
                "--coverage-report",
                str(coverage),
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("review_high", state["roles"]["final_reviewer"]["coverage_gate"]["reviewer_profile"])
            coverage.write_text("{}", encoding="utf-8")
            verify = self.run_cli("verify", str(state_file), expected=1)
            self.assertIn("coverage report가 변경되었거나 누락됨", verify.stdout)

    def test_deep_final_review_is_one_sol_xhigh_call_per_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init",
                str(inputs),
                "--lecture-id",
                "deep-premium-budget",
                "--root",
                str(root),
                "--note-mode",
                "deep",
            )
            state_file = Path(result.stdout.strip())
            source_map = self.write_source_map(state_file)
            for role in ("source_mapper", "writer", "pedagogy_editor", "layout_builder"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            calls = state["cost_usage"]["premium_final_reviews"]
            self.assertEqual(1, len(calls))
            self.assertEqual("deep_final_sol_xhigh", calls[0]["route"])
            self.assertEqual("gpt-5.6-sol", calls[0]["model"])
            self.assertEqual("xhigh", calls[0]["reasoning_effort"])
            self.assertEqual("running", calls[0]["status"])

            self.run_cli(
                "fail",
                str(state_file),
                "--role",
                "final_reviewer",
                "--reason",
                "국소 수정안을 같은 호출에서 반영할 수 없음",
            )
            retry = self.run_cli(
                "start",
                str(state_file),
                "--role",
                "final_reviewer",
                expected=2,
            )
            self.assertIn("재검수 한도를 초과", retry.stderr)
            bypass = self.run_cli(
                "rerun",
                str(state_file),
                "--role",
                "layout_builder",
                "--change-kind",
                "output_contract",
                "--reason",
                "최종 검수 실패 우회 시도",
                expected=2,
            )
            self.assertIn("실패 복구에는 rerun을 사용할 수 없습니다", bypass.stderr)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(1, len(state["cost_usage"]["premium_final_reviews"]))
            self.assertEqual("failed", state["cost_usage"]["premium_final_reviews"][0]["status"])

    def test_same_final_artifacts_cannot_be_reaudited_by_incrementing_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "fingerprint-budget", "--root", str(root)
            )
            state_file = Path(result.stdout.strip())
            source_map = self.write_source_map(state_file)
            artifacts: dict[str, Path] = {}
            for role in ("source_mapper", "writer", "layout_builder", "final_reviewer"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                artifacts[role] = artifact
                self.run_cli("start", str(state_file), "--role", role)
                if role == "final_reviewer":
                    self.complete_final_review(state_file, artifact, source_map, "faithful")
                else:
                    self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            direct = self.run_cli(
                "rerun",
                str(state_file),
                "--role",
                "final_reviewer",
                "--change-kind",
                "user_request",
                "--reason",
                "같은 완성본 다시 검수",
                expected=2,
            )
            self.assertIn("직접 재실행할 수 없습니다", direct.stderr)

            self.run_cli(
                "rerun",
                str(state_file),
                "--role",
                "writer",
                "--change-kind",
                "user_request",
                "--reason",
                "같은 초안으로 편집 요청 확인",
            )
            self.run_cli("start", str(state_file), "--role", "writer")
            self.run_cli(
                "complete",
                str(state_file),
                "--role",
                "writer",
                "--artifact",
                str(artifacts["writer"]),
            )
            duplicate = self.run_cli(
                "start", str(state_file), "--role", "final_reviewer", expected=2
            )
            self.assertIn("동일한 source map과 완성본", duplicate.stderr)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(1, len(state["cost_usage"]["premium_final_reviews"]))

    def test_layout_and_invalid_categories_cannot_use_sol_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "escalation-scope", "--root", str(root)
            )
            state_file = Path(result.stdout.strip())
            packet = state_file.parent / "review_packet.json"
            packet.write_text(
                json.dumps(
                    {"model_input": True, "kind": "review_packet", "target": "한 페이지"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            layout = self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "layout_builder",
                "--packet",
                str(packet),
                "--category",
                "final_blocker",
                "--reason",
                "레이아웃 문제",
                expected=2,
            )
            self.assertIn("승격을 지원하지 않는 역할", layout.stderr)

            self.run_cli("start", str(state_file), "--role", "source_mapper")
            invalid = self.run_cli(
                "escalate",
                str(state_file),
                "--role",
                "source_mapper",
                "--packet",
                str(packet),
                "--category",
                "logic_gap",
                "--reason",
                "분류 불일치",
                expected=2,
            )
            self.assertIn("허용되지 않은 승격 분류", invalid.stderr)

    def test_rerun_only_invalidates_selected_role_and_descendants(self) -> None:
        # 조판 취향만 바뀌면 매핑·집필을 반복하지 않고 조판 이후만 다시 실행한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli("init", str(inputs), "--lecture-id", "rerun", "--root", str(root))
            state_file = Path(result.stdout.strip())

            source_map = self.write_source_map(state_file)
            for role in ("source_mapper", "writer", "layout_builder", "final_reviewer"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                if role == "final_reviewer":
                    self.complete_final_review(state_file, artifact, source_map, "faithful")
                else:
                    self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            self.run_cli(
                "rerun",
                str(state_file),
                "--role",
                "layout_builder",
                "--reason",
                "교안 순서형 조판으로 변경",
                "--change-kind",
                "output_contract",
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("passed", state["roles"]["source_mapper"]["status"])
            self.assertEqual("passed", state["roles"]["writer"]["status"])
            self.assertEqual("ready", state["roles"]["layout_builder"]["status"])
            # 조판은 내용을 바꾸지 않으므로 집필 초안을 검수한 결과는 유지된다.
            self.assertEqual("passed", state["roles"]["final_reviewer"]["status"])

            self.run_cli("start", str(state_file), "--role", "layout_builder")
            self.run_cli("complete", str(state_file), "--role", "layout_builder", "--artifact", str(state_file.parent / "layout_builder.txt"))
            self.run_cli(
                "rerun", str(state_file), "--role", "writer", "--reason", "사용자 편집 요청", "--change-kind", "user_request"
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("ready", state["roles"]["writer"]["status"])
            self.assertEqual("blocked", state["roles"]["layout_builder"]["status"])
            self.assertEqual("blocked", state["roles"]["final_reviewer"]["status"])

    def test_maintainer_is_optional_and_can_be_activated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli("init", str(inputs), "--lecture-id", "delivery", "--root", str(root))
            state_file = Path(result.stdout.strip())
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertFalse(state["roles"]["maintainer"]["active"])

            self.run_cli(
                "activate",
                str(state_file),
                "--role",
                "maintainer",
                "--reason",
                "복수 파일 패키징 필요",
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(state["roles"]["maintainer"]["active"])
            self.assertEqual(["layout_builder", "final_reviewer"], state["roles"]["maintainer"]["dependencies"])

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

    def test_init_requires_note_mode_and_runtime_when_undetectable(self) -> None:
        # 제작 모드는 사용자가 골라야 하므로 기본값이 없고, 런타임은 감지 실패 시 명시가 필요하다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            no_mode = self.run_raw(
                "init", str(inputs), "--lecture-id", "no-mode", "--root", str(root), "--runtime", "codex",
                expected=2,
            )
            self.assertIn("--note-mode", no_mode.stderr)
            no_runtime = self.run_raw(
                "init", str(inputs), "--lecture-id", "no-runtime", "--root", str(root), "--note-mode", "faithful",
                expected=2,
                env=runtime_neutral_env(),
            )
            self.assertIn("--runtime", no_runtime.stderr)
            self.assertFalse((root / "workspace" / "no-runtime").exists())

            detected = self.run_raw(
                "init", str(inputs), "--lecture-id", "detected", "--root", str(root), "--note-mode", "deep",
                env=runtime_neutral_env(CLAUDECODE="1"),
            )
            state = json.loads(Path(detected.stdout.strip()).read_text(encoding="utf-8"))
            self.assertEqual("claude", state["runtime"])
            self.assertEqual("claude-sonnet-5", state["runtime_model_table"]["economy_high"]["model"])
            self.assertEqual("claude-opus-5", state["runtime_model_table"]["quality_xhigh"]["model"])

    def test_claude_runtime_resolves_models_and_verifies_against_snapshot(self) -> None:
        # 같은 프로필이 Claude에서는 Sonnet/Opus로 해석돼 기록되고, 검증도 그 스냅샷을 기준으로 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "claude-deep", "--root", str(root),
                "--runtime", "claude", "--note-mode", "deep",
            )
            state_file = Path(result.stdout.strip())
            next_payload = json.loads(self.run_cli("next", str(state_file)).stdout)
            self.assertEqual("claude", next_payload["runtime"])
            mapper = next(item for item in next_payload["ready"] if item["role"] == "source_mapper")
            self.assertEqual({"model": "claude-sonnet-5", "effort": "high"}, {k: mapper["resolved_profile"][k] for k in ("model", "effort")})

            source_map = self.write_source_map(state_file)
            for role in ("source_mapper", "writer", "pedagogy_editor", "layout_builder"):
                artifact = source_map if role == "source_mapper" else state_file.parent / f"{role}.txt"
                if role != "source_mapper":
                    artifact.write_text(role, encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", role)
                self.run_cli("complete", str(state_file), "--role", role, "--artifact", str(artifact))

            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            call = state["cost_usage"]["premium_final_reviews"][0]
            self.assertEqual({"runtime": "claude", "model": "claude-opus-5", "reasoning_effort": "xhigh", "agent": "deep_note_reviewer"}, {k: call[k] for k in ("runtime", "model", "reasoning_effort", "agent")})
            artifact = state_file.parent / "final_review.md"
            artifact.write_text("검수", encoding="utf-8")
            self.complete_final_review(state_file, artifact, source_map, "deep")
            self.run_cli("verify", str(state_file))

            # Codex 모델명을 심으면 스냅샷과 어긋나므로 변조로 잡혀야 한다.
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["cost_usage"]["premium_final_reviews"][0]["model"] = "gpt-5.6-sol"
            state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tampered = self.run_cli("verify", str(state_file), expected=1)
            self.assertIn("변조", tampered.stdout)

    def test_claude_escalation_uses_claude_quality_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli(
                "init", str(inputs), "--lecture-id", "claude-escalate", "--root", str(root), "--runtime", "claude",
            )
            state_file = Path(result.stdout.strip())
            packet = state_file.parent / "packet.json"
            packet.write_text(
                json.dumps({"model_input": True, "kind": "review_packet", "target": {"claim": "수치 불명확"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.run_cli("start", str(state_file), "--role", "source_mapper")
            dispatch = self.run_cli(
                "escalate", str(state_file), "--role", "source_mapper", "--packet", str(packet),
                "--category", "number", "--reason", "핵심 수치 충돌",
            )
            payload = json.loads(dispatch.stdout)
            self.assertEqual("claude", payload["execution"]["runtime"])
            self.assertEqual("claude-opus-5", payload["execution"]["model"])
            self.assertEqual("high", payload["execution"]["reasoning_effort"])
            self.run_cli("status", str(state_file))

    def test_schema_v2_state_gains_runtime_without_duplicating_premium_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            result = self.run_cli("init", str(inputs), "--lecture-id", "v2-state", "--root", str(root))
            state_file = Path(result.stdout.strip())
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["schema_version"] = 2
            state.pop("runtime")
            state.pop("runtime_model_table")
            state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            self.run_cli("refresh-inputs", str(state_file))
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(3, migrated["schema_version"])
            self.assertEqual("codex", migrated["runtime"])
            self.assertEqual([], migrated["cost_usage"]["premium_final_reviews"])
            self.run_cli("verify", str(state_file), expected=1)  # 활성 역할 미통과는 그대로 실패

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


    def test_output_format_defaults_to_md_for_faithful_and_pdf_for_deep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "handout.pdf").write_bytes(b"test-pdf")
            faithful = Path(self.run_cli("init", str(inputs), "--lecture-id", "fmt-faithful", "--root", str(root)).stdout.strip())
            self.assertEqual("md", json.loads(faithful.read_text(encoding="utf-8"))["output_format"])
            deep = Path(
                self.run_cli("init", str(inputs), "--lecture-id", "fmt-deep", "--root", str(root), "--note-mode", "deep").stdout.strip()
            )
            self.assertEqual("pdf", json.loads(deep.read_text(encoding="utf-8"))["output_format"])
            explicit = Path(
                self.run_cli("init", str(inputs), "--lecture-id", "fmt-explicit", "--root", str(root), "--output-format", "pdf").stdout.strip()
            )
            self.assertEqual("pdf", json.loads(explicit.read_text(encoding="utf-8"))["output_format"])

    def _run_until_writer(self, lecture_id: str, root: Path) -> tuple[Path, Path, Path]:
        inputs = root / "input"
        inputs.mkdir()
        (inputs / "handout.pdf").write_bytes(b"test-pdf")
        result = self.run_cli("init", str(inputs), "--lecture-id", lecture_id, "--root", str(root))
        state_file = Path(result.stdout.strip())
        source_map = self.write_source_map(state_file)
        self.run_cli("start", str(state_file), "--role", "source_mapper")
        self.run_cli("complete", str(state_file), "--role", "source_mapper", "--artifact", str(source_map))
        draft = state_file.parent / "note_draft.md"
        draft.write_text("# 초안\n\n첫 번째 판", encoding="utf-8")
        self.run_cli("start", str(state_file), "--role", "writer")
        self.run_cli("complete", str(state_file), "--role", "writer", "--artifact", str(draft))
        return state_file, source_map, draft

    def _complete_layout(self, state_file: Path) -> Path:
        pdf = state_file.parent / "note.pdf"
        pdf.write_bytes(b"%PDF-1.4 layout")
        self.run_cli("start", str(state_file), "--role", "layout_builder")
        self.run_cli("complete", str(state_file), "--role", "layout_builder", "--artifact", str(pdf))
        return pdf

    def test_final_review_runs_next_to_layout_and_patched_artifacts_are_rerecorded(self) -> None:
        # 조판은 결정적이라 최종 검수가 조판을 기다리지 않는다. 검수가 같은 호출 안에서
        # 초안을 국소 수정했으면 --patched 로 새 해시를 기록해야 verify 가 변조로 보지 않는다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file, source_map, draft = self._run_until_writer("parallel-review", root)
            payload = json.loads(self.run_cli("next", str(state_file)).stdout)
            ready = {item["role"] for item in payload["ready"]}
            self.assertEqual({"layout_builder", "final_reviewer"}, ready)
            self.assertEqual("python", payload["ready"][0]["execution"]["executor"] if payload["ready"][0]["role"] == "layout_builder" else "python")

            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            draft.write_text("# 초안\n\n첫 번째 판 (검수 호출 안에서 국소 수정)", encoding="utf-8")
            report = state_file.parent / "final_review.md"
            report.write_text("통과", encoding="utf-8")
            stray = state_file.parent / "stray.txt"
            stray.write_text("x", encoding="utf-8")
            coverage = self.write_coverage(state_file, "faithful")
            rejected = self.run_cli(
                "complete", str(state_file), "--role", "final_reviewer",
                "--artifact", str(report), "--source-map", str(source_map), "--coverage-report", str(coverage),
                "--patched", str(stray), expected=2,
            )
            self.assertIn("기록된 산출물이어야", rejected.stderr)
            self.run_cli(
                "complete", str(state_file), "--role", "final_reviewer",
                "--artifact", str(report), "--source-map", str(source_map), "--coverage-report", str(coverage),
                "--patched", str(draft),
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            recorded = state["roles"]["writer"]["artifacts"][0]
            self.assertEqual(hashlib.sha256(draft.read_bytes()).hexdigest(), recorded["sha256"])
            self.assertTrue(any(event["event"] == "review_patched" for event in state["events"]))

            self._complete_layout(state_file)
            self.run_raw("verify", str(state_file), "--check-inputs")

            misuse = self.run_cli(
                "start", str(state_file), "--role", "writer", expected=2
            )
            self.assertIn("시작할 수 없는 상태", misuse.stderr)

    def test_patched_accepts_layout_artifact_rebuilt_from_patched_draft(self) -> None:
        # 조판은 검수의 선행이 아니지만, 패치된 초안으로 다시 만든 조판 산출물도 --patched 로 재기록할 수 있어야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file, source_map, draft = self._run_until_writer("patched-layout", root)
            pdf = self._complete_layout(state_file)
            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            draft.write_text("# 초안\n\n국소 수정", encoding="utf-8")
            pdf.write_bytes(b"%PDF-1.4 rebuilt from patched draft")
            report = state_file.parent / "final_review.md"
            report.write_text("통과", encoding="utf-8")
            coverage = self.write_coverage(state_file, "faithful")
            self.run_cli(
                "complete", str(state_file), "--role", "final_reviewer",
                "--artifact", str(report), "--source-map", str(source_map), "--coverage-report", str(coverage),
                "--patched", str(draft), "--patched", str(pdf),
            )
            self.run_raw("verify", str(state_file), "--check-inputs")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(), state["roles"]["layout_builder"]["artifacts"][0]["sha256"])
            self.assertEqual(2, len(state["cost_usage"]["premium_final_reviews"][0]["patched_artifacts"]))

    def test_repair_reopens_writer_after_review_rejection_and_is_limited(self) -> None:
        # 검수 반려 → repair 로 집필을 다시 열고 새 cycle 에서 다시 검수한다. rerun 은 여전히 거부된다.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file, source_map, draft = self._run_until_writer("review-repair", root)
            pdf = self._complete_layout(state_file)
            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            report = state_file.parent / "final_review.md"
            report.write_text("수정 필요: 2장 도입 발언 누락", encoding="utf-8")

            not_upstream = self.run_cli(
                "repair", str(state_file), "--reopen", "layout_builder", "--reason", "조판", expected=2
            )
            self.assertIn("선행 역할이 아닙니다", not_upstream.stderr)

            self.run_cli(
                "repair", str(state_file), "--reopen", "writer",
                "--reason", "2장 도입 발언 누락 — 검수 반려", "--findings", str(report),
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(2, state["review_cycle"])
            self.assertEqual("ready", state["roles"]["writer"]["status"])
            self.assertEqual("blocked", state["roles"]["final_reviewer"]["status"])
            self.assertEqual(0, state["roles"]["final_reviewer"]["attempts"])
            self.assertEqual("blocked", state["roles"]["layout_builder"]["status"])
            self.assertEqual("failed", state["cost_usage"]["premium_final_reviews"][0]["status"])
            repairs = state["cost_usage"]["review_repairs"]
            self.assertEqual(1, len(repairs))
            self.assertEqual("writer", repairs[0]["reopened_role"])
            self.assertEqual(report.name, Path(repairs[0]["findings"]["path"]).name)

            blocked = self.run_cli(
                "rerun", str(state_file), "--role", "writer", "--change-kind", "user_request", "--reason", "x", expected=2
            )
            self.assertIn("통과한 역할만", blocked.stderr)

            draft.write_text("# 초안\n\n두 번째 판 (도입 발언 복원)", encoding="utf-8")
            self.run_cli("start", str(state_file), "--role", "writer")
            self.run_cli("complete", str(state_file), "--role", "writer", "--artifact", str(draft))
            self.run_cli("start", str(state_file), "--role", "final_reviewer")
            self.complete_final_review(state_file, report, source_map, "faithful")
            self._complete_layout(state_file)
            self.run_raw("verify", str(state_file), "--check-inputs")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(
                ["failed", "passed"],
                [item["status"] for item in state["cost_usage"]["premium_final_reviews"]],
            )

            done = self.run_cli(
                "repair", str(state_file), "--reopen", "writer", "--reason", "통과한 검수", expected=2
            )
            self.assertIn("실행 중이거나 실패한 상태", done.stderr)

            # 두 번째 반려 수정까지는 허용, 세 번째는 거부한다.
            self.run_cli(
                "rerun", str(state_file), "--role", "writer", "--change-kind", "user_request", "--reason", "편집 요청"
            )
            for round_index in (2, 3):
                draft.write_text(f"# 초안\n\n{round_index}번째 판", encoding="utf-8")
                self.run_cli("start", str(state_file), "--role", "writer")
                self.run_cli("complete", str(state_file), "--role", "writer", "--artifact", str(draft))
                self.run_cli("start", str(state_file), "--role", "final_reviewer")
                self.run_cli("fail", str(state_file), "--role", "final_reviewer", "--reason", "반려")
                expected = 0 if round_index == 2 else 2
                result = self.run_cli(
                    "repair", str(state_file), "--reopen", "writer", "--reason", f"{round_index}차 반려", expected=expected
                )
                if round_index == 3:
                    self.assertIn("이미 사용했습니다", result.stderr)


if __name__ == "__main__":
    unittest.main()
