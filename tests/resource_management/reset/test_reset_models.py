"""Tests for reset models."""

from aws_bench.resource_management.reset.models import (
    ResetFailure,
    ResetResult,
)

# ===========================================================================
# ResetResult — result dataclass
# ===========================================================================


def test_reset_result_creation():
    """Test ResetResult dataclass creation."""
    result = ResetResult(
        success=True,
        reason="Reset completed successfully",
        details={"stacks_fixed": 2, "resources_deleted": 5},
    )

    assert result.success is True
    assert result.reason == "Reset completed successfully"
    assert result.details == {"stacks_fixed": 2, "resources_deleted": 5}


# ===========================================================================
# ResetFailure — failure exception
# ===========================================================================


def test_reset_failure_exception():
    """Test ResetFailure exception creation."""
    error = ResetFailure("Failed to delete stack")

    assert "Failed to delete stack" in str(error)


def test_reset_failure_with_details():
    """Test ResetFailure with structured details."""
    error = ResetFailure(
        reason="Failed to fix drift on 2 stacks",
        details={"failed_stacks": ["stack1", "stack2"]},
        suggestion="Run 'aws-bench env cleanup' for full reset",
    )

    assert "Failed to fix drift on 2 stacks" in str(error)
    assert error.details == {"failed_stacks": ["stack1", "stack2"]}
    assert error.suggestion == "Run 'aws-bench env cleanup' for full reset"


def test_reset_failure_attributes():
    """Test ResetFailure preserves all attributes."""
    error = ResetFailure(
        reason="Test failure",
        details={"key": "value"},
        suggestion="Try again",
    )

    assert error.reason == "Test failure"
    assert error.details == {"key": "value"}
    assert error.suggestion == "Try again"
