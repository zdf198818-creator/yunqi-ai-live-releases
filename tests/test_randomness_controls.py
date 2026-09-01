from __future__ import annotations

from pathlib import Path


def test_public_settings_replace_apply_speed_with_randomness_controls() -> None:
    project = Path(__file__).resolve().parent.parent
    source = (project / "src" / "ailive" / "client" / "app.py").read_text(
        encoding="utf-8"
    )

    assert "应用语速到全部话术" not in source
    assert "降低随机性（语气更稳定）" in source
    assert "关闭随机性（尽量固定）" in source
    assert 'settings.setValue("tts/randomness"' in source
