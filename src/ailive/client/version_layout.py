from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

CLIENT_EXECUTABLE = "云祺AI直播客户端.exe"
LAUNCHER_EXECUTABLE = "云祺AI直播.exe"
UPDATER_EXECUTABLE = "云祺AI直播更新器.exe"
CURRENT_FILENAME = "current.json"


def normalize_version(value: str) -> str:
    version = value.strip().removeprefix("v")
    if not version or any(character in version for character in '<>:"/\\|?*'):
        raise ValueError(f"版本号无效: {value}")
    return version


def ensure_install_layout(install_root: Path) -> None:
    install_root.mkdir(parents=True, exist_ok=True)
    (install_root / "versions").mkdir(exist_ok=True)
    (install_root / "用户数据").mkdir(exist_ok=True)
    (install_root / "updates").mkdir(exist_ok=True)


def read_current_state(install_root: Path) -> dict[str, object]:
    path = install_root / CURRENT_FILENAME
    if not path.is_file():
        return {"current": "", "previous": ""}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise TypeError("current.json 格式错误")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def version_directory(install_root: Path, version: str) -> Path:
    return install_root / "versions" / normalize_version(version)


def version_executable(install_root: Path, version: str) -> Path:
    return version_directory(install_root, version) / CLIENT_EXECUTABLE


def validate_version(install_root: Path, version: str) -> Path:
    executable = version_executable(install_root, version)
    if not executable.is_file():
        raise FileNotFoundError(f"版本 {version} 缺少 {CLIENT_EXECUTABLE}")
    return executable


def switch_current_version(install_root: Path, version: str) -> dict[str, object]:
    ensure_install_layout(install_root)
    version = normalize_version(version)
    validate_version(install_root, version)
    old_state = read_current_state(install_root)
    old_current = str(old_state.get("current") or "")
    previous = old_current if old_current and old_current != version else str(
        old_state.get("previous") or ""
    )
    state: dict[str, object] = {
        "current": version,
        "previous": previous,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _atomic_write_json(install_root / CURRENT_FILENAME, state)
    return state


def rollback_current_version(install_root: Path) -> dict[str, object]:
    state = read_current_state(install_root)
    current = str(state.get("current") or "")
    previous = str(state.get("previous") or "")
    if not previous:
        raise RuntimeError("没有可恢复的上一版本")
    validate_version(install_root, previous)
    rolled_back: dict[str, object] = {
        "current": previous,
        "previous": current,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reason": "manual-rollback",
    }
    _atomic_write_json(install_root / CURRENT_FILENAME, rolled_back)
    return rolled_back


def resolve_current_executable(install_root: Path) -> Path:
    state = read_current_state(install_root)
    current = str(state.get("current") or "")
    if current:
        try:
            return validate_version(install_root, current)
        except FileNotFoundError:
            pass
    previous = str(state.get("previous") or "")
    if previous:
        return validate_version(install_root, previous)
    raise FileNotFoundError("没有可启动的客户端版本")


def cleanup_old_versions(install_root: Path, *, keep_days: int = 7) -> list[str]:
    state = read_current_state(install_root)
    protected = {
        str(state.get("current") or ""),
        str(state.get("previous") or ""),
    }
    cutoff = time.time() - keep_days * 24 * 60 * 60
    removed: list[str] = []
    versions_root = install_root / "versions"
    if not versions_root.is_dir():
        return removed
    for directory in versions_root.iterdir():
        if not directory.is_dir() or directory.name in protected:
            continue
        if directory.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(directory)
        removed.append(directory.name)
    return removed
