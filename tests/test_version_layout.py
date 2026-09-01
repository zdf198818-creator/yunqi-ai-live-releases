import json
import os
import time
from pathlib import Path

import pytest

from ailive.client.version_layout import (
    CLIENT_EXECUTABLE,
    cleanup_old_versions,
    resolve_current_executable,
    rollback_current_version,
    switch_current_version,
)


def make_version(root: Path, version: str) -> Path:
    directory = root / "versions" / version
    directory.mkdir(parents=True)
    executable = directory / CLIENT_EXECUTABLE
    executable.write_bytes(version.encode())
    return executable


def test_switch_and_rollback_are_atomic(tmp_path: Path) -> None:
    first = make_version(tmp_path, "0.9.10")
    second = make_version(tmp_path, "0.9.11")
    switch_current_version(tmp_path, "0.9.10")
    state = switch_current_version(tmp_path, "0.9.11")
    assert state["current"] == "0.9.11"
    assert state["previous"] == "0.9.10"
    assert resolve_current_executable(tmp_path) == second

    rolled_back = rollback_current_version(tmp_path)
    assert rolled_back["current"] == "0.9.10"
    assert resolve_current_executable(tmp_path) == first


def test_missing_current_falls_back_to_previous(tmp_path: Path) -> None:
    previous = make_version(tmp_path, "0.9.10")
    (tmp_path / "current.json").write_text(
        json.dumps({"current": "broken", "previous": "0.9.10"}), encoding="utf-8"
    )
    assert resolve_current_executable(tmp_path) == previous


def test_cleanup_preserves_current_and_previous(tmp_path: Path) -> None:
    for version in ("0.9.8", "0.9.9", "0.9.10", "0.9.11"):
        make_version(tmp_path, version)
    (tmp_path / "current.json").write_text(
        json.dumps({"current": "0.9.11", "previous": "0.9.10"}), encoding="utf-8"
    )
    old = time.time() - 9 * 24 * 60 * 60
    for version in ("0.9.8", "0.9.9"):
        os.utime(tmp_path / "versions" / version, (old, old))
    removed = cleanup_old_versions(tmp_path, keep_days=7)
    assert set(removed) == {"0.9.8", "0.9.9"}
    assert (tmp_path / "versions" / "0.9.10").is_dir()
    assert (tmp_path / "versions" / "0.9.11").is_dir()


def test_invalid_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        switch_current_version(tmp_path, "../outside")
