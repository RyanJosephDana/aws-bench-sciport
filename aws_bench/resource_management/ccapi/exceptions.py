"""Exception classes for the CCAPI package."""

from __future__ import annotations

from typing import Any

from aws_bench.exceptions import AWSBenchError


class CloudControlError(AWSBenchError):
    """Base exception for Cloud Control API operations."""


class CloudControlResourceDeletionException(CloudControlError):
    """Raised when a CCAPI delete_resource_request call fails."""

    def __init__(self, resource: Any, original_error: Exception):
        """Initialize with the failed resource and the original exception."""
        self.resource = resource
        self.original_error = original_error
        super().__init__(
            f"Failed to delete {resource.type} {resource.identifier}: {original_error}"
        )


class ResourceExistenceCheckError(CloudControlError):
    """Raised when checking resource existence fails unexpectedly."""


class ResourceExistenceThrottledError(ResourceExistenceCheckError):
    """An existence check failed due to API throttling.

    Subclasses :class:`ResourceExistenceCheckError` (so existing handlers still catch it) but
    lets callers distinguish a throttle (transient, unverified) from an unsupported type.
    """


class ResourceExistenceUnsupportedError(ResourceExistenceCheckError):
    """An existence check failed because CCAPI cannot operate on this resource type.

    Subclasses :class:`ResourceExistenceCheckError` (so existing handlers still catch it) but
    lets callers distinguish a *permanently unsupported* type — where deletion via CCAPI is
    equally impossible, so skipping is correct — from a *transient/unverified* failure, where
    existence is merely unknown (not confirmed gone) and a known orphan must still be attempted
    rather than silently leaked.
    """


# CCAPI wraps service-level "not found" errors as GeneralServiceException.
# These patterns in the error message indicate the resource (or its parent) is gone.
_NOT_FOUND_PATTERNS = ("does not exist", "is not found", "not found", "could not be found")


def is_not_found_error(exc: Exception) -> bool:
    """Return True if the CCAPI exception indicates a resource or its parent is gone."""
    error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    if error_code != "GeneralServiceException":
        return False
    error_msg = str(exc).lower()
    return any(pattern in error_msg for pattern in _NOT_FOUND_PATTERNS)
