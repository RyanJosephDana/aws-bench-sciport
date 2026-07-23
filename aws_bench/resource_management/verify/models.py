"""Data models for verification operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class VerifyResult:
    """Result of account state verification."""

    success: bool
    reason: str
    details: dict[str, Any] | str | None = None
    suggestion: str | None = None

    # Categorization flags for reset decisions
    is_dataset_mismatch: bool = False
    is_script_mismatch: bool = False
    # A stack's template drifted from baseline. Per-stack recoverable (reset delete+redeploys),
    # unlike a dataset mismatch which needs full cleanup.
    is_template_mismatch: bool = False

    # Specific failure data
    new_resources: dict[str, list[dict]] | None = None
    stack_status_failures: dict[str, dict[str, str]] | None = None
    drift_differences: dict[str, dict] | None = None
    # Stacks resource-unreadable now but detectable at baseline; reset recreates them.
    drift_undetectable: list[str] | None = None
    # Stacks whose template hash drifted from baseline; reset deletes them for re-setup.
    template_mismatch_stacks: list[str] | None = None


@dataclass
class DriftDetectionResult:
    """Result of drift detection and comparison."""

    success: bool
    error_reason: str
    error_details: dict | str | None = None
    drift_differences: dict | None = None
    # Stacks resource-unreadable now but detectable at baseline; reset recreates them.
    drift_undetectable: list[str] | None = None


@dataclass
class StackDriftCheckResult:
    """Result of checking drift for a single stack."""

    success: bool
    error_reason: str
    drift_diff_or_error: dict | str | None = None


@dataclass
class StackStatusCheckResult:
    """Result of stack status validation."""

    success: bool
    error_reason: str
    error_details: dict | None = None


@dataclass
class TemplateHashCheckResult:
    """Result of template hash validation."""

    success: bool
    error_reason: str
    error_details: dict | str | None = None


@dataclass
class RegionVerifyResult:
    """Result of verifying a single region."""

    region: str
    success: bool
    error_message: str | None = None
    suggestion: str | None = None


@dataclass
class AccountVerifyResult:
    """Result of verifying an entire account across multiple regions."""

    account_id: str
    environment_id: str
    success: bool
    region_results: list[RegionVerifyResult]
    error_message: str | None = None


@dataclass
class VerificationReport:
    """Aggregate result of verifying every scenario's account before a run.

    ``results`` is flat; each ``AccountVerifyResult`` carries its scenario in
    ``environment_id``. Grouping by scenario is a display concern.
    """

    passed: bool
    env_name: str
    results: list[AccountVerifyResult]
