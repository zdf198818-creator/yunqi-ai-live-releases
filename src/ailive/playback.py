from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlaybackState(str, Enum):
    IDLE = "idle"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSE_PENDING = "pause_pending"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(slots=True)
class PlaybackController:
    total_lines: int = 0
    current_index: int = 0
    state: PlaybackState = PlaybackState.IDLE

    def start(self, total_lines: int) -> None:
        if total_lines <= 0:
            raise ValueError("至少需要一句话术")
        self.total_lines = total_lines
        self.current_index = 0
        self.state = PlaybackState.BUFFERING

    def on_buffer_ready(self) -> None:
        if self.state != PlaybackState.BUFFERING:
            raise RuntimeError("只有缓冲状态可以开始播放")
        self.state = PlaybackState.PLAYING

    def request_sentence_end_pause(self) -> None:
        if self.state == PlaybackState.PLAYING:
            self.state = PlaybackState.PAUSE_PENDING
        elif self.state == PlaybackState.PAUSE_PENDING:
            self.state = PlaybackState.PLAYING
        else:
            raise RuntimeError("当前状态不能请求句末暂停")

    def on_line_complete(self) -> None:
        if self.state not in {PlaybackState.PLAYING, PlaybackState.PAUSE_PENDING}:
            raise RuntimeError("当前没有正在播放的话术")

        self.current_index += 1
        if self.current_index >= self.total_lines:
            self.state = PlaybackState.COMPLETED
        elif self.state == PlaybackState.PAUSE_PENDING:
            self.state = PlaybackState.PAUSED
        else:
            self.state = PlaybackState.PLAYING

    def resume(self) -> None:
        if self.state != PlaybackState.PAUSED:
            raise RuntimeError("只有已暂停状态可以继续")
        self.state = PlaybackState.PLAYING

    def fail(self) -> None:
        self.state = PlaybackState.ERROR

    def end_live(self) -> None:
        self.total_lines = 0
        self.current_index = 0
        self.state = PlaybackState.IDLE
