from ailive.client.app import MainWindow
from ailive.domain import ScriptLine


def _line(number: int) -> ScriptLine:
    return ScriptLine(
        line_id=f"line-{number}",
        reference_id="voice",
        speed=1.0,
        language="Chinese",
        text=f"第{number}句",
    )


def test_slice_lines_starts_from_selected_line() -> None:
    lines = [_line(1), _line(2), _line(3)]
    assert [line.line_id for line in MainWindow._slice_lines_from_id(lines, "line-2")] == [
        "line-2",
        "line-3",
    ]


def test_missing_start_line_falls_back_to_beginning() -> None:
    lines = [_line(1), _line(2)]
    assert MainWindow._slice_lines_from_id(lines, "missing") == lines
