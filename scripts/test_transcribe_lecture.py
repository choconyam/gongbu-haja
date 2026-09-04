#!/usr/bin/env python3
"""외부 패키지 없이 transcribe_lecture.py의 모델 자동 선택 계획을 검증한다."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcribe_lecture as tl  # noqa: E402


# os.name을 "nt"로 바꾸면 Linux의 pathlib이 WindowsPath를 만들다 실패하고,
# os.add_dll_directory는 Windows에만 있다. DLL 검색 경로 자체가 Windows 전용 기능이다.
@unittest.skipUnless(sys.platform == "win32", "Windows 전용 NVIDIA DLL 검색 경로")
class NvidiaDllDiscoveryTests(unittest.TestCase):
    def test_discovers_all_installed_nvidia_bin_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_site = root / "system-site"
            user_site = root / "user-site"
            cublas_bin = system_site / "nvidia" / "cublas" / "bin"
            cudnn_bin = user_site / "nvidia" / "cudnn" / "bin"
            cublas_bin.mkdir(parents=True)
            cudnn_bin.mkdir(parents=True)
            (cublas_bin / "cublas64_12.dll").write_bytes(b"dll")
            (cudnn_bin / "cudnn64_9.dll").write_bytes(b"dll")

            with (
                patch.object(tl.os, "name", "nt"),
                patch.object(tl.site, "getsitepackages", return_value=[str(system_site)]),
                patch.object(tl.site, "getusersitepackages", return_value=str(user_site)),
            ):
                discovered = tl.discover_nvidia_dll_dirs()

            self.assertEqual((cublas_bin.resolve(), cudnn_bin.resolve()), discovered)

    def test_registration_keeps_handles_and_only_changes_process_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "nvidia" / "cublas" / "bin"
            second = Path(temporary) / "nvidia" / "cudnn" / "bin"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            handles = [object(), object()]

            with (
                patch.object(tl, "discover_nvidia_dll_dirs", return_value=(first, second)),
                patch.object(tl.os, "add_dll_directory", side_effect=handles),
                patch.object(tl, "_NVIDIA_DLL_DIR_HANDLES", []),
                patch.dict(tl.os.environ, {"PATH": "existing"}, clear=False),
            ):
                registered = tl.add_nvidia_dll_dirs()
                process_path = tl.os.environ["PATH"]
                kept_handles = list(tl._NVIDIA_DLL_DIR_HANDLES)

            self.assertEqual((first, second), registered)
            self.assertEqual(handles, kept_handles)
            self.assertEqual(
                [str(first), str(second), "existing"],
                process_path.split(tl.os.pathsep),
            )


class TranscriptionPlanTests(unittest.TestCase):
    # GPU 메모리 크기별로 가장 정확하면서 실제로 돌아가는 모델을 골라야 한다.
    def test_auto_tiers_by_vram(self) -> None:
        cases = (
            (12_282, "large-v3", "float16", "gpu_large-v3_float16"),
            (10_000, "large-v3", "float16", "gpu_large-v3_float16"),
            (8_192, "large-v3", "int8_float16", "gpu_large-v3_int8"),
            (4_096, "medium", "int8_float16", "gpu_medium_int8"),
            (2_048, "small", "int8_float16", "gpu_small_int8"),
        )
        for vram, model, compute, tier in cases:
            with self.subTest(vram=vram):
                plan = tl.build_transcription_plan("auto", "auto", vram)
                self.assertEqual(model, plan.model)
                self.assertEqual((model, "cuda", compute), plan.attempts[0])
                self.assertEqual(tier, plan.tier)
                self.assertEqual("auto", plan.selection)
                # 장치가 auto면 GPU 실패에 대비한 CPU 후보가 뒤에 있어야 한다.
                self.assertEqual(("small", "cpu", "int8"), plan.attempts[-1])

    def test_auto_without_gpu_uses_cpu_small(self) -> None:
        plan = tl.build_transcription_plan("auto", "auto", None)
        self.assertEqual("small", plan.model)
        self.assertEqual((("small", "cpu", "int8"),), plan.attempts)
        self.assertEqual("cpu_small_int8", plan.tier)

    def test_auto_with_tiny_vram_falls_back_to_cpu(self) -> None:
        plan = tl.build_transcription_plan("auto", "auto", 1_024)
        self.assertEqual((("small", "cpu", "int8"),), plan.attempts)

    def test_auto_with_forced_cpu_ignores_gpu(self) -> None:
        plan = tl.build_transcription_plan("auto", "cpu", 12_282)
        self.assertEqual((("small", "cpu", "int8"),), plan.attempts)
        self.assertEqual("cpu_small_int8", plan.tier)

    def test_auto_with_forced_cuda_and_unknown_vram_keeps_legacy_attempt(self) -> None:
        # GPU를 강제한 사용자는 CPU로 조용히 대체되기보다 CUDA 실패를 봐야 한다.
        plan = tl.build_transcription_plan("auto", "cuda", None)
        self.assertEqual((("large-v3", "cuda", "float16"),), plan.attempts)
        self.assertEqual("gpu_unknown", plan.tier)

    def test_auto_with_forced_cuda_uses_tier_without_cpu_fallback(self) -> None:
        plan = tl.build_transcription_plan("auto", "cuda", 8_192)
        self.assertEqual((("large-v3", "cuda", "int8_float16"),), plan.attempts)

    def test_auto_with_forced_cuda_and_tiny_vram_picks_smallest_gpu_tier(self) -> None:
        # 1GB GPU를 강제한 사용자에게 최대 모델을 배정해 OOM을 예약하면 안 된다.
        plan = tl.build_transcription_plan("auto", "cuda", 1_024)
        self.assertEqual((("small", "cuda", "int8_float16"),), plan.attempts)
        self.assertEqual("gpu_small_int8", plan.tier)
        self.assertNotEqual("gpu_unknown", plan.tier)

    def test_manual_model_keeps_legacy_behavior(self) -> None:
        # 모델을 직접 지정한 사용자는 자동 티어의 영향을 받지 않아야 한다.
        plan = tl.build_transcription_plan("medium", "auto", 12_282)
        self.assertEqual("manual", plan.selection)
        self.assertEqual(
            (("medium", "cuda", "float16"), ("medium", "cpu", "int8")), plan.attempts
        )
        cuda_only = tl.build_transcription_plan("large-v3", "cuda", None)
        self.assertEqual((("large-v3", "cuda", "float16"),), cuda_only.attempts)
        cpu_only = tl.build_transcription_plan("large-v3", "cpu", None)
        self.assertEqual((("large-v3", "cpu", "int8"),), cpu_only.attempts)

    def test_estimate_minutes_never_returns_zero(self) -> None:
        self.assertEqual(1, tl.estimate_minutes(30.0, 10.0))
        self.assertEqual(6, tl.estimate_minutes(3_600.0, 10.0))

    def test_describe_plan_mentions_selection_and_estimate(self) -> None:
        plan = tl.build_transcription_plan("auto", "auto", 12_282)
        lines = tl.describe_plan(plan, 3_600.0)
        self.assertTrue(any("자동 감지" in line for line in lines))
        self.assertTrue(any("예상" in line for line in lines))
        manual = tl.build_transcription_plan("small", "cpu", None)
        self.assertTrue(any("수동 설정" in line for line in tl.describe_plan(manual, None)))


class SubjectInferenceTests(unittest.TestCase):
    def test_recommended_naming_does_not_duplicate_lecture_type(self) -> None:
        # 권장 형식(날짜_과목_본강의) 파일명에서 '본강의'가 과목에 섞이면 안 된다.
        self.assertEqual("과목A", tl.infer_subject("2026-03-10_과목A_본강의"))
        args = Namespace(lecture_id=None, subject=None, lecture_date=None, lecture_type=None)
        identity = tl.resolve_identity(args, Path("2026-03-10_과목A_본강의.m4a"))
        self.assertEqual("2026-03-10_과목A_본강의", identity.lecture_id)
        self.assertEqual("과목A", identity.subject)


class OutputGuardTests(unittest.TestCase):
    def make_paths(self, root: Path) -> tl.OutputPaths:
        identity = tl.LectureIdentity("2026-03-10_테스트_본강의", "테스트", "2026-03-10", "본강의", True)
        _, paths = tl.build_output_paths(root, identity)
        return paths

    def write_parts(self, paths: tl.OutputPaths, names: list[str]) -> None:
        for name in names:
            target = Path(getattr(paths, name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("기존", encoding="utf-8")

    def test_complete_package_raises_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_paths(Path(temporary))
            self.write_parts(
                paths, ["raw_text", "raw_srt", "draft_markdown", "segments_json", "manifest_json"]
            )
            with self.assertRaises(FileExistsError):
                tl.ensure_outputs_available(paths, force=False)

    def test_partial_package_raises_runtime_error(self) -> None:
        # 중단 흔적은 '이미 전사됨'(종료 3)이 아니라 오류(종료 1)로 구분돼야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_paths(Path(temporary))
            self.write_parts(paths, ["raw_srt"])
            with self.assertRaises(RuntimeError):
                tl.ensure_outputs_available(paths, force=False)

    def test_force_allows_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_paths(Path(temporary))
            self.write_parts(paths, ["raw_srt"])
            tl.ensure_outputs_available(paths, force=True)


class ManifestTests(unittest.TestCase):
    RECORDS = [{"id": 1, "start": 0.0, "end": 1.0, "text": "안녕하세요"}]

    def write_manifest(self, root: Path, used_attempt: tuple[str, str, str]) -> dict:
        audio = root / "lecture.m4a"
        audio.write_bytes(b"fake-audio")
        identity = tl.LectureIdentity("2026-03-10_테스트_본강의", "테스트", "2026-03-10", "본강의", True)
        _, paths = tl.build_output_paths(root / "ws", identity)
        plan = tl.build_transcription_plan("auto", "auto", 12_282)
        info = SimpleNamespace(language="ko", language_probability=0.99, duration=10.0)
        args = Namespace(beam_size=5, no_vad=False, force=False)
        device_used = f"{used_attempt[1]}({used_attempt[2]})"
        tl.write_outputs(
            audio, identity, paths, self.RECORDS, info, device_used, "1.2.1", used_attempt, plan, args
        )
        return json.loads(Path(paths.manifest_json).read_text(encoding="utf-8"))

    def test_manifest_records_actual_tier_on_primary_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.write_manifest(Path(temporary), ("large-v3", "cuda", "float16"))
            self.assertEqual("large-v3", manifest["model"])
            self.assertEqual("gpu_large-v3_float16", manifest["model_tier"])
            self.assertFalse(manifest["model_fallback"])
            self.assertIn("model=large-v3", manifest["transcription_method"])

    def test_manifest_records_actual_tier_on_cpu_fallback(self) -> None:
        # CUDA 실패 후 CPU 폴백이 성공하면 계획 티어가 아니라 실제 티어를 남겨야 한다.
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.write_manifest(Path(temporary), ("small", "cpu", "int8"))
            self.assertEqual("small", manifest["model"])
            self.assertEqual("cpu_small_int8", manifest["model_tier"])
            self.assertTrue(manifest["model_fallback"])
            self.assertIn("model=small", manifest["transcription_method"])


class DurationProbeTests(unittest.TestCase):
    def test_probe_returns_real_seconds_not_microsecond_ticks(self) -> None:
        # PyAV 버전에 따라 av.time_base 표현이 달라도 초 단위를 돌려줘야 한다.
        try:
            import av  # type: ignore  # noqa: F401
        except ImportError:
            self.skipTest("av 미설치 환경")
        with tempfile.TemporaryDirectory() as temporary:
            wav_path = Path(temporary) / "tone.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16_000)
                handle.writeframes(b"\x00\x00" * 16_000 * 2)  # 정확히 2초
            duration = tl.probe_audio_duration_seconds(wav_path)
            self.assertIsNotNone(duration)
            self.assertAlmostEqual(2.0, duration, delta=0.2)


if __name__ == "__main__":
    unittest.main()
