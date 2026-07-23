"""Tests for aws_bench.account_management.utils."""

from __future__ import annotations

from unittest.mock import patch

from aws_bench.account_management.utils import _sanitize, generate_account_email

# ── _sanitize ──


def test_sanitize_strips_leading_trailing_whitespace():
    """Strips leading and trailing whitespace."""
    assert _sanitize("  hello  ") == "hello"


def test_sanitize_collapses_internal_spaces_to_hyphens():
    """Collapses internal spaces to hyphens."""
    assert _sanitize("hello world") == "hello-world"


def test_sanitize_collapses_multiple_spaces():
    """Collapses multiple consecutive spaces to a single hyphen."""
    assert _sanitize("a   b   c") == "a-b-c"


def test_sanitize_handles_tabs_and_newlines():
    """Converts tabs and newlines to hyphens."""
    assert _sanitize("a\tb\nc") == "a-b-c"


def test_sanitize_empty_string():
    """Returns empty string for empty input."""
    assert _sanitize("") == ""


def test_sanitize_no_whitespace_unchanged():
    """Returns input unchanged when no whitespace present."""
    assert _sanitize("test-env") == "test-env"


# ── generate_account_email ──


@patch("aws_bench.account_management.utils.datetime")
def test_generate_account_email_basic_format(mock_dt):
    """Generates email in the expected format."""
    mock_dt.now.return_value.strftime.return_value = "20260303120000"
    mock_dt.side_effect = lambda *a, **kw: mock_dt

    email = generate_account_email("example.com", "test-env", "agent")
    assert email == "test-env-agent-20260303120000@example.com"


@patch("aws_bench.account_management.utils.datetime")
def test_generate_account_email_sanitizes_whitespace(mock_dt):
    """Sanitizes whitespace in all inputs."""
    mock_dt.now.return_value.strftime.return_value = "20260303120000"
    mock_dt.side_effect = lambda *a, **kw: mock_dt

    email = generate_account_email("  example.com  ", "  test env  ", "  my agent  ")
    assert email == "test-env-my-agent-20260303120000@example.com"


@patch("aws_bench.account_management.utils.datetime")
def test_generate_account_email_truncates_when_exceeding_64_chars(mock_dt):
    """Truncates prefix to keep email within 64-char AWS limit."""
    mock_dt.now.return_value.strftime.return_value = "20260303120000"
    mock_dt.side_effect = lambda *a, **kw: mock_dt

    email = generate_account_email("example.com", "a" * 30, "b" * 30)
    assert len(email) <= 64
    assert email.endswith("-20260303120000@example.com")


@patch("aws_bench.account_management.utils.datetime")
def test_generate_account_email_short_not_truncated(mock_dt):
    """Does not truncate when email is already within the limit."""
    mock_dt.now.return_value.strftime.return_value = "20260303120000"
    mock_dt.side_effect = lambda *a, **kw: mock_dt

    email = generate_account_email("x.co", "e", "a")
    assert email == "e-a-20260303120000@x.co"
    assert len(email) <= 64
