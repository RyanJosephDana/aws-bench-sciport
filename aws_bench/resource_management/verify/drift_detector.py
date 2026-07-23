"""Drift detection operations for verification."""

from __future__ import annotations

import boto3
import botocore.exceptions

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.snapshot.drift import (
    detect_stacks_drift,
    get_drift_client,
    is_resource_unreadable_reason,
)
from aws_bench.resource_management.snapshot.models import DriftBaseline
from aws_bench.resource_management.utils.cloudformation import (
    DRIFT_DETECTION_COMPLETE,
    DRIFT_DETECTION_FAILED,
)
from aws_bench.resource_management.verify.comparators import drifts_match
from aws_bench.resource_management.verify.models import DriftDetectionResult, StackDriftCheckResult

logger = get_logger(__name__)


class DriftDetector:
    """Handles CloudFormation drift detection and comparison."""

    def __init__(
        self,
        session: boto3.Session,
        region: str | None = None,
    ):
        """Initialize drift detector.

        Args:
            session: Boto3 session for AWS API calls (scan account)
            region: Optional AWS region override
        """
        self._cfn_client = get_drift_client(session, region_name=region)

    def _success_result(self, drift_diff: dict | None = None) -> StackDriftCheckResult:
        """Create success result."""
        return StackDriftCheckResult(success=True, error_reason="", drift_diff_or_error=drift_diff)

    def detect_and_compare_drift(
        self,
        drift_baseline: dict[str, DriftBaseline],
    ) -> DriftDetectionResult:
        """Detect drift on all stacks (statuses resolved first) and compare to baseline.

        Each stack lands in one of: matches baseline; ``drift_differences`` (reset
        reverts in place); ``error_details.failed_stacks`` (transient, non-actionable);
        or ``drift_undetectable`` — detected at baseline but now resource-unreadable,
        which reset recreates via delete + re-setup.
        """
        logger.debug(f"Checking drift for {len(drift_baseline)} stack(s)")
        logger.debug(f"Stacks to check: {list(drift_baseline.keys())}")

        # Skipped baselines have nothing to compare.
        checkable = {
            name: baseline
            for name, baseline in drift_baseline.items()
            if not baseline.detection_status.startswith("SKIPPED_")
        }

        # A describe failure means we can't verify that stack — fail fast.
        stack_statuses: dict[str, str] = {}
        for stack_name in checkable:
            status = self._get_stack_status(stack_name)
            if isinstance(status, StackDriftCheckResult):
                return self._handle_detection_failure(status, stack_name)
            stack_statuses[stack_name] = status

        current_drifts = detect_stacks_drift(self._cfn_client, list(stack_statuses.items()))

        drift_differences = {}
        detection_failures: list[str] = []
        drift_undetectable: list[str] = []
        for stack_name, baseline_drift in checkable.items():
            # detect_stacks_drift returns an entry per input stack; treat a
            # missing one as a detection failure rather than crashing.
            current_drift = current_drifts.get(stack_name)
            if current_drift is None:
                detection_failures.append(stack_name)
                logger.debug(f"Failed: {stack_name} - no drift result returned")
                continue

            if self._is_undetectable_regression(baseline_drift, current_drift):
                drift_undetectable.append(stack_name)
                logger.debug(f"Undetectable now (was detectable at baseline): {stack_name}")
                continue

            check_result = self._compare_current_drift(stack_name, baseline_drift, current_drift)

            if not check_result.success:
                # Collect all failures, not just the first — a systemic cause
                # fails many stacks at once and reporting one hides the scale.
                detection_failures.append(stack_name)
                logger.debug(f"Failed: {stack_name} - {check_result.error_reason}")
            elif check_result.drift_diff_or_error is not None:
                drift_differences[stack_name] = check_result.drift_diff_or_error
                logger.debug(f"Drift mismatch: {stack_name}")
            else:
                logger.debug(f"Drift matches: {stack_name}")

        if detection_failures or drift_undetectable:
            # Keep mismatches from stacks that DID detect so reset fixes them in
            # this pass; a sibling's failure must not drop fixable drift.
            return self._detection_failure_result(
                detection_failures, drift_differences or None, drift_undetectable or None
            )

        return self._build_result(drift_differences)

    def _is_undetectable_regression(
        self, baseline_drift: DriftBaseline, current_drift: DriftBaseline
    ) -> bool:
        """True if a stack drifted-clean at baseline but is now resource-unreadable.

        Gated on the failure reason, not the bare status: ``DETECTION_FAILED`` also
        covers transient causes (throttle/timeout) that must not be deleted.
        """
        return (
            current_drift.detection_status == DRIFT_DETECTION_FAILED
            and baseline_drift.detection_status == DRIFT_DETECTION_COMPLETE
            and is_resource_unreadable_reason(current_drift.detection_status_reason)
        )

    def _compare_current_drift(
        self, stack_name: str, baseline_drift: DriftBaseline, current_drift: DriftBaseline
    ) -> StackDriftCheckResult:
        """Compare an already-detected current drift against its baseline."""
        if current_drift.detection_status == DRIFT_DETECTION_FAILED:
            return StackDriftCheckResult(
                success=False,
                error_reason=f"Drift detection failed for {stack_name}",
                drift_diff_or_error="Detection failed",
            )

        # A stack now in a drift-unsupported state yields an empty SKIPPED_<state>
        # current; comparing it to a real baseline would falsely report drift.
        # The stack-status check already owns it, so defer rather than mismatch.
        if current_drift.detection_status.startswith("SKIPPED_"):
            logger.debug(
                f"Skipping drift comparison for {stack_name}: current state "
                f"{current_drift.detection_status} is handled by the stack-status check"
            )
            return self._success_result()

        return self._compare_drift(baseline_drift, current_drift)

    def _get_stack_status(self, stack_name: str) -> str | StackDriftCheckResult:
        """Get current stack status, or error result if failed."""
        try:
            stack_resp = self._cfn_client.describe_stacks(StackName=stack_name)
            return stack_resp["Stacks"][0]["StackStatus"]
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            logger.error(f"Error checking stack status for '{stack_name}': {e}")
            return StackDriftCheckResult(
                success=False,
                error_reason=f"Failed to verify drift for {stack_name}",
                drift_diff_or_error=str(e),
            )

    def _compare_drift(
        self, baseline_drift: DriftBaseline, current_drift: DriftBaseline
    ) -> StackDriftCheckResult:
        """Compare baseline and current drift."""
        baseline_list = self._drift_to_comparable(baseline_drift)
        current_list = self._drift_to_comparable(current_drift)

        if not drifts_match(current_list, baseline_list):
            return StackDriftCheckResult(
                success=True,
                error_reason="",
                drift_diff_or_error={"baseline": baseline_list, "current": current_list},
            )

        return self._success_result()

    def _drift_to_comparable(self, drift: DriftBaseline) -> list[dict]:
        """Convert DriftBaseline to comparable format."""
        return [
            {
                "LogicalResourceId": rd.logical_resource_id,
                "StackResourceDriftStatus": rd.stack_resource_drift_status,
                "PropertyDifferences": rd.property_differences,
            }
            for rd in drift.resource_drifts
        ]

    def _handle_detection_failure(
        self, check_result: StackDriftCheckResult, stack_name: str
    ) -> DriftDetectionResult:
        """Handle a single-stack failure raised before detection (status lookup)."""
        logger.debug(f"Failed: {stack_name} - {check_result.error_reason}")
        return DriftDetectionResult(
            success=False,
            error_reason=check_result.error_reason,
            error_details=check_result.drift_diff_or_error,
            drift_differences=None,
        )

    def _detection_failure_result(
        self,
        failed_stacks: list[str],
        drift_differences: dict | None = None,
        drift_undetectable: list[str] | None = None,
    ) -> DriftDetectionResult:
        """Build the failure result with each category in its own bucket for reset."""
        undetectable = sorted(drift_undetectable or [])
        reason_parts = []
        if failed_stacks:
            reason_parts.append(
                f"Drift detection failed for {len(failed_stacks)} stack(s): "
                f"{', '.join(sorted(failed_stacks))}"
            )
        if undetectable:
            reason_parts.append(
                f"{len(undetectable)} stack(s) no longer drift-detectable "
                f"(will be recreated): {', '.join(undetectable)}"
            )
        logger.debug(
            f"Drift detection failed for {len(failed_stacks)} stack(s); "
            f"{len(undetectable)} undetectable regression(s)"
        )
        return DriftDetectionResult(
            success=False,
            error_reason="; ".join(reason_parts),
            error_details={"failed_stacks": sorted(failed_stacks)} if failed_stacks else None,
            drift_differences=drift_differences,
            drift_undetectable=undetectable or None,
        )

    def _build_result(self, drift_differences: dict) -> DriftDetectionResult:
        """Build final drift detection result."""
        if drift_differences:
            num_stacks = len(drift_differences)
            logger.debug(f"Drift detection: {num_stacks} mismatch(es) found")
            return DriftDetectionResult(
                success=False,
                error_reason=f"{num_stacks} stack(s) have different drift from baseline",
                error_details=None,
                drift_differences=drift_differences,
            )

        logger.debug("Drift detection: all stacks match baseline")
        return DriftDetectionResult(
            success=True, error_reason="", error_details=None, drift_differences=None
        )
