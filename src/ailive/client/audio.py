from __future__ import annotations

import io
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSink, QMediaDevices, QtAudio


@dataclass(frozen=True, slots=True)
class AudioToken:
    kind: Literal["audio", "pause"]
    wav_bytes: bytes = b""
    duration_ms: int = 0


@dataclass(slots=True)
class AudioLine:
    line_id: str
    tokens: list[AudioToken] = field(default_factory=list)
    generation_seconds: float = 0.0
    audio_seconds: float = 0.0


def decode_pcm16_wav(wav_bytes: bytes) -> tuple[bytes, QAudioFormat]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("只支持未压缩WAV音频")
        if source.getsampwidth() != 2:
            raise ValueError("只支持16位PCM WAV音频")
        audio_format = QAudioFormat()
        audio_format.setSampleRate(source.getframerate())
        audio_format.setChannelCount(source.getnchannels())
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        return source.readframes(source.getnframes()), audio_format


def convert_pcm16_wav(wav_bytes: bytes, target_format: QAudioFormat) -> bytes:
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("只支持未压缩16位PCM WAV音频")
        source_rate = source.getframerate()
        source_channels = source.getnchannels()
        source_frames = source.getnframes()
        samples = np.frombuffer(source.readframes(source_frames), dtype="<i2").reshape(
            -1, source_channels
        )

    target_rate = target_format.sampleRate()
    target_channels = target_format.channelCount()
    if source_rate != target_rate:
        target_frames = max(1, round(len(samples) * target_rate / source_rate))
        old_axis = np.linspace(0.0, 1.0, len(samples), endpoint=False)
        new_axis = np.linspace(0.0, 1.0, target_frames, endpoint=False)
        samples = np.column_stack(
            [
                np.interp(new_axis, old_axis, samples[:, channel])
                for channel in range(source_channels)
            ]
        )

    if source_channels == 1 and target_channels > 1:
        samples = np.repeat(samples, target_channels, axis=1)
    elif source_channels > 1 and target_channels == 1:
        samples = samples.mean(axis=1, keepdims=True)
    elif source_channels != target_channels:
        raise ValueError(f"无法从{source_channels}声道转换为{target_channels}声道")

    samples = np.clip(samples, -32768, 32767)
    sample_format = target_format.sampleFormat()
    if sample_format == QAudioFormat.SampleFormat.Int16:
        return samples.astype("<i2").tobytes()
    if sample_format == QAudioFormat.SampleFormat.Int32:
        return (samples.astype("<i4") << 16).tobytes()
    if sample_format == QAudioFormat.SampleFormat.Float:
        return (samples.astype("<f4") / 32768.0).tobytes()
    if sample_format == QAudioFormat.SampleFormat.UInt8:
        return ((samples + 32768.0) / 256.0).astype("u1").tobytes()
    raise ValueError("输出设备返回了未知采样格式")


def silence_for_format(audio_format: QAudioFormat, duration_ms: int) -> bytes:
    frame_count = round(audio_format.sampleRate() * duration_ms / 1000)
    sample_count = frame_count * audio_format.channelCount()
    sample_format = audio_format.sampleFormat()
    if sample_format == QAudioFormat.SampleFormat.UInt8:
        return bytes([128]) * sample_count
    if sample_format == QAudioFormat.SampleFormat.Int16:
        return bytes(sample_count * 2)
    if sample_format in {
        QAudioFormat.SampleFormat.Int32,
        QAudioFormat.SampleFormat.Float,
    }:
        return bytes(sample_count * 4)
    raise ValueError("输出设备返回了未知采样格式")


def render_line_tokens(tokens: list[AudioToken], target_format: QAudioFormat) -> bytes:
    chunks: list[bytes] = []
    for token in tokens:
        if token.kind == "pause":
            chunks.append(silence_for_format(target_format, token.duration_ms))
        else:
            chunks.append(convert_pcm16_wav(token.wav_bytes, target_format))
    return b"".join(chunks)


def formats_match(left: QAudioFormat, right: QAudioFormat) -> bool:
    return (
        left.sampleRate() == right.sampleRate()
        and left.channelCount() == right.channelCount()
        and left.sampleFormat() == right.sampleFormat()
    )


class AudioQueuePlayer(QObject):
    lineStarted = Signal(str)
    lineFinished = Signal(str)
    sentenceEndPaused = Signal()
    bufferDepthChanged = Signal(int)
    buffering = Signal()
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: deque[AudioLine] = deque()
        self._priority_line_ids: set[str] = set()
        self._current_line: AudioLine | None = None
        self._token_index = 0
        self._audio_sink: QAudioSink | None = None
        self._audio_buffer: QBuffer | None = None
        self._batch_lines: deque[tuple[AudioLine, int]] = deque()
        self._batch_active = False
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.timeout.connect(self._advance_batch_line)
        self._output_device = QMediaDevices.defaultAudioOutput()
        self._active = False
        self._paused = False
        self._pause_after_line = False
        self._cleanup_in_progress = False
        self._timer_generation = 0
        self._next_line_timer = QTimer(self)
        self._next_line_timer.setSingleShot(True)
        self._next_line_timer.timeout.connect(self._begin_next_line)
        self._pause_timer = QTimer(self)
        self._pause_timer.setSingleShot(True)
        self._pause_timer.timeout.connect(self._on_pause_elapsed)
        self._stream_device: QIODevice | None = None
        self._stream_pcm = b""
        self._stream_offset = 0
        self._stream_duration = 0.0
        self._stream_started_at = 0.0
        self._stream_line_started = False
        self._reported_buffering = False
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(10)
        self._stream_timer.timeout.connect(self._pump_stream)

    @property
    def buffered_line_count(self) -> int:
        return (
            len(self._queue)
            + len(self._batch_lines)
            + (1 if self._current_line is not None else 0)
        )

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def has_current_line(self) -> bool:
        return self._current_line is not None

    def set_output_device(self, device: QAudioDevice) -> None:
        self._output_device = device

    def enqueue(self, line: AudioLine) -> None:
        self._queue.append(line)
        self._reported_buffering = False
        self.bufferDepthChanged.emit(self.buffered_line_count)
        if self._active and not self._paused:
            self._ensure_stream()
            self._stream_timer.start()

    def enqueue_priority(self, line: AudioLine) -> None:
        """Play *line* immediately after the sentence that is already playing."""
        insert_at = 0
        for queued in self._queue:
            if queued.line_id not in self._priority_line_ids:
                break
            insert_at += 1
        self._queue.insert(insert_at, line)
        self._priority_line_ids.add(line.line_id)
        self._reported_buffering = False
        self.bufferDepthChanged.emit(self.buffered_line_count)
        if self._active and not self._paused:
            self._ensure_stream()
            self._stream_timer.start()

    def remove_queued_lines(self, line_ids: set[str]) -> int:
        """Remove generated lines that have not started playing yet."""
        before = len(self._queue)
        self._queue = deque(
            line for line in self._queue if line.line_id not in line_ids
        )
        self._priority_line_ids.difference_update(line_ids)
        removed = before - len(self._queue)
        if removed:
            self.bufferDepthChanged.emit(self.buffered_line_count)
        return removed

    def start(self) -> None:
        self._active = True
        self._paused = False
        self._ensure_stream()
        self._stream_timer.start()
        self._pump_stream()

    def request_pause_after_current_line(self) -> None:
        if self._current_line is None:
            raise RuntimeError("当前没有正在播放的话术")
        self._pause_after_line = True

    def cancel_pause_after_current_line(self) -> None:
        self._pause_after_line = False

    def resume(self) -> None:
        if not self._paused:
            raise RuntimeError("当前不在句末暂停状态")
        self._paused = False
        self._active = True
        self._ensure_stream()
        self._stream_timer.start()
        self._pump_stream()

    def reset(self) -> None:
        self._timer_generation += 1
        self._next_line_timer.stop()
        self._batch_timer.stop()
        self._batch_lines.clear()
        self._batch_active = False
        self._pause_timer.stop()
        self._stream_timer.stop()
        self._active = False
        self._paused = False
        self._pause_after_line = False
        self._queue.clear()
        self._priority_line_ids.clear()
        self._current_line = None
        self._token_index = 0
        self._stream_pcm = b""
        self._stream_offset = 0
        self._stream_duration = 0.0
        self._stream_started_at = 0.0
        self._stream_line_started = False
        self._reported_buffering = False
        self._stream_device = None
        self._dispose_audio()
        self.bufferDepthChanged.emit(0)

    def _ensure_stream(self) -> None:
        if self._audio_sink is not None and self._stream_device is not None:
            return
        self._dispose_audio()
        target_format = self._output_device.preferredFormat()
        self._audio_sink = QAudioSink(self._output_device, target_format, self)
        self._stream_device = self._audio_sink.start()
        if self._stream_device is None:
            raise RuntimeError("无法打开持续音频输出设备")

    def _pump_stream(self) -> None:
        if not self._active or self._paused:
            return
        try:
            self._ensure_stream()
            if self._current_line is None:
                if not self._queue:
                    if not self._reported_buffering:
                        self._reported_buffering = True
                        self.buffering.emit()
                    return
                self._reported_buffering = False
                self._current_line = self._queue.popleft()
                self._priority_line_ids.discard(self._current_line.line_id)
                if not any(
                    token.kind == "audio" for token in self._current_line.tokens
                ):
                    self._token_index = 0
                    self.lineStarted.emit(self._current_line.line_id)
                    self.bufferDepthChanged.emit(self.buffered_line_count)
                    self._play_next_token()
                    return
                target_format = self._audio_sink.format()
                self._stream_pcm = render_line_tokens(
                    self._current_line.tokens, target_format
                )
                bytes_per_sample = {
                    QAudioFormat.SampleFormat.UInt8: 1,
                    QAudioFormat.SampleFormat.Int16: 2,
                    QAudioFormat.SampleFormat.Int32: 4,
                    QAudioFormat.SampleFormat.Float: 4,
                }[target_format.sampleFormat()]
                bytes_per_second = (
                    target_format.sampleRate()
                    * target_format.channelCount()
                    * bytes_per_sample
                )
                self._stream_duration = (
                    len(self._stream_pcm) / bytes_per_second if bytes_per_second else 0.0
                )
                self._stream_offset = 0
                self._stream_started_at = 0.0
                self._stream_line_started = False
                self.lineStarted.emit(self._current_line.line_id)
                self.bufferDepthChanged.emit(self.buffered_line_count)

            if self._stream_device is None or self._audio_sink is None:
                return
            available = max(0, self._audio_sink.bytesFree())
            if available and self._stream_offset < len(self._stream_pcm):
                chunk = self._stream_pcm[
                    self._stream_offset : self._stream_offset + available
                ]
                written = self._stream_device.write(QByteArray(chunk))
                if written > 0:
                    if not self._stream_line_started:
                        self._stream_started_at = time.monotonic()
                        self._stream_line_started = True
                    self._stream_offset += int(written)

            if (
                self._stream_offset >= len(self._stream_pcm)
                and self._stream_line_started
                and time.monotonic() - self._stream_started_at
                >= self._stream_duration + 0.04
            ):
                self._finish_stream_line()
        except Exception as error:  # noqa: BLE001
            self.errorOccurred.emit(str(error))

    def _finish_stream_line(self) -> None:
        if self._current_line is None:
            return
        line_id = self._current_line.line_id
        self._current_line = None
        self._stream_pcm = b""
        self._stream_offset = 0
        self._stream_duration = 0.0
        self._stream_started_at = 0.0
        self._stream_line_started = False
        self.lineFinished.emit(line_id)
        self.bufferDepthChanged.emit(self.buffered_line_count)
        if self._pause_after_line:
            self._pause_after_line = False
            self._paused = True
            self.sentenceEndPaused.emit()
            return
        self._pump_stream()

    def _begin_next_line(self) -> None:
        if not self._active or self._paused or self._current_line is not None:
            return
        if not self._queue:
            self.buffering.emit()
            return
        # When several generated sentences are ready, play them through one
        # QAudioSink stream.  Reopening a Windows/Voicemeeter endpoint for
        # every sentence can make every stream after the first one silent.
        if len(self._queue) > 1 and all(
            any(token.kind == "audio" for token in line.tokens) for line in self._queue
        ):
            self._begin_audio_batch()
            return
        self._current_line = self._queue.popleft()
        self._priority_line_ids.discard(self._current_line.line_id)
        self._token_index = 0
        self.lineStarted.emit(self._current_line.line_id)
        self.bufferDepthChanged.emit(self.buffered_line_count)
        self._play_next_token()

    def _begin_audio_batch(self) -> None:
        try:
            target_format = self._output_device.preferredFormat()
            batch = list(self._queue)
            self._queue.clear()
            self._priority_line_ids.clear()
            pcm_chunks: list[bytes] = []
            durations: list[int] = []
            bytes_per_sample = {
                QAudioFormat.SampleFormat.UInt8: 1,
                QAudioFormat.SampleFormat.Int16: 2,
                QAudioFormat.SampleFormat.Int32: 4,
                QAudioFormat.SampleFormat.Float: 4,
            }[target_format.sampleFormat()]
            bytes_per_second = (
                target_format.sampleRate()
                * target_format.channelCount()
                * bytes_per_sample
            )
            gap = silence_for_format(target_format, 180)
            for index, line in enumerate(batch):
                pcm = render_line_tokens(line.tokens, target_format)
                pcm_chunks.append(pcm)
                duration_ms = max(1, round(len(pcm) * 1000 / bytes_per_second))
                durations.append(duration_ms)
                if index + 1 < len(batch):
                    pcm_chunks.append(gap)

            self._batch_lines = deque(zip(batch, durations, strict=True))
            self._batch_active = True
            self._current_line, first_duration = self._batch_lines.popleft()
            self._token_index = len(self._current_line.tokens)
            self.lineStarted.emit(self._current_line.line_id)
            self.bufferDepthChanged.emit(self.buffered_line_count)

            self._dispose_audio()
            self._audio_sink = QAudioSink(self._output_device, target_format, self)
            self._audio_sink.stateChanged.connect(self._on_audio_state_changed)
            self._audio_buffer = QBuffer(self)
            self._audio_buffer.setData(QByteArray(b"".join(pcm_chunks)))
            self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self._audio_sink.start(self._audio_buffer)
            self._batch_timer.start(first_duration + 180)
        except Exception as error:  # noqa: BLE001
            self.errorOccurred.emit(str(error))

    def _advance_batch_line(self) -> None:
        if self._current_line is None:
            return
        finished_id = self._current_line.line_id
        self._current_line = None
        self.lineFinished.emit(finished_id)
        if not self._batch_lines:
            self._batch_active = False
            self._dispose_audio()
            self.bufferDepthChanged.emit(self.buffered_line_count)
            self._next_line_timer.start(180)
            return
        self._current_line, duration_ms = self._batch_lines.popleft()
        self.lineStarted.emit(self._current_line.line_id)
        self.bufferDepthChanged.emit(self.buffered_line_count)
        # Advance every line, including the final line, from the PCM duration.
        # Some Windows/Voicemeeter endpoints do not reliably emit IdleState at
        # the end of a long stream, which previously stopped exactly at the
        # configured initial buffer size.
        self._batch_timer.start(duration_ms + (180 if self._batch_lines else 0))

    def _play_next_token(self) -> None:
        if self._current_line is None:
            return
        if self._token_index >= len(self._current_line.tokens):
            self._finish_current_line()
            return

        # Play every speech segment and in-line pause as one continuous device
        # stream. Reopening a Windows/Voicemeeter device after each #pause# can
        # leave the following segment silent on some drivers.
        if self._token_index == 0 and any(
            token.kind == "audio" for token in self._current_line.tokens
        ):
            self._play_current_line_as_stream()
            return

        token = self._current_line.tokens[self._token_index]
        self._token_index += 1
        if token.kind == "pause":
            self._pause_timer.setProperty("generation", self._timer_generation)
            self._pause_timer.start(token.duration_ms)
            return

        try:
            pcm_bytes, audio_format = decode_pcm16_wav(token.wav_bytes)
            if not self._output_device.isFormatSupported(audio_format):
                audio_format = self._output_device.preferredFormat()
                pcm_bytes = convert_pcm16_wav(token.wav_bytes, audio_format)
            self._audio_buffer = QBuffer(self)
            self._audio_buffer.setData(QByteArray(pcm_bytes))
            self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self._audio_sink = QAudioSink(self._output_device, audio_format, self)
            self._audio_sink.stateChanged.connect(self._on_audio_state_changed)
            self._audio_sink.start(self._audio_buffer)
        except Exception as error:  # noqa: BLE001 - surface device/decode errors to UI
            self.errorOccurred.emit(str(error))

    def _play_current_line_as_stream(self) -> None:
        if self._current_line is None:
            return
        try:
            first_audio = next(
                token for token in self._current_line.tokens if token.kind == "audio"
            )
            # Always feed the device its preferred format.  Some Windows
            # endpoints accept the 24 kHz model format for the first stream,
            # then silently discard subsequent streams after reopening.
            decode_pcm16_wav(first_audio.wav_bytes)
            target_format = self._output_device.preferredFormat()
            pcm_bytes = render_line_tokens(self._current_line.tokens, target_format)
            self._token_index = len(self._current_line.tokens)
            if self._audio_sink is None or not formats_match(
                self._audio_sink.format(), target_format
            ):
                self._dispose_audio()
                self._audio_sink = QAudioSink(self._output_device, target_format, self)
                self._audio_sink.stateChanged.connect(self._on_audio_state_changed)
            self._audio_buffer = QBuffer(self)
            self._audio_buffer.setData(QByteArray(pcm_bytes))
            self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self._audio_sink.start(self._audio_buffer)
        except Exception as error:  # noqa: BLE001 - surface device/decode errors to UI
            self.errorOccurred.emit(str(error))

    def _on_pause_elapsed(self) -> None:
        generation = int(self._pause_timer.property("generation"))
        if generation == self._timer_generation and self._active:
            self._play_next_token()

    def _on_audio_state_changed(self, state: QtAudio.State) -> None:
        if self._audio_sink is None or self._cleanup_in_progress:
            return
        if state == QtAudio.State.IdleState:
            if self._batch_active:
                self._batch_timer.stop()
                while self._current_line is not None or self._batch_lines:
                    self._advance_batch_line()
                self._batch_active = False
                self._dispose_audio()
                self._next_line_timer.start(180)
                return
            # A QAudioSink that has just entered IdleState is not reliably
            # restartable from inside its own stateChanged callback on
            # Windows (notably with Voicemeeter).  That made the first line
            # audible while the following queued lines appeared to play but
            # stayed silent.  Tear down the completed stream and continue on
            # the next event-loop turn with a fresh sink.
            self._dispose_audio()
            QTimer.singleShot(0, self._play_next_token)
        elif state == QtAudio.State.StoppedState:
            error = self._audio_sink.error()
            if error != QtAudio.Error.NoError:
                self.errorOccurred.emit(f"音频输出错误: {error.name}")

    def _dispose_audio(self) -> None:
        self._cleanup_in_progress = True
        try:
            if self._audio_sink is not None:
                self._audio_sink.stop()
                self._audio_sink.deleteLater()
                self._audio_sink = None
            self._stream_device = None
            self._dispose_buffer()
        finally:
            self._cleanup_in_progress = False

    def _dispose_buffer(self) -> None:
        if self._audio_buffer is not None:
            self._audio_buffer.close()
            self._audio_buffer.deleteLater()
            self._audio_buffer = None

    def _finish_current_line(self) -> None:
        if self._current_line is None:
            return
        line_id = self._current_line.line_id
        self._current_line = None
        self._token_index = 0
        self.lineFinished.emit(line_id)
        self.bufferDepthChanged.emit(self.buffered_line_count)

        if self._pause_after_line:
            self._pause_after_line = False
            self._paused = True
            self.sentenceEndPaused.emit()
            return
        # Give Windows/Voicemeeter time to release the completed endpoint.
        # Starting the next QAudioSink immediately can result in a silent
        # stream even though Qt reports that the whole line was consumed.
        self._next_line_timer.start(180)
