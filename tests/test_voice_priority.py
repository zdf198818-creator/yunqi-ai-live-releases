from __future__ import annotations

from types import SimpleNamespace

from ailive.client.app import MainWindow


def test_row_voice_overrides_default_only_for_that_row() -> None:
    window = SimpleNamespace(
        local_voice_remote_ids={
            "local:default": "remote-default",
            "local:row": "remote-row",
        }
    )

    assert (
        MainWindow._resolve_row_reference_id(window, "local:row", "local:default")
        == "remote-row"
    )
    assert (
        MainWindow._resolve_row_reference_id(window, "", "local:default")
        == "remote-default"
    )
