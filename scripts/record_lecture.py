#!/usr/bin/env python3
"""Windows WASAPI loopback으로 온라인 강의의 시스템 오디오를 안전하게 녹음한다."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_ROOT = PROJECT_ROOT / "input"
DEFAULT_FRAMES_PER_BUFFER = 1_024


class RecordingDependencyError(RuntimeError):
    """녹음 백엔드가 없거나 WASAPI loopback 기능을 제공하지 않을 때 발생한다."""


class DeviceSelectionError(ValueError):
    """요청한 시스템 오디오 loopback 장치를 선택할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class RecordingResult:
    status: str
    capture_source: str
    lecture_id: str
    output: str
    device_index: int
    device_name: str
    sample_rate: int
    channels: int
    captured_frames: int
    duration_seconds: float
    bytes: int
    started_at: str
    finished_at: str


def load_pyaudio_backend() -> Any:
    """PyAudioWPatch를 지연 로드해 설치 오류를 실행 시점에 명확히 알린다."""
    try:
        backend = importlib.import_module("pyaudiowpatch")
    except ImportError as exc:
        raise RecordingDependencyError(
            "PyAudioWPatch가 설치되지 않았습니다. 프로젝트 루트에서 "
            "`python -m pip install -r requirements-recording.txt`를 실행하십시오."
        ) from exc

    if not hasattr(backend, "paWASAPI") or not hasattr(backend, "PyAudio"):
        raise RecordingDependencyError(
            "설치된 오디오 패키지가 PyAudioWPatch WASAPI loopback 기능을 제공하지 않습니다. "
            "requirements-recording.txt의 PyAudioWPatch를 다시 설치하십시오."
        )
    return backend


def sanitize_lecture_id(value: str) -> str:
    """lecture_id를 Windows 파일명과 단일 폴더에 안전한 값으로 제한한다."""
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value.strip())
    cleaned = re.sub(r"[\s_]+", "_", cleaned).strip(" ._")
    cleaned = cleaned[:120]
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("lecture-id가 비어 있거나 안전한 폴더 이름으로 변환될 수 없습니다.")
    return cleaned


def build_default_output_path(
    input_root: Path,
    lecture_id: str,
    now: datetime | None = None,
) -> Path:
    """input/<lecture_id>/ 아래에 초 단위 시각을 포함한 WAV 경로를 만든다."""
    safe_id = sanitize_lecture_id(lecture_id)
    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S")
    root = input_root.expanduser().resolve()
    return root / safe_id / f"{safe_id}_{timestamp}.wav"


def ensure_output_available(path: Path) -> None:
    if path.suffix.lower() != ".wav":
        raise ValueError(f"출력 파일은 .wav 형식이어야 합니다: {path}")
    if path.exists():
        raise FileExistsError(
            "기존 녹음을 덮어쓰지 않기 위해 중단했습니다. "
            f"다른 출력 경로 또는 시각을 사용하십시오: {path}"
        )


def _device_index(info: dict[str, Any]) -> int:
    try:
        return int(info["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceSelectionError("오디오 장치 정보에 유효한 index가 없습니다.") from exc


def get_loopback_devices(audio: Any) -> list[dict[str, Any]]:
    """PyAudioWPatch가 노출한 WASAPI loopback 장치 원본 정보를 반환한다."""
    generator = getattr(audio, "get_loopback_device_info_generator", None)
    if generator is None:
        raise RecordingDependencyError(
            "현재 오디오 백엔드에 WASAPI loopback 장치 열거 기능이 없습니다. "
            "일반 PyAudio가 아니라 PyAudioWPatch가 필요합니다."
        )
    try:
        devices = [dict(item) for item in generator()]
    except OSError as exc:
        raise DeviceSelectionError(f"WASAPI loopback 장치를 조회하지 못했습니다: {exc}") from exc
    if not devices:
        raise DeviceSelectionError(
            "온라인 강의 시스템 오디오를 받을 WASAPI loopback 장치가 없습니다. "
            "Windows 출력 장치가 활성화되어 있는지 확인하십시오. 마이크 입력은 지원하지 않습니다."
        )
    return devices


def _device_summary(info: dict[str, Any], default_output_index: int | None = None) -> dict[str, Any]:
    index = _device_index(info)
    return {
        "index": index,
        "name": str(info.get("name", "알 수 없는 장치")),
        "channels": int(info.get("maxInputChannels", 0)),
        "sample_rate": int(float(info.get("defaultSampleRate", 0))),
        "is_default_output_loopback": bool(
            default_output_index is not None and index == default_output_index
        ),
    }


def get_default_output_info(audio: Any, backend: Any) -> dict[str, Any] | None:
    try:
        host = audio.get_host_api_info_by_type(backend.paWASAPI)
        index = int(host["defaultOutputDevice"])
        return dict(audio.get_device_info_by_index(index))
    except (KeyError, OSError, TypeError, ValueError):
        return None


def find_default_loopback(
    audio: Any,
    backend: Any,
    loopbacks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """PyAudioWPatch의 기본 loopback helper를 우선하고, 구버전에서는 이름으로 찾는다."""
    by_index = {_device_index(item): item for item in loopbacks}
    get_default = getattr(audio, "get_default_wasapi_loopback", None)
    if callable(get_default):
        try:
            candidate = dict(get_default())
            candidate_index = _device_index(candidate)
            if candidate_index in by_index:
                return by_index[candidate_index]
        except (DeviceSelectionError, OSError, TypeError, ValueError):
            pass

    default_output = get_default_output_info(audio, backend)
    if default_output is None:
        return None
    default_index = _device_index(default_output)
    if default_index in by_index:
        return by_index[default_index]
    default_name = str(default_output.get("name", "")).casefold()
    named_matches = [
        item
        for item in loopbacks
        if default_name and default_name in str(item.get("name", "")).casefold()
    ]
    return named_matches[0] if len(named_matches) == 1 else None


def list_loopback_device_summaries(audio: Any, backend: Any) -> list[dict[str, Any]]:
    loopbacks = get_loopback_devices(audio)
    default_loopback = find_default_loopback(audio, backend, loopbacks)
    default_loopback_index = (
        _device_index(default_loopback) if default_loopback is not None else None
    )
    return [_device_summary(item, default_loopback_index) for item in loopbacks]


def select_loopback_device(
    audio: Any,
    backend: Any,
    requested_index: int | None = None,
) -> dict[str, Any]:
    """명시된 loopback 장치 또는 Windows 기본 출력에 대응하는 장치를 선택한다."""
    loopbacks = get_loopback_devices(audio)
    by_index = {_device_index(item): item for item in loopbacks}

    if requested_index is not None:
        if requested_index not in by_index:
            available = ", ".join(str(index) for index in sorted(by_index))
            raise DeviceSelectionError(
                f"장치 {requested_index}는 WASAPI loopback 장치가 아닙니다. "
                f"사용 가능한 index: {available}"
            )
        return by_index[requested_index]

    default_loopback = find_default_loopback(audio, backend, loopbacks)
    if default_loopback is not None:
        return default_loopback

    if len(loopbacks) == 1:
        return loopbacks[0]
    raise DeviceSelectionError(
        "온라인 강의가 재생되는 Windows 기본 출력의 loopback 장치를 자동 선택하지 못했습니다. "
        "--list-devices로 확인한 뒤 --device-index를 지정하십시오."
    )


def _close_stream(stream: Any) -> None:
    if stream is None:
        return
    try:
        is_stopped = getattr(stream, "is_stopped", None)
        if is_stopped is None or not is_stopped():
            stream.stop_stream()
    finally:
        stream.close()


def capture_to_wav(
    audio: Any,
    backend: Any,
    device: dict[str, Any],
    target: Path,
    lecture_id: str,
    duration: float | None = None,
    frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
    now_factory: Callable[[], datetime] | None = None,
) -> RecordingResult:
    """loopback PCM을 임시 WAV에 기록하고 정상 종료 뒤 최종 경로로 이동한다."""
    ensure_output_available(target)
    if duration is not None and duration <= 0:
        raise ValueError("duration은 0보다 큰 초 단위 값이어야 합니다.")
    if frames_per_buffer < 1:
        raise ValueError("frames-per-buffer는 1 이상이어야 합니다.")

    device_index = _device_index(device)
    device_name = str(device.get("name", f"device-{device_index}"))
    channels = int(device.get("maxInputChannels", 0))
    sample_rate = int(float(device.get("defaultSampleRate", 0)))
    if channels < 1 or sample_rate < 1:
        raise DeviceSelectionError(
            f"장치의 채널 수 또는 표본화율이 유효하지 않습니다: {device_name}"
        )

    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_output_available(target)
    clock = now_factory or (lambda: datetime.now().astimezone())
    started_at = clock()
    interrupted = False
    captured_frames = 0
    stream: Any = None

    temporary = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=target.parent,
        prefix=target.name + ".",
        suffix=".part.wav",
    )
    temp_path = Path(temporary.name)
    temporary.close()

    try:
        sample_format = backend.paInt16
        sample_width = int(audio.get_sample_size(sample_format))
        stream = audio.open(
            format=sample_format,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=frames_per_buffer,
        )
        frame_limit = math.ceil(duration * sample_rate) if duration is not None else None
        with wave.open(str(temp_path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(sample_width)
            output.setframerate(sample_rate)
            try:
                while frame_limit is None or captured_frames < frame_limit:
                    requested = frames_per_buffer
                    if frame_limit is not None:
                        requested = min(requested, frame_limit - captured_frames)
                    data = stream.read(requested, exception_on_overflow=False)
                    output.writeframesraw(data)
                    captured_frames += requested
            except KeyboardInterrupt:
                interrupted = True
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        try:
            _close_stream(stream)
        except Exception:
            if not interrupted:
                temp_path.unlink(missing_ok=True)
                raise

    # os.rename은 Windows의 동일 볼륨에서 원자적이며 대상이 생겼다면 덮어쓰지 않고 실패한다.
    ensure_output_available(target)
    try:
        os.rename(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    finished_at = clock()
    return RecordingResult(
        status="interrupted_saved" if interrupted else "completed",
        capture_source="online_lecture_system_audio",
        lecture_id=lecture_id,
        output=str(target),
        device_index=device_index,
        device_name=device_name,
        sample_rate=sample_rate,
        channels=channels,
        captured_frames=captured_frames,
        duration_seconds=round(captured_frames / sample_rate, 3),
        bytes=target.stat().st_size,
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Windows WASAPI loopback으로 온라인 강의 재생 소리만 WAV에 녹음합니다. "
            "마이크와 대면 수업 녹음은 지원하지 않습니다."
        )
    )
    parser.add_argument("--lecture-id", help="녹음할 강의 식별자(녹음 시 필수)")
    parser.add_argument(
        "--duration",
        type=float,
        help="녹음할 시간(초). 생략하면 Ctrl+C를 누를 때까지 녹음",
    )
    parser.add_argument(
        "--device-index",
        type=int,
        help="온라인 강의가 재생되는 WASAPI loopback 출력 장치 index(마이크 index 불가)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="온라인 강의 시스템 오디오용 loopback 장치만 JSON으로 표시",
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output", type=Path, help="기본 input/<lecture_id>/... 대신 사용할 .wav 경로")
    parser.add_argument("--frames-per-buffer", type=int, default=DEFAULT_FRAMES_PER_BUFFER)
    return parser.parse_args(argv)


def _terminate_audio(audio: Any) -> None:
    if audio is not None:
        audio.terminate()


def main(
    argv: Sequence[str] | None = None,
    backend_loader: Callable[[], Any] = load_pyaudio_backend,
    now_factory: Callable[[], datetime] | None = None,
) -> int:
    args = parse_args(argv)
    if args.duration is not None and args.duration <= 0:
        print("[오류] --duration은 0보다 커야 합니다.", file=sys.stderr)
        return 2
    if args.device_index is not None and args.device_index < 0:
        print("[오류] --device-index는 0 이상이어야 합니다.", file=sys.stderr)
        return 2
    if args.frames_per_buffer < 1:
        print("[오류] --frames-per-buffer는 1 이상이어야 합니다.", file=sys.stderr)
        return 2
    if not args.list_devices and not args.lecture_id:
        print("[오류] 녹음하려면 --lecture-id가 필요합니다.", file=sys.stderr)
        return 2

    audio: Any = None
    try:
        backend = backend_loader()
        audio = backend.PyAudio()
        if args.list_devices:
            payload = {
                "status": "ok",
                "capture_source": "online_lecture_system_audio",
                "backend": "PyAudioWPatch WASAPI loopback",
                "devices": list_loopback_device_summaries(audio, backend),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        lecture_id = sanitize_lecture_id(args.lecture_id)
        started = now_factory() if now_factory is not None else datetime.now().astimezone()
        target = (
            args.output.expanduser().resolve()
            if args.output is not None
            else build_default_output_path(args.input_root, lecture_id, started)
        )
        ensure_output_available(target)
        device = select_loopback_device(audio, backend, args.device_index)
        print(
            f"[온라인 강의 시스템 오디오 녹음 중] "
            f"{device.get('name', '알 수 없는 장치')} → {target}",
            file=sys.stderr,
        )
        if args.duration is None:
            print("[안내] 녹음을 끝내고 저장하려면 Ctrl+C를 누르십시오.", file=sys.stderr)
        result = capture_to_wav(
            audio=audio,
            backend=backend,
            device=device,
            target=target,
            lecture_id=lecture_id,
            duration=args.duration,
            frames_per_buffer=args.frames_per_buffer,
            now_factory=now_factory,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        # 무기한 녹음에서는 Ctrl+C가 문서를 닫는 정상 조작이다. 저장에 성공했으면 성공 코드로 끝낸다.
        return 0
    except (RecordingDependencyError, DeviceSelectionError, FileExistsError, OSError, ValueError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            _terminate_audio(audio)
        except Exception as exc:
            print(f"[경고] 오디오 백엔드를 종료하는 중 오류가 발생했습니다: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
