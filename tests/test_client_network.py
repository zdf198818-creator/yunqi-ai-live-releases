import json
import unittest

from ailive.client.network import TTSWorker, server_url_from_parts, websocket_url
from ailive.domain import ScriptLine


class FakeSocket:
    def __init__(self, messages: list[str | bytes]) -> None:
        self.messages = iter(messages)
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self) -> str | bytes:
        return next(self.messages)


class ClientNetworkTests(unittest.TestCase):
    def test_builds_server_url_from_address_and_port(self) -> None:
        self.assertEqual(
            server_url_from_parts("127.0.0.1", 18000),
            "http://127.0.0.1:18000",
        )
        self.assertEqual(
            server_url_from_parts("https://tts.example.com/", 443, "https"),
            "https://tts.example.com:443",
        )

    def test_builds_secure_websocket_url(self) -> None:
        self.assertEqual(
            websocket_url("https://tts.example.com/", "secret token"),
            "wss://tts.example.com/tts/stream?token=secret+token",
        )

    def test_collects_audio_and_pause_events_for_one_line(self) -> None:
        worker = TTSWorker("http://127.0.0.1:8000")
        fake_socket = FakeSocket(
            [
                json.dumps({"type": "line_started"}),
                json.dumps({"type": "audio_header"}),
                b"RIFF-audio",
                json.dumps({"type": "pause", "duration_ms": 500}),
                json.dumps({"type": "line_complete"}),
            ]
        )
        worker._socket = fake_socket  # type: ignore[assignment]
        line = ScriptLine("line-1", "voice-1", "第一段#500#", 1.0)

        generated = worker._synthesize_line(line)

        self.assertEqual(generated.line_id, "line-1")
        self.assertEqual(generated.tokens[0].wav_bytes, b"RIFF-audio")
        self.assertEqual(generated.tokens[1].duration_ms, 500)
        self.assertEqual(json.loads(fake_socket.sent[0])["line"]["speed"], 1.0)


if __name__ == "__main__":
    unittest.main()
