from pathlib import Path
from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_nested_voice_selector_uses_folder_name(tmp_path: Path) -> None:
    root = tmp_path / "reference_audio"
    audio = root / "voice_1" / "long_reference_transcript.wav"
    audio.parent.mkdir(parents=True)
    audio.touch()
    window = SimpleNamespace(reference_audio_dir=root)
    assert MainWindow._voice_selector_name(window, audio) == "voice_1"


def test_root_voice_selector_falls_back_to_wav_name(tmp_path: Path) -> None:
    root = tmp_path / "reference_audio"
    audio = root / "standalone.wav"
    root.mkdir(parents=True)
    audio.touch()
    window = SimpleNamespace(reference_audio_dir=root)
    assert MainWindow._voice_selector_name(window, audio) == "standalone"
