from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_client_release import copy_public_data

CLIENT_NAME = "云祺AI直播客户端"
LAUNCHER_NAME = "云祺AI直播"
UPDATER_NAME = "云祺AI直播更新器"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pyinstaller(
    project_root: Path,
    *,
    entry: Path,
    name: str,
    dist: Path,
    work: Path,
    spec: Path,
    icon: Path,
    onefile: bool,
    extra_data: list[tuple[Path, str]] | None = None,
) -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile" if onefile else "--onedir",
        "--name",
        name,
        "--icon",
        str(icon),
        "--paths",
        str(project_root / "src"),
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
    ]
    for source, destination in extra_data or []:
        command.extend(["--add-data", f"{source}{os.pathsep}{destination}"])
    command.append(str(entry))
    subprocess.run(command, check=True, cwd=project_root)


def build_release(args: argparse.Namespace) -> dict[str, str]:
    project_root = args.project_root.resolve()
    version = args.version.strip().removeprefix("v")
    tag = f"v{version}"
    release_root = project_root / "releases"
    build_root = project_root / ".versioned-release-build"
    dist_root = build_root / "dist"
    work_root = build_root / "work"
    spec_root = build_root / "spec"
    payload_root = build_root / "payload"
    starter_root = build_root / "starter"
    icon_png = project_root / "assets" / "yunqi-ai-live-icon-v2.png"
    icon_ico = project_root / "assets" / "yunqi-ai-live-icon-v2.ico"
    if not icon_png.is_file() or not icon_ico.is_file():
        raise FileNotFoundError("客户端图标文件缺失")
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    release_root.mkdir(parents=True, exist_ok=True)

    client_entry = build_root / "run_client.py"
    client_entry.write_text(
        "from ailive.client.app import run\n\nif __name__ == '__main__':\n    run()\n",
        encoding="utf-8",
    )
    run_pyinstaller(
        project_root,
        entry=client_entry,
        name=CLIENT_NAME,
        dist=dist_root / "client",
        work=work_root / "client",
        spec=spec_root / "client",
        icon=icon_ico,
        onefile=False,
        extra_data=[(icon_png, "assets")],
    )
    client_dir = dist_root / "client" / CLIENT_NAME
    starter_manifest = copy_public_data(
        project_root,
        starter_root,
        script_name=args.script,
        line_limit=args.lines,
        audio_limit=args.audio,
    )
    version_manifest = {
        **starter_manifest,
        "version": version,
        "channel": args.channel,
    }
    (client_dir / "version.json").write_text(
        json.dumps(version_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    run_pyinstaller(
        project_root,
        entry=project_root / "src" / "ailive" / "client" / "client_launcher.py",
        name=LAUNCHER_NAME,
        dist=dist_root / "fixed",
        work=work_root / "launcher",
        spec=spec_root / "launcher",
        icon=icon_ico,
        onefile=True,
    )
    run_pyinstaller(
        project_root,
        entry=project_root / "src" / "ailive" / "client" / "updater.py",
        name=UPDATER_NAME,
        dist=dist_root / "fixed",
        work=work_root / "updater",
        spec=spec_root / "updater",
        icon=icon_ico,
        onefile=True,
    )

    package_name = f"yunqi-ai-live-client-{version}-update.zip"
    package_base = payload_root / package_name.removesuffix(".zip")
    payload_root.mkdir(parents=True, exist_ok=True)
    package = Path(shutil.make_archive(str(package_base), "zip", client_dir))
    package_sha256 = sha256_file(package)
    embedded_package = payload_root / "client-update.zip"
    shutil.copy2(package, embedded_package)
    manifest = {
        "version": version,
        "download_url": (
            f"https://github.com/{args.repository}/releases/download/{tag}/{package_name}"
        ),
        "sha256": package_sha256,
        "notes": args.notes,
    }
    manifest_path = payload_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    offline_name = f"云祺AI直播离线升级-{version}"
    run_pyinstaller(
        project_root,
        entry=project_root / "src" / "ailive" / "client" / "offline_updater.py",
        name=offline_name,
        dist=dist_root / "offline",
        work=work_root / "offline",
        spec=spec_root / "offline",
        icon=icon_ico,
        onefile=True,
        extra_data=[(embedded_package, "payload"), (manifest_path, "payload")],
    )

    shutil.copy2(dist_root / "fixed" / f"{LAUNCHER_NAME}.exe", payload_root)
    shutil.copy2(dist_root / "fixed" / f"{UPDATER_NAME}.exe", payload_root)
    shutil.copytree(starter_root / "用户数据", payload_root / "用户数据")
    installer_name = "云祺AI直播安装程序"
    run_pyinstaller(
        project_root,
        entry=project_root / "src" / "ailive" / "client" / "full_installer.py",
        name=installer_name,
        dist=dist_root / "installer",
        work=work_root / "installer",
        spec=spec_root / "installer",
        icon=icon_ico,
        onefile=True,
        extra_data=[
            (embedded_package, "payload"),
            (manifest_path, "payload"),
            (payload_root / f"{LAUNCHER_NAME}.exe", "payload"),
            (payload_root / f"{UPDATER_NAME}.exe", "payload"),
            (payload_root / "用户数据", "payload/用户数据"),
        ],
    )

    install_tree = release_root / f"云祺AI直播-{version}-固定安装版"
    if install_tree.exists():
        shutil.rmtree(install_tree)
    (install_tree / "versions" / version).mkdir(parents=True)
    shutil.copytree(client_dir, install_tree / "versions" / version, dirs_exist_ok=True)
    shutil.copytree(starter_root / "用户数据", install_tree / "用户数据")
    (install_tree / "updates").mkdir()
    shutil.copy2(dist_root / "fixed" / f"{LAUNCHER_NAME}.exe", install_tree)
    shutil.copy2(dist_root / "fixed" / f"{UPDATER_NAME}.exe", install_tree)
    (install_tree / "current.json").write_text(
        json.dumps({"current": version, "previous": ""}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    outputs = {
        "installer": str(release_root / f"{installer_name}.exe"),
        "offline_updater": str(release_root / f"{offline_name}.exe"),
        "online_package": str(release_root / package.name),
        "online_manifest": str(release_root / "update.json"),
        "install_tree": str(install_tree),
    }
    shutil.copy2(dist_root / "installer" / f"{installer_name}.exe", outputs["installer"])
    shutil.copy2(dist_root / "offline" / f"{offline_name}.exe", outputs["offline_updater"])
    shutil.copy2(package, outputs["online_package"])
    shutil.copy2(manifest_path, outputs["online_manifest"])
    (release_root / f"{offline_name}.sha256.txt").write_text(
        f"{sha256_file(Path(outputs['offline_updater']))}  {Path(outputs['offline_updater']).name}\n",
        encoding="utf-8",
    )
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建固定启动器和版本化升级包")
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--version", default="0.9.12")
    parser.add_argument("--channel", default="test")
    parser.add_argument(
        "--repository", default="zdf198818-creator/yunqi-ai-live-releases"
    )
    parser.add_argument("--notes", default="固定启动器、独立用户数据和原子版本切换。")
    parser.add_argument("--script", default="111.txt")
    parser.add_argument("--lines", type=int, default=20)
    parser.add_argument("--audio", type=int, default=20)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(build_release(parse_args()), ensure_ascii=False, indent=2))
