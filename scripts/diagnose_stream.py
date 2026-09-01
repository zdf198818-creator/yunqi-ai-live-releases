from __future__ import annotations

import json
import os
from urllib.parse import urlencode

import httpx
import websocket

base_url = "http://127.0.0.1:18000"
token = os.environ["AILIVE_DIAG_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}
voices = httpx.get(f"{base_url}/voices", headers=headers, timeout=15).json()
if not voices:
    raise RuntimeError("云端没有参考音色")

voice_id = str(voices[0]["reference_id"])
socket = websocket.create_connection(
    f"ws://127.0.0.1:18000/tts/stream?{urlencode({'token': token})}",
    timeout=180,
)
socket.send(
    json.dumps(
        {
            "type": "synthesize",
            "task_id": "diagnostic",
            "line": {
                "line_id": "diagnostic-line",
                "reference_id": voice_id,
                "text": "来，所有同学进入直播间#500#咱们开始今天的课程#1000#",
                "speed": 1.0,
                "language": "Chinese",
            },
        },
        ensure_ascii=False,
    )
)

events: list[str] = []
audio_sizes: list[int] = []
while True:
    message = socket.recv()
    if isinstance(message, bytes):
        audio_sizes.append(len(message))
        continue
    event = json.loads(message)
    event_type = str(event.get("type"))
    events.append(event_type)
    if event_type in {"line_complete", "error"}:
        if event_type == "error":
            print("云端错误:", event.get("message", "未知错误"))
        break

socket.close()
print("事件顺序:", " -> ".join(events))
print("音频片段数:", len(audio_sizes))
print("音频字节数:", audio_sizes)
