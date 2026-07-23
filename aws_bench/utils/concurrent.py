"""Concurrency utilities with graceful interrupt handling."""

from __future__ import annotations

import contextvars
import functools
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Generator, TypeVar

from aws_bench.exceptions import OperationCancelled
from aws_bench.logging.logger import get_logger

logger = get_logger(__name__)

_T = TypeVar("_T")
_R = TypeVar("_R")

# Process-global cooperative-shutdown flag. A signal (Ctrl+C / SIGTERM) is only
# delivered to the main thread, so blocking work fanned out across worker
# threads (e.g. the CCAPI scan under ``asyncio.to_thread``) cannot be
# interrupted — it has to poll. The signal handler sets this; long scan loops
# check it and unwind via ``raise_if_shutdown``.
_shutdown_event = threading.Event()


def request_shutdown() -> None:
    """Signal every polling worker to stop at its next checkpoint."""
    _shutdown_event.set()


def shutdown_requested() -> bool:
    """Whether a shutdown has been requested."""
    return _shutdown_event.is_set()


def reset_shutdown() -> None:
    """Clear the flag. For tests — a real run is one process, one shutdown."""
    _shutdown_event.clear()


# boto3 client/session construction is not thread-safe: concurrent builds race
# process-global OpenSSL C state and crash the interpreter (SIGSEGV). aws-bench
# builds from many worker threads, so every construction funnels through this one
# process-global lock. Only construction serializes; built clients stay concurrent.
_client_build_lock = threading.Lock()


def build_client(session: Any, service_name: str, **kwargs: Any) -> Any:
    """Build a boto3 client under the shared construction lock (thread-safe)."""
    with _client_build_lock:
        return session.client(service_name, **kwargs)


def build_session(_factory: Callable[[], _T]) -> _T:
    """Run ``_factory`` (a ``boto3.Session(...)`` thunk) under the shared build lock.

    Session construction races the same state as client construction, so it takes
    the same lock; a thunk lets callers keep their own Session constructor args.
    """
    with _client_build_lock:
        return _factory()


def raise_if_shutdown() -> None:
    """Raise ``OperationCancelled`` if a shutdown has been requested.

    Called at the top of long loops and worker tasks. ``OperationCancelled`` is
    a ``BaseException``, so it travels past the per-account ``except Exception``
    handlers and unwinds the work to the trial's clean-teardown boundary rather
    than being reclassified there as a failed result.
    """
    if _shutdown_event.is_set():
        raise OperationCancelled("operation cancelled by shutdown signal")


def reraise_if_cancelled(results: Iterable[object]) -> None:
    """Re-raise the first ``OperationCancelled`` captured in gather results.

    ``asyncio.gather(return_exceptions=True)`` captures a ``BaseException`` as a
    result *value* rather than propagating it, and the ``isinstance(_, Exception)``
    filters that classify those results do not match it. Call this on the results
    before classifying them so a shutdown unwinds instead of being mis-shaped into
    a per-unit failure record.
    """
    for result in results:
        if isinstance(result, OperationCancelled):
            raise result


class _ContextCopyingExecutor:
    """Wraps a ThreadPoolExecutor so each submit runs in a copy of the caller's context.

    ``ThreadPoolExecutor`` does not propagate :mod:`contextvars` into its worker
    threads. This wrapper copies the submitting thread's context at submit time
    and runs the callable inside it, so the ambient ``log_context`` label (e.g.
    a trial name) flows into fan-out workers and their log lines stay tagged.
    Other executor methods are delegated unchanged.
    """

    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self._executor = executor

    def submit(self, fn: Callable[..., _T], /, *args: object, **kwargs: object) -> Future[_T]:
        """Submit ``fn`` to run inside a copy of the caller's current context.

        Each submit captures its own context copy, so concurrent workers never
        share a ``Context`` (which is not re-entrant).
        """
        ctx = contextvars.copy_context()
        return self._executor.submit(ctx.run, functools.partial(fn, *args, **kwargs))

    def map(self, fn: Callable[[_T], _R], iterable: Iterable[_T]) -> Iterator[_R]:
        """Like ``Executor.map`` (single iterable) but context-propagating.

        Built on :meth:`submit` so each element runs in its own context copy.
        Results are yielded in submission order, matching ``Executor.map``.
        """
        futures = [self.submit(fn, item) for item in iterable]
        return (f.result() for f in futures)

    def __getattr__(self, name: str) -> object:
        """Delegate everything else (shutdown, ...) to the real executor."""
        return getattr(self._executor, name)


@contextmanager
def interruptible_executor(
    max_workers: int | None = None,
) -> Generator[_ContextCopyingExecutor, None, None]:
    """Yield a context-propagating executor that cancels pending work on Ctrl+C.

    Copies the caller's :mod:`contextvars` context into workers (so
    ``log_context`` tags survive fan-out). On ``KeyboardInterrupt`` pending
    futures are cancelled and the interrupt re-raised.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            yield _ContextCopyingExecutor(executor)
        except KeyboardInterrupt:
            logger.warning("Operation interrupted by user, canceling pending tasks...")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
