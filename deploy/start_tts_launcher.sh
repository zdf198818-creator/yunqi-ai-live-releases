#!/bin/bash
set -u

LAUNCHER_PYTHON="/usr/local/miniconda3/envs/ailive-tts/bin/python"
LAUNCHER_LOG="/workspace/ailive-launcher.log"

if curl -fsS http://127.0.0.1:7860/health >/dev/null 2>&1; then
    exit 0
fi

export AILIVE_WORKSPACE="/workspace"
export AILIVE_LAUNCHER_PORT="7860"
export AILIVE_TTS_PORT="8000"
export AILIVE_MODEL_PATH="/workspace/models/Qwen3-TTS-12Hz-1.7B-Base"
export AILIVE_DATA_DIR="/workspace/ailive-data"
export AILIVE_API_TOKEN="change-me"

nohup "$LAUNCHER_PYTHON" -m ailive.launcher >"$LAUNCHER_LOG" 2>&1 &
