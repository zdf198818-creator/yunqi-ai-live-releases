from types import SimpleNamespace

from ailive.client.app import MainWindow


class _Worker:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    def submit(self, line: object) -> None:
        self.submitted.append(line)


def _window(
    *,
    buffered: int,
    inflight: int,
    next_index: int,
    manually_paused: bool = True,
    playback_started: bool = False,
    target_buffer_lines: int = 3,
) -> SimpleNamespace:
    worker = _Worker()
    lines = [SimpleNamespace(line_id=f"line-{index}") for index in range(30)]
    return SimpleNamespace(
        worker=worker,
        manually_paused=manually_paused,
        playback_started=playback_started,
        target_buffer_lines=target_buffer_lines,
        rolling_buffer_lines=20,
        player=SimpleNamespace(buffered_line_count=buffered),
        inflight_count=inflight,
        next_submit_index=next_index,
        playback_lines=lines,
        blocked_line_ids=set(),
        line_positions={line.line_id: index + 1 for index, line in enumerate(lines)},
        _log=lambda _message: None,
    )


def test_paused_playback_keeps_generating_past_startup_waterline() -> None:
    window = _window(buffered=7, inflight=2, next_index=9)

    MainWindow._fill_generation_buffer(window)

    assert len(window.worker.submitted) == 11
    assert window.inflight_count == 13
    assert window.next_submit_index == 20


def test_paused_playback_stops_at_twenty_cached_or_inflight_lines() -> None:
    window = _window(buffered=18, inflight=2, next_index=20)

    MainWindow._fill_generation_buffer(window)

    assert window.worker.submitted == []
    assert window.next_submit_index == 20


def test_startup_keeps_filling_twenty_when_playback_threshold_is_ten() -> None:
    window = _window(
        buffered=10,
        inflight=9,
        next_index=19,
        manually_paused=False,
        playback_started=False,
        target_buffer_lines=10,
    )

    MainWindow._fill_generation_buffer(window)

    assert len(window.worker.submitted) == 1
    assert window.inflight_count == 10
    assert window.next_submit_index == 20


def test_empty_startup_primes_twenty_serial_worker_jobs() -> None:
    window = _window(
        buffered=0,
        inflight=0,
        next_index=0,
        manually_paused=False,
        playback_started=False,
        target_buffer_lines=3,
    )

    MainWindow._fill_generation_buffer(window)

    assert len(window.worker.submitted) == 20
    assert window.inflight_count == 20
    assert window.next_submit_index == 20
