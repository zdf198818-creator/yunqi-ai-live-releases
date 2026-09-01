from __future__ import annotations

from ailive.client.app import MainWindow


def test_saved_splitter_sizes_accept_integer_lists() -> None:
    assert MainWindow._saved_splitter_sizes([300, "1000", 400]) == [300, 1000, 400]


def test_saved_splitter_sizes_reject_invalid_values() -> None:
    assert MainWindow._saved_splitter_sizes(None) == []
    assert MainWindow._saved_splitter_sizes([300, "bad", 400]) == []
