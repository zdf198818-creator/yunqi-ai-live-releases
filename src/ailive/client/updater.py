from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from ailive.client.version_layout import (
    CLIENT_EXECUTABLE,
    LAUNCHER_EXECUTABLE,
    cleanup_old_versions,
    ensure_install_layout,
    rollback_current_version,
    switch_current_version,
)


def wait_for_process(pid: int, timeout: float = 60.0) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if str(pid) not in result.stdout:
            return
        time.sleep(0.5)
    raise TimeoutError("客户端未能在规定时间内退出")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_package(package: Path, expected_sha256: str = "") -> str:
    if not package.is_file():
        raise FileNotFoundError(f"升级包不存在: {package}")
    actual = sha256_file(package)
    if expected_sha256 and actual.casefold() != expected_sha256.strip().casefold():
        raise ValueError("升级包 SHA256 校验失败，已拒绝安装")
    if not zipfile.is_zipfile(package):
        raise ValueError("升级包不是有效的 ZIP 文件")
    return actual


def safe_extract(package: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(package) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if destination != target and destination not in target.parents:
                raise ValueError(f"升级包包含不安全路径: {member.filename}")
        archive.extractall(destination)


def _payload_root(staging: Path) -> Path:
    candidates = [staging, staging / "客户端"]
    candidates.extend(path for path in staging.iterdir() if path.is_dir())
    for candidate in candidates:
        if (candidate / CLIENT_EXECUTABLE).is_file():
            return candidate
    raise FileNotFoundError(f"升级包缺少 {CLIENT_EXECUTABLE}")


def _package_version(payload_root: Path, requested: str = "") -> str:
    if requested.strip():
        return requested.strip().removeprefix("v")
    manifest_path = payload_root / "version.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("升级包缺少 version.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    version = str(manifest.get("version") or "").strip().removeprefix("v")
    if not version:
        raise ValueError("version.json 缺少版本号")
    return version


def backup_settings(install_root: Path) -> Path | None:
    settings = install_root / "用户数据" / "窗口与连接设置"
    if not settings.is_dir():
        return None
    backup_root = install_root / "用户数据" / "升级前设置备份"
    destination = backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    shutil.copytree(settings, destination)
    return destination


def launch_fixed_launcher(install_root: Path) -> None:
    launcher = install_root / LAUNCHER_EXECUTABLE
    if not launcher.is_file():
        raise FileNotFoundError(f"固定启动器不存在: {launcher}")
    subprocess.Popen(
        [str(launcher)],
        cwd=install_root,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def install_update(
    package: Path,
    install_root: Path,
    *,
    version: str = "",
    expected_sha256: str = "",
) -> str:
    install_root = install_root.resolve()
    ensure_install_layout(install_root)
    verify_package(package, expected_sha256)
    updates_root = install_root / "updates"
    operation_id = uuid.uuid4().hex[:10]
    staging = updates_root / f".staging-{operation_id}"
    ready = updates_root / f".ready-{operation_id}"
    replaced_backup = updates_root / f".replaced-{operation_id}"
    target: Path | None = None
    staging.mkdir(parents=True)
    try:
        safe_extract(package, staging)
        payload = _payload_root(staging)
        version = _package_version(payload, version)
        target = install_root / "versions" / version
        shutil.copytree(payload, ready)
        try:
            if target.exists():
                target.replace(replaced_backup)
            ready.replace(target)
            backup_settings(install_root)
            switch_current_version(install_root, version)
        except Exception:
            if target is not None and target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if replaced_backup.exists():
                replaced_backup.replace(target)
            raise
        if replaced_backup.exists():
            shutil.rmtree(replaced_backup, ignore_errors=True)
        try:
            cleanup_old_versions(install_root, keep_days=7)
        except OSError:
            # Cleanup is best-effort and must never turn a successful version
            # switch into a failed update.
            pass
        return version
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(ready, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="云祺AI直播版本切换更新器")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--client-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--version", default="")
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--executable", default="", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    install_root = args.install_root
    if install_root is None and args.client_dir is not None:
        client_dir = args.client_dir.resolve()
        install_root = (
            client_dir.parent.parent
            if client_dir.parent.name.casefold() == "versions"
            else client_dir.parent
            if client_dir.name == "客户端"
            else client_dir
        )
    if install_root is None:
        raise ValueError("缺少 --install-root")
    install_root = install_root.resolve()
    try:
        wait_for_process(args.pid)
        if args.rollback:
            rollback_current_version(install_root)
        else:
            if args.package is None:
                raise ValueError("缺少 --package")
            install_update(
                args.package.resolve(),
                install_root,
                version=args.version,
                expected_sha256=args.expected_sha256,
            )
        launch_fixed_launcher(install_root)
    except Exception as error:
        install_root.mkdir(parents=True, exist_ok=True)
        (install_root / "更新失败.log").write_text(str(error), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
