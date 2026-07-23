"""Tests for storage exceptions."""

from aws_bench.resource_management.storage.exceptions import (
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
)


def test_storage_error_retryable():
    """Test StorageError message."""
    error = StorageError("test error")
    assert str(error) == "test error"


def test_storage_error_not_retryable_default():
    """Test StorageError message."""
    error = StorageError("test error")
    assert str(error) == "test error"


def test_storage_conflict_error():
    """Test StorageConflictError attributes."""
    error = StorageConflictError("test/key", "etag1", "etag2")
    assert error.key == "test/key"
    assert error.expected_etag == "etag1"
    assert error.actual_etag == "etag2"
    assert "Conflict on test/key" in str(error)


def test_storage_not_found_error():
    """Test StorageNotFoundError attributes."""
    error = StorageNotFoundError("test/key")
    assert error.key == "test/key"
    assert "Key not found: test/key" in str(error)
