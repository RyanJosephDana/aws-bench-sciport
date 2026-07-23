"""Tests for the ELBv2 load balancer and target group cleanup handlers.

The load balancer needs a custom elbv2-API delete to release its service-managed
ENIs, and waits for terminal deletion so its listeners are gone. The target group
has its own elbv2-API delete handler (a target group still referenced by a
listener/rule fails to delete via CCAPI), which runs after the load balancer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError, WaiterError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import CUSTOM_DELETION_REGISTRY
from aws_bench.resource_management.cleanup.handlers.elbv2 import _delete_load_balancer
from aws_bench.resource_management.cleanup.models import HandlerStatus

_LB = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/x/abc"


def _resource() -> Resource:
    return Resource(type="AWS::ElasticLoadBalancingV2::LoadBalancer", identifier=_LB)


def test_delete_load_balancer_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete_load_balancer(_resource(), session)
    client.delete_load_balancer.assert_called_once_with(LoadBalancerArn=_LB)
    # The handler must WAIT for terminal deletion before returning success, so the
    # listeners are gone before the CCAPI phase deletes the paired target group.
    client.get_waiter.assert_called_once_with("load_balancers_deleted")
    waiter = client.get_waiter.return_value
    waiter.wait.assert_called_once()
    assert waiter.wait.call_args.kwargs["LoadBalancerArns"] == [_LB]
    assert result.status == HandlerStatus.SUCCESS


def test_delete_load_balancer_waiter_timeout_but_gone_is_success():
    """A waiter timeout is success if the load balancer is actually gone."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_waiter.return_value.wait.side_effect = WaiterError(
        "load_balancers_deleted", "max attempts exceeded", {}
    )
    client.describe_load_balancers.side_effect = ClientError(
        {"Error": {"Code": "LoadBalancerNotFound"}}, "DescribeLoadBalancers"
    )
    result = _delete_load_balancer(_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_load_balancer_waiter_timeout_still_present_is_failure():
    """A waiter timeout with the load balancer still present is a FAILURE."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_waiter.return_value.wait.side_effect = WaiterError(
        "load_balancers_deleted", "max attempts exceeded", {}
    )
    # describe returns normally -> still present.
    client.describe_load_balancers.return_value = {"LoadBalancers": [{"LoadBalancerArn": _LB}]}
    result = _delete_load_balancer(_resource(), session)
    assert result.status == HandlerStatus.FAILED


def test_delete_load_balancer_already_gone_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_load_balancer.side_effect = ClientError(
        {"Error": {"Code": "LoadBalancerNotFound"}}, "DeleteLoadBalancer"
    )
    result = _delete_load_balancer(_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_load_balancer_failure_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_load_balancer.side_effect = ClientError(
        {"Error": {"Code": "OperationNotPermitted"}}, "DeleteLoadBalancer"
    )
    result = _delete_load_balancer(_resource(), session)
    assert result.status == HandlerStatus.FAILED


def test_target_group_has_custom_delete_handler():
    """The target group now has a first-class elbv2-API delete handler.

    A target group still referenced by a listener/rule fails to delete via CCAPI
    (observed: opensearch stack DELETE_FAILED on TargetGroup…), so it gets an
    explicit elbv2-API delete handler that runs after the load balancer (whose
    handler waits for terminal deletion, removing the referencing listener).
    """
    assert "AWS::ElasticLoadBalancingV2::LoadBalancer" in CUSTOM_DELETION_REGISTRY
    assert "AWS::ElasticLoadBalancingV2::TargetGroup" in CUSTOM_DELETION_REGISTRY


_TG = "arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/x/abc"


def _tg_resource() -> Resource:
    return Resource(type="AWS::ElasticLoadBalancingV2::TargetGroup", identifier=_TG)


def test_delete_target_group_success():
    from aws_bench.resource_management.cleanup.handlers.elbv2 import _delete_target_group

    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete_target_group(_tg_resource(), session)
    client.delete_target_group.assert_called_once_with(TargetGroupArn=_TG)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_target_group_already_gone_is_success():
    from aws_bench.resource_management.cleanup.handlers.elbv2 import _delete_target_group

    session = MagicMock()
    client = MagicMock()
    client.delete_target_group.side_effect = ClientError(
        {"Error": {"Code": "TargetGroupNotFound", "Message": "gone"}}, "DeleteTargetGroup"
    )
    session.client.return_value = client
    result = _delete_target_group(_tg_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_target_group_in_use_is_failure():
    """A target group still referenced by a listener -> FAILED (retried later)."""
    from aws_bench.resource_management.cleanup.handlers.elbv2 import _delete_target_group

    session = MagicMock()
    client = MagicMock()
    client.delete_target_group.side_effect = ClientError(
        {"Error": {"Code": "ResourceInUse", "Message": "in use by a listener or a rule"}},
        "DeleteTargetGroup",
    )
    session.client.return_value = client
    result = _delete_target_group(_tg_resource(), session)
    assert result.status == HandlerStatus.FAILED
