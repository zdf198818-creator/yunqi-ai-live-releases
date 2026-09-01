from __future__ import annotations

import io
import os
import wave
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path

from ailive.client.audio import AudioToken
from ailive.client.app import MainWindow
from ailive.client.local_synthesis import (
    cleanup_history,
    prepare_output_batch,
    spoken_filename,
    unique_wav_path,
    write_audio_tokens_wav,
)


def _wav(duration_frames: int = 240) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24_000)
        target.writeframes(bytes(duration_frames * 2))
    return output.getvalue()


def test_spoken_filename_removes_markers_and_illegal_characters() -> None:
    assert spoken_filename("你好#500#世界[甲|乙]:？") == "你好世界？"


def test_unique_wav_path_adds_numeric_suffix(tmp_path: Path) -> None:
    (tmp_path / "同一句.wav").touch()
    assert unique_wav_path(tmp_path, "同一句").name == "同一句-2.wav"


def test_write_audio_tokens_wav_includes_pause(tmp_path: Path) -> None:
    output = tmp_path / "result.wav"
    write_audio_tokens_wav(
        [
            AudioToken(kind="audio", wav_bytes=_wav(240)),
            AudioToken(kind="pause", duration_ms=500),
            AudioToken(kind="audio", wav_bytes=_wav(240)),
        ],
        output,
    )
    with wave.open(str(output), "rb") as source:
        assert source.getframerate() == 24_000
        assert source.getnframes() == 240 + 12_000 + 240


def test_prepare_output_batch_archives_previous_run(tmp_path: Path) -> None:
    current_root = tmp_path / "current"
    history_root = tmp_path / "history"
    previous = current_root / "课程"
    previous.mkdir(parents=True)
    (previous / "旧.wav").touch()
    current, archived = prepare_output_batch(
        current_root,
        history_root,
        "课程",
        now=datetime(2026, 8, 27, 12, 30, 0),
    )
    assert current.is_dir()
    assert archived == history_root / "20260827-123000-课程"
    assert (archived / "旧.wav").is_file()


def test_cleanup_history_keeps_seven_days(tmp_path: Path) -> None:
    history = tmp_path / "history"
    old = history / "old"
    recent = history / "recent"
    old.mkdir(parents=True)
    recent.mkdir()
    now = datetime(2026, 8, 27, 12, 0, 0)
    old_time = (now - timedelta(days=8)).timestamp()
    recent_time = (now - timedelta(days=6)).timestamp()
    os.utime(old, (old_time, old_time))
    os.utime(recent, (recent_time, recent_time))
    removed = cleanup_history(history, now=now)
    assert old in removed
    assert not old.exists()
    assert recent.exists()


def test_failed_sentence_retries_until_third_attempt() -> None:
    submitted: list[str] = []
    recorded: list[str] = []
    line = SimpleNamespace(line_id="line-1")
    window = SimpleNamespace(
        local_synthesis_active=True,
        local_line_index=0,
        local_lines=[line],
        local_attempts={"line-1": 1},
        _log=lambda _message: None,
        _submit_current_local_line=lambda: submitted.append("retry"),
        _record_local_failure=lambda _line, message: recorded.append(message),
    )
    MainWindow._on_local_line_error(window, "line-1", "temporary")
    assert submitted == ["retry"]
    assert recorded == []

    window.local_attempts["line-1"] = 3
    MainWindow._on_local_line_error(window, "line-1", "final")
    assert recorded == ["final"]
