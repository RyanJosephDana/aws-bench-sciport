"""Tests for aws_bench.resource_management.utils.polling."""

from __future__ import annotations

import pytest

from aws_bench.exceptions import OperationCancelled
from aws_bench.resource_management.utils.polling import wait_until
from aws_bench.utils import concurrent


def test_wait_until_returns_true_immediately():
    assert wait_until(lambda: True, timeout=1, interval=0.01) is True


def test_wait_until_returns_false_on_timeout():
    assert wait_until(lambda: False, timeout=0.05, interval=0.01) is False


def test_wait_until_retries_until_true():
    calls = {"count": 0}

    def predicate():
        calls["count"] += 1
        return calls["count"] >= 3

    assert wait_until(predicate, timeout=5, interval=0.01) is True
    assert calls["count"] == 3


def test_wait_until_handles_predicate_exception():
    """Predicate exceptions are caught and treated as False."""
    calls = {"count": 0}

    def predicate():
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("boom")
        return True

    assert wait_until(predicate, timeout=5, interval=0.01) is True
    assert calls["count"] == 3


def test_wait_until_times_out_on_persistent_exception():
    def predicate():
        raise ValueError("always fails")

    assert wait_until(predicate, timeout=0.05, interval=0.01) is False


def test_wait_until_raises_on_shutdown_without_being_swallowed():
    """A shutdown unwinds wait_until rather than being caught by its except block."""
    concurrent.reset_shutdown()
    concurrent.request_shutdown()
    try:
        with pytest.raises(OperationCancelled):
            wait_until(lambda: False, timeout=5, interval=0.01)
    finally:
        concurrent.reset_shutdown()
