"""Tests for the shared CLI console and its tee into run.log.

The console renders to the terminal as before, and additionally tees a de-styled
plaintext copy through the ``aws_bench.ui`` logger so it lands in the
per-invocation ``run.log`` (fed by the ledger's file handler on the ``aws_bench``
tree). The console ``RichHandler`` filters those records back out so nothing
renders to the terminal twice.
"""

from __future__ import annotations

import logging

import pytest

import aws_bench.logging.logger as _logger_mod
from aws_bench.cli import ui
from aws_bench.logging.logger import UI_LOGGER_NAME, build_console_handler


@pytest.fixture(autouse=True)
def _reset_logging():
    """Isolate the ``aws_bench`` logger tree per test (handlers + configured flag)."""
    root = logging.getLogger("aws_bench")
    saved, saved_level = root.handlers.copy(), root.level
    root.handlers.clear()
    _logger_mod._configured = False
    yield
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            h.close()
    root.handlers.clear()
    root.handlers.extend(saved)
    root.setLevel(saved_level)
    _logger_mod._configured = False


def test_print_writes_to_stdout(capsys):
    ui.console.print("hello world")
    assert "hello world" in capsys.readouterr().out


def test_print_tees_to_ui_logger(caplog):
    with caplog.at_level(logging.INFO, logger=UI_LOGGER_NAME):
        ui.console.print("teed line")
    assert "teed line" in caplog.text


def test_print_tees_destyled_plaintext(capsys, caplog):
    """A styled ``console.print`` reaches run.log as plaintext (no markup, no ANSI)."""
    with caplog.at_level(logging.INFO, logger=UI_LOGGER_NAME):
        ui.console.print("[red]hot[/red]")
    assert "hot" in capsys.readouterr().out
    assert "hot" in caplog.text
    assert "[red]" not in caplog.text
    assert "\x1b[" not in caplog.text  # no ANSI escape codes in the log


def test_print_does_not_wrap_long_plain_lines(capsys):
    """A plain status line wider than the console prints on one line, not wrapped.

    ``soft_wrap`` defaults on, so a long cleanup verdict does not split
    mid-sentence at the detected width (80 under a pipe).
    """
    line = "  123456789012: FAILED - " + "x" * 120
    ui.console.print(line)
    assert capsys.readouterr().out == line + "\n"


def test_print_does_not_soft_wrap_rich_renderables(caplog):
    """A Panel wrapping long free Text still wraps (not cropped) at a narrow width.

    ``soft_wrap`` defaults on only for bare ``str`` status lines; a Rich
    renderable keeps its own layout, so long text inside it wraps rather than
    truncating — otherwise a long error in an ``env show`` panel loses its tail.
    The tee mirrors the terminal, so asserting on the teed run.log copy checks
    both the terminal render and the capture in one shot.
    """
    from rich.panel import Panel
    from rich.text import Text

    from aws_bench.cli.ui import TeeConsole

    narrow = TeeConsole(width=40)
    long_text = "AccessDenied: not authorized to perform servicequotas:ListServiceQuotas"
    with caplog.at_level(logging.INFO, logger=UI_LOGGER_NAME):
        narrow.print(Panel(Text(long_text)))
    # The tail survives because the panel wrapped the text across lines.
    assert "ListServiceQuotas" in caplog.text


def test_blank_print_does_not_tee_an_empty_log_line(capsys, caplog):
    """A spacer ``console.print()`` prints a blank line but adds no run.log record."""
    with caplog.at_level(logging.INFO, logger=UI_LOGGER_NAME):
        ui.console.print()
    assert capsys.readouterr().out == "\n"
    assert caplog.records == []


def test_console_handler_excludes_ui_records():
    """The console handler drops ``aws_bench.ui`` records (they printed directly).

    Guards the double-render: the shared console already wrote the line to the
    terminal, so the record teed for run.log must not render again here.
    """
    handler = build_console_handler()

    def _record(name: str) -> logging.LogRecord:
        return logging.LogRecord(name, logging.INFO, __file__, 0, "x", None, None)

    # Handler.filter returns falsy to drop, truthy (the record) to keep.
    assert not handler.filter(_record(UI_LOGGER_NAME))
    assert handler.filter(_record("aws_bench.resource_management.scanner"))
    # A same-prefixed sibling is NOT the UI logger — it must pass through.
    assert handler.filter(_record(UI_LOGGER_NAME + "x"))


def test_ui_line_reaches_a_file_handler_on_the_aws_bench_tree(tmp_path):
    """An ``aws_bench`` file handler (as the ledger installs) captures console output.

    This is the run.log path in miniature: attach a DEBUG file handler to the
    namespace root, print, and the line is in the file.
    """
    log_file = tmp_path / "run.log"
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    logging.getLogger("aws_bench").addHandler(handler)
    try:
        ui.console.print("into the run log")
    finally:
        logging.getLogger("aws_bench").removeHandler(handler)
        handler.close()

    assert "into the run log" in log_file.read_text()
