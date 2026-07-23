"""Polling utilities."""

from __future__ import annotations

import time
from collections.abc import Callable

from aws_bench.logging.logger import get_logger
from aws_bench.utils.concurrent import raise_if_shutdown

logger = get_logger(__name__)


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 300,
    interval: float = 10,
) -> bool:
    """Poll *predicate* until it returns True or *timeout* seconds elapse.

    If the predicate raises, the exception is logged and treated as False.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raise_if_shutdown()
        try:
            if predicate():
                return True
        except Exception as e:
            logger.debug("wait_until predicate raised: %s", e)
        time.sleep(interval)
    return False
