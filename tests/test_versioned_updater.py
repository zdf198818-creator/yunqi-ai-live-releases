import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from ailive.client import updater
from ailive.client.updater import install_update, verify_package
from ailive.client.version_layout import CLIENT_EXECUTABLE, read_current_state


def make_update_package(tmp_path: Path, version: str, marker: bytes = b"new") -> Path:
    source = tmp_path / f"payload-{version}"
    source.mkdir()
    (source / CLIENT_EXECUTABLE).write_bytes(marker)
    (source / "version.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    package = tmp_path / f"update-{version}.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source))
    return package


def seed_installed_version(root: Path, version: str, marker: bytes = b"old") -> None:
    directory = root / "versions" / version
    directory.mkdir(parents=True)
    (directory / CLIENT_EXECUTABLE).write_bytes(marker)
    (root / "current.json").write_text(
        json.dumps({"current": version, "previous": ""}), encoding="utf-8"
    )


def test_install_switches_version_and_preserves_user_data(tmp_path: Path) -> None:
    seed_installed_version(tmp_path, "0.9.10")
    reference = tmp_path / "用户数据" / "参考音频" / "voice.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"audio")
    settings = tmp_path / "用户数据" / "窗口与连接设置" / "layout.ini"
    settings.parent.mkdir(parents=True)
    settings.write_text("layout", encoding="utf-8")
    package = make_update_package(tmp_path, "0.9.11")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    assert install_update(package, tmp_path, expected_sha256=digest) == "0.9.11"
    state = read_current_state(tmp_path)
    assert state["current"] == "0.9.11"
    assert state["previous"] == "0.9.10"
    assert reference.read_bytes() == b"audio"
    backups = list((tmp_path / "用户数据" / "升级前设置备份").glob("*/layout.ini"))
    assert len(backups) == 1
    assert not list((tmp_path / "用户数据" / "升级前设置备份").rglob("voice.wav"))


def test_bad_sha_does_not_switch(tmp_path: Path) -> None:
    seed_installed_version(tmp_path, "0.9.10")
    package = make_update_package(tmp_path, "0.9.11")
    with pytest.raises(ValueError, match="SHA256"):
        install_update(package, tmp_path, expected_sha256="0" * 64)
    assert read_current_state(tmp_path)["current"] == "0.9.10"
    assert not (tmp_path / "versions" / "0.9.11").exists()


def test_invalid_zip_is_rejected(tmp_path: Path) -> None:
    package = tmp_path / "broken.zip"
    package.write_bytes(b"not-a-zip")
    with pytest.raises(ValueError, match="ZIP"):
        verify_package(package)


def test_zip_path_traversal_is_rejected(tmp_path: Path) -> None:
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../escaped.txt", "unsafe")
    with pytest.raises(ValueError, match="不安全路径"):
        install_update(package, tmp_path, version="0.9.11")
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_failed_switch_restores_replaced_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seed_installed_version(tmp_path, "0.9.10")
    old_target = tmp_path / "versions" / "0.9.11"
    old_target.mkdir(parents=True)
    (old_target / CLIENT_EXECUTABLE).write_bytes(b"previous-copy")
    package = make_update_package(tmp_path, "0.9.11", b"new-copy")

    def fail_backup(_root: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(updater, "backup_settings", fail_backup)
    with pytest.raises(OSError, match="simulated"):
        install_update(package, tmp_path)
    assert (old_target / CLIENT_EXECUTABLE).read_bytes() == b"previous-copy"
    assert read_current_state(tmp_path)["current"] == "0.9.10"
