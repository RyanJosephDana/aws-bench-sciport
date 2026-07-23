"""Tests for the MediaLive cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import CUSTOM_DELETION_REGISTRY
from aws_bench.resource_management.cleanup.models import HandlerStatus


def test_delete_input_security_group_via_registry():
    """The handler deletes by the emitted InputSecurityGroup Id (the CCAPI primary identifier)."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    handler = CUSTOM_DELETION_REGISTRY.get("AWS::MediaLive::InputSecurityGroup")
    assert handler is not None
    result = handler(
        Resource(type="AWS::MediaLive::InputSecurityGroup", identifier="9589704"), session
    )
    assert result.status is HandlerStatus.SUCCESS
    client.delete_input_security_group.assert_called_once_with(InputSecurityGroupId="9589704")
