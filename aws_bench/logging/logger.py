"""Logging utilities for aws-bench.

For per-trial / per-account / per-region context, bind ``log_context`` at a
concurrency boundary; it flows into ``asyncio`` tasks, ``asyncio.to_thread``
work, and ``interruptible_executor`` worker threads, tagging every line beneath
it as ``[a][b]``::

    from aws_bench.logging.logger import get_logger, log_context

    logger = get_logger(__name__)

    with log_context(region):
        logger.info("Starting...")  # → [us-east-1] Starting...

Console and file handlers share the same format (timestamp, level, module,
context, message). The console shows INFO and ERROR+ only — WARNING is our
"recovered but real" level and is routed to the files (see ConsoleLevelFilter),
so an operator watching a succeeding run isn't alarmed by handled failures.
``file_logging`` writes DEBUG to whatever path the caller passes — per-job
``job.log`` / ``trial.log`` and the per-invocation ledger ``run.log`` (TRACE)
under ``~/.aws-bench/logs/``.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from rich.logging import RichHandler

NAMESPACE = "aws_bench"
# Child logger the CLI tees console output onto: records reach the ledger file
# handler (→ run.log) but are filtered off the console handler, which already
# printed the line directly (see ExcludeUILogFilter).
UI_LOGGER_NAME = f"{NAMESPACE}.ui"
_configured = False

# A level below DEBUG for high-volume, expected-benign per-resource lines (e.g.
# "lister unavailable in region", "CCAPI can't check this type") — thousands per
# run. Kept out of the DEBUG file sinks (job.log / trial.log) so they stay
# readable, but still captured in the always-on ledger run.log, whose handler
# level is lowered to TRACE. Emit with ``logger.trace(...)``.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


class TraceLogger(logging.Logger):
    """Logger with a ``trace()`` method at :data:`TRACE` (below DEBUG).

    Registered process-wide via ``setLoggerClass`` so every ``get_logger``
    result has ``.trace()`` alongside the stdlib ``.debug()``/``.info()``. The
    subclass only adds a method; all existing behavior is unchanged.
    """

    def trace(self, msg: str, *args: object, **kwargs: object) -> None:
        """Log ``msg % args`` at :data:`TRACE`. See TRACE for when to use it."""
        if self.isEnabledFor(TRACE):
            # stacklevel=2 attributes the record to the caller, not this wrapper.
            kwargs.setdefault("stacklevel", 2)
            self._log(TRACE, msg, args, **kwargs)  # type: ignore[arg-type]


logging.setLoggerClass(TraceLogger)


# Ambient label stack rendered as "[a][b] " on every record; see log_context().
_LOG_CONTEXT: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "aws_bench_log_context", default=()
)


def _is_ui_record(name: str) -> bool:
    """True if ``name`` is the UI logger or a child of it (dot boundary, not prefix)."""
    return name == UI_LOGGER_NAME or name.startswith(f"{UI_LOGGER_NAME}.")


def render_log_context(parts: tuple[str, ...]) -> str:
    """Render bound context segments into a ``"[a][b] "`` prefix (empty → ``""``)."""
    return "".join(f"[{p}]" for p in parts) + " " if parts else ""


FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d — %(ctx)s%(message)s"
# Console mirrors the file format — operators want the timestamp + module
# name to triage long-running env init / setup runs. INFO+ on console, DEBUG
# on file (set per-handler).
CONSOLE_FORMAT = FILE_FORMAT


class DefaultPrefixFilter(logging.Filter):
    """Fills the ``ctx`` format slot so ``FILE_FORMAT`` never KeyErrors.

    Must be attached to every handler that uses ``FILE_FORMAT``/the console
    format. ``ctx`` is set by assignment (not append), so it stays correct when a
    record passes this filter on more than one handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Set ``record.ctx`` from the ambient log context."""
        record.ctx = render_log_context(_LOG_CONTEXT.get())  # type: ignore[attr-defined]
        return True


class ExcludeUILogFilter(logging.Filter):
    """Drop ``aws_bench.ui`` records from the console handler.

    The CLI console already printed the line directly; this stops the copy it
    tees for run.log from rendering to the terminal a second time.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Pass every record except those from the UI logger."""
        return not _is_ui_record(record.name)


class ConsoleLevelFilter(logging.Filter):
    """Drop WARNING from the console in normal mode; show it under ``--debug``.

    WARNING is our "recovered but real" level — a genuine failure a backstop
    handles. In normal (INFO) mode it stays greppable in the files but is kept
    off the console so it doesn't alarm an operator watching a succeeding run.
    Under ``--debug`` (the handler lowered to DEBUG) the operator asked for
    verbosity, so WARNING passes through.
    """

    def __init__(self, handler: logging.Handler) -> None:
        """Bind the console handler whose level decides whether WARNING shows."""
        super().__init__()
        self._handler = handler

    def filter(self, record: logging.LogRecord) -> bool:
        """Drop WARNING only while the console handler is at INFO (normal mode)."""
        if record.levelno == logging.WARNING:
            return self._handler.level < logging.INFO
        return True


class _LogContextScopeFilter(logging.Filter):
    """Pass only records emitted within a ``log_context`` that includes ``scope``.

    ``logging`` runs filters synchronously in the emitting context, so reading
    ``_LOG_CONTEXT`` here sees the emitter's tags — letting a handler on the
    shared logger keep only one trial's records out of all concurrent trials'.
    """

    def __init__(self, scope: str) -> None:
        super().__init__()
        self._scope = scope

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        return self._scope in _LOG_CONTEXT.get()


@contextmanager
def log_context(label: str) -> Generator[None]:
    """Tag every log line emitted in this block with ``[label]``.

    Append-only: nested binds stack (e.g. a trial then a region render
    ``[trial][region]``). The stack rides a context var, so it flows into nested
    ``asyncio`` tasks, ``asyncio.to_thread`` work, and the
    :func:`aws_bench.utils.concurrent.interruptible_executor` worker threads with
    no plumbing — bind once at a concurrency boundary and every line beneath it,
    even from background workers, carries the tag.
    """
    token = _LOG_CONTEXT.set((*_LOG_CONTEXT.get(), label))
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


# Root segments that mark where a meaningful logger name starts.
_NAME_ROOTS = ("harbor", "aws_bench")


def shorten_logger_name(name: str) -> str:
    """Trim a dotted logger name to the last root segment onward.

    Harbor builds per-trial logger names by stacking ``getChild()`` calls, so a
    name accretes its whole parentage chain — e.g.
    ``harbor.utils.logger.harbor.trial.trial.<trial>.harbor.agents.base``. Only
    the tail is informative, so keep from the last ``harbor``/``aws_bench``
    segment: that line becomes ``harbor.agents.base``, a trial's own line stays
    ``harbor.trial.trial.<trial>`` (still naming the trial), and an already-clean
    ``aws_bench.resource_management.ccapi.scanner`` is unchanged. A name with no
    root segment is returned as-is.
    """
    parts = name.split(".")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in _NAME_ROOTS:
            return ".".join(parts[i:])
    return name


class ShortNameFormatter(logging.Formatter):
    """File formatter that renders ``%(name)s`` via :func:`shorten_logger_name`.

    Swaps the shortened name in only for the duration of one ``format`` call and
    restores it, so the same record formatted by another handler (a record
    propagates to every handler up the tree) still sees its real name.

    :data:`UI_LOGGER_NAME` records are the console's already-rendered lines; they
    render as their raw message (no prefix) so run.log mirrors the terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record``; UI lines raw, everything else via the shortened name."""
        if _is_ui_record(record.name):
            return record.getMessage()
        original = record.name
        record.name = shorten_logger_name(original)
        try:
            return super().format(record)
        finally:
            record.name = original


def get_logger(name: str, *filters: logging.Filter) -> TraceLogger:
    """Return a named logger, auto-configuring handlers on first call.

    Typed as :class:`TraceLogger` (the registered logger class) so callers get
    ``.trace()`` without a cast at every use site.
    """
    _configure()
    logger = logging.getLogger(name)
    for f in filters:
        logger.addFilter(f)
    return cast("TraceLogger", logger)


def build_console_handler(level: int = logging.INFO) -> RichHandler:
    """Build the aws-bench ``RichHandler``.

    Rich renders the time/level/module columns; the formatter emits only the
    message body so timestamps are not duplicated.
    """
    handler = RichHandler(
        level=level,
        show_time=True,
        show_level=True,
        show_path=True,
        markup=False,
        rich_tracebacks=False,
        omit_repeated_times=False,
        log_time_format="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(logging.Formatter("%(ctx)s%(message)s"))
    handler.addFilter(DefaultPrefixFilter())
    handler.addFilter(ExcludeUILogFilter())
    handler.addFilter(ConsoleLevelFilter(handler))
    return handler


def set_console_level(level: int) -> None:
    """Set the aws-bench console handler level (e.g. DEBUG under ``--debug``)."""
    _configure()
    for handler in logging.getLogger(NAMESPACE).handlers:
        if isinstance(handler, RichHandler):
            handler.setLevel(level)


def _configure() -> None:
    """One-time setup: the console handler on the aws_bench namespace logger."""
    global _configured
    if _configured:
        return

    root = logging.getLogger(NAMESPACE)
    # TRACE, not DEBUG: the logger level is the floor for the whole tree, so it
    # must be the lowest level any handler wants. Only the ledger run.log handler
    # keeps TRACE; console (INFO) and the job/trial file handlers (DEBUG) filter
    # it back out. A DEBUG floor here would starve run.log of TRACE records.
    root.setLevel(TRACE)
    root.addHandler(build_console_handler())
    _configured = True


@contextmanager
def file_logging(
    log_file: Path,
    logger_name: str | None = None,
    level: int = logging.DEBUG,
    fmt: str = FILE_FORMAT,
    scope: str | None = None,
) -> Generator[None, None, None]:
    """Context manager that temporarily adds an additional file handler.

    The handler attaches to the shared ``aws_bench`` logger, so when several of
    these are active at once (e.g. concurrent trials) each would otherwise
    capture every other's records. Pass ``scope`` to isolate this writer: the
    handler keeps only records carrying ``scope`` in their ``log_context``, and
    this manager binds ``log_context(scope)`` for the duration so every record
    emitted in the block — including from tasks and worker threads it spawns —
    is tagged. Binding and filtering are coupled here so a scoped handler can
    never be installed without the context that feeds it.
    """
    target = logging.getLogger(logger_name or NAMESPACE)
    handler = logging.FileHandler(log_file)
    handler.setLevel(level)
    handler.setFormatter(ShortNameFormatter(fmt))
    handler.addFilter(DefaultPrefixFilter())
    if scope is not None:
        handler.addFilter(_LogContextScopeFilter(scope))
    target.addHandler(handler)
    try:
        with _maybe_log_context(scope):
            yield
    finally:
        target.removeHandler(handler)
        handler.close()


@contextmanager
def _maybe_log_context(label: str | None) -> Generator[None]:
    """Bind ``log_context(label)`` when ``label`` is set; otherwise a no-op."""
    if label is None:
        yield
        return
    with log_context(label):
        yield
