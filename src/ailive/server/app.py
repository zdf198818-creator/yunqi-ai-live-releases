from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from ailive import __version__
from ailive.domain import ScriptLine
from ailive.parser import parse_script
from ailive.server.audio import change_speed, pcm16_wav
from ailive.server.backends import build_backend
from ailive.server.config import Settings
from ailive.server.repository import VoiceRepository

settings = Settings.from_env()
repository = VoiceRepository(settings.data_dir)
backend = build_backend(settings.backend, settings.model_path)
# Qwen3-TTS holds one model on one GPU.  Multiple WebSocket clients (the main
# script and an interjection) must not enter inference at the same time: doing
# so makes both requests slower and can destabilize the long-lived connection.
logger = logging.getLogger("uvicorn.error")
inference_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.environ.get("AILIVE_USE_OPTIMIZED_QWEN", "0") != "1":
        yield
        return
    voices = repository.list_all()
    if not voices:
        logger.warning("[WARMUP] skipped: no reference voice")
    else:
        logger.info("[WARMUP] compiling optimized Qwen3-TTS pipeline...")
        started_at = time.perf_counter()
        samples, sample_rate = await asyncio.to_thread(
            backend.synthesize, "系统预热。", voices[0], "Chinese", "normal"
        )
        duration = len(samples) / sample_rate
        elapsed = time.perf_counter() - started_at
        logger.info(
            "[WARMUP] complete generation=%.2fs audio=%.2fs RTF=%.2f",
            elapsed,
            duration,
            elapsed / duration if duration else 0.0,
        )
    yield


app = FastAPI(title="AI Live Voice TTS", version=__version__, lifespan=lifespan)


def require_api_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if settings.api_token == "change-me":
        return
    expected = f"Bearer {settings.api_token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="未授权")


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "backend": backend.name, "ready": True}


@app.get("/voices")
def list_voices(
    _authorized: Annotated[None, Depends(require_api_token)],
) -> list[dict[str, object]]:
    return [voice.to_dict() for voice in repository.list_all()]


@app.post("/voices", status_code=201)
async def create_voice(
    name: Annotated[str, Form(min_length=1, max_length=100)],
    reference_text: Annotated[str, Form(min_length=1)],
    audio: Annotated[UploadFile, File()],
    _authorized: Annotated[None, Depends(require_api_token)],
) -> dict[str, object]:
    content = await audio.read()
    if not content:
        raise HTTPException(status_code=400, detail="参考音频不能为空")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="参考音频不能超过50MB")
    profile = repository.create(
        name=name,
        reference_text=reference_text,
        suffix=Path(audio.filename or "reference.wav").suffix,
        content=content,
    )
    return profile.to_dict()


@app.websocket("/tts/stream")
async def tts_stream(websocket: WebSocket) -> None:
    supplied_token = websocket.query_params.get("token", "")
    if settings.api_token != "change-me" and supplied_token != settings.api_token:
        await websocket.close(code=4401, reason="未授权")
        return
    await websocket.accept()

    try:
        while True:
            request = await websocket.receive_json()
            if request.get("type") != "synthesize":
                await websocket.send_json({"type": "error", "message": "不支持的消息类型"})
                continue

            task_id = str(request.get("task_id", ""))
            try:
                line = ScriptLine.from_dict(request["line"])
                voice = repository.get(line.reference_id)
                if voice is None:
                    raise ValueError(f"参考音色不存在: {line.reference_id}")
                tokens = parse_script(line.text)
                started_at = time.perf_counter()
                generated_audio_seconds = 0.0
                logger.info(
                    "[REQ] task=%s line=%s chars=%d segments=%d",
                    task_id,
                    line.line_id,
                    len(line.text),
                    sum(token.kind != "pause" for token in tokens),
                )
                await websocket.send_json(
                    {"type": "line_started", "task_id": task_id, "line_id": line.line_id}
                )

                for index, token in enumerate(tokens):
                    if token.kind == "pause":
                        await websocket.send_json(
                            {
                                "type": "pause",
                                "token_index": index,
                                "duration_ms": token.duration_ms,
                            }
                        )
                        continue

                    async with inference_lock:
                        samples, sample_rate = await asyncio.to_thread(
                            backend.synthesize,
                            token.text,
                            voice,
                            line.language,
                            line.randomness,
                        )
                    wav_bytes = pcm16_wav(samples, sample_rate)
                    wav_bytes = await asyncio.to_thread(
                        change_speed,
                        wav_bytes,
                        line.speed,
                        settings.ffmpeg,
                        settings.backend == "mock",
                    )
                    # PCM16 mono WAV: the data payload begins after the
                    # standard 44-byte header and contains two bytes/sample.
                    generated_audio_seconds += max(0, len(wav_bytes) - 44) / (
                        2 * sample_rate
                    )
                    await websocket.send_json(
                        {
                            "type": "audio_header",
                            "token_index": index,
                            "format": "wav",
                            "byte_length": len(wav_bytes),
                        }
                    )
                    await websocket.send_bytes(wav_bytes)

                await websocket.send_json(
                    {"type": "line_complete", "task_id": task_id, "line_id": line.line_id}
                )
                elapsed = time.perf_counter() - started_at
                rtf = elapsed / generated_audio_seconds if generated_audio_seconds else 0.0
                logger.info(
                    "[DONE] task=%s line=%s generation=%.2fs audio=%.2fs RTF=%.2f",
                    task_id,
                    line.line_id,
                    elapsed,
                    generated_audio_seconds,
                    rtf,
                )
            except Exception as error:
                logger.exception("[ERROR] task=%s generation failed", task_id)
                await websocket.send_json(
                    {"type": "error", "task_id": task_id, "message": str(error)}
                )
    except WebSocketDisconnect:
        return


def run() -> None:
    import uvicorn

    uvicorn.run("ailive.server.app:app", host="0.0.0.0", port=8000)
