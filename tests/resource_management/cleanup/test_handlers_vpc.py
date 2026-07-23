"""Tests for VPC pre-delete discovery hook."""

from __future__ import annotations

from unittest.mock import MagicMock

from aws_bench.resource_management.cleanup.handlers.vpc import (
    _discover_vpc_dynamic_resources,
)
from aws_bench.resource_management.cleanup.models import StackResource


def test_discover_vpc_skips_empty_ids():
    session = MagicMock()
    resources = [StackResource("L1", "", "AWS::EC2::VPC", "CREATE_COMPLETE")]
    result = _discover_vpc_dynamic_resources(resources, session)
    assert result == []


def test_discover_vpc_calls_discover_function():
    from unittest.mock import patch

    session = MagicMock()
    resources = [StackResource("L1", "vpc-123", "AWS::EC2::VPC", "CREATE_COMPLETE")]
    with patch(
        "aws_bench.resource_management.cleanup.handlers.vpc.discover_vpc_dynamic_resources",
        return_value=[],
    ) as mock_discover:
        result = _discover_vpc_dynamic_resources(resources, session)
    mock_discover.assert_called_once_with(["vpc-123"], session)
    assert result == []


def test_discover_vpc_skips_non_vpc_resources():
    session = MagicMock()
    resources = [StackResource("L1", "bucket", "AWS::S3::Bucket", "CREATE_COMPLETE")]
    result = _discover_vpc_dynamic_resources(resources, session)
    assert result == []
