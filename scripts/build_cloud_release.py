from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def _ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__", ".pytest_cache", ".ruff_cache"}
    ignored.update(name for name in names if name.endswith((".pyc", ".pyo")))
    ignored.update(name for name in names if name.endswith(".egg-info"))
    return ignored


def build_cloud_release(project_root: Path, version: str) -> Path:
    project_root = project_root.resolve()
    release_parent = project_root / "releases"
    release_root = release_parent / f"云祺TTS云端-{version}"
    if release_root.exists():
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(project_root / "pyproject.toml", release_root / "pyproject.toml")
    shutil.copy2(project_root / "README.md", release_root / "README.md")
    shutil.copytree(project_root / "src", release_root / "src", ignore=_ignore)
    shutil.rmtree(release_root / "src" / "ailive" / "client", ignore_errors=True)

    deploy_dir = release_root / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(
        project_root / "deploy" / "start_tts_launcher.sh",
        deploy_dir / "start_tts_launcher.sh",
    )
    (deploy_dir / "install_or_update.sh").write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "PROJECT_DIR=\"${AILIVE_PROJECT_DIR:-/workspace/ailive-cloud}\"\n"
        "PYTHON=\"${AILIVE_PYTHON:-/usr/local/miniconda3/envs/ailive-tts/bin/python}\"\n"
        "\"$PYTHON\" -m pip install -e \"$PROJECT_DIR[server]\"\n"
        "mkdir -p /workspace/ailive-data\n"
        "chmod +x \"$PROJECT_DIR/deploy/start_tts_launcher.sh\"\n"
        "echo \"云祺TTS服务代码安装完成\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (deploy_dir / "check_service.sh").write_text(
        "#!/bin/bash\n"
        "set -e\n"
        "echo \"启动器状态:\"\n"
        "curl -fsS http://127.0.0.1:7860/health && echo\n"
        "echo \"TTS状态:\"\n"
        "curl -fsS http://127.0.0.1:8000/health && echo\n",
        encoding="utf-8",
        newline="\n",
    )
    (deploy_dir / "镜像部署说明.txt").write_text(
        f"云祺TTS云端 {version} 部署说明\n\n"
        "1. 将本目录放到 /workspace/ailive-cloud。\n"
        "2. 使用已有的 ailive-tts Python环境执行 deploy/install_or_update.sh。\n"
        "3. 执行 deploy/start_tts_launcher.sh，启动器端口为7860。\n"
        "4. 在启动器页面点击“启动Qwen3TTS API”，服务端口为8000。\n"
        "5. 客户端只填写实例公网IP和端口8000。\n"
        "6. 模型目录固定为 /workspace/models/Qwen3-TTS-12Hz-1.7B-Base。\n"
        "7. 平台镜像快捷入口需要映射启动器端口7860。\n",
        encoding="utf-8",
    )
    for shell_script in deploy_dir.glob("*.sh"):
        shell_script.chmod(0o755)

    manifest = {
        "version": version,
        "service": "Qwen3-TTS-12Hz-1.7B-Base",
        "launcher_port": 7860,
        "tts_port": 8000,
        "model_path": "/workspace/models/Qwen3-TTS-12Hz-1.7B-Base",
        "authentication": "disabled-for-private-instance",
    }
    (release_root / "version.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    archive = Path(shutil.make_archive(str(release_root), "zip", release_root))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256.txt").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    return archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建云祺TTS云端镜像发布包")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--version", default="v0.9.0")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    print(build_cloud_release(arguments.project_root, arguments.version))
