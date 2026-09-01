$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
$python = Join-Path $workspace ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "没有找到 .venv，请先按照 README 安装依赖。"
}

$env:AILIVE_BACKEND = "mock"
$env:AILIVE_DATA_DIR = Join-Path $workspace "data"
Set-Location -LiteralPath $workspace
& $python -m uvicorn ailive.server.app:app --host 127.0.0.1 --port 8000
