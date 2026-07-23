"""Tests for the EventBridge rule cleanup handler.

The handler removes a rule's targets and deletes the rule with ``Force=True`` so a
service-managed rule (e.g. EKS Auto Mode's ``EKSComputeManagedRule``) — which a
plain ``DeleteRule`` rejects — is actually removed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import CUSTOM_DELETION_REGISTRY
from aws_bench.resource_management.cleanup.handlers.events import (
    _delete_rule,
    _parse_rule_identifier,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_RULE_ARN = "arn:aws:events:us-east-1:111122223333:rule/EKSComputeManagedRule"


def _resource(identifier: str = _RULE_ARN) -> Resource:
    return Resource(type="AWS::Events::Rule", identifier=identifier)


def test_rule_handler_is_registered():
    assert "AWS::Events::Rule" in CUSTOM_DELETION_REGISTRY


def test_parse_rule_identifier_default_bus_arn():
    assert _parse_rule_identifier(_RULE_ARN) == ("default", "EKSComputeManagedRule")


def test_parse_rule_identifier_custom_bus_arn():
    arn = "arn:aws:events:us-east-1:111122223333:rule/my-bus/my-rule"
    assert _parse_rule_identifier(arn) == ("my-bus", "my-rule")


def test_parse_rule_identifier_ccapi_composite():
    assert _parse_rule_identifier("my-bus|my-rule") == ("my-bus", "my-rule")


def test_parse_rule_identifier_bare_name():
    assert _parse_rule_identifier("MyRule") == ("default", "MyRule")


def test_delete_rule_removes_targets_then_deletes_with_force():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Targets": [{"Id": "t1"}, {"Id": "t2"}]}]
    client.get_paginator.return_value = paginator

    result = _delete_rule(_resource(), session)

    client.remove_targets.assert_called_once_with(
        Rule="EKSComputeManagedRule", EventBusName="default", Ids=["t1", "t2"], Force=True
    )
    client.delete_rule.assert_called_once_with(
        Name="EKSComputeManagedRule", EventBusName="default", Force=True
    )
    assert result.status == HandlerStatus.SUCCESS


def test_delete_rule_no_targets_skips_remove_targets():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Targets": []}]
    client.get_paginator.return_value = paginator

    result = _delete_rule(_resource(), session)

    client.remove_targets.assert_not_called()
    client.delete_rule.assert_called_once_with(
        Name="EKSComputeManagedRule", EventBusName="default", Force=True
    )
    assert result.status == HandlerStatus.SUCCESS


def test_delete_rule_already_gone_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    paginator = MagicMock()
    paginator.paginate.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "ListTargetsByRule"
    )
    client.get_paginator.return_value = paginator

    result = _delete_rule(_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_rule_failure_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Targets": []}]
    client.get_paginator.return_value = paginator
    client.delete_rule.side_effect = ClientError(
        {"Error": {"Code": "ManagedRuleException"}}, "DeleteRule"
    )

    result = _delete_rule(_resource(), session)
    assert result.status == HandlerStatus.FAILED
