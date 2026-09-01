import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import ailive.server.app as server_app
from ailive.server.backends import MockTTSBackend
from ailive.server.config import Settings
from ailive.server.repository import VoiceRepository


def test_voice_upload_and_websocket_synthesis() -> None:
    with tempfile.TemporaryDirectory() as directory:
        server_app.repository = VoiceRepository(Path(directory))
        server_app.backend = MockTTSBackend()
        client = TestClient(server_app.app)

        response = client.post(
            "/voices",
            data={"name": "测试音色", "reference_text": "这是参考文本。"},
            files={"audio": ("reference.wav", b"RIFF-test", "audio/wav")},
        )
        assert response.status_code == 201
        reference_id = response.json()["reference_id"]

        with client.websocket_connect("/tts/stream") as websocket:
            websocket.send_json(
                {
                    "type": "synthesize",
                    "task_id": "task-1",
                    "line": {
                        "line_id": "line-1",
                        "reference_id": reference_id,
                        "text": "第一段#500#第二段",
                        "speed": 1.0,
                        "language": "Chinese",
                    },
                }
            )

            assert websocket.receive_json()["type"] == "line_started"
            first_header = websocket.receive_json()
            assert first_header["type"] == "audio_header"
            assert websocket.receive_bytes().startswith(b"RIFF")
            assert websocket.receive_json() == {
                "type": "pause",
                "token_index": 1,
                "duration_ms": 500,
            }
            second_header = websocket.receive_json()
            assert second_header["type"] == "audio_header"
            assert websocket.receive_bytes().startswith(b"RIFF")
            assert websocket.receive_json()["type"] == "line_complete"


def test_voice_routes_require_bearer_token() -> None:
    original_settings = server_app.settings
    original_repository = server_app.repository
    with tempfile.TemporaryDirectory() as directory:
        try:
            data_dir = Path(directory)
            server_app.settings = Settings(
                backend="mock",
                model_path="unused",
                data_dir=data_dir,
                api_token="test-secret",
                ffmpeg="ffmpeg",
            )
            server_app.repository = VoiceRepository(data_dir)
            client = TestClient(server_app.app)

            assert client.get("/voices").status_code == 401
            assert (
                client.get("/voices", headers={"Authorization": "Bearer test-secret"}).status_code
                == 200
            )
        finally:
            server_app.settings = original_settings
            server_app.repository = original_repository
