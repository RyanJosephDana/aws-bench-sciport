"""Shared retry policies and transient-error classifiers."""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from typing import TypeVar

import tenacity
from botocore.exceptions import ClientError

from aws_bench.account_management.constants import FRESH_ACCOUNT_TRANSIENT_CODES

_T = TypeVar("_T")


def is_fresh_account_transient(exc: BaseException) -> bool:
    """True when ``exc`` is a not-yet-converged subscription error on a new account.

    The code set lives in ``account_management.constants`` (a dep-free leaf), shared with the
    in-Lambda engine sweep which cannot import this tenacity-backed module.
    """
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") in FRESH_ACCOUNT_TRANSIENT_CODES
    )


@tenacity.retry(
    stop=tenacity.stop_after_attempt(5),
    wait=tenacity.wait_exponential(min=5, max=60) + tenacity.wait_random(0, 10),
    retry=tenacity.retry_if_exception_type(subprocess.CalledProcessError),
    reraise=True,
)
async def retrying_git_fetch(fetch: Callable[[], Awaitable[_T]]) -> _T:
    """Run a git fetch thunk, retrying transient failures; reraise the original on exhaustion."""
    return await fetch()
