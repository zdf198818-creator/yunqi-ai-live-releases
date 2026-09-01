from pathlib import Path
from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_nested_voice_metadata_uses_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "参考音频"
    audio = root / "音色甲" / "参考.wav"
    audio.parent.mkdir(parents=True)
    audio.touch()
    window = SimpleNamespace(reference_audio_dir=root)
    assert MainWindow._voice_metadata_key(window, audio) == "音色甲/参考.wav"


def test_nested_voice_display_name_contains_folder(tmp_path: Path) -> None:
    root = tmp_path / "参考音频"
    audio = root / "科目一" / "老师.wav"
    audio.parent.mkdir(parents=True)
    audio.touch()
    window = SimpleNamespace(reference_audio_dir=root)
    assert MainWindow._voice_display_name(window, audio) == "科目一 / 老师"
