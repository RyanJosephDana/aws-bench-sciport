"""Utility functions for account management."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from aws_bench.account_management.constants import POLL_TIMEOUT_SEC
from aws_bench.account_management.exceptions import AccountCreationTimeoutError

# AWS Organizations enforces a 64-character limit on account emails.
_MAX_EMAIL_LENGTH = 64


def _sanitize(value: str) -> str:
    """Strip whitespace and collapse internal spaces to hyphens."""
    return re.sub(r"\s+", "-", value.strip())


def generate_account_email(domain: str, ou_name: str, environment_id: str) -> str:
    """Generate a unique email for a new account.

    Inputs are trimmed and sanitized so that leading/trailing whitespace
    or internal spaces do not cause account creation failures.

    Appends a timestamp to avoid collisions with closed accounts
    during the 90-day suspension hold. If the resulting email would exceed
    the AWS 64-character limit, the local part prefix is truncated to fit
    while preserving the timestamp and domain.
    """
    ou_name = _sanitize(ou_name)
    environment_id = _sanitize(environment_id)
    domain = domain.strip()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    suffix = f"-{ts}@{domain}"
    max_prefix_len = _MAX_EMAIL_LENGTH - len(suffix)
    prefix = f"{ou_name}-{environment_id}"
    if len(prefix) > max_prefix_len:
        prefix = prefix[:max_prefix_len]

    if prefix:
        return f"{prefix}{suffix}"
    return f"{ts}@{domain}"


def raise_account_creation_timeout(retry_state: object) -> None:
    """Raise AccountCreationTimeoutError when retries are exhausted.

    Used as retry_error_callback for tenacity to replace the default
    RetryError with a specific exception.
    """
    raise AccountCreationTimeoutError(f"Account creation timed out after {POLL_TIMEOUT_SEC}s")
