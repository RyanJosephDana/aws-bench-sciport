"""Tests for DriftDetector."""

import json
from unittest.mock import patch

import boto3
from moto import mock_aws

from aws_bench.resource_management.snapshot.models import DriftBaseline, ResourceDrift
from aws_bench.resource_management.verify.drift_detector import DriftDetector
from aws_bench.resource_management.verify.models import DriftDetectionResult

# Patch target: detect_stacks_drift is imported into the drift_detector namespace.
_DETECT = "aws_bench.resource_management.verify.drift_detector.detect_stacks_drift"


def _error_details_dict(result: DriftDetectionResult) -> dict:
    """error_details narrowed to a dict for assertions (None or str -> empty dict)."""
    return result.error_details if isinstance(result.error_details, dict) else {}


# ===========================================================================
# DriftDetector initialization
# ===========================================================================


@mock_aws
def test_drift_detector_initialization():
    """Test DriftDetector initialization."""
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session, region="us-east-1")

    assert detector._cfn_client is not None


# ===========================================================================
# detect_and_compare_drift - success scenarios
# ===========================================================================


@mock_aws
def test_detect_and_compare_drift_all_stacks_match():
    """Test drift detection when all stacks match baseline.

    All baselines are SKIPPED, so there is nothing to detect or compare and the
    batch detector is never invoked.
    """
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    drift_baseline = {
        "stack-1": DriftBaseline(detection_status="SKIPPED_ROLLBACK_COMPLETE", resource_drifts=[]),
        "stack-2": DriftBaseline(detection_status="SKIPPED_DELETE_IN_PROGRESS", resource_drifts=[]),
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is True
    assert result.error_reason == ""
    assert result.drift_differences is None


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_with_matching_drift(mock_detect):
    """Test drift detection when current drift matches baseline."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    baseline_drifts = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="IN_SYNC",
            property_differences=[],
        )
    ]
    mock_detect.return_value = {
        "test-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE", resource_drifts=baseline_drifts
        )
    }

    drift_baseline = {
        "test-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE",
            resource_drifts=baseline_drifts,
        )
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is True
    assert result.drift_differences is None


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_skipped_current_is_not_a_mismatch(mock_detect):
    """A stack whose CURRENT drift detection was skipped is NOT a drift mismatch.

    When the stack is in a drift-unsupported state at reset time (e.g.
    ROLLBACK_COMPLETE / DELETE_FAILED), detect_stacks_drift returns a
    SKIPPED_<state> baseline with empty resource_drifts. Comparing that empty
    'current' against a real baseline would falsely report drift — but the
    stack-status check already owns that stack, so drift must defer to it and
    NOT emit a mismatch (which would double-handle the stack and fail reset).
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    # Baseline had real drift recorded at setup time...
    baseline_drifts = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="IN_SYNC",
            property_differences=[],
        )
    ]
    # ...but current detection is SKIPPED (stack now in an unsupported state).
    mock_detect.return_value = {
        "test-stack": DriftBaseline(
            detection_status="SKIPPED_ROLLBACK_COMPLETE", resource_drifts=[]
        )
    }

    drift_baseline = {
        "test-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE",
            resource_drifts=baseline_drifts,
        )
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is True
    assert result.drift_differences is None


# ===========================================================================
# detect_and_compare_drift - failure scenarios
# ===========================================================================


@mock_aws
def test_detect_and_compare_drift_stack_not_found():
    """Test drift detection when stack doesn't exist.

    The current-status lookup fails before any detection is attempted.
    """
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    drift_baseline = {
        "missing-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE",
            resource_drifts=[],
        )
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert "Failed to verify drift" in result.error_reason
    assert result.drift_differences is None


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_detection_failed(mock_detect):
    """A baseline that itself failed detection stays a generic detection failure.

    (A stack that detected at baseline but fails now is a regression handled by
    test_baseline_detected_but_now_undetectable_routes_to_delete_resetup.)
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    mock_detect.return_value = {
        "test-stack": DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[])
    }

    drift_baseline = {
        "test-stack": DriftBaseline(
            detection_status="DETECTION_FAILED",
            resource_drifts=[],
        )
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert "Drift detection failed for 1 stack(s)" in result.error_reason
    assert "test-stack" in result.error_reason
    assert result.error_details == {"failed_stacks": ["test-stack"]}
    assert not result.drift_undetectable


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_reports_all_failed_stacks(mock_detect):
    """When many stacks fail detection, the result names all of them, not just the first.

    A systemic cause (e.g. CloudFormation's concurrent-detection limit) fails a
    batch of stacks at once; reporting only the first hides the scale and makes
    it look like a single broken stack.
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    stacks = ["stack-a", "stack-b", "stack-c"]
    for name in stacks:
        cfn.create_stack(
            StackName=name,
            TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
        )

    # All three fail detection; their baselines also failed, so these are
    # generic failures (not detectable-then-regressed).
    mock_detect.return_value = {
        name: DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[])
        for name in stacks
    }
    drift_baseline = {
        name: DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[])
        for name in stacks
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert "Drift detection failed for 3 stack(s)" in result.error_reason
    assert result.error_details == {"failed_stacks": stacks}
    for name in stacks:
        assert name in result.error_reason


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_missing_result_is_a_failure(mock_detect):
    """A stack absent from detect_stacks_drift's result is treated as failed, not a crash."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    # detect_stacks_drift returns nothing for the stack (contract violation).
    mock_detect.return_value = {}
    drift_baseline = {
        "test-stack": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[])
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert "test-stack" in result.error_reason


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_carries_mismatches_alongside_failures(mock_detect):
    """A detection failure must not discard drift mismatches found on other stacks.

    reset fixes every category in one pass, so the result carries both
    failed_stacks and drift_differences.
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    for name in ["failed-stack", "drifted-stack"]:
        cfn.create_stack(
            StackName=name,
            TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
        )

    drifted = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="MODIFIED",
            property_differences=[],
        )
    ]
    mock_detect.return_value = {
        "failed-stack": DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[]),
        "drifted-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE", resource_drifts=drifted
        ),
    }
    drift_baseline = {
        # baseline also failed → generic detection failure, not a regression
        "failed-stack": DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[]),
        "drifted-stack": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[]),
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert result.error_details == {"failed_stacks": ["failed-stack"]}
    assert result.drift_differences is not None
    assert "drifted-stack" in result.drift_differences
    assert "failed-stack" not in result.drift_differences


@mock_aws
@patch(_DETECT)
def test_baseline_detected_but_now_undetectable_routes_to_delete_resetup(mock_detect):
    """A stack that drift-detected at baseline but fails now is drift_undetectable.

    The benchmark agent can delete a CloudFormation-tracked resource (e.g. an ECS
    task-definition revision), after which DetectStackDrift can no longer read it
    and returns DETECTION_FAILED forever. Retrying and in-place drift-revert both
    no-op, so such a stack must be deleted and recreated by setup. It is reported
    separately from generic detection failures so reset routes it to re-setup.
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="ecsroll-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    # Baseline detected fine (the stack WAS drift-readable at setup); current
    # detection fails with a RESOURCE-UNREADABLE reason (a tracked resource was
    # deleted out-of-band — CloudFormation can no longer describe it).
    mock_detect.return_value = {
        "ecsroll-stack": DriftBaseline(
            detection_status="DETECTION_FAILED",
            resource_drifts=[],
            detection_status_reason=(
                '{"Summary":"Failed to detect drift on resources [TaskDefinitionB36D86D9]",'
                '"Failures":[{"Resource":"TaskDefinitionB36D86D9","FailureReason":'
                '"Invalid request provided: Read TaskDefinition: Unable to describe '
                'task definition."}]}'
            ),
        )
    }
    drift_baseline = {
        "ecsroll-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE",
            resource_drifts=[
                ResourceDrift(
                    logical_resource_id="TaskDefinition",
                    stack_resource_drift_status="IN_SYNC",
                    property_differences=[],
                )
            ],
        )
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert result.drift_undetectable == ["ecsroll-stack"]
    # A regressed stack is NOT a generic detection failure: it must not land in
    # failed_stacks (which reset leaves alone), else it would never be recreated.
    assert not _error_details_dict(result).get("failed_stacks")


@mock_aws
@patch(_DETECT)
def test_transient_detection_failure_stays_generic_not_undetectable(mock_detect):
    """A TRANSIENT current failure (throttle/Internal-Failure/timeout) is NOT undetectable.

    detect_stacks_drift emits DETECTION_FAILED for transient causes too. Treating
    those as out-of-band deletion would DELETE a healthy, in-sync stack under a
    drift-API throttle storm. Only a resource-unreadable reason routes to delete;
    everything else stays a generic (non-destructive) detection failure.
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="throttled-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    for reason in (
        "Rate exceeded",
        "Internal Failure",
        "",  # timeout / submit-error path records no reason
        # Stack-level summary with a THROTTLED per-resource read: shares the
        # "Failed to detect drift on resources" summary but is not resource-gone,
        # so the reason-gate (which keys on the per-resource FailureReason) must
        # NOT classify it undetectable.
        '{"Summary":"Failed to detect drift on resources [R]","Failures":'
        '[{"Resource":"R","FailureReason":"Rate exceeded (Service: CloudFormation)"}]}',
    ):
        mock_detect.return_value = {
            "throttled-stack": DriftBaseline(
                detection_status="DETECTION_FAILED",
                resource_drifts=[],
                detection_status_reason=reason,
            )
        }
        drift_baseline = {
            "throttled-stack": DriftBaseline(
                detection_status="DETECTION_COMPLETE", resource_drifts=[]
            )
        }

        result = detector.detect_and_compare_drift(drift_baseline)

        assert result.success is False, reason
        # Transient failure must NOT be routed to destructive delete.
        assert not result.drift_undetectable, reason
        assert _error_details_dict(result).get("failed_stacks") == ["throttled-stack"], reason


@mock_aws
@patch(_DETECT)
def test_generic_failure_and_undetectable_regression_reported_together(mock_detect):
    """One result carries BOTH a generic failure and an undetectable regression.

    Reset fixes every category in one pass, so a transient-failed stack (generic)
    and a resource-unreadable stack (undetectable) detected in the same run must
    both surface on the single result, in their own buckets.
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    for name in ("throttled-stack", "deleted-resource-stack"):
        cfn.create_stack(
            StackName=name,
            TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
        )

    mock_detect.return_value = {
        "throttled-stack": DriftBaseline(
            detection_status="DETECTION_FAILED",
            resource_drifts=[],
            detection_status_reason="Rate exceeded",
        ),
        "deleted-resource-stack": DriftBaseline(
            detection_status="DETECTION_FAILED",
            resource_drifts=[],
            detection_status_reason=(
                "Failed to detect drift on resources [Foo]: Unable to describe task definition."
            ),
        ),
    }
    drift_baseline = {
        name: DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[])
        for name in ("throttled-stack", "deleted-resource-stack")
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert _error_details_dict(result).get("failed_stacks") == ["throttled-stack"]
    assert result.drift_undetectable == ["deleted-resource-stack"]
    # Two-part reason names both categories.
    assert "throttled-stack" in result.error_reason
    assert "deleted-resource-stack" in result.error_reason


@mock_aws
@patch(_DETECT)
def test_baseline_already_failed_stays_generic_failure(mock_detect):
    """If the baseline itself never detected, a current failure is not a regression.

    There is no known-good drift state to restore, so delete+resetup is not
    warranted; it stays a generic detection failure (not drift_undetectable).
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="always-broken",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    mock_detect.return_value = {
        "always-broken": DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[])
    }
    drift_baseline = {
        "always-broken": DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[])
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert not result.drift_undetectable
    assert _error_details_dict(result).get("failed_stacks") == ["always-broken"]


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_differences_found(mock_detect):
    """Test drift detection when drift differs from baseline."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    current_drifts = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="MODIFIED",
            property_differences=[{"PropertyPath": "/Prop", "ActualValue": "new"}],
        )
    ]
    mock_detect.return_value = {
        "test-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE", resource_drifts=current_drifts
        )
    }

    baseline_drifts = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="IN_SYNC",
            property_differences=[],
        )
    ]
    drift_baseline = {
        "test-stack": DriftBaseline(
            detection_status="DETECTION_COMPLETE",
            resource_drifts=baseline_drifts,
        )
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert "have different drift from baseline" in result.error_reason
    assert result.drift_differences is not None
    assert "test-stack" in result.drift_differences


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_multiple_stacks_with_differences(mock_detect):
    """Test drift detection with multiple stacks having differences."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    for stack_name in ["stack-1", "stack-2"]:
        cfn.create_stack(
            StackName=stack_name,
            TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
        )

    # Both stacks come back with drift that differs from their (empty) baseline.
    drifted = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="MODIFIED",
            property_differences=[],
        )
    ]
    mock_detect.return_value = {
        "stack-1": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=drifted),
        "stack-2": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=drifted),
    }

    drift_baseline = {
        "stack-1": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[]),
        "stack-2": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[]),
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is False
    assert "2 stack(s) have different drift" in result.error_reason
    assert result.drift_differences is not None
    assert len(result.drift_differences) == 2


@mock_aws
@patch(_DETECT)
def test_detect_and_compare_drift_only_checks_non_skipped_stacks(mock_detect):
    """Baselines that were SKIPPED at capture are excluded from current detection."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    detector = DriftDetector(session)

    cfn.create_stack(
        StackName="live-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    mock_detect.return_value = {
        "live-stack": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[])
    }

    drift_baseline = {
        "live-stack": DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[]),
        "skipped-stack": DriftBaseline(
            detection_status="SKIPPED_ROLLBACK_COMPLETE", resource_drifts=[]
        ),
    }

    result = detector.detect_and_compare_drift(drift_baseline)

    assert result.success is True
    # Only the non-skipped stack was submitted for detection.
    submitted_stacks = [name for name, _ in mock_detect.call_args.args[1]]
    assert submitted_stacks == ["live-stack"]


# ===========================================================================
# _compare_current_drift - single-stack comparison unit
# ===========================================================================


@mock_aws
def test_compare_current_drift_skipped_current_defers():
    """A SKIPPED current drift defers to the stack-status check (success, no diff)."""
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    baseline = DriftBaseline(
        detection_status="DETECTION_COMPLETE",
        resource_drifts=[
            ResourceDrift(
                logical_resource_id="Resource1",
                stack_resource_drift_status="IN_SYNC",
                property_differences=[],
            )
        ],
    )
    current = DriftBaseline(detection_status="SKIPPED_ROLLBACK_COMPLETE", resource_drifts=[])

    result = detector._compare_current_drift("test-stack", baseline, current)

    assert result.success is True
    assert result.drift_diff_or_error is None


@mock_aws
def test_compare_current_drift_detection_failed():
    """A DETECTION_FAILED current drift is reported as a verification failure."""
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    baseline = DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=[])
    current = DriftBaseline(detection_status="DETECTION_FAILED", resource_drifts=[])

    result = detector._compare_current_drift("test-stack", baseline, current)

    assert result.success is False
    assert "Drift detection failed" in result.error_reason


@mock_aws
def test_compare_current_drift_matches_baseline():
    """Matching current and baseline drift yields success with no diff."""
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    drifts = [
        ResourceDrift(
            logical_resource_id="Resource1",
            stack_resource_drift_status="IN_SYNC",
            property_differences=[],
        )
    ]
    baseline = DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=drifts)
    current = DriftBaseline(detection_status="DETECTION_COMPLETE", resource_drifts=drifts)

    result = detector._compare_current_drift("test-stack", baseline, current)

    assert result.success is True
    assert result.drift_diff_or_error is None


@mock_aws
def test_compare_current_drift_differs_from_baseline():
    """Differing current and baseline drift yields a populated diff."""
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    current = DriftBaseline(
        detection_status="DETECTION_COMPLETE",
        resource_drifts=[
            ResourceDrift(
                logical_resource_id="Resource1",
                stack_resource_drift_status="MODIFIED",
                property_differences=[{"PropertyPath": "/Prop"}],
            )
        ],
    )
    baseline = DriftBaseline(
        detection_status="DETECTION_COMPLETE",
        resource_drifts=[
            ResourceDrift(
                logical_resource_id="Resource1",
                stack_resource_drift_status="IN_SYNC",
                property_differences=[],
            )
        ],
    )

    result = detector._compare_current_drift("test-stack", baseline, current)

    assert result.success is True
    assert result.drift_diff_or_error is not None
    assert "baseline" in result.drift_diff_or_error
    assert "current" in result.drift_diff_or_error


# ===========================================================================
# _get_stack_status - current status lookup
# ===========================================================================


@mock_aws
def test_get_stack_status_missing_stack_returns_failure():
    """A missing stack yields a failure result rather than raising."""
    session = boto3.Session(region_name="us-east-1")
    detector = DriftDetector(session)

    result = detector._get_stack_status("missing-stack")

    # Non-string (StackDriftCheckResult) signals failure to the caller.
    assert not isinstance(result, str)
    assert result.success is False
    assert "Failed to verify drift" in result.error_reason
