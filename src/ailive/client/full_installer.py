from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import subprocess
import sys
from pathlib import Path

from ailive.client.updater import install_update, launch_fixed_launcher
from ailive.client.version_layout import LAUNCHER_EXECUTABLE, UPDATER_EXECUTABLE

DEFAULT_INSTALL_ROOT = Path(r"D:\云祺AI直播")


def bundled_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def show_message(title: str, message: str, *, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    ctypes.windll.user32.MessageBoxW(None, message, title, flags)


def copy_missing_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for source_path in source.rglob("*"):
        if not source_path.is_file():
            continue
        target = destination / source_path.relative_to(source)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)


def create_desktop_shortcut(root: Path) -> None:
    launcher = root / LAUNCHER_EXECUTABLE
    escaped_launcher = str(launcher).replace("'", "''")
    escaped_root = str(root).replace("'", "''")
    script = (
        "$desktop=[Environment]::GetFolderPath('Desktop');"
        "$link=Join-Path $desktop '云祺AI直播.lnk';"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$shortcut=$shell.CreateShortcut($link);"
        f"$shortcut.TargetPath='{escaped_launcher}';"
        f"$shortcut.WorkingDirectory='{escaped_root}';"
        f"$shortcut.IconLocation='{escaped_launcher},0';"
        "$shortcut.Save()"
    )
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def install_full(root: Path, payload: Path) -> str:
    subprocess.run(
        ["taskkill", "/IM", "云祺AI直播客户端.exe", "/T", "/F"],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    root.mkdir(parents=True, exist_ok=True)
    for filename in (LAUNCHER_EXECUTABLE, UPDATER_EXECUTABLE):
        source = payload / filename
        if not source.is_file():
            raise FileNotFoundError(f"安装程序缺少 {filename}")
        shutil.copy2(source, root / filename)
    manifest = json.loads((payload / "manifest.json").read_text(encoding="utf-8-sig"))
    copy_missing_tree(payload / "用户数据", root / "用户数据")
    version = install_update(
        payload / "client-update.zip",
        root,
        version=str(manifest.get("version") or ""),
        expected_sha256=str(manifest.get("sha256") or ""),
    )
    create_desktop_shortcut(root)
    launch_fixed_launcher(root)
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="云祺AI直播完整安装程序")
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        version = install_full(args.install_root.resolve(), bundled_root() / "payload")
    except Exception as error:
        show_message("安装失败", str(error), error=True)
        raise SystemExit(1) from error
    show_message("安装完成", f"云祺AI直播 {version} 已安装并启动。")


if __name__ == "__main__":
    main()
