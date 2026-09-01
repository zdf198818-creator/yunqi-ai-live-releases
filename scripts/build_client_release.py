from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def natural_key(path: Path) -> list[object]:
    relative = path.as_posix().casefold()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", relative)]


def load_row_settings(config_dir: Path, script_name: str) -> list[dict[str, object]]:
    for json_path in config_dir.glob("*.json"):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for saved_path, rows in payload.items():
            if Path(str(saved_path)).name.casefold() == script_name.casefold() and isinstance(
                rows, list
            ):
                return [row for row in rows if isinstance(row, dict)]
    return []


def load_voice_metadata(config_dir: Path) -> dict[str, str]:
    for json_path in config_dir.glob("*.json"):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload and isinstance(payload, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        ):
            return dict(payload)
    return {}


def reference_id(audio_path: Path) -> str:
    digest = hashlib.sha1(str(audio_path.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"local:{digest}"


def copy_public_data(
    project_root: Path,
    release_root: Path,
    *,
    script_name: str,
    line_limit: int,
    audio_limit: int,
) -> dict[str, object]:
    source_data = project_root / "data"
    source_script = source_data / "scripts" / script_name
    if not source_script.is_file():
        raise FileNotFoundError(f"话术文件不存在: {source_script}")

    sys.path.insert(0, str(project_root / "src"))
    from ailive.parser import split_script_sentences

    sentences = split_script_sentences(source_script.read_text(encoding="utf-8-sig"))
    sentences = sentences[:line_limit]
    if not sentences:
        raise RuntimeError("话术文件中没有可发布的句子")

    target_data = release_root / "用户数据"
    target_scripts = target_data / "直播话术"
    target_config = target_data / "窗口与连接设置"
    target_row_settings = target_data / "话术行配置"
    target_interjections = target_data / "插播话术"
    target_audio_root = target_data / "参考音频"
    target_scripts.mkdir(parents=True, exist_ok=True)
    target_config.mkdir(parents=True, exist_ok=True)
    target_row_settings.mkdir(parents=True, exist_ok=True)
    target_interjections.mkdir(parents=True, exist_ok=True)
    target_audio_root.mkdir(parents=True, exist_ok=True)
    (target_scripts / script_name).write_text(
        "\n".join(sentences) + "\n", encoding="utf-8"
    )

    source_config = source_data / ".config"
    rows = load_row_settings(source_config, script_name)
    public_rows: list[dict[str, object]] = []
    requested_reference_ids: list[str] = []
    for index, text in enumerate(sentences):
        saved = rows[index] if index < len(rows) else {}
        saved_reference = str(saved.get("reference_id") or "")
        if saved_reference and saved_reference not in requested_reference_ids:
            requested_reference_ids.append(saved_reference)
        public_rows.append(
            {
                "text": text,
                "reference_id": None,
                "speed": float(saved.get("speed", 1.0)),
                "line_id": str(saved.get("line_id") or f"release-line-{index + 1}"),
            }
        )

    source_audio_root = source_data / "参考音频"
    all_audio = sorted(
        (path for path in source_audio_root.rglob("*.wav") if path.is_file()),
        key=lambda path: natural_key(path.relative_to(source_audio_root)),
    )
    by_reference_id = {reference_id(path): path for path in all_audio}
    selected_audio: list[Path] = []
    for saved_reference in requested_reference_ids:
        audio_path = by_reference_id.get(saved_reference)
        if audio_path is not None and audio_path not in selected_audio:
            selected_audio.append(audio_path)
        if len(selected_audio) >= audio_limit:
            break
    for audio_path in all_audio:
        if len(selected_audio) >= audio_limit:
            break
        if audio_path not in selected_audio:
            selected_audio.append(audio_path)

    for audio_path in selected_audio:
        relative = audio_path.relative_to(source_audio_root)
        destination = target_audio_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_path, destination)

    metadata = load_voice_metadata(source_config)
    selected_keys: set[str] = set()
    selected_names: set[str] = set()
    for audio_path in selected_audio:
        selected_keys.add(audio_path.relative_to(source_audio_root).as_posix())
        selected_names.add(audio_path.name)
    public_metadata = {
        key: value
        for key, value in metadata.items()
        if key in selected_keys or Path(key).name in selected_names
    }
    (target_config / "音色文案.json").write_text(
        json.dumps(public_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (target_row_settings / "话术行配置.json").write_text(
        json.dumps({script_name: public_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (target_interjections / "插播话术.json").write_text(
        json.dumps({"version": 1, "lines": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "script": script_name,
        "script_lines": len(sentences),
        "reference_audio": len(selected_audio),
        "reference_audio_files": [
            path.relative_to(source_audio_root).as_posix() for path in selected_audio
        ],
    }


def build_release(args: argparse.Namespace) -> Path:
    project_root = args.project_root.resolve()
    release_parent = project_root / "releases"
    release_root = release_parent / f"云祺AI直播-{args.version}-测试版"
    build_root = project_root / ".release-build"
    entry_path = build_root / "run_client.py"
    dist_root = build_root / "dist"
    work_root = build_root / "work"
    spec_root = build_root / "spec"
    icon_png = project_root / "assets" / "yunqi-ai-live-icon-v2.png"
    icon_ico = project_root / "assets" / "yunqi-ai-live-icon-v2.ico"
    if not icon_png.is_file() or not icon_ico.is_file():
        raise FileNotFoundError("客户端图标文件缺失，请先运行 scripts/build_icon.py")

    if build_root.exists():
        shutil.rmtree(build_root)
    if release_root.exists():
        shutil.rmtree(release_root)
    build_root.mkdir(parents=True)
    release_parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(
        "from ailive.client.app import run\n\nif __name__ == '__main__':\n    run()\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--windowed",
            "--onedir",
            "--name",
            "云祺AI直播客户端",
            "--icon",
            str(icon_ico),
            "--add-data",
            f"{icon_png}{os.pathsep}assets",
            "--paths",
            str(project_root / "src"),
            "--distpath",
            str(dist_root),
            "--workpath",
            str(work_root),
            "--specpath",
            str(spec_root),
            str(entry_path),
        ],
        check=True,
        cwd=project_root,
    )

    built_dir = dist_root / "云祺AI直播客户端"
    client_root = release_root / "客户端"
    shutil.copytree(built_dir, client_root)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--windowed",
            "--onefile",
            "--name",
            "云祺AI直播更新器",
            "--icon",
            str(icon_ico),
            "--distpath",
            str(dist_root / "updater"),
            "--workpath",
            str(work_root / "updater"),
            "--specpath",
            str(spec_root / "updater"),
            str(project_root / "src" / "ailive" / "client" / "updater.py"),
        ],
        check=True,
        cwd=project_root,
    )
    shutil.copy2(
        dist_root / "updater" / "云祺AI直播更新器.exe",
        client_root / "云祺AI直播更新器.exe",
    )
    manifest = copy_public_data(
        project_root,
        release_root,
        script_name=args.script,
        line_limit=args.lines,
        audio_limit=args.audio,
    )
    manifest.update({"version": args.version, "channel": "test"})
    (client_root / "version.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (release_root / "使用说明.txt").write_text(
        "云祺AI直播测试版\n\n"
        "1. 打开“客户端”文件夹，双击“云祺AI直播客户端.exe”。\n"
        "2. 启动云端TTS后，填写公网IP和端口8000。\n"
        "3. 选择默认音色并点击“连接并读取音色”。\n"
        "4. 本测试包仅包含前20句话术和前20个参考音频。\n"
        "5. “用户数据”文件夹永久保存话术、参考音频和个人设置。\n"
        "6. 客户端右上角“检查更新”可自动下载、替换并重新打开，用户数据不会被覆盖。\n",
        encoding="utf-8",
    )
    update_asset_name = f"yunqi-ai-live-client-{args.version}-update.zip"
    update_archive_base = release_parent / update_asset_name.removesuffix(".zip")
    update_archive = Path(shutil.make_archive(str(update_archive_base), "zip", client_root))
    update_sha256 = hashlib.sha256(update_archive.read_bytes()).hexdigest()
    clean_version = args.version.removeprefix("v")
    update_manifest = {
        "version": clean_version,
        "download_url": (
            f"https://github.com/{args.repository}/releases/download/"
            f"{args.version}/{update_asset_name}"
        ),
        "sha256": update_sha256,
        "notes": args.notes,
    }
    (release_parent / "update.json").write_text(
        json.dumps(update_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    archive_path = shutil.make_archive(str(release_root), "zip", release_root)
    archive_sha256 = hashlib.sha256(Path(archive_path).read_bytes()).hexdigest()
    Path(f"{archive_path}.sha256.txt").write_text(
        f"{archive_sha256}  {Path(archive_path).name}\n", encoding="utf-8"
    )
    return Path(archive_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建云祺AI直播对外测试版")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--version", default="v0.9.0")
    parser.add_argument(
        "--repository", default="zdf198818-creator/yunqi-ai-live-releases"
    )
    parser.add_argument("--notes", default="优化客户端并支持用户数据永久保留和一键更新。")
    parser.add_argument("--script", default="111.txt")
    parser.add_argument("--lines", type=int, default=20)
    parser.add_argument("--audio", type=int, default=20)
    return parser.parse_args()


if __name__ == "__main__":
    print(build_release(parse_args()))
