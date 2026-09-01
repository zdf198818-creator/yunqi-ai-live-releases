from pathlib import Path

from ailive.client import full_installer


def test_create_desktop_shortcut_points_to_fixed_launcher(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs))

    monkeypatch.setattr(full_installer.subprocess, "run", fake_run)
    full_installer.create_desktop_shortcut(tmp_path)

    command, kwargs = calls[0]
    assert command[0] == "powershell.exe"
    assert str(tmp_path / full_installer.LAUNCHER_EXECUTABLE) in command[-1]
    assert "云祺AI直播.lnk" in command[-1]
    assert kwargs["check"] is True
