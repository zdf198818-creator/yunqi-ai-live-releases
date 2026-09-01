from __future__ import annotations

import io
import re
import shutil
import wave
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from ailive.parser import SPECIAL_MARKER_PATTERN


_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def spoken_filename(text: str, *, max_length: int = 120) -> str:
    """Build a safe WAV filename stem from the text actually sent to TTS."""
    value = SPECIAL_MARKER_PATTERN.sub("", text)
    value = _ILLEGAL_FILENAME.sub("", value)
    value = _WHITESPACE.sub("", value).strip(" .")
    if not value:
        value = "未命名音频"
    if value.upper() in _WINDOWS_RESERVED:
        value = f"{value}_音频"
    return value[:max_length].rstrip(" .") or "未命名音频"


def unique_wav_path(directory: Path, text: str) -> Path:
    """Return 文案.wav, 文案-2.wav, ... without overwriting this run."""
    stem = spoken_filename(text)
    candidate = directory / f"{stem}.wav"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{suffix}.wav"
        suffix += 1
    return candidate


def write_audio_tokens_wav(tokens: Iterable[object], output_path: Path) -> None:
    """Merge model WAV segments and #pause# tokens into one PCM WAV file."""
    token_list = list(tokens)
    first_audio = next(
        (
            token
            for token in token_list
            if getattr(token, "kind", "") == "audio"
            and getattr(token, "wav_bytes", b"")
        ),
        None,
    )
    if first_audio is None:
        raise ValueError("合成结果中没有音频")

    with wave.open(io.BytesIO(first_audio.wav_bytes), "rb") as source:
        parameters = source.getparams()
    if parameters.comptype != "NONE":
        raise ValueError("只支持未压缩 WAV 音频")

    chunks: list[bytes] = []
    for token in token_list:
        if getattr(token, "kind", "") == "pause":
            duration_ms = max(0, int(getattr(token, "duration_ms", 0)))
            frame_count = round(parameters.framerate * duration_ms / 1000)
            chunks.append(bytes(frame_count * parameters.nchannels * parameters.sampwidth))
            continue
        wav_bytes = getattr(token, "wav_bytes", b"")
        if not wav_bytes:
            continue
        with wave.open(io.BytesIO(wav_bytes), "rb") as source:
            current = source.getparams()
            signature = (
                current.nchannels,
                current.sampwidth,
                current.framerate,
                current.comptype,
            )
            expected = (
                parameters.nchannels,
                parameters.sampwidth,
                parameters.framerate,
                parameters.comptype,
            )
            if signature != expected:
                raise ValueError("同一句中的音频格式不一致")
            chunks.append(source.readframes(source.getnframes()))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as target:
        target.setnchannels(parameters.nchannels)
        target.setsampwidth(parameters.sampwidth)
        target.setframerate(parameters.framerate)
        target.writeframes(b"".join(chunks))


def safe_folder_name(value: str) -> str:
    cleaned = _ILLEGAL_FILENAME.sub("", value).strip(" .")
    return cleaned[:80] or "未命名话术"


def prepare_output_batch(
    current_root: Path,
    history_root: Path,
    script_name: str,
    *,
    now: datetime | None = None,
) -> tuple[Path, Path | None]:
    """Archive the previous batch and create a clean current output folder."""
    timestamp = now or datetime.now()
    name = safe_folder_name(script_name)
    current = current_root / name
    archived: Path | None = None
    if current.exists():
        if any(current.iterdir()):
            history_root.mkdir(parents=True, exist_ok=True)
            base = history_root / f"{timestamp:%Y%m%d-%H%M%S}-{name}"
            archived = base
            suffix = 2
            while archived.exists():
                archived = history_root / f"{base.name}-{suffix}"
                suffix += 1
            shutil.move(str(current), str(archived))
        else:
            current.rmdir()
    current.mkdir(parents=True, exist_ok=True)
    return current, archived


def cleanup_history(
    history_root: Path,
    *,
    retention_days: int = 7,
    now: datetime | None = None,
) -> list[Path]:
    """Delete archived batches whose modification time is older than retention."""
    if not history_root.is_dir():
        return []
    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    removed: list[Path] = []
    for entry in history_root.iterdir():
        modified = datetime.fromtimestamp(entry.stat().st_mtime)
        if modified >= cutoff:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(entry)
    return removed
