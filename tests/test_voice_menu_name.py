from pathlib import Path
from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_voice_menu_name_uses_folder_and_first_five_characters(tmp_path: Path) -> None:
    root = tmp_path / "reference_audio"
    audio = root / "赵1" / "long.wav"
    audio.parent.mkdir(parents=True)
    window = SimpleNamespace(reference_audio_dir=root)
    window._voice_selector_name = lambda path: MainWindow._voice_selector_name(window, path)
    assert MainWindow._voice_menu_name(window, audio, "来所有同学进入直播间") == "赵1 / 来所有同学"
