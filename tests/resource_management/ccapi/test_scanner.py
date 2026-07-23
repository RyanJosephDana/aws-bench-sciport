"""Tests for aws_bench.resource_management.ccapi.scanner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aws_bench.exceptions import OperationCancelled
from aws_bench.resource_management.ccapi.scanner import (
    Scanner,
)
from aws_bench.resource_management.ccapi.type_registry import TypeRegistry
from aws_bench.utils import concurrent

# -- Scanner.scan_resources --


def test_scan_resources_collects_detected_and_failed():
    session = MagicMock()
    client = MagicMock()

    paginator = MagicMock()
    client.get_paginator.return_value = paginator

    def paginate_side_effect(TypeName):
        if TypeName == "AWS::S3::Bucket":
            return [{"ResourceDescriptions": [{"Identifier": "b1"}]}]
        raise Exception("unsupported")

    paginator.paginate.side_effect = paginate_side_effect

    scanner = Scanner(client, session=session)
    result = scanner.scan_resources(["AWS::S3::Bucket", "AWS::Bad::Type"], max_workers=1)
    assert "AWS::S3::Bucket" in result.detected
    assert "AWS::Bad::Type" in result.failed


def test_scan_resources_auto_discovers_types_when_none():
    """Scanner should call get_scannable_types() when resource_types is None."""
    session = MagicMock()
    client = MagicMock()

    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [{"ResourceDescriptions": [{"Identifier": "b1"}]}]

    scanner = Scanner(client, session=session)
    with patch.object(scanner, "get_scannable_types", return_value=["AWS::S3::Bucket"]) as mock_get:
        result = scanner.scan_resources(None, max_workers=1)

        mock_get.assert_called_once()
        assert "AWS::S3::Bucket" in result.detected


# -- Scanner.get_scannable_types --


@patch("aws_bench.resource_management.ccapi.scanner.TypeRegistry")
def test_get_scannable_types_excludes_skip_types(MockTypeRegistry):
    MockTypeRegistry.return_value.load_skip_types.return_value = {"AWS::Bad::Type"}
    MockTypeRegistry.return_value.list_all_resource_types.return_value = [
        "AWS::S3::Bucket",
        "AWS::Bad::Type",
    ]
    scanner = Scanner(MagicMock(), session=MagicMock())
    result = scanner.get_scannable_types()
    assert result == ["AWS::S3::Bucket"]


@patch("aws_bench.resource_management.ccapi.scanner.TypeRegistry")
def test_get_scannable_types_caches_results(MockTypeRegistry):
    """get_scannable_types should cache results and not recompute on subsequent calls."""
    MockTypeRegistry.return_value.load_skip_types.return_value = set()
    MockTypeRegistry.return_value.list_all_resource_types.return_value = [
        "AWS::S3::Bucket",
        "AWS::EC2::Instance",
    ]
    scanner = Scanner(MagicMock(), session=MagicMock())

    # First call
    result1 = scanner.get_scannable_types()
    # Second call
    result2 = scanner.get_scannable_types()

    # TypeRegistry should only be instantiated once
    assert MockTypeRegistry.call_count == 1
    # Results should be identical
    assert result1 == result2
    assert result1 == ["AWS::S3::Bucket", "AWS::EC2::Instance"]


@patch("aws_bench.resource_management.ccapi.scanner.TypeRegistry")
def test_get_scannable_types_returns_defensive_copy(MockTypeRegistry):
    """get_scannable_types should return a copy to protect the cache from mutation."""
    MockTypeRegistry.return_value.load_skip_types.return_value = set()
    MockTypeRegistry.return_value.list_all_resource_types.return_value = [
        "AWS::S3::Bucket",
        "AWS::EC2::Instance",
    ]
    scanner = Scanner(MagicMock(), session=MagicMock())

    # Get first result and mutate it
    result1 = scanner.get_scannable_types()
    result1.append("AWS::Lambda::Function")
    result1.remove("AWS::S3::Bucket")

    # Get second result - should be unchanged
    result2 = scanner.get_scannable_types()

    # Original cache should be unaffected
    assert result2 == ["AWS::S3::Bucket", "AWS::EC2::Instance"]
    # Mutated list should be different
    assert result1 == ["AWS::EC2::Instance", "AWS::Lambda::Function"]


# -- TypeRegistry.list_all_resource_types --


def test_list_all_resource_types_returns_types():
    session = MagicMock()
    cfn = MagicMock()
    session.client.return_value = cfn
    cfn.get_paginator.return_value.paginate.return_value = [
        {"TypeSummaries": [{"TypeName": "AWS::S3::Bucket"}]}
    ]
    registry = TypeRegistry(session, scan_fn=MagicMock())
    assert registry.list_all_resource_types() == ["AWS::S3::Bucket"]


def test_list_all_resource_types_raises_on_error():
    session = MagicMock()
    cfn = MagicMock()
    session.client.return_value = cfn
    cfn.get_paginator.return_value.paginate.side_effect = Exception("fail")
    registry = TypeRegistry(session, scan_fn=MagicMock())
    with pytest.raises(Exception):
        registry.list_all_resource_types()


def test_scanner_passes_region_name_to_type_registry():
    """TypeRegistry in Scanner.get_scannable_types should receive region_name."""
    session = MagicMock()
    session.region_name = "us-west-1"

    scanner = Scanner(MagicMock(), session=session, region_name="ap-southeast-1")

    with patch("aws_bench.resource_management.ccapi.scanner.TypeRegistry") as MockTypeRegistry:
        MockTypeRegistry.return_value.load_skip_types.return_value = set()
        MockTypeRegistry.return_value.list_all_resource_types.return_value = []

        scanner.get_scannable_types()

        # TypeRegistry should be called with region_name
        MockTypeRegistry.assert_called_once()
        call_args = MockTypeRegistry.call_args
        assert call_args[0][0] == session
        assert call_args[1].get("region_name") == "ap-southeast-1"


# -- Scanner retry behavior --


def test_scanner_does_not_retry_throttling_errors():
    """Scanner should not retry throttling errors as botocore already handles them."""
    from botocore.exceptions import ClientError

    session = MagicMock()
    session.region_name = "us-east-1"
    scanner = Scanner(MagicMock(), session=session)

    throttling_error = ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "ListResources"
    )

    mock_list = MagicMock(side_effect=throttling_error)
    with patch.object(scanner, "_list_resources_by_type", mock_list):
        result = scanner.scan_resources(["AWS::S3::Bucket"], max_workers=1)

        assert "AWS::S3::Bucket" in result.failed
        assert mock_list.call_count == 1


def test_scanner_retries_service_unavailable():
    """Scanner should retry ServiceUnavailableException as botocore may not handle it."""
    from botocore.exceptions import ClientError

    session = MagicMock()
    session.region_name = "us-east-1"
    scanner = Scanner(MagicMock(), session=session)

    call_count = 0

    def failing_then_success(*args):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailableException", "Message": "unavailable"}},
                "ListResources",
            )
        return []

    with (
        patch.object(scanner, "_list_resources_by_type", side_effect=failing_then_success),
        patch("aws_bench.resource_management.ccapi.scanner.time.sleep"),
    ):
        result = scanner.scan_resources(["AWS::S3::Bucket"], max_workers=1)

        assert "AWS::S3::Bucket" not in result.failed
        assert call_count == 3


# -- Scanner cooperative cancellation --


@pytest.fixture
def _clear_shutdown():
    """Reset the process-global shutdown flag around a test."""
    concurrent.reset_shutdown()
    yield
    concurrent.reset_shutdown()


def test_scan_skips_work_when_shutdown_already_requested(_clear_shutdown):
    """A shutdown flagged before the scan drains every queued type without AWS calls."""
    concurrent.request_shutdown()
    scanner = Scanner(MagicMock(), session=MagicMock(), region_name="us-east-1")

    mock_list = MagicMock(return_value=[{"Identifier": "x"}])
    with patch.object(scanner, "_list_resources_by_type", mock_list):
        with pytest.raises(OperationCancelled):
            scanner.scan_resources(["AWS::S3::Bucket", "AWS::EC2::Instance"], max_workers=2)

    # No per-type listing ran: every worker bailed at its entry checkpoint.
    mock_list.assert_not_called()


def test_scan_raises_when_shutdown_requested_mid_scan(_clear_shutdown):
    """A shutdown set partway through aborts instead of returning a partial scan.

    The first type lists normally, then sets the flag; the post-collect
    checkpoint must raise so the partial ``detected`` is never returned (and
    so never saved as a baseline).
    """
    scanner = Scanner(MagicMock(), session=MagicMock(), region_name="us-east-1")

    def list_then_flag(resource_type):
        concurrent.request_shutdown()
        return [{"Identifier": "x"}]

    with patch.object(scanner, "_list_resources_by_type", side_effect=list_then_flag):
        with pytest.raises(OperationCancelled):
            scanner.scan_resources(["AWS::S3::Bucket"], max_workers=1)


def test_scan_skips_retry_backoff_sleep_on_shutdown(_clear_shutdown):
    """A retryable error during shutdown must not sit out the backoff sleep."""
    from botocore.exceptions import ClientError

    scanner = Scanner(MagicMock(), session=MagicMock(), region_name="us-east-1")

    def fail_and_flag(resource_type):
        concurrent.request_shutdown()
        raise ClientError(
            {"Error": {"Code": "ServiceUnavailableException", "Message": "unavailable"}},
            "ListResources",
        )

    with (
        patch.object(scanner, "_list_resources_by_type", side_effect=fail_and_flag),
        patch("aws_bench.resource_management.ccapi.scanner.time.sleep") as mock_sleep,
    ):
        with pytest.raises(OperationCancelled):
            scanner.scan_resources(["AWS::S3::Bucket"], max_workers=1)

    mock_sleep.assert_not_called()
