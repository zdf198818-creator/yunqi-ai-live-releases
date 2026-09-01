from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ailive.client.version_layout import (
    cleanup_old_versions,
    resolve_current_executable,
    rollback_current_version,
)


def default_install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def launch_current(install_root: Path) -> Path:
    executable = resolve_current_executable(install_root)
    environment = os.environ.copy()
    environment["AILIVE_INSTALL_ROOT"] = str(install_root.resolve())
    environment["AILIVE_USER_DATA_ROOT"] = str(
        (install_root / "用户数据").resolve()
    )
    subprocess.Popen(
        [str(executable)],
        cwd=executable.parent,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    cleanup_old_versions(install_root)
    return executable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="云祺AI直播固定启动器")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--rollback", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    install_root = (args.install_root or default_install_root()).resolve()
    if args.rollback:
        rollback_current_version(install_root)
    launch_current(install_root)


if __name__ == "__main__":
    main()
