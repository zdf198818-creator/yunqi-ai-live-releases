from __future__ import annotations

import io
import json
import queue
import time
import wave
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websocket
from PySide6.QtCore import QThread, Signal

from ailive.client.audio import AudioLine, AudioToken
from ailive.domain import ScriptLine


def server_url_from_parts(host: str, port: int, scheme: str = "http") -> str:
    normalized_host = host.strip().rstrip("/")
    for prefix in ("http://", "https://", "ws://", "wss://"):
        if normalized_host.lower().startswith(prefix):
            normalized_host = normalized_host[len(prefix) :]
            break
    if not normalized_host:
        raise ValueError("服务地址不能为空")
    if "/" in normalized_host:
        raise ValueError("服务地址只填写IP或域名，不要填写路径")
    return f"{scheme}://{normalized_host}:{port}"


def websocket_url(base_url: str, token: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.startswith("https://"):
        normalized = "wss://" + normalized.removeprefix("https://")
    elif normalized.startswith("http://"):
        normalized = "ws://" + normalized.removeprefix("http://")
    elif not normalized.startswith(("ws://", "wss://")):
        normalized = "ws://" + normalized
    query = "?" + urlencode({"token": token}) if token else ""
    return f"{normalized}/tts/stream{query}"


def list_voices(base_url: str, token: str = "") -> list[dict[str, object]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.get(f"{base_url.rstrip('/')}/voices", headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()


def upload_voice(
    base_url: str,
    name: str,
    reference_text: str,
    audio_path: Path,
    token: str = "",
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with audio_path.open("rb") as audio:
        response = httpx.post(
            f"{base_url.rstrip('/')}/voices",
            headers=headers,
            data={"name": name, "reference_text": reference_text},
            files={"audio": (audio_path.name, audio, "application/octet-stream")},
            timeout=60,
        )
    response.raise_for_status()
    return response.json()


class TTSWorker(QThread):
    connected = Signal()
    disconnected = Signal(str)
    lineReady = Signal(object)
    lineRetry = Signal(str, int, str)
    lineError = Signal(str, str)

    def __init__(
        self, base_url: str, token: str = "", max_attempts: int = 1
    ) -> None:
        super().__init__()
        self.ws_url = websocket_url(base_url, token)
        self.max_attempts = max(1, int(max_attempts))
        self._jobs: queue.Queue[ScriptLine | None] = queue.Queue()
        self._running = True
        self._socket: websocket.WebSocket | None = None

    def submit(self, line: ScriptLine) -> None:
        self._jobs.put(line)

    def shutdown(self) -> None:
        self._running = False
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
        self._jobs.put(None)
        if self._socket is not None:
            self._socket.close()

    def run(self) -> None:
        while self._running:
            line = self._jobs.get()
            if line is None:
                break
            for attempt in range(1, self.max_attempts + 1):
                if not self._running:
                    break
                try:
                    self._ensure_connection()
                    generated = self._synthesize_line(line)
                    if self._running:
                        self.lineReady.emit(generated)
                    break
                except Exception as error:  # noqa: BLE001 - worker reports failures
                    self._close_socket()
                    if not self._running:
                        break
                    if attempt < self.max_attempts:
                        self.lineRetry.emit(line.line_id, attempt + 1, str(error))
                        self.msleep(250 * attempt)
                        continue
                    self.lineError.emit(line.line_id, str(error))
        self._close_socket()

    def _ensure_connection(self) -> None:
        if self._socket is not None and self._socket.connected:
            return
        self._socket = websocket.create_connection(self.ws_url, timeout=90)
        self.connected.emit()

    def _synthesize_line(self, line: ScriptLine) -> AudioLine:
        if self._socket is None:
            raise RuntimeError("TTS连接尚未建立")
        started_at = time.monotonic()
        self._socket.send(
            json.dumps(
                {
                    "type": "synthesize",
                    "task_id": f"task-{line.line_id}",
                    "line": line.to_dict(),
                },
                ensure_ascii=False,
            )
        )

        tokens: list[AudioToken] = []
        expecting_audio = False
        while self._running:
            message = self._socket.recv()
            if isinstance(message, bytes):
                if not expecting_audio:
                    raise RuntimeError("收到未声明的音频数据")
                tokens.append(AudioToken(kind="audio", wav_bytes=message))
                expecting_audio = False
                continue

            event = json.loads(message)
            event_type = event.get("type")
            if event_type == "audio_header":
                expecting_audio = True
            elif event_type == "pause":
                tokens.append(AudioToken(kind="pause", duration_ms=int(event["duration_ms"])))
            elif event_type == "line_complete":
                if expecting_audio:
                    raise RuntimeError("音频头之后缺少二进制数据")
                generation_seconds = time.monotonic() - started_at
                audio_seconds = 0.0
                for token in tokens:
                    if token.kind == "pause":
                        audio_seconds += token.duration_ms / 1000.0
                    else:
                        try:
                            with wave.open(io.BytesIO(token.wav_bytes), "rb") as source:
                                audio_seconds += (
                                    source.getnframes() / source.getframerate()
                                )
                        except (EOFError, wave.Error):
                            # Keep receiving compatible with test/mocked or
                            # malformed payloads; playback will surface an
                            # actual decode error if the bytes are used.
                            pass
                return AudioLine(
                    line_id=line.line_id,
                    tokens=tokens,
                    generation_seconds=generation_seconds,
                    audio_seconds=audio_seconds,
                )
            elif event_type == "error":
                raise RuntimeError(str(event.get("message", "TTS生成失败")))
        raise RuntimeError("TTS工作线程已停止")

    def _close_socket(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
                self.disconnected.emit("连接已关闭")


class VoiceSyncWorker(QThread):
    """Prepare selected local reference voices without blocking the Qt UI."""

    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        base_url: str,
        token: str,
        profiles: list[dict[str, object]],
        cached_mapping: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.base_url = base_url
        self.token = token
        self.profiles = [dict(profile) for profile in profiles]
        self.cached_mapping = dict(cached_mapping or {})

    @staticmethod
    def _matching_remote(
        profile: dict[str, object],
        remote_profiles: list[dict[str, object]],
    ) -> dict[str, object] | None:
        reference_text = str(profile.get("reference_text", "")).strip()
        accepted_names = {
            str(profile.get("name", "")),
            str(profile.get("cloud_name", "")),
        }
        exact = next(
            (
                remote
                for remote in remote_profiles
                if str(remote.get("name", "")) in accepted_names
                and str(remote.get("reference_text", "")).strip() == reference_text
            ),
            None,
        )
        if exact is not None:
            return exact

        # Folder names and absolute install paths may change on another PC.
        # Reuse by reference text only when that identifies exactly one voice.
        same_text = [
            remote
            for remote in remote_profiles
            if reference_text
            and str(remote.get("reference_text", "")).strip() == reference_text
        ]
        return same_text[0] if len(same_text) == 1 else None

    def run(self) -> None:
        try:
            remote_profiles = list_voices(self.base_url, self.token)
            remote_ids = {
                str(profile.get("reference_id", "")) for profile in remote_profiles
            }
            mapping: dict[str, str] = {}
            total = len(self.profiles)
            for index, profile in enumerate(self.profiles, start=1):
                if self.isInterruptionRequested():
                    self.failed.emit("已取消准备参考音色")
                    return
                local_id = str(profile.get("reference_id", ""))
                name = str(profile.get("name", "参考音色"))
                cached_remote_id = self.cached_mapping.get(local_id, "")
                if cached_remote_id and cached_remote_id in remote_ids:
                    mapping[local_id] = cached_remote_id
                    self.progress.emit(index, total, f"已缓存：{name}")
                    continue

                existing = self._matching_remote(profile, remote_profiles)
                if existing is not None:
                    remote_id = str(existing["reference_id"])
                    mapping[local_id] = remote_id
                    self.progress.emit(index, total, f"云端已有：{name}")
                    continue

                uploaded = upload_voice(
                    self.base_url,
                    str(profile.get("cloud_name") or name),
                    str(profile.get("reference_text", "")).strip(),
                    Path(str(profile["audio_path"])),
                    self.token,
                )
                remote_id = str(uploaded["reference_id"])
                mapping[local_id] = remote_id
                remote_ids.add(remote_id)
                remote_profiles.append(uploaded)
                self.progress.emit(index, total, f"上传完成：{name}")
            self.completed.emit(mapping)
        except Exception as error:  # noqa: BLE001 - worker reports network/file errors
            self.failed.emit(str(error))
