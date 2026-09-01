from __future__ import annotations

import json

import httpx
import websocket

BASE_URL = "http://127.0.0.1:8765"


def main() -> None:
    response = httpx.post(
        f"{BASE_URL}/voices",
        data={"name": "端到端测试音色", "reference_text": "这是参考音频的准确文本。"},
        files={"audio": ("reference.wav", b"RIFF-smoke", "audio/wav")},
        timeout=15,
    )
    response.raise_for_status()
    reference_id = response.json()["reference_id"]

    connection = websocket.create_connection("ws://127.0.0.1:8765/tts/stream", timeout=30)
    try:
        connection.send(
            json.dumps(
                {
                    "type": "synthesize",
                    "task_id": "smoke-task",
                    "line": {
                        "line_id": "smoke-line",
                        "reference_id": reference_id,
                        "text": "第一段#300#第二段#500#",
                        "speed": 1.2,
                        "language": "Chinese",
                    },
                },
                ensure_ascii=False,
            )
        )
        event_types: list[str] = []
        audio_bytes = 0
        while True:
            message = connection.recv()
            if isinstance(message, bytes):
                audio_bytes += len(message)
                continue
            event = json.loads(message)
            event_types.append(event["type"])
            if event["type"] == "line_complete":
                break
        print(
            json.dumps(
                {
                    "reference_id": reference_id,
                    "events": event_types,
                    "audio_bytes": audio_bytes,
                },
                ensure_ascii=False,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
