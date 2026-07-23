"""Tests for aws_bench.utils.concurrent — cooperative-shutdown flag."""

from __future__ import annotations

import pytest

from aws_bench.exceptions import OperationCancelled
from aws_bench.utils import concurrent


@pytest.fixture(autouse=True)
def _clear_flag():
    """Each test starts and ends with a clear process-global flag."""
    concurrent.reset_shutdown()
    yield
    concurrent.reset_shutdown()


def test_shutdown_not_requested_by_default():
    assert concurrent.shutdown_requested() is False


def test_request_shutdown_sets_flag():
    concurrent.request_shutdown()
    assert concurrent.shutdown_requested() is True


def test_raise_if_shutdown_is_noop_when_not_requested():
    concurrent.raise_if_shutdown()  # must not raise


def test_raise_if_shutdown_raises_after_request():
    concurrent.request_shutdown()
    with pytest.raises(OperationCancelled):
        concurrent.raise_if_shutdown()


def test_reset_shutdown_clears_flag():
    concurrent.request_shutdown()
    concurrent.reset_shutdown()
    assert concurrent.shutdown_requested() is False
    concurrent.raise_if_shutdown()  # must not raise


def test_reraise_if_cancelled_reraises_captured_cancel():
    cancel = OperationCancelled("stop")
    with pytest.raises(OperationCancelled) as exc_info:
        concurrent.reraise_if_cancelled(["ok", ValueError("boom"), cancel])
    assert exc_info.value is cancel


def test_reraise_if_cancelled_raises_the_first_captured():
    first, second = OperationCancelled("a"), OperationCancelled("b")
    with pytest.raises(OperationCancelled) as exc_info:
        concurrent.reraise_if_cancelled([first, "ok", second])
    assert exc_info.value is first


def test_reraise_if_cancelled_ignores_plain_exceptions():
    # A captured Exception is left for the caller's normal classification.
    concurrent.reraise_if_cancelled(["ok", ValueError("boom")])  # must not raise


def test_reraise_if_cancelled_noop_on_all_values():
    concurrent.reraise_if_cancelled([1, "two", object()])  # must not raise


# -- shared client-construction lock --


def test_build_client_delegates_to_session():
    """build_client returns exactly what session.client(...) returns, forwarding kwargs."""
    calls = []

    class _Session:
        def client(self, service, **kw):
            calls.append((service, kw))
            return f"client:{service}"

    result = concurrent.build_client(_Session(), "cloudformation", region_name="us-west-2")
    assert result == "client:cloudformation"
    assert calls == [("cloudformation", {"region_name": "us-west-2"})]


def test_client_and_session_construction_never_overlap():
    """build_client and build_session serialize construction against EACH OTHER.

    boto3 client/session construction races process-global OpenSSL C state; two
    builds overlapping — even from different subsystems (a fast-scan client vs a
    snapshot drift client) — can SIGSEGV. This asserts one shared lock serializes
    ALL of them: no two constructions are ever in flight at once, regardless of
    which helper started them.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    in_flight = 0
    max_overlap = 0
    guard = threading.Lock()

    def _touch_global_state():
        nonlocal in_flight, max_overlap
        with guard:
            in_flight += 1
            max_overlap = max(max_overlap, in_flight)
        time.sleep(0.005)  # widen the window a concurrent build would be caught in
        with guard:
            in_flight -= 1

    class _SlowSession:
        def client(self, _service, **_kw):
            _touch_global_state()
            return "client"

    def _build_via_client(_i):
        return concurrent.build_client(_SlowSession(), "s3")

    def _build_via_session(_i):
        # build_session must take the SAME lock as build_client.
        return concurrent.build_session(_factory=lambda: _touch_global_state())

    # Mix both helpers across the pool: if they used different locks, a client
    # build and a session build would overlap and max_overlap would exceed 1.
    work = [_build_via_client, _build_via_session] * 6
    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        list(pool.map(lambda f: f(0), work))

    assert max_overlap == 1, f"construction overlapped ({max_overlap} concurrent builds)"
