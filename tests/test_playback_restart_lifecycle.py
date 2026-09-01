from __future__ import annotations

from types import SimpleNamespace

from ailive.client.app import MainWindow
from ailive.client.network import TTSWorker


class _Widget:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = True

    def setText(self, text: str) -> None:
        self.text = text

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)


class _StoppedWorker:
    def __init__(self) -> None:
        self.finished = _Signal()
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def wait(self, _timeout: int) -> bool:
        return True

    def isRunning(self) -> bool:
        return False


def test_playback_completion_fully_resets_restart_state() -> None:
    shutdown_calls: list[bool] = []
    window = SimpleNamespace(
        status_label=_Widget(),
        current_line_label=_Widget(),
        pause_button=_Widget(),
        start_button=_Widget(),
        playback_started=True,
        countdown_active=True,
        manually_paused=True,
        pause_requested=True,
        play_asap_requested=True,
        inflight_count=7,
        _shutdown_worker=lambda: shutdown_calls.append(True),
    )

    MainWindow._mark_playback_complete(window)

    assert window.playback_started is False
    assert window.countdown_active is False
    assert window.manually_paused is False
    assert window.pause_requested is False
    assert window.play_asap_requested is False
    assert window.inflight_count == 0
    assert shutdown_calls == [True]


def test_late_worker_events_cannot_touch_a_new_playback_session() -> None:
    ready: list[object] = []
    errors: list[tuple[str, str]] = []
    window = SimpleNamespace(
        playback_session_id=4,
        _on_line_ready=lambda generated: ready.append(generated),
        _on_line_error=lambda line_id, message: errors.append((line_id, message)),
    )
    generated = object()

    MainWindow._on_worker_line_ready(window, 3, generated)
    MainWindow._on_worker_line_error(window, 3, "old-line", "old error")
    assert ready == []
    assert errors == []

    MainWindow._on_worker_line_ready(window, 4, generated)
    MainWindow._on_worker_line_error(window, 4, "new-line", "new error")
    assert ready == [generated]
    assert errors == [("new-line", "new error")]


def test_worker_shutdown_discards_pending_generation_jobs() -> None:
    worker = TTSWorker("http://127.0.0.1:8000")
    for index in range(20):
        worker.submit(SimpleNamespace(line_id=f"line-{index}"))

    worker.shutdown()

    assert worker._jobs.qsize() == 1
    assert worker._jobs.get_nowait() is None


def test_ten_repeated_stops_invalidate_every_previous_session() -> None:
    window = SimpleNamespace(
        playback_session_id=0,
        worker=None,
        _retired_workers=[],
        interjection_worker=None,
        interjection_lines={},
        interjection_auto_pause=False,
    )
    window._release_retired_worker = lambda worker: MainWindow._release_retired_worker(
        window, worker
    )

    for expected_session in range(1, 11):
        worker = _StoppedWorker()
        window.worker = worker

        MainWindow._shutdown_worker(window)

        assert window.playback_session_id == expected_session
        assert window.worker is None
        assert worker.shutdown_calls == 1
        assert window._retired_workers == []


def test_final_generation_failure_skips_line_without_resetting_session() -> None:
    logs: list[str] = []
    refills: list[bool] = []
    completions: list[bool] = []
    window = SimpleNamespace(
        inflight_count=2,
        finished_count=0,
        line_positions={"bad-line": 2},
        playback_lines=[SimpleNamespace(), SimpleNamespace(), SimpleNamespace()],
        status_label=_Widget(),
        _log=logs.append,
        _fill_generation_buffer=lambda: refills.append(True),
        _mark_playback_complete=lambda: completions.append(True),
    )

    MainWindow._on_line_error(window, "bad-line", "temporary network failure")

    assert window.inflight_count == 1
    assert window.finished_count == 1
    assert refills == [True]
    assert completions == []
    assert "已自动跳过" in window.status_label.text
    assert "继续" in logs[0]


def test_live_worker_retries_same_line_before_advancing() -> None:
    worker = TTSWorker("http://127.0.0.1:8000", max_attempts=3)
    line = SimpleNamespace(line_id="retry-line")
    generated = object()
    attempts: list[str] = []
    retries: list[tuple[str, int, str]] = []
    ready: list[object] = []
    errors: list[tuple[str, str]] = []

    worker._ensure_connection = lambda: None

    def synthesize(current_line: object) -> object:
        attempts.append(current_line.line_id)
        if len(attempts) < 3:
            raise TimeoutError("temporary timeout")
        return generated

    worker._synthesize_line = synthesize
    worker.lineRetry.connect(
        lambda line_id, attempt, message: retries.append((line_id, attempt, message))
    )
    worker.lineReady.connect(ready.append)
    worker.lineError.connect(lambda line_id, message: errors.append((line_id, message)))
    worker.submit(line)
    worker._jobs.put(None)

    worker.run()

    assert attempts == ["retry-line", "retry-line", "retry-line"]
    assert [retry[1] for retry in retries] == [2, 3]
    assert ready == [generated]
    assert errors == []
