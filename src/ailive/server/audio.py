from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


def pcm16_wav(samples: object, sample_rate: int) -> bytes:
    import numpy as np

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    array = np.clip(array, -1.0, 1.0)
    pcm = (array * 32767.0).astype("<i2").tobytes()

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def change_speed(
    wav_bytes: bytes,
    speed: float,
    ffmpeg: str = "ffmpeg",
    allow_mock_fallback: bool = False,
) -> bytes:
    """Apply deterministic pitch-preserving speed control with FFmpeg atempo."""
    if abs(speed - 1.0) < 0.001:
        return wav_bytes
    executable = shutil.which(ffmpeg)
    if executable is None:
        if allow_mock_fallback:
            return _mock_change_speed(wav_bytes, speed)
        raise RuntimeError("调节语速需要安装FFmpeg")

    with tempfile.TemporaryDirectory(prefix="ailive-speed-") as temporary:
        input_path = Path(temporary) / "input.wav"
        output_path = Path(temporary) / "output.wav"
        input_path.write_bytes(wav_bytes)
        result = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_path),
                "-filter:a",
                f"atempo={speed:.3f}",
                str(output_path),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        return output_path.read_bytes()


def _mock_change_speed(wav_bytes: bytes, speed: float) -> bytes:
    """Simple resampling fallback for local tone tests; production uses FFmpeg atempo."""
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        frames = source.getnframes()
        samples = np.frombuffer(source.readframes(frames), dtype="<i2").reshape(-1, channels)

    target_frames = max(1, int(len(samples) / speed))
    old_axis = np.linspace(0.0, 1.0, len(samples), endpoint=False)
    new_axis = np.linspace(0.0, 1.0, target_frames, endpoint=False)
    adjusted = np.column_stack(
        [np.interp(new_axis, old_axis, samples[:, channel]) for channel in range(channels)]
    ).astype("<i2")

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(adjusted.tobytes())
    return output.getvalue()
