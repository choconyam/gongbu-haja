#!/usr/bin/env python3
"""실제 오디오 하드웨어 없이 record_lecture.py의 안전 경로를 검증한다."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import record_lecture as rl  # noqa: E402


class FakeBackend:
    paWASAPI = 13
    paInt16 = 8


class FakeStream:
    def __init__(self, interrupt_after: int | None = None) -> None:
        self.read_count = 0
        self.interrupt_after = interrupt_after
        self.stopped = False
        self.closed = False

    def read(self, frames: int, exception_on_overflow: bool = False) -> bytes:
        del exception_on_overflow
        if self.interrupt_after is not None and self.read_count >= self.interrupt_after:
            raise KeyboardInterrupt
        self.read_count += 1
        return b"\x00\x00" * frames * 2

    def is_stopped(self) -> bool:
        return self.stopped

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakeAudio:
    def __init__(self, devices: list[dict], default_output_index: int = 1, stream: FakeStream | None = None) -> None:
        self.devices = {int(item["index"]): item for item in devices}
        self.default_output_index = default_output_index
        self.stream = stream or FakeStream()
        self.open_kwargs: dict = {}
        self.terminated = False

    def get_loopback_device_info_generator(self):
        return (item for item in self.devices.values() if item.get("isLoopbackDevice"))

    def get_host_api_info_by_type(self, host_type: int) -> dict:
        self.last_host_type = host_type
        return {"defaultOutputDevice": self.default_output_index}

    def get_default_wasapi_loopback(self) -> dict:
        default_name = self.devices[self.default_output_index]["name"].casefold()
        matches = [
            item
            for item in self.devices.values()
            if item.get("isLoopbackDevice")
            and default_name in str(item.get("name", "")).casefold()
        ]
        if len(matches) != 1:
            raise OSError("no default loopback")
        return matches[0]

    def get_device_info_by_index(self, index: int) -> dict:
        return self.devices[index]

    def get_sample_size(self, sample_format: int) -> int:
        self.last_sample_format = sample_format
        return 2

    def open(self, **kwargs):
        self.open_kwargs = kwargs
        return self.stream

    def terminate(self) -> None:
        self.terminated = True


def devices() -> list[dict]:
    return [
        {
            "index": 1,
            "name": "Speakers (USB Audio)",
            "maxInputChannels": 0,
            "defaultSampleRate": 48_000.0,
            "isLoopbackDevice": False,
        },
        {
            "index": 5,
            "name": "Speakers (USB Audio) [Loopback]",
            "maxInputChannels": 2,
            "defaultSampleRate": 48_000.0,
            "isLoopbackDevice": True,
        },
        {
            "index": 7,
            "name": "Monitor HDMI [Loopback]",
            "maxInputChannels": 2,
            "defaultSampleRate": 44_100.0,
            "isLoopbackDevice": True,
        },
    ]


class PathTests(unittest.TestCase):
    def test_default_path_is_timestamped_under_sanitized_lecture_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 9, 2, 13, 14, 15, tzinfo=timezone.utc)
            output = rl.build_default_output_path(root, "../세계사 3주차", now)
            self.assertEqual(root.resolve(), output.parents[1])
            self.assertEqual("세계사_3주차", output.parent.name)
            self.assertEqual("세계사_3주차_20260902_131415.wav", output.name)

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.wav"
            output.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                rl.ensure_output_available(output)
            with self.assertRaises(FileExistsError):
                rl.capture_to_wav(
                    FakeAudio(devices()),
                    FakeBackend,
                    devices()[1],
                    output,
                    "lecture-existing",
                    duration=0.01,
                )
            self.assertEqual(b"original", output.read_bytes())


class DeviceSelectionTests(unittest.TestCase):
    def test_default_output_name_selects_matching_loopback(self) -> None:
        audio = FakeAudio(devices())
        selected = rl.select_loopback_device(audio, FakeBackend)
        self.assertEqual(5, selected["index"])

    def test_explicit_loopback_index_is_selected(self) -> None:
        audio = FakeAudio(devices())
        selected = rl.select_loopback_device(audio, FakeBackend, requested_index=7)
        self.assertEqual("Monitor HDMI [Loopback]", selected["name"])

    def test_non_loopback_index_is_rejected(self) -> None:
        audio = FakeAudio(devices())
        with self.assertRaises(rl.DeviceSelectionError):
            rl.select_loopback_device(audio, FakeBackend, requested_index=1)

    def test_pyaudiowpatch_default_loopback_helper_has_priority(self) -> None:
        audio = FakeAudio(devices())
        audio.get_default_wasapi_loopback = lambda: audio.devices[7]
        selected = rl.select_loopback_device(audio, FakeBackend)
        self.assertEqual(7, selected["index"])


class CaptureTests(unittest.TestCase):
    def test_duration_capture_writes_valid_wav_and_removes_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "lecture.wav"
            audio = FakeAudio(devices())
            device = devices()[1]
            times = iter(
                [
                    datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
                    datetime(2026, 9, 2, 9, 1, tzinfo=timezone.utc),
                ]
            )
            result = rl.capture_to_wav(
                audio,
                FakeBackend,
                device,
                output,
                "lecture-1",
                duration=0.02,
                frames_per_buffer=512,
                now_factory=lambda: next(times),
            )
            self.assertEqual("completed", result.status)
            self.assertTrue(output.is_file())
            self.assertEqual([], list(output.parent.glob("*.part.wav")))
            with wave.open(str(output), "rb") as recorded:
                self.assertEqual(2, recorded.getnchannels())
                self.assertEqual(48_000, recorded.getframerate())
                self.assertEqual(960, recorded.getnframes())
            self.assertEqual(5, audio.open_kwargs["input_device_index"])
            self.assertFalse(audio.open_kwargs.get("output", False))

    def test_ctrl_c_finalizes_and_atomically_saves_partial_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "interrupted.wav"
            stream = FakeStream(interrupt_after=1)
            audio = FakeAudio(devices(), stream=stream)
            result = rl.capture_to_wav(
                audio,
                FakeBackend,
                devices()[1],
                output,
                "lecture-2",
                duration=None,
                frames_per_buffer=256,
            )
            self.assertEqual("interrupted_saved", result.status)
            self.assertTrue(output.is_file())
            self.assertTrue(stream.stopped)
            self.assertTrue(stream.closed)
            self.assertEqual([], list(output.parent.glob("*.part.wav")))
            with wave.open(str(output), "rb") as recorded:
                self.assertEqual(256, recorded.getnframes())


class DependencyTests(unittest.TestCase):
    def test_missing_dependency_has_install_command(self) -> None:
        original = rl.importlib.import_module

        def missing(name: str):
            raise ImportError(name)

        try:
            rl.importlib.import_module = missing
            with self.assertRaisesRegex(rl.RecordingDependencyError, "requirements-recording.txt"):
                rl.load_pyaudio_backend()
        finally:
            rl.importlib.import_module = original


class CliTests(unittest.TestCase):
    def test_recording_requires_lecture_id_without_loading_backend(self) -> None:
        loaded = False

        def loader():
            nonlocal loaded
            loaded = True
            return FakeBackend

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = rl.main([], backend_loader=loader)
        self.assertEqual(2, code)
        self.assertFalse(loaded)
        self.assertIn("--lecture-id", stderr.getvalue())

    def test_list_devices_needs_no_hardware_or_lecture_id_and_omits_microphone(self) -> None:
        audio = FakeAudio(devices())

        class Backend(FakeBackend):
            @staticmethod
            def PyAudio():
                return audio

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = rl.main(["--list-devices"], backend_loader=lambda: Backend)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("online_lecture_system_audio", payload["capture_source"])
        self.assertEqual([5, 7], [item["index"] for item in payload["devices"]])
        self.assertTrue(audio.terminated)

    def test_ctrl_c_is_success_when_partial_recording_was_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ctrl-c.wav"
            audio = FakeAudio(devices(), stream=FakeStream(interrupt_after=1))

            class Backend(FakeBackend):
                @staticmethod
                def PyAudio():
                    return audio

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = rl.main(
                    ["--lecture-id", "online-lecture", "--output", str(output)],
                    backend_loader=lambda: Backend,
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, code)
            self.assertEqual("interrupted_saved", payload["status"])
            self.assertTrue(output.is_file())
            self.assertTrue(audio.terminated)


if __name__ == "__main__":
    unittest.main()
