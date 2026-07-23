"""Exceptions for storage operations."""


class StorageError(Exception):
    """Base exception for storage operations."""

    def __init__(self, message: str):
        """Initialize with message.

        Args:
            message: Error description
        """
        super().__init__(message)


class StorageConflictError(StorageError):
    """ETag mismatch - concurrent modification detected."""

    def __init__(self, key: str, expected: str | None, actual: str):
        """Initialize with conflict details.

        Args:
            key: Storage key that had conflict
            expected: Expected ETag value
            actual: Actual ETag value found
        """
        super().__init__(f"Conflict on {key}: expected etag {expected}, found {actual}")
        self.key = key
        self.expected_etag = expected
        self.actual_etag = actual


class StorageNotFoundError(StorageError):
    """Key doesn't exist in storage."""

    def __init__(self, key: str):
        """Initialize with missing key.

        Args:
            key: Storage key that was not found
        """
        super().__init__(f"Key not found: {key}")
        self.key = key
