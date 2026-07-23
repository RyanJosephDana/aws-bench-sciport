"""Tests for CCAPI scanner retry logic with transient errors."""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.scanner import Scanner


def test_list_resources_retries_throttling_errors():
    """Throttling errors should not be retried (botocore handles them)."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"

    throttle_error = ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "list_resources"
    )

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = throttle_error
    mock_client.get_paginator.return_value = mock_paginator

    scanner = Scanner(mock_client, session=session)

    # Should fail immediately without custom retry (botocore already retried)
    with pytest.raises(ClientError, match="Throttling"):
        scanner._list_resources_by_type_with_retry("AWS::S3::Bucket")

    # Should not retry - only called once
    assert mock_paginator.paginate.call_count == 1


def test_list_resources_fails_fast_on_permission_errors():
    """Fails immediately on non-retryable permission errors."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"

    permission_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "No permission"}},
        "list_resources",
    )

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = permission_error
    mock_client.get_paginator.return_value = mock_paginator

    scanner = Scanner(mock_client, session=session)

    with pytest.raises(ClientError, match="AccessDeniedException"):
        scanner._list_resources_by_type_with_retry("AWS::S3::Bucket")

    # Should not retry - only called once
    assert mock_paginator.paginate.call_count == 1


def test_list_resources_retries_service_unavailable():
    """Retries on ServiceUnavailableException errors."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"

    service_error = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "Service down"}},
        "list_resources",
    )

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = [
        service_error,
        [{"ResourceDescriptions": [{"Identifier": "test-456"}]}],
    ]
    mock_client.get_paginator.return_value = mock_paginator

    scanner = Scanner(mock_client, session=session)

    with patch("time.sleep"):
        result = scanner._list_resources_by_type_with_retry("AWS::EC2::Instance")

    assert len(result) == 1
    assert mock_paginator.paginate.call_count == 2


def test_list_resources_fails_fast_on_unsupported_operation():
    """Fails immediately on UnsupportedActionException."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"

    unsupported_error = ClientError(
        {"Error": {"Code": "UnsupportedActionException", "Message": "Not supported"}},
        "list_resources",
    )

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = unsupported_error
    mock_client.get_paginator.return_value = mock_paginator

    scanner = Scanner(mock_client, session=session)

    with pytest.raises(ClientError, match="UnsupportedActionException"):
        scanner._list_resources_by_type_with_retry("AWS::CustomResource")

    assert mock_paginator.paginate.call_count == 1


def test_list_resources_raises_after_max_retries():
    """Raises after exhausting retries on retryable service errors."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"

    service_error = ClientError(
        {"Error": {"Code": "InternalServiceException", "Message": "Internal error"}},
        "list_resources",
    )

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = service_error
    mock_client.get_paginator.return_value = mock_paginator

    scanner = Scanner(mock_client, session=session)

    with patch("time.sleep"):
        with pytest.raises(ClientError, match="InternalServiceException"):
            scanner._list_resources_by_type_with_retry("AWS::Lambda::Function")

    # Should retry 3 times total
    assert mock_paginator.paginate.call_count == 3


def test_list_resources_fails_fast_on_non_client_errors():
    """Fails immediately on non-ClientError exceptions."""
    session = MagicMock(spec=boto3.Session)
    session.region_name = "us-east-1"

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = ValueError("Invalid parameter")
    mock_client.get_paginator.return_value = mock_paginator

    scanner = Scanner(mock_client, session=session)

    with pytest.raises(ValueError, match="Invalid parameter"):
        scanner._list_resources_by_type_with_retry("AWS::S3::Bucket")

    # Should not retry on non-ClientError
    assert mock_paginator.paginate.call_count == 1
