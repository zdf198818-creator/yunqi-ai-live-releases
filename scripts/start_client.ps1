$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
$python = Join-Path $workspace ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found."
}

Set-Location -LiteralPath $workspace
$env:PYTHONPATH = Join-Path $workspace "src"
try {
    & $python -m ailive.client.app
}
finally {
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
}
