"""Tests for the EKS add-on cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import CUSTOM_DELETION_REGISTRY
from aws_bench.resource_management.cleanup.handlers.cross_service import _delete_eks_addon
from aws_bench.resource_management.cleanup.models import HandlerStatus


def _resource(identifier: str = "my-cluster|vpc-cni") -> Resource:
    return Resource(type="AWS::EKS::Addon", identifier=identifier)


def test_addon_handler_is_registered():
    assert "AWS::EKS::Addon" in CUSTOM_DELETION_REGISTRY


def test_delete_addon_splits_composite_id():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    result = _delete_eks_addon(_resource(), session)

    client.delete_addon.assert_called_once_with(clusterName="my-cluster", addonName="vpc-cni")
    assert result.status == HandlerStatus.SUCCESS


def test_delete_addon_bare_identifier_fails():
    """A bare id without ``|`` can't be routed to a cluster, so it is a hard failure."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    result = _delete_eks_addon(_resource("vpc-cni"), session)

    client.delete_addon.assert_not_called()
    assert result.status == HandlerStatus.FAILED


def test_delete_addon_already_gone_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_addon.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DeleteAddon"
    )

    result = _delete_eks_addon(_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_addon_failure_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_addon.side_effect = ClientError(
        {"Error": {"Code": "InvalidRequestException"}}, "DeleteAddon"
    )

    result = _delete_eks_addon(_resource(), session)
    assert result.status == HandlerStatus.FAILED


def test_delete_addon_connection_error_fails():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_addon.side_effect = BotoCoreError()

    result = _delete_eks_addon(_resource(), session)
    assert result.status == HandlerStatus.FAILED
