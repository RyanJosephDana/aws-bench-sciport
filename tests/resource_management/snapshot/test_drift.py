"""Tests for snapshot.drift module."""

import json
from unittest.mock import MagicMock, patch

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from aws_bench.resource_management.snapshot.drift import (
    DRIFT_UNSUPPORTED_STATES,
    detect_stacks_drift,
    get_drift_client,
    is_resource_unreadable_reason,
)


def _reason(failure_reason: str, summary: str = "Failed to detect drift on resources [R]") -> str:
    """A DetectionStatusReason JSON blob with one Failure carrying failure_reason."""
    return json.dumps(
        {"Summary": summary, "Failures": [{"Resource": "R", "FailureReason": failure_reason}]}
    )


# ===========================================================================
# is_resource_unreadable_reason — gates a destructive delete
# ===========================================================================


def test_resource_unreadable_true_for_resource_gone_failure_reason():
    """A per-resource FailureReason meaning the resource is gone -> True."""
    assert is_resource_unreadable_reason(
        _reason(
            "Invalid request provided: Read TaskDefinition: Unable to describe task definition."
        )
    )
    assert is_resource_unreadable_reason(_reason("The resource does not exist"))
    assert is_resource_unreadable_reason(
        _reason("NoSuchBucket: The specified bucket dne (Service: S3)")
    )
    assert is_resource_unreadable_reason(_reason("ResourceNotFoundException: no such table"))


def test_resource_unreadable_false_for_transient_failure_reason():
    """A transient per-resource FailureReason -> False (must not be deleted)."""
    assert not is_resource_unreadable_reason(_reason("Rate exceeded (Service: CloudFormation)"))
    assert not is_resource_unreadable_reason(_reason("Internal Failure"))


def test_resource_unreadable_ignores_summary_only_match():
    """A resource-gone phrase in the SUMMARY but a TRANSIENT FailureReason -> False.

    Regression guard: the markers must be matched only against the per-resource
    FailureReason, never the summary, or a throttle whose summary happens to contain
    a marker would trigger a destructive delete of a healthy stack.
    """
    reason = _reason(
        failure_reason="Rate exceeded",
        summary="Failed to detect drift on resources [R]: resource not found in cache",
    )
    assert not is_resource_unreadable_reason(reason)


def test_resource_unreadable_falls_back_to_raw_string_when_not_json():
    """A non-JSON reason is scanned as a whole string (best-effort fallback)."""
    assert is_resource_unreadable_reason("does not exist")
    assert not is_resource_unreadable_reason("Rate exceeded")
    assert not is_resource_unreadable_reason("")


# ===========================================================================
# detect_stacks_drift - unsupported states
# ===========================================================================


@mock_aws
def test_detect_stacks_drift_skips_unsupported_states():
    """Stacks in unsupported states are SKIPPED with no API call."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")

    stacks = [(f"stack-{state}", state) for state in DRIFT_UNSUPPORTED_STATES]
    results = detect_stacks_drift(cfn, stacks)

    for state in DRIFT_UNSUPPORTED_STATES:
        result = results[f"stack-{state}"]
        assert result.detection_status == f"SKIPPED_{state}"
        assert result.resource_drifts == []


# ===========================================================================
# detect_stacks_drift - successful detection
# ===========================================================================


def _cfn_with_status(*statuses: str) -> MagicMock:
    """A CFN client mock whose drift-detection status walks ``statuses``."""
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "detection-123"}
    mock_cfn.describe_stack_drift_detection_status.side_effect = [
        {"DetectionStatus": s} for s in statuses
    ]
    return mock_cfn


@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_success(mock_get_drifts):
    """Completed detection returns the parsed resource drifts."""
    mock_cfn = _cfn_with_status("DETECTION_COMPLETE")
    mock_get_drifts.return_value = [
        {"LogicalResourceId": "Resource1", "StackResourceDriftStatus": "IN_SYNC"},
        {
            "LogicalResourceId": "Resource2",
            "StackResourceDriftStatus": "MODIFIED",
            "PropertyDifferences": [{"PropertyPath": "/Properties/Prop1", "ActualValue": "new"}],
        },
    ]

    results = detect_stacks_drift(mock_cfn, [("test-stack", "CREATE_COMPLETE")])

    result = results["test-stack"]
    assert result.detection_status == "DETECTION_COMPLETE"
    assert len(result.resource_drifts) == 2
    assert result.resource_drifts[0].logical_resource_id == "Resource1"
    assert result.resource_drifts[1].stack_resource_drift_status == "MODIFIED"


@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_no_drifts(mock_get_drifts):
    """Completed detection with no drifts returns an empty drift list."""
    mock_cfn = _cfn_with_status("DETECTION_COMPLETE")
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(mock_cfn, [("test-stack", "CREATE_COMPLETE")])

    result = results["test-stack"]
    assert result.detection_status == "DETECTION_COMPLETE"
    assert result.resource_drifts == []


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_polls_until_complete(mock_get_drifts, _mock_sleep):
    """An IN_PROGRESS status is polled until it reaches a terminal state."""
    mock_cfn = _cfn_with_status(
        "DETECTION_IN_PROGRESS", "DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"
    )
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(mock_cfn, [("test-stack", "CREATE_COMPLETE")])

    assert results["test-stack"].detection_status == "DETECTION_COMPLETE"
    assert mock_cfn.describe_stack_drift_detection_status.call_count == 3


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_multiple_stacks_concurrent(mock_get_drifts, _mock_sleep):
    """All stacks are submitted before any is polled, and each result is collected."""
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.side_effect = [
        {"StackDriftDetectionId": "det-1"},
        {"StackDriftDetectionId": "det-2"},
    ]
    # det-1 done immediately; det-2 still running once, then done.
    status_by_id = {
        "det-1": ["DETECTION_COMPLETE"],
        "det-2": ["DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"],
    }

    def describe(StackDriftDetectionId):
        return {"DetectionStatus": status_by_id[StackDriftDetectionId].pop(0)}

    mock_cfn.describe_stack_drift_detection_status.side_effect = describe
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(
        mock_cfn, [("stack-1", "CREATE_COMPLETE"), ("stack-2", "UPDATE_COMPLETE")]
    )

    # Both detections submitted up front (submit-all-then-poll).
    assert mock_cfn.detect_stack_drift.call_count == 2
    assert results["stack-1"].detection_status == "DETECTION_COMPLETE"
    assert results["stack-2"].detection_status == "DETECTION_COMPLETE"


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_bounds_in_flight_to_max(mock_get_drifts, _mock_sleep):
    """No more than ``max_in_flight`` detections are ever in flight at once.

    The wave loop must submit, poll to completion, and collect one wave before
    submitting the next — the guard against overwhelming CloudFormation.
    """
    mock_cfn = MagicMock()
    in_flight = 0
    peak = 0
    counter = {"n": 0}

    def detect(StackName):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        counter["n"] += 1
        return {"StackDriftDetectionId": f"det-{counter['n']}"}

    def describe(StackDriftDetectionId):
        # Each detection completes on its first poll, freeing a slot.
        nonlocal in_flight
        in_flight -= 1
        return {"DetectionStatus": "DETECTION_COMPLETE"}

    mock_cfn.detect_stack_drift.side_effect = detect
    mock_cfn.describe_stack_drift_detection_status.side_effect = describe
    mock_get_drifts.return_value = []

    stacks = [(f"stack-{i}", "CREATE_COMPLETE") for i in range(5)]
    results = detect_stacks_drift(mock_cfn, stacks, max_in_flight=2)

    assert peak <= 2
    assert mock_cfn.detect_stack_drift.call_count == 5
    assert len(results) == 5
    assert all(b.detection_status == "DETECTION_COMPLETE" for b in results.values())


@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_mixed_skipped_and_detected(mock_get_drifts):
    """A batch mixing supported and unsupported stacks resolves each correctly."""
    mock_cfn = _cfn_with_status("DETECTION_COMPLETE")
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(
        mock_cfn,
        [("good-stack", "CREATE_COMPLETE"), ("bad-stack", "ROLLBACK_COMPLETE")],
    )

    assert results["good-stack"].detection_status == "DETECTION_COMPLETE"
    assert results["bad-stack"].detection_status == "SKIPPED_ROLLBACK_COMPLETE"
    # Only the supported stack triggered an API call.
    assert mock_cfn.detect_stack_drift.call_count == 1


# ===========================================================================
# detect_stacks_drift - failure scenarios
# ===========================================================================


def test_detect_stacks_drift_handles_submit_exception():
    """An AWS SDK error while starting detection degrades that stack to DETECTION_FAILED.

    ``budget_multiplier=1`` isolates single-attempt behavior (no retry budget).
    """
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.side_effect = botocore.exceptions.ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "DetectStackDrift"
    )

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=1
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"
    assert results["test-stack"].resource_drifts == []


def test_detect_stacks_drift_failed_status():
    """A DETECTION_FAILED status returns a DETECTION_FAILED baseline.

    ``budget_multiplier=1`` isolates single-attempt terminal behavior (no
    retry budget); retry recovery is covered separately.
    """
    mock_cfn = _cfn_with_status("DETECTION_FAILED")

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=1
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"
    assert results["test-stack"].resource_drifts == []


def test_detect_stacks_drift_captures_failure_reason():
    """A DETECTION_FAILED baseline carries CloudFormation's DetectionStatusReason.

    End-to-end plumbing check: the reason must survive
    poll -> TerminalDrift -> DriftBaseline.detection_status_reason, because the
    reset classifier gates the destructive delete on it (a resource-unreadable
    reason vs a transient one). If this plumbing regresses, the reason arrives
    empty and the fix silently stops recreating undetectable stacks.
    """
    reason = (
        '{"Summary":"Failed to detect drift on resources [TaskDefinitionB36D86D9]",'
        '"Failures":[{"Resource":"TaskDefinitionB36D86D9","FailureReason":'
        '"Invalid request provided: Read TaskDefinition: Unable to describe task definition."}]}'
    )
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "detection-123"}
    mock_cfn.describe_stack_drift_detection_status.return_value = {
        "DetectionStatus": "DETECTION_FAILED",
        "DetectionStatusReason": reason,
    }

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=1
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"
    assert results["test-stack"].detection_status_reason == reason


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
def test_detect_stacks_drift_does_not_retry_resource_gone(mock_sleep):
    """A resource-unreadable failure is permanent — detected ONCE, never re-queued.

    Retrying can't recover a deleted resource; re-queueing would just burn the
    budget with backoff sleeps on every reset of a reused account.
    """
    reason = (
        '{"Summary":"Failed to detect drift on resources [R]",'
        '"Failures":[{"Resource":"R","FailureReason":"Unable to describe task definition."}]}'
    )
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "det-1"}
    mock_cfn.describe_stack_drift_detection_status.return_value = {
        "DetectionStatus": "DETECTION_FAILED",
        "DetectionStatusReason": reason,
    }

    # budget_multiplier=3 would allow retries; a resource-gone failure must NOT use them.
    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=3
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"
    assert mock_cfn.detect_stack_drift.call_count == 1  # not retried
    mock_sleep.assert_not_called()  # no backoff burned


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_retries_failed_stack(mock_get_drifts, _mock_sleep):
    """A DETECTION_FAILED stack is re-run and can recover on a retry pass.

    Only the failed subset is retried, and a subsequent success replaces the
    failed baseline — the fix for transient CloudFormation 'Internal Failure'.
    """
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "det-1"}
    # First attempt fails, retry attempt completes.
    mock_cfn.describe_stack_drift_detection_status.side_effect = [
        {"DetectionStatus": "DETECTION_FAILED"},
        {"DetectionStatus": "DETECTION_COMPLETE"},
    ]
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=3
    )

    # Recovered on retry; the failed stack was re-submitted.
    assert results["test-stack"].detection_status == "DETECTION_COMPLETE"
    assert mock_cfn.detect_stack_drift.call_count == 2


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_persistent_failure_stays_failed_after_retries(
    mock_get_drifts, _mock_sleep
):
    """A stack that fails every attempt stays DETECTION_FAILED (retry never masks)."""
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "det-1"}
    mock_cfn.describe_stack_drift_detection_status.return_value = {
        "DetectionStatus": "DETECTION_FAILED"
    }
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=3
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"
    # 1 initial + 2 retries = 3 detection attempts.
    assert mock_cfn.detect_stack_drift.call_count == 3


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.get_stack_resource_drifts")
def test_detect_stacks_drift_retries_transient_poll_error(mock_get_drifts, _mock_sleep):
    """A transient error while polling is retried, not treated as a failure.

    A failed status *read* does not mean the detection failed — it is still
    running server-side. The stack must stay pending and be re-polled, and a
    subsequent successful read should complete it normally.
    """
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "detection-123"}
    # First poll raises (transient), second poll succeeds.
    mock_cfn.describe_stack_drift_detection_status.side_effect = [
        botocore.exceptions.ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "DescribeStackDriftDetectionStatus",
        ),
        {"DetectionStatus": "DETECTION_COMPLETE"},
    ]
    mock_get_drifts.return_value = []

    results = detect_stacks_drift(mock_cfn, [("test-stack", "CREATE_COMPLETE")])

    # Recovered on the retry rather than being marked failed.
    assert results["test-stack"].detection_status == "DETECTION_COMPLETE"
    assert mock_cfn.describe_stack_drift_detection_status.call_count == 2


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.time.monotonic")
def test_detect_stacks_drift_persistent_poll_error_times_out(mock_monotonic, _mock_sleep):
    """A poll error that never clears eventually times out to DETECTION_FAILED.

    The stack stays pending across sweeps; the set-wide timeout is the backstop.
    """
    # First monotonic read sets the deadline; the next is already past it, so
    # the loop exits after one sweep. budget_multiplier=1: no retry pass.
    mock_monotonic.side_effect = [0.0, 1000.0]
    mock_cfn = MagicMock()
    mock_cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "detection-123"}
    mock_cfn.describe_stack_drift_detection_status.side_effect = botocore.exceptions.ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
        "DescribeStackDriftDetectionStatus",
    )

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=1
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"


@patch("aws_bench.resource_management.snapshot.drift.time.sleep")
@patch("aws_bench.resource_management.snapshot.drift.time.monotonic")
def test_detect_stacks_drift_times_out(mock_monotonic, _mock_sleep):
    """Detection that never reaches a terminal status degrades to DETECTION_FAILED."""
    # First monotonic read sets the deadline; the next is past it. budget_multiplier=1: no retry.
    mock_monotonic.side_effect = [0.0, 1000.0]
    mock_cfn = _cfn_with_status("DETECTION_IN_PROGRESS")

    results = detect_stacks_drift(
        mock_cfn, [("test-stack", "CREATE_COMPLETE")], budget_multiplier=1
    )

    assert results["test-stack"].detection_status == "DETECTION_FAILED"
    assert results["test-stack"].resource_drifts == []


def test_detect_stacks_drift_raises_on_shutdown():
    """A shutdown set before submitting unwinds instead of proceeding."""
    from aws_bench.exceptions import OperationCancelled
    from aws_bench.utils import concurrent

    mock_cfn = _cfn_with_status("DETECTION_IN_PROGRESS")

    concurrent.reset_shutdown()
    concurrent.request_shutdown()
    try:
        with pytest.raises(OperationCancelled):
            detect_stacks_drift(mock_cfn, [("test-stack", "CREATE_COMPLETE")])
    finally:
        concurrent.reset_shutdown()


def test_detect_stacks_drift_empty_input():
    """An empty stack list returns an empty result without any API calls."""
    mock_cfn = MagicMock()

    results = detect_stacks_drift(mock_cfn, [])

    assert results == {}
    mock_cfn.detect_stack_drift.assert_not_called()


# ===========================================================================
# get_drift_client - client creation
# ===========================================================================


@mock_aws
def test_get_drift_client_default_region():
    """Test getting drift client with default region."""
    session = boto3.Session(region_name="us-east-1")

    client = get_drift_client(session)

    assert client is not None
    assert client.meta.service_model.service_name == "cloudformation"


@mock_aws
def test_get_drift_client_custom_region():
    """Test getting drift client with custom region."""
    session = boto3.Session(region_name="us-east-1")

    client = get_drift_client(session, region_name="us-west-2")

    assert client is not None
    assert client.meta.service_model.service_name == "cloudformation"
    assert client.meta.region_name == "us-west-2"


@mock_aws
def test_get_drift_client_uses_adaptive_retries():
    """The drift client is configured with adaptive retries for throttle safety."""
    session = boto3.Session(region_name="us-east-1")

    client = get_drift_client(session)

    assert client.meta.config.retries["mode"] == "adaptive"
