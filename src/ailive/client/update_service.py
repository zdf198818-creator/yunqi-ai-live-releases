from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from PySide6.QtCore import QThread, Signal

from ailive.client.storage import install_root
from ailive.client.version_layout import UPDATER_EXECUTABLE

APP_VERSION = "0.9.12"
DEFAULT_MANIFEST_URL = (
    "https://github.com/zdf198818-creator/yunqi-ai-live-releases/"
    "releases/latest/download/update.json"
)


def version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers[:4]) or (0,)


def is_newer_version(remote: str, current: str = APP_VERSION) -> bool:
    return version_key(remote) > version_key(current)


def fetch_manifest(url: str | None = None) -> dict[str, object]:
    manifest_url = url or os.environ.get("AILIVE_UPDATE_MANIFEST_URL", DEFAULT_MANIFEST_URL)
    response = httpx.get(manifest_url, follow_redirects=True, timeout=20.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("更新清单格式错误")
    for key in ("version", "download_url", "sha256"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"更新清单缺少 {key}")
    return payload


def download_package(
    manifest: dict[str, object], progress: callable | None = None
) -> Path:
    url = str(manifest["download_url"])
    destination = Path(tempfile.mkdtemp(prefix="yunqi-update-")) / "client-update.zip"
    digest = hashlib.sha256()
    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        received = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress is not None and total:
                    progress(min(100, int(received * 100 / total)))
    expected = str(manifest["sha256"]).strip().lower()
    if digest.hexdigest().lower() != expected:
        destination.unlink(missing_ok=True)
        raise ValueError("更新包校验失败，已停止安装")
    return destination


def launch_updater(package: Path, app_root: Path) -> None:
    root = install_root(app_root)
    fixed_updater = root / UPDATER_EXECUTABLE
    if not fixed_updater.is_file():
        raise FileNotFoundError("固定更新器不存在，请先安装一次固定启动器版本")
    temporary_updater = package.parent / fixed_updater.name
    shutil.copy2(fixed_updater, temporary_updater)
    subprocess.Popen(
        [
            str(temporary_updater),
            "--package",
            str(package),
            "--install-root",
            str(root),
            "--pid",
            str(os.getpid()),
        ],
        cwd=package.parent,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def launch_rollback(app_root: Path) -> None:
    root = install_root(app_root)
    fixed_updater = root / UPDATER_EXECUTABLE
    if not fixed_updater.is_file():
        raise FileNotFoundError("固定更新器不存在，无法恢复上一版本")
    subprocess.Popen(
        [
            str(fixed_updater),
            "--install-root",
            str(root),
            "--pid",
            str(os.getpid()),
            "--rollback",
        ],
        cwd=root,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


class UpdateCheckWorker(QThread):
    available = Signal(dict)
    current = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            manifest = fetch_manifest()
            remote_version = str(manifest["version"])
            if is_newer_version(remote_version):
                self.available.emit(manifest)
            else:
                self.current.emit(APP_VERSION)
        except Exception as error:  # noqa: BLE001 - report worker failures through Qt
            self.failed.emit(str(error))


class UpdateDownloadWorker(QThread):
    progress = Signal(int)
    downloaded = Signal(str)
    failed = Signal(str)

    def __init__(self, manifest: dict[str, object]) -> None:
        super().__init__()
        self.manifest = manifest

    def run(self) -> None:
        try:
            package = download_package(self.manifest, self.progress.emit)
            self.downloaded.emit(str(package))
        except Exception as error:  # noqa: BLE001 - report worker failures through Qt
            self.failed.emit(str(error))
