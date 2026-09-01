from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path

from ailive.client.updater import install_update, launch_fixed_launcher

DEFAULT_INSTALL_ROOT = Path(r"D:\云祺AI直播")


def bundled_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def show_message(title: str, message: str, *, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    ctypes.windll.user32.MessageBoxW(None, message, title, flags)


def stop_running_client() -> None:
    subprocess.run(
        ["taskkill", "/IM", "云祺AI直播客户端.exe", "/T", "/F"],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def load_embedded_manifest(root: Path) -> dict[str, object]:
    path = root / "payload" / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError("离线升级程序缺少 manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise TypeError("离线升级清单格式错误")
    return payload


def run_offline_update(
    install_root: Path,
    package: Path,
    *,
    version: str = "",
    expected_sha256: str = "",
) -> str:
    if not (install_root / "云祺AI直播.exe").is_file():
        raise FileNotFoundError(
            "未找到固定启动器。请先运行一次“云祺AI直播安装程序.exe”。"
        )
    stop_running_client()
    installed = install_update(
        package,
        install_root,
        version=version,
        expected_sha256=expected_sha256,
    )
    launch_fixed_launcher(install_root)
    return installed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="云祺AI直播离线升级")
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--package", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = bundled_root()
    try:
        if args.package is not None:
            package = args.package.resolve()
            manifest: dict[str, object] = {}
        else:
            package = root / "payload" / "client-update.zip"
            manifest = load_embedded_manifest(root)
        version = str(manifest.get("version") or "")
        expected = str(manifest.get("sha256") or "")
        installed = run_offline_update(
            args.install_root.resolve(),
            package,
            version=version,
            expected_sha256=expected,
        )
    except Exception as error:
        show_message("升级失败", f"没有切换新版本，旧版本仍可继续使用。\n\n{error}", error=True)
        raise SystemExit(1) from error
    show_message("升级完成", f"云祺AI直播已升级到 {installed}，用户数据保持不变。")


if __name__ == "__main__":
    main()
