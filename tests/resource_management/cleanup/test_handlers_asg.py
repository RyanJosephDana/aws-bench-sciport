"""Tests for AutoScaling Group cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.asg import (
    _all_stopped,
    _disable_scaling,
    _find_instances,
    _find_via_asg_api,
    _find_via_ec2_tags,
    _terminate_instances,
)
from aws_bench.resource_management.cleanup.handlers.asg import (
    _prepare as _prepare_asg,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus


def test_prepare_asg_full_flow():
    session = MagicMock()
    asg = MagicMock()
    ec2 = MagicMock()
    session.client.side_effect = lambda svc: asg if svc == "autoscaling" else ec2
    asg.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"Instances": [{"InstanceId": "i-1"}]}]
    }
    ec2.describe_instances.return_value = {"Reservations": []}
    ec2.terminate_instances.return_value = {}
    with patch(
        "aws_bench.resource_management.cleanup.handlers.asg.wait_until",
        return_value=True,
    ):
        _prepare_asg(
            Resource(type="AWS::AutoScaling::AutoScalingGroup", identifier="asg-1"), session
        )
    ec2.terminate_instances.assert_called_once()


def test_prepare_asg_noop_no_instances():
    session = MagicMock()
    asg = MagicMock()
    ec2 = MagicMock()
    session.client.side_effect = lambda svc: asg if svc == "autoscaling" else ec2
    asg.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [{"Instances": []}]}
    ec2.describe_instances.return_value = {"Reservations": []}
    _prepare_asg(Resource(type="AWS::AutoScaling::AutoScalingGroup", identifier="asg-1"), session)
    ec2.terminate_instances.assert_not_called()


def test_prepare_asg_early_return_on_scaling_skip():
    """Test early return when scaling disable is skipped."""
    session = MagicMock()
    asg = MagicMock()
    ec2 = MagicMock()
    session.client.side_effect = lambda svc: asg if svc == "autoscaling" else ec2
    # Scaling disable fails
    asg.suspend_processes.side_effect = Exception("ASG not found")
    result = _prepare_asg(
        Resource(type="AWS::AutoScaling::AutoScalingGroup", identifier="asg-1"), session
    )
    # Should return SKIPPED status from _disable_scaling
    assert result.status == HandlerStatus.SKIPPED
    assert "Could not update ASG" in result.message


def test_disable_scaling_success():
    asg = MagicMock()
    _disable_scaling(asg, "asg-1")
    asg.suspend_processes.assert_called_once()
    asg.update_auto_scaling_group.assert_called_once()


def test_disable_scaling_handles_error():
    asg = MagicMock()
    asg.suspend_processes.side_effect = Exception("fail")
    _disable_scaling(asg, "asg-1")


def test_terminate_instances_success():
    asg = MagicMock()
    ec2 = MagicMock()
    with patch(
        "aws_bench.resource_management.cleanup.handlers.asg.wait_until",
        return_value=True,
    ):
        _terminate_instances(asg, ec2, "asg-1", ["i-1"])
    ec2.terminate_instances.assert_called_once()


def test_terminate_instances_handles_protection_error():
    asg = MagicMock()
    ec2 = MagicMock()
    asg.set_instance_protection.side_effect = Exception("fail")
    with patch(
        "aws_bench.resource_management.cleanup.handlers.asg.wait_until",
        return_value=True,
    ):
        _terminate_instances(asg, ec2, "asg-1", ["i-1"])
    ec2.terminate_instances.assert_called_once()


def test_terminate_instances_returns_failed_on_terminate_error():
    asg = MagicMock()
    ec2 = MagicMock()
    ec2.terminate_instances.side_effect = Exception("fail")
    result = _terminate_instances(asg, ec2, "asg-1", ["i-1"])
    assert result.status == HandlerStatus.FAILED


def test_all_stopped_true():
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "terminated"}}]}]
    }
    assert _all_stopped(ec2, ["i-1"]) is True


def test_all_stopped_false():
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}]
    }
    assert _all_stopped(ec2, ["i-1"]) is False


def test_all_stopped_error():
    ec2 = MagicMock()
    ec2.describe_instances.side_effect = Exception("fail")
    assert _all_stopped(ec2, ["i-1"]) is False


def test_all_stopped_empty_reservations():
    """Test defensive check for empty Reservations list."""
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": []}
    assert _all_stopped(ec2, ["i-1"]) is False


def test_all_stopped_none_reservations():
    """Test defensive check for None Reservations value."""
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": None}
    assert _all_stopped(ec2, ["i-1"]) is False


def test_all_stopped_empty_instances():
    """Test defensive check for empty Instances list."""
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": []}]}
    assert _all_stopped(ec2, ["i-1"]) is False


def test_all_stopped_none_instances():
    """Test defensive check for None Instances value."""
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": None}]}
    assert _all_stopped(ec2, ["i-1"]) is False


def test_find_instances_combines_sources():
    asg = MagicMock()
    ec2 = MagicMock()
    asg.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"Instances": [{"InstanceId": "i-1"}]}]
    }
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"InstanceId": "i-2"}]}]}
    result = _find_instances(asg, ec2, "asg-1")
    assert set(result) == {"i-1", "i-2"}


def test_find_via_asg_api_success():
    asg = MagicMock()
    asg.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"Instances": [{"InstanceId": "i-1"}]}]
    }
    assert _find_via_asg_api(asg, "asg-1") == ["i-1"]


def test_find_via_asg_api_error():
    asg = MagicMock()
    asg.describe_auto_scaling_groups.side_effect = Exception("fail")
    with pytest.raises(Exception):
        _find_via_asg_api(asg, "asg-1")


def test_find_via_ec2_tags_success():
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}
    assert _find_via_ec2_tags(ec2, "asg-1", exclude=set()) == ["i-1"]


def test_find_via_ec2_tags_excludes():
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}
    assert _find_via_ec2_tags(ec2, "asg-1", exclude={"i-1"}) == []


def test_find_via_ec2_tags_error():
    ec2 = MagicMock()
    ec2.describe_instances.side_effect = Exception("fail")
    with pytest.raises(Exception):
        _find_via_ec2_tags(ec2, "asg-1", exclude=set())


def test_find_instances_returns_empty_when_both_discovery_methods_fail():
    asg = MagicMock()
    ec2 = MagicMock()
    asg.describe_auto_scaling_groups.side_effect = Exception("asg fail")
    ec2.describe_instances.side_effect = Exception("ec2 fail")
    assert _find_instances(asg, ec2, "asg-1") == []


def test_find_instances_succeeds_when_one_method_fails():
    asg = MagicMock()
    ec2 = MagicMock()
    asg.describe_auto_scaling_groups.side_effect = Exception("asg fail")
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}
    result = _find_instances(asg, ec2, "asg-1")
    assert result == ["i-1"]


def test_prepare_asg_returns_on_terminate_failure():
    from aws_bench.resource_management.cleanup.models import HandlerStatus

    session = MagicMock()
    asg = MagicMock()
    ec2 = MagicMock()
    session.client.side_effect = lambda svc: asg if svc == "autoscaling" else ec2
    asg.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"Instances": [{"InstanceId": "i-1"}]}]
    }
    ec2.describe_instances.return_value = {"Reservations": []}
    with patch(
        "aws_bench.resource_management.cleanup.handlers.asg.wait_until",
        return_value=False,
    ):
        result = _prepare_asg(
            Resource(type="AWS::AutoScaling::AutoScalingGroup", identifier="asg-1"), session
        )
    assert result.status == HandlerStatus.FAILED


def test_terminate_instances_timeout():
    from aws_bench.resource_management.cleanup.models import HandlerStatus

    asg = MagicMock()
    ec2 = MagicMock()
    with patch(
        "aws_bench.resource_management.cleanup.handlers.asg.wait_until",
        return_value=False,
    ):
        result = _terminate_instances(asg, ec2, "asg-1", ["i-1"])
    assert result.status == HandlerStatus.FAILED
    assert "Timed out" in result.message
