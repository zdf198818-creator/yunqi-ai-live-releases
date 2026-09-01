from pathlib import Path
from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_metadata_is_stored_outside_reference_audio_folder() -> None:
    project = Path(__file__).resolve().parent.parent
    source = (project / "src" / "ailive" / "client" / "app.py").read_text(
        encoding="utf-8"
    )
    assert 'self.config_dir / "音色文案.json"' in source
    assert 'old_path = self.reference_audio_dir / "音色文案.json"' in source


def test_load_voice_metadata_returns_dictionary(tmp_path: Path) -> None:
    metadata_path = tmp_path / "音色文案.json"
    metadata_path.write_text('{"1/参考.wav": "参考原文"}', encoding="utf-8")
    window = SimpleNamespace(voice_metadata_path=metadata_path)
    assert MainWindow._load_voice_metadata(window) == {"1/参考.wav": "参考原文"}
