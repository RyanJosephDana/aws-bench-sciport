"""Tests for logging utilities."""

from __future__ import annotations

import logging
import re
import sys
from io import StringIO
from pathlib import Path

import pytest

import aws_bench.logging.logger as _logger_mod
from aws_bench.logging.logger import (
    TRACE,
    ConsoleLevelFilter,
    DefaultPrefixFilter,
    TraceLogger,
    file_logging,
    get_logger,
    log_context,
    set_console_level,
    shorten_logger_name,
)


@pytest.fixture(autouse=True)
def _reset_console():
    """Ensure the aws_bench logger has no handlers before/after each test."""
    root = logging.getLogger("aws_bench")
    saved = root.handlers.copy()
    saved_level = root.level
    root.handlers.clear()
    _logger_mod._configured = False
    yield
    # Close file handlers before clearing to avoid resource leaks
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            h.close()
    root.handlers.clear()
    root.handlers.extend(saved)
    root.setLevel(saved_level)
    _logger_mod._configured = False


# -- get_logger ---------------------------------------------------------------


def test_get_logger_auto_configures_console_handler():
    """get_logger() attaches a single console handler on first call (no file handler)."""
    from rich.logging import RichHandler

    root = logging.getLogger("aws_bench")
    assert len(root.handlers) == 0
    get_logger(__name__)
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], RichHandler)


def test_get_logger_is_idempotent():
    """Multiple get_logger calls add only the one console handler."""
    root = logging.getLogger("aws_bench")
    get_logger("a")
    get_logger("b")
    assert len(root.handlers) == 1


def test_get_logger_preserves_module_name():
    """get_logger() returns a logger with the given name."""
    logger = get_logger("aws_bench.some.module")
    assert logger.name == "aws_bench.some.module"


def test_console_handler_level_is_info():
    """Console handler defaults to INFO."""
    get_logger("x")
    handler = logging.getLogger("aws_bench").handlers[0]
    assert handler.level == logging.INFO


def test_set_console_level_adjusts_console_handler():
    """--debug routes through set_console_level to raise the console handler to DEBUG."""
    from aws_bench.logging.logger import set_console_level

    get_logger("x")
    handler = logging.getLogger("aws_bench").handlers[0]

    set_console_level(logging.DEBUG)
    assert handler.level == logging.DEBUG
    set_console_level(logging.INFO)
    assert handler.level == logging.INFO


def test_namespace_logger_level_is_trace():
    """Namespace logger is TRACE so no handler (incl. run.log at TRACE) is starved."""
    get_logger("x")
    assert logging.getLogger("aws_bench").level == TRACE


def test_get_logger_returns_tracelogger_with_trace_method():
    """get_logger yields a TraceLogger, so callers can use logger.trace(...)."""
    logger = get_logger("aws_bench.trace_api_probe")
    assert isinstance(logger, TraceLogger)
    assert callable(logger.trace)


def test_trace_filtered_off_debug_file_sink_but_kept_by_trace_handler(tmp_path):
    """logger.trace() lands in a TRACE-level sink but not the default (DEBUG) file sink."""
    logger = get_logger("aws_bench.trace_probe")

    # A TRACE-level handler (like the ledger run.log) keeps the record.
    trace_log = tmp_path / "run.log"
    trace_handler = logging.FileHandler(trace_log)
    trace_handler.setLevel(TRACE)
    trace_handler.addFilter(DefaultPrefixFilter())
    logging.getLogger("aws_bench").addHandler(trace_handler)
    try:
        # file_logging defaults to DEBUG — the job.log / trial.log sink.
        debug_log = tmp_path / "job.log"
        with file_logging(debug_log):
            logger.trace("high volume line")
            logger.debug("ordinary debug line")
    finally:
        logging.getLogger("aws_bench").removeHandler(trace_handler)
        trace_handler.close()

    debug_text = debug_log.read_text()
    assert "high volume line" not in debug_text  # TRACE filtered off DEBUG sink
    assert "ordinary debug line" in debug_text

    trace_text = trace_log.read_text()
    assert "high volume line" in trace_text  # TRACE kept by the TRACE sink


def test_console_handler_has_default_prefix_filter():
    """Console handler carries a DefaultPrefixFilter."""
    get_logger("x")
    handler = logging.getLogger("aws_bench").handlers[0]
    assert any(isinstance(f, DefaultPrefixFilter) for f in handler.filters)


def test_console_handler_has_console_level_filter():
    """Console handler carries a ConsoleLevelFilter (WARNING stays file-only)."""
    get_logger("x")
    handler = logging.getLogger("aws_bench").handlers[0]
    assert any(isinstance(f, ConsoleLevelFilter) for f in handler.filters)


def test_console_level_filter_drops_warning_only_in_normal_mode():
    """WARNING is dropped while the handler is at INFO, but passes under --debug (DEBUG)."""

    def _record(level: int) -> logging.LogRecord:
        return logging.LogRecord("aws_bench.x", level, "f", 1, "m", None, None)

    handler = logging.NullHandler()
    f = ConsoleLevelFilter(handler)

    # Normal mode (INFO): DEBUG/INFO/ERROR pass, WARNING is dropped.
    handler.setLevel(logging.INFO)
    assert f.filter(_record(logging.DEBUG)) is True
    assert f.filter(_record(logging.INFO)) is True
    assert f.filter(_record(logging.WARNING)) is False
    assert f.filter(_record(logging.ERROR)) is True
    assert f.filter(_record(logging.CRITICAL)) is True

    # --debug (DEBUG): the operator asked for verbosity, so WARNING passes too.
    handler.setLevel(logging.DEBUG)
    assert f.filter(_record(logging.WARNING)) is True


def test_warning_is_file_only_not_on_console(tmp_path: Path):
    """A WARNING reaches the file sink but not the console (INFO/ERROR do reach console)."""
    logger = get_logger("aws_bench.warn_probe")

    # File sink keeps WARNING...
    log_file = tmp_path / "job.log"
    with file_logging(log_file):
        logger.warning("recovered-but-real failure")
    assert "recovered-but-real failure" in log_file.read_text()

    # ...but in normal (INFO) console mode it's dropped, while INFO and ERROR still show.
    assert "console warning" not in _capture_console(logger, "console warning", logging.WARNING)
    assert "console info" in _capture_console(logger, "console info", logging.INFO)
    assert "console error" in _capture_console(logger, "console error", logging.ERROR)


def test_warning_reaches_console_under_debug():
    """Under --debug (console at DEBUG), WARNING is shown — the operator asked for verbosity."""
    logger = get_logger("aws_bench.warn_debug_probe")
    set_console_level(logging.DEBUG)
    try:
        assert "verbose warning" in _capture_console(logger, "verbose warning", logging.WARNING)
    finally:
        set_console_level(logging.INFO)


# -- console output -----------------------------------------------------------


def _capture_console(
    logger: logging.Logger | logging.LoggerAdapter,
    message: str,
    level: int = logging.INFO,
) -> str:
    """Capture what the console handler emits for a single log call at ``level``.

    The console handler is a ``RichHandler`` that writes to a ``rich.Console``.
    Swap its console for one whose underlying file is a ``StringIO`` so the
    rendered text is capturable.
    """
    from rich.console import Console
    from rich.logging import RichHandler

    root = logging.getLogger("aws_bench")
    handler = root.handlers[0]
    assert isinstance(handler, RichHandler), f"expected RichHandler, got {type(handler).__name__}"
    stream = StringIO()
    capture_console = Console(file=stream, force_terminal=False, width=200)
    original_console = handler.console
    handler.console = capture_console
    try:
        logger.log(level, message)
        handler.flush()
        return stream.getvalue().strip()
    finally:
        handler.console = original_console


def test_no_prefix_gives_clean_output():
    """Without a prefixed logger, the message body has no prefix injected."""
    logger = get_logger("aws_bench.clean_test")
    output = _capture_console(logger, "hello")
    assert "hello" in output
    # No prefix ⇒ no bracketed prefix anywhere in the rendered line.
    assert "[deploy]" not in output


# -- file_logging -------------------------------------------------------------


def test_file_logging_creates_log_file(tmp_path: Path):
    """file_logging writes records to the given file."""
    log_file = tmp_path / "test.log"
    logger = get_logger("aws_bench.file_test")
    with file_logging(log_file):
        logger.debug("hello file")
    assert "hello file" in log_file.read_text()


def test_file_logging_removes_handler_on_exit(tmp_path: Path):
    """Handler is removed when the context manager exits."""
    target = logging.getLogger("aws_bench")
    before = len(target.handlers)
    with file_logging(tmp_path / "test.log"):
        assert len(target.handlers) == before + 1
    assert len(target.handlers) == before


def test_file_logging_custom_logger(tmp_path: Path):
    """file_logging can target a specific logger by name."""
    log_file = tmp_path / "custom.log"
    custom = logging.getLogger("aws_bench.custom")
    custom.setLevel(logging.DEBUG)
    with file_logging(log_file, logger_name="aws_bench.custom"):
        custom.info("custom msg")
    assert "custom msg" in log_file.read_text()


def test_file_logging_custom_level(tmp_path: Path):
    """File handler respects a custom level."""
    log_file = tmp_path / "test.log"
    logger = get_logger("aws_bench.level_test")
    with file_logging(log_file, level=logging.WARNING):
        logger.info("should-not-appear")
        logger.warning("should-appear")
    content = log_file.read_text()
    assert "should-not-appear" not in content
    assert "should-appear" in content


# -- DefaultPrefixFilter ------------------------------------------------------


def test_default_prefix_filter_sets_ctx_from_context():
    """DefaultPrefixFilter sets record.ctx from the ambient log context."""
    f = DefaultPrefixFilter()
    record = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
    with log_context("east"):
        f.filter(record)
    assert record.ctx == "[east] "  # type: ignore[attr-defined]


def test_default_prefix_filter_empty_ctx_without_context():
    """DefaultPrefixFilter sets ctx to empty when no context is bound."""
    f = DefaultPrefixFilter()
    record = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
    f.filter(record)
    assert record.ctx == ""  # type: ignore[attr-defined]


# -- log_context --------------------------------------------------------------


def test_log_context_tags_console_output():
    """A bound log_context appears as a [tag] prefix in console output."""
    logger = get_logger("aws_bench.ctx_test")
    with log_context("east"):
        output = _capture_console(logger, "hello")
    assert "[east] hello" in output


def test_log_context_nests_tags():
    """Nested log_context binds stack as [a][b] in order."""
    logger = get_logger("aws_bench.ctx_nest")
    with log_context("acct"), log_context("us-east-1"):
        output = _capture_console(logger, "msg")
    assert "[acct][us-east-1] msg" in output


def test_log_context_unbinds_on_exit():
    """A tag is gone once its log_context block exits."""
    logger = get_logger("aws_bench.ctx_exit")
    with log_context("east"):
        pass
    output = _capture_console(logger, "after")
    assert "[east]" not in output


def test_log_context_in_file_logging(tmp_path: Path):
    """A bound log_context appears in file log output."""
    log_file = tmp_path / "test.log"
    logger = get_logger("aws_bench.ctx_file")
    with file_logging(log_file), log_context("west"):
        logger.info("scanning")
    assert "[west] scanning" in log_file.read_text()


def test_scope_binds_its_own_context(tmp_path: Path):
    """A scoped handler tags and keeps its records without any external bind.

    file_logging binds the scope context itself, so a caller that only opens the
    scoped handler still gets a populated, isolated file.
    """
    log_file = tmp_path / "scoped.log"
    logger = get_logger("aws_bench.scope_self")

    with file_logging(log_file, scope="trial-a"):
        logger.info("hello")

    text = log_file.read_text()
    assert "[trial-a] hello" in text


def test_scope_isolates_concurrent_trial_tasks(tmp_path: Path):
    """Concurrent trial tasks each write only their own records to their own file.

    Two trials run as sibling asyncio tasks (as the trial queue runs them), each
    opening its own scoped file_logging. The handlers coexist on the shared
    logger, but asyncio gives each task an isolated contextvar copy, so the scope
    filter routes each trial's records to only its file.
    """
    import asyncio

    log_a = tmp_path / "a.log"
    log_b = tmp_path / "b.log"
    logger = get_logger("aws_bench.scope_iso")

    async def trial(scope: str, path: Path, ready: asyncio.Event, peer: asyncio.Event) -> None:
        with file_logging(path, scope=scope):
            ready.set()
            await peer.wait()  # ensure both handlers are attached at the same time
            logger.info("from %s", scope)

    async def run() -> None:
        a_ready, b_ready = asyncio.Event(), asyncio.Event()
        await asyncio.gather(
            trial("trial-a", log_a, a_ready, b_ready),
            trial("trial-b", log_b, b_ready, a_ready),
        )

    asyncio.run(run())

    a_text = log_a.read_text()
    b_text = log_b.read_text()
    assert "from trial-a" in a_text
    assert "from trial-b" not in a_text
    assert "from trial-b" in b_text
    assert "from trial-a" not in b_text


def test_unscoped_file_logging_captures_all_records(tmp_path: Path):
    """Without scope, a handler still captures every record (aggregate-log path)."""
    log_file = tmp_path / "all.log"
    logger = get_logger("aws_bench.no_scope")

    with file_logging(log_file):
        with log_context("trial-a"):
            logger.info("from a")
        with log_context("trial-b"):
            logger.info("from b")

    text = log_file.read_text()
    assert "from a" in text
    assert "from b" in text


def test_file_log_reports_caller_lineno(tmp_path: Path):
    """%(lineno)d in file log points to the caller."""
    log_file = tmp_path / "test.log"
    logger = get_logger("aws_bench.lineno")
    with file_logging(log_file):
        expected_line = sys._getframe().f_lineno + 1
        logger.info("marker")

    content = log_file.read_text()
    # FILE_FORMAT: "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d — ..."
    match = re.search(r":(\d+) —", content)
    assert match, f"No lineno found in file log: {content!r}"
    assert int(match.group(1)) == expected_line


def test_ui_records_render_raw_without_prefix(tmp_path: Path):
    """UI-logger lines land in the file verbatim — no timestamp/level/name prefix.

    The CLI console tees its output onto ``aws_bench.ui``; that output is already
    a finished user-facing line, so run.log must show it as-is, not wrapped in
    ``FILE_FORMAT``. A real log line in the same file keeps the full prefix.
    """
    from aws_bench.logging.logger import UI_LOGGER_NAME

    log_file = tmp_path / "run.log"
    ui_logger = get_logger(UI_LOGGER_NAME)
    other = get_logger("aws_bench.some_module")
    with file_logging(log_file):
        ui_logger.info("  123456789012: deleted 3 stack(s)")
        other.info("a genuine log line")

    lines = log_file.read_text().splitlines()
    ui_line = next(ln for ln in lines if "123456789012" in ln)
    log_line = next(ln for ln in lines if "a genuine log line" in ln)
    # UI line is exactly the message — no "TS LEVEL name:lineno —" prefix.
    assert ui_line == "  123456789012: deleted 3 stack(s)"
    # A normal record still carries the full format.
    assert " — a genuine log line" in log_line
    assert "aws_bench.some_module:" in log_line


def test_ui_prefix_sibling_logger_keeps_full_format(tmp_path: Path):
    """A logger whose name only shares the ``aws_bench.ui`` prefix is not a UI logger.

    The UI-record special-case matches ``aws_bench.ui`` and its subtree, not a
    same-prefixed sibling like ``aws_bench.uix`` — that must render with the full
    FILE_FORMAT prefix, not raw.
    """
    log_file = tmp_path / "run.log"
    sibling = get_logger("aws_bench.uix")
    with file_logging(log_file):
        sibling.info("not a ui line")

    line = next(ln for ln in log_file.read_text().splitlines() if "not a ui line" in ln)
    assert " — not a ui line" in line
    assert "aws_bench.uix:" in line


# -- shorten_logger_name ------------------------------------------------------


def test_shorten_keeps_from_last_root_segment():
    """A stacked Harbor child name trims to the last harbor/aws_bench segment."""
    name = "harbor.utils.logger.harbor.trial.trial.ec2-multiregion__G6woMoM.harbor.agents.base"
    assert shorten_logger_name(name) == "harbor.agents.base"


def test_shorten_keeps_trial_name_when_tail_is_the_trial():
    """A trial's own logger (no deeper child) keeps the trial-naming tail."""
    name = "harbor.utils.logger.harbor.trial.trial.ec2-multiregion__G6woMoM"
    assert shorten_logger_name(name) == "harbor.trial.trial.ec2-multiregion__G6woMoM"


def test_shorten_leaves_clean_aws_bench_name_unchanged():
    """An aws_bench name already starts at its root, so it is unchanged."""
    name = "aws_bench.resource_management.ccapi.scanner"
    assert shorten_logger_name(name) == name


def test_shorten_returns_name_without_root_segment_as_is():
    """A name with no harbor/aws_bench segment is returned verbatim."""
    assert shorten_logger_name("some.third.party.lib") == "some.third.party.lib"


def test_file_log_renders_shortened_name(tmp_path: Path):
    """file_logging writes the shortened logger name into the file line."""
    log_file = tmp_path / "test.log"
    deep = logging.getLogger("harbor.utils.logger.harbor.trial.trial.t__abc.harbor.agents.base")
    deep.setLevel(logging.DEBUG)
    with file_logging(log_file, logger_name="harbor.utils.logger"):
        deep.info("hello")
    content = log_file.read_text()
    assert "harbor.agents.base:" in content
    assert "harbor.utils.logger.harbor.trial" not in content
