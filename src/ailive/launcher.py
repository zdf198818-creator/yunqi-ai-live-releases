from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse


TTS_PORT = int(os.getenv("AILIVE_TTS_PORT", "8000"))
LAUNCHER_PORT = int(os.getenv("AILIVE_LAUNCHER_PORT", "7860"))
WORKSPACE = Path(os.getenv("AILIVE_WORKSPACE", "/workspace"))
LOG_PATH = Path(os.getenv("AILIVE_SERVER_LOG", str(WORKSPACE / "ailive-server.log")))
PID_PATH = Path(os.getenv("AILIVE_SERVER_PID", str(WORKSPACE / "ailive-server.pid")))
MODEL_PATH = os.getenv(
    "AILIVE_MODEL_PATH", "/workspace/models/Qwen3-TTS-12Hz-1.7B-Base"
)

app = FastAPI(title="云祺 TTS 启动器", version="1.0.0")
_process_lock = threading.Lock()
_process: subprocess.Popen[bytes] | None = None


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _status() -> dict[str, object]:
    pid = _process.pid if _process is not None and _process.poll() is None else _read_pid()
    alive = _pid_is_alive(pid)
    ready = _port_is_open(TTS_PORT)
    return {
        "service": "Qwen3TTS API",
        "pid": pid if alive else None,
        "running": alive,
        "ready": ready,
        "port": TTS_PORT,
        "model": MODEL_PATH,
    }


def _server_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AILIVE_BACKEND": "qwen",
            "AILIVE_MODEL_PATH": MODEL_PATH,
            "AILIVE_DATA_DIR": os.getenv("AILIVE_DATA_DIR", "/workspace/ailive-data"),
            # The desktop client connects by public IP and port only.
            "AILIVE_API_TOKEN": "change-me",
            "AILIVE_USE_OPTIMIZED_QWEN": os.getenv(
                "AILIVE_USE_OPTIMIZED_QWEN", "1"
            ),
            "AILIVE_USE_TORCH_COMPILE": os.getenv(
                "AILIVE_USE_TORCH_COMPILE", "0"
            ),
            "AILIVE_COMPILE_MODE": os.getenv(
                "AILIVE_COMPILE_MODE", "reduce-overhead"
            ),
        }
    )
    return env


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "launcher_port": LAUNCHER_PORT}


@app.get("/api/status")
def api_status() -> dict[str, object]:
    return _status()


@app.post("/api/start")
def api_start() -> dict[str, object]:
    global _process
    with _process_lock:
        current = _status()
        if current["running"]:
            return current
        if _port_is_open(TTS_PORT):
            raise HTTPException(status_code=409, detail=f"端口 {TTS_PORT} 已被占用")
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_file = LOG_PATH.open("ab", buffering=0)
        try:
            _process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from ailive.server.app import run; run()",
                ],
                cwd=str(WORKSPACE),
                env=_server_environment(),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise
        PID_PATH.write_text(str(_process.pid), encoding="utf-8")
        return _status()


@app.post("/api/stop")
def api_stop() -> dict[str, object]:
    global _process
    with _process_lock:
        pid = _process.pid if _process is not None and _process.poll() is None else _read_pid()
        if _pid_is_alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        _process = None
        PID_PATH.unlink(missing_ok=True)
        return _status()


@app.get("/api/log", response_class=PlainTextResponse)
def api_log() -> str:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "暂无日志"
    return "\n".join(lines[-220:]) or "暂无日志"


def run() -> None:
    import uvicorn

    uvicorn.run("ailive.launcher:app", host="0.0.0.0", port=LAUNCHER_PORT)


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>云祺 TTS 启动器</title>
  <style>
    :root{--blue:#467bf4;--ink:#111827;--muted:#64748b;--line:#e5eaf2;--bg:#f4f6fb;--ok:#10b981}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "Microsoft YaHei",sans-serif}
    .wrap{max-width:1080px;margin:28px auto;padding:0 18px}.hero,.card{background:#fff;border:1px solid var(--line);border-radius:13px;box-shadow:0 4px 18px #27364d10}
    .hero{padding:18px 22px;margin-bottom:18px}.hero h1{font-size:23px;margin:0 0 6px}.hero p{margin:0;color:var(--muted)}
    .card{padding:16px}.tabs{display:flex;gap:8px;margin-bottom:14px}.tab{border:0;border-radius:7px;padding:8px 17px;font-weight:700}.tab.active{background:var(--blue);color:#fff}.tab.inactive{background:#e8ecf3;color:#334155}
    .service{display:grid;grid-template-columns:1fr auto;align-items:center;padding:16px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
    .service strong{font-size:18px}.btn{border:0;border-radius:7px;background:var(--blue);color:#fff;padding:9px 22px;font-weight:700;cursor:pointer}.btn.stop{background:#ef4444}.btn:disabled{opacity:.55;cursor:wait}
    .status{padding:15px 0 10px}.status b{color:var(--ok)}.meta{font-size:13px;color:var(--muted);margin-top:3px}
    pre{min-height:270px;max-height:470px;overflow:auto;margin:8px 0 0;padding:15px;border-radius:8px;background:#071225;color:#edf7ff;font:13px/1.45 Consolas,monospace;white-space:pre-wrap}
  </style>
</head>
<body><main class="wrap">
  <section class="hero"><h1>云祺 TTS 启动器</h1><p>本页面用于手动启动 Qwen3TTS API，并查看服务状态和实时日志。服务就绪后，客户端使用实例公网 IP 和端口 8000 连接。</p></section>
  <section class="card">
    <div class="tabs"><button class="tab active">API</button><button class="tab inactive" disabled>WebUI</button></div>
    <div class="service"><strong>Qwen3TTS API</strong><button id="action" class="btn" onclick="toggleService()">启动</button></div>
    <div class="status"><div>当前状态：<b id="state">正在读取...</b></div><div class="meta" id="meta">端口 8000</div></div>
    <pre id="log">正在读取日志...</pre>
  </section>
</main>
<script>
let running=false;
async function refresh(){
  try{
    const s=await fetch('/api/status',{cache:'no-store'}).then(r=>r.json());
    running=!!s.running;
    document.querySelector('#state').textContent=s.ready?'已启动，可连接':(s.running?'正在加载模型':'未启动');
    document.querySelector('#meta').textContent=`端口 ${s.port} ${s.ready?'已就绪':'空闲或加载中'}${s.pid?' | PID: '+s.pid:''}`;
    const b=document.querySelector('#action'); b.textContent=running?'停止':'启动'; b.className=running?'btn stop':'btn'; b.disabled=false;
    document.querySelector('#log').textContent=await fetch('/api/log',{cache:'no-store'}).then(r=>r.text());
    const pre=document.querySelector('#log'); pre.scrollTop=pre.scrollHeight;
  }catch(e){document.querySelector('#state').textContent='启动器连接异常';}
}
async function toggleService(){
  const b=document.querySelector('#action'); b.disabled=true; b.textContent=running?'正在停止':'正在启动';
  const path=running?'/api/stop':'/api/start';
  const r=await fetch(path,{method:'POST'}); if(!r.ok){alert(await r.text());} await refresh();
}
refresh(); setInterval(refresh,2000);
</script></body></html>"""


if __name__ == "__main__":
    run()
