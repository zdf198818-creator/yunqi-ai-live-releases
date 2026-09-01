from pathlib import Path
from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_cloud_voice_name_is_stable_and_within_api_limit() -> None:
    window = SimpleNamespace(
        _voice_selector_name=lambda path: "赵152" + "很长的文件夹名" * 30
    )
    profile = {
        "reference_id": "local:5be0176283a6",
        "audio_path": str(Path("参考音频") / "赵152" / ("长文案" * 60 + ".wav")),
    }

    name = MainWindow._cloud_voice_name(window, profile)

    assert len(name) <= 100
    assert name.endswith("-5be0176283a6")
