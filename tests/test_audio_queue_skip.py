from collections import deque
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from ailive.client.audio import AudioQueuePlayer


def test_remove_queued_lines_filters_selected_ids() -> None:
    _application = QCoreApplication.instance() or QCoreApplication([])
    player = AudioQueuePlayer()
    player._queue = deque(
        [SimpleNamespace(line_id="one"), SimpleNamespace(line_id="two")]
    )
    removed = AudioQueuePlayer.remove_queued_lines(player, {"two"})
    assert removed == 1
    assert [line.line_id for line in player._queue] == ["one"]


def test_priority_line_is_inserted_before_buffered_lines() -> None:
    _application = QCoreApplication.instance() or QCoreApplication([])
    player = AudioQueuePlayer()
    player._queue = deque(
        [SimpleNamespace(line_id="normal-1"), SimpleNamespace(line_id="normal-2")]
    )

    AudioQueuePlayer.enqueue_priority(
        player, SimpleNamespace(line_id="interjection")
    )

    assert [line.line_id for line in player._queue] == [
        "interjection",
        "normal-1",
        "normal-2",
    ]


def test_multiple_priority_lines_keep_click_order() -> None:
    _application = QCoreApplication.instance() or QCoreApplication([])
    player = AudioQueuePlayer()
    player._queue = deque([SimpleNamespace(line_id="normal")])

    AudioQueuePlayer.enqueue_priority(player, SimpleNamespace(line_id="first"))
    AudioQueuePlayer.enqueue_priority(player, SimpleNamespace(line_id="second"))

    assert [line.line_id for line in player._queue] == [
        "first",
        "second",
        "normal",
    ]
