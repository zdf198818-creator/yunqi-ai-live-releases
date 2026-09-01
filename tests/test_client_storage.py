from pathlib import Path

from ailive.client import storage


def test_frozen_user_data_is_sibling_of_client(monkeypatch, tmp_path: Path) -> None:
    client_dir = tmp_path / "客户端"
    client_dir.mkdir()
    executable = client_dir / "云祺AI直播客户端.exe"
    monkeypatch.setattr(storage.sys, "frozen", True, raising=False)
    monkeypatch.setattr(storage.sys, "executable", str(executable))

    assert storage.application_root() == client_dir
    assert storage.user_data_root() == tmp_path / "用户数据"


def test_prepare_user_data_migrates_without_overwriting(monkeypatch, tmp_path: Path) -> None:
    client_dir = tmp_path / "客户端"
    legacy = tmp_path / "data"
    (legacy / "scripts").mkdir(parents=True)
    (legacy / "参考音频").mkdir()
    (legacy / ".config").mkdir()
    (legacy / "scripts" / "旧话术.txt").write_text("旧内容", encoding="utf-8")
    (legacy / "参考音频" / "声音.wav").write_bytes(b"old-audio")
    (legacy / ".config" / "话术行配置.json").write_text("{}", encoding="utf-8")
    client_dir.mkdir()
    monkeypatch.setattr(storage.sys, "frozen", True, raising=False)
    monkeypatch.setattr(storage.sys, "executable", str(client_dir / "app.exe"))

    paths = storage.prepare_user_data()
    script = paths["scripts"] / "旧话术.txt"
    assert script.read_text(encoding="utf-8") == "旧内容"
    script.write_text("用户修改", encoding="utf-8")
    storage.prepare_user_data()
    assert script.read_text(encoding="utf-8") == "用户修改"
    assert (paths["reference_audio"] / "声音.wav").is_file()
    assert (paths["row_settings"] / "话术行配置.json").is_file()


def test_version_directory_uses_stable_user_data_root(monkeypatch, tmp_path: Path) -> None:
    version_dir = tmp_path / "versions" / "0.9.11"
    version_dir.mkdir(parents=True)
    monkeypatch.setattr(storage.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        storage.sys, "executable", str(version_dir / "云祺AI直播客户端.exe")
    )

    assert storage.install_root() == tmp_path
    assert storage.user_data_root() == tmp_path / "用户数据"
