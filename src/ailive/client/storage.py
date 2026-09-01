from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def application_root() -> Path:
    """Return the directory containing the source tree or frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def install_root(app_root: Path | None = None) -> Path:
    """Return the stable installation root shared by program and user data."""
    configured = os.environ.get("AILIVE_INSTALL_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    root = (app_root or application_root()).resolve()
    if root.parent.name.casefold() == "versions":
        return root.parent.parent
    if getattr(sys, "frozen", False) and root.name == "客户端":
        return root.parent
    return root


def user_data_root(app_root: Path | None = None) -> Path:
    """Use the source data directory in development and 用户数据 when frozen."""
    root = (app_root or application_root()).resolve()
    if getattr(sys, "frozen", False):
        configured = os.environ.get("AILIVE_USER_DATA_ROOT", "").strip()
        return Path(configured).resolve() if configured else install_root(root) / "用户数据"
    return root / "data"


def _copy_missing_tree(source: Path, destination: Path) -> int:
    """Copy only missing files, so an upgrade never overwrites user changes."""
    if not source.is_dir():
        return 0
    copied = 0
    for source_path in source.rglob("*"):
        if not source_path.is_file():
            continue
        target_path = destination / source_path.relative_to(source)
        if target_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied += 1
    return copied


def prepare_user_data(app_root: Path | None = None) -> dict[str, Path]:
    """Create stable user folders and migrate a legacy bundled data folder."""
    root = (app_root or application_root()).resolve()
    data_root = user_data_root(root)
    paths = {
        "root": data_root,
        "scripts": data_root / ("直播话术" if getattr(sys, "frozen", False) else "scripts"),
        "reference_audio": data_root / "参考音频",
        "row_settings": data_root / ("话术行配置" if getattr(sys, "frozen", False) else ".config"),
        "interjections": data_root / ("插播话术" if getattr(sys, "frozen", False) else ".config"),
        "settings": data_root / ("窗口与连接设置" if getattr(sys, "frozen", False) else ".config"),
        "local_audio": data_root / "本地合成音频",
        "audio_history": data_root / "历史音频",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    if not getattr(sys, "frozen", False):
        return paths

    # Packages before v0.9.3 kept mutable files under 客户端/data or beside
    # the executable. Copy only missing files and leave the legacy folder intact.
    legacy_candidates = [root / "data", install_root(root) / "data"]
    for legacy in legacy_candidates:
        if not legacy.is_dir() or legacy.resolve() == data_root.resolve():
            continue
        _copy_missing_tree(legacy / "scripts", paths["scripts"])
        _copy_missing_tree(legacy / "参考音频", paths["reference_audio"])
        legacy_config = legacy / ".config"
        for filename, key in (
            ("话术行配置.json", "row_settings"),
            ("插播话术.json", "interjections"),
            ("音色文案.json", "settings"),
            ("window-layout.ini", "settings"),
        ):
            source = legacy_config / filename
            destination = paths[key] / filename
            if source.is_file() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    return paths
