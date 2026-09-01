import json
from pathlib import Path
from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_load_script_row_settings_returns_empty_for_missing_file(tmp_path: Path) -> None:
    window = SimpleNamespace(script_row_settings_path=tmp_path / "missing.json")
    assert MainWindow._load_script_row_settings(window) == {}


def test_load_script_row_settings_reads_saved_rows(tmp_path: Path) -> None:
    path = tmp_path / "rows.json"
    path.write_text(json.dumps({"script.txt": [{"text": "第一句"}]}), encoding="utf-8")
    window = SimpleNamespace(script_row_settings_path=path)
    assert MainWindow._load_script_row_settings(window)["script.txt"][0]["text"] == "第一句"


def test_save_current_text_row_settings_persists_manual_reference(tmp_path: Path) -> None:
    settings_path = tmp_path / "rows.json"
    script_path = tmp_path / "script.txt"
    script_path.write_text("第一句", encoding="utf-8")
    row = {
        "line_id": "line-1",
        "reference_id": "local:voice-1",
        "speed": 1.1,
        "text": "第一句",
    }
    window = SimpleNamespace(
        script_row_settings_path=settings_path,
        _snapshot_rows=lambda: [row],
    )
    window._load_script_row_settings = lambda: MainWindow._load_script_row_settings(window)

    MainWindow._save_current_text_row_settings(window, script_path)

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved[str(script_path.resolve())][0]["reference_id"] == "local:voice-1"


def test_saved_text_rows_falls_back_to_unique_filename(tmp_path: Path) -> None:
    settings_path = tmp_path / "rows.json"
    settings_path.write_text(
        json.dumps({"D:/old-computer/scripts/demo.txt": [{"text": "第一句"}]}),
        encoding="utf-8",
    )
    window = SimpleNamespace(script_row_settings_path=settings_path)
    window._load_script_row_settings = lambda: MainWindow._load_script_row_settings(window)

    rows = MainWindow._saved_text_rows(window, tmp_path / "demo.txt")

    assert rows == [{"text": "第一句"}]


def test_restore_saved_reference_uses_relative_path_after_client_moves() -> None:
    window = SimpleNamespace(
        voice_profiles=[
            {
                "reference_id": "local:new-id",
                "audio_path": "D:/new-client/用户数据/参考音频/赵1/sample.wav",
                "reference_text": "参考文案",
            }
        ],
        local_voice_remote_ids={},
        _voice_metadata_key=lambda _path: "赵1/sample.wav",
    )

    restored = MainWindow._restore_saved_reference_id(
        window,
        {
            "reference_id": "local:old-id",
            "reference_path": "赵1/sample.wav",
            "reference_text": "参考文案",
        },
    )

    assert restored == "local:new-id"


def test_restore_saved_reference_uses_unique_transcript_when_path_changed() -> None:
    window = SimpleNamespace(
        voice_profiles=[
            {
                "reference_id": "local:new-id",
                "audio_path": "D:/new-client/用户数据/参考音频/新文件夹/sample.wav",
                "reference_text": "唯一参考文案",
            }
        ],
        local_voice_remote_ids={},
        _voice_metadata_key=lambda _path: "新文件夹/sample.wav",
    )

    restored = MainWindow._restore_saved_reference_id(
        window,
        {
            "reference_id": "local:old-id",
            "reference_path": "旧文件夹/sample.wav",
            "reference_text": "唯一参考文案",
        },
    )

    assert restored == "local:new-id"
