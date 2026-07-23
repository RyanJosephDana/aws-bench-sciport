"""AutoScaling Group cleanup handler."""

from __future__ import annotations

import boto3

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.resource_management.utils.polling import wait_until
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_TERMINATE_TIMEOUT = 300
_TERMINATE_INTERVAL = 10


@resource_handler("AWS::AutoScaling::AutoScalingGroup", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    asg_client = build_client(session, "autoscaling")
    ec2 = build_client(session, "ec2")
    asg_name = resource.identifier

    scaling_result = _disable_scaling(asg_client, asg_name)
    if scaling_result.status == HandlerStatus.SKIPPED:
        return scaling_result
    instance_ids = _find_instances(asg_client, ec2, asg_name)
    if not instance_ids:
        return scaling_result

    terminate_result = _terminate_instances(asg_client, ec2, asg_name, instance_ids)
    if terminate_result.status == HandlerStatus.FAILED:
        return terminate_result
    return HandlerResult(
        resource_id=asg_name,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message=f"{scaling_result.message}; {terminate_result.message}",
    )


def _disable_scaling(asg_client, asg_name: str) -> HandlerResult:
    try:
        asg_client.suspend_processes(
            AutoScalingGroupName=asg_name,
            ScalingProcesses=[
                "Launch",
                "AZRebalance",
                "AlarmNotification",
                "ScheduledActions",
                "ReplaceUnhealthy",
            ],
        )
        asg_client.update_auto_scaling_group(
            AutoScalingGroupName=asg_name, MinSize=0, MaxSize=0, DesiredCapacity=0
        )
        return HandlerResult(
            resource_id=asg_name,
            resource_type="AWS::AutoScaling::AutoScalingGroup",
            action="disable_scaling",
            status=HandlerStatus.SUCCESS,
        )
    except Exception as e:
        return HandlerResult(
            resource_id=asg_name,
            resource_type="AWS::AutoScaling::AutoScalingGroup",
            action="disable_scaling",
            status=HandlerStatus.SKIPPED,
            message=f"Could not update ASG: {e}",
        )


def _terminate_instances(asg_client, ec2, asg_name: str, instance_ids: list[str]) -> HandlerResult:
    try:
        asg_client.set_instance_protection(
            InstanceIds=instance_ids, AutoScalingGroupName=asg_name, ProtectedFromScaleIn=False
        )
    except Exception as e:
        logger.warning(
            "Could not remove scale-in protection for '%s': %s", asg_name[:LOG_TRUNCATE_MEDIUM], e
        )

    try:
        ec2.terminate_instances(InstanceIds=instance_ids)
    except Exception as e:
        return HandlerResult(
            resource_id=asg_name,
            resource_type="AWS::AutoScaling::AutoScalingGroup",
            action="terminate_instances",
            status=HandlerStatus.FAILED,
            message=f"Failed to terminate instances for ASG '{asg_name}': {e}",
        )

    if not wait_until(
        lambda: _all_stopped(ec2, instance_ids),
        timeout=_TERMINATE_TIMEOUT,
        interval=_TERMINATE_INTERVAL,
    ):
        return HandlerResult(
            resource_id=asg_name,
            resource_type="AWS::AutoScaling::AutoScalingGroup",
            action="terminate_instances",
            status=HandlerStatus.FAILED,
            message=f"Timed out waiting for {len(instance_ids)} instances to terminate",
        )
    return HandlerResult(
        resource_id=asg_name,
        resource_type="AWS::AutoScaling::AutoScalingGroup",
        action="terminate_instances",
        status=HandlerStatus.SUCCESS,
        message=f"Terminated {len(instance_ids)} instances",
    )


def _all_stopped(ec2, instance_ids: list[str]) -> bool:
    try:
        resp = ec2.describe_instances(InstanceIds=instance_ids)
        reservations = resp.get("Reservations") or []
        if not reservations:
            return False
        instances = [inst for res in reservations for inst in res.get("Instances") or []]
        if not instances:
            return False
        return all(inst["State"]["Name"] in ("terminated", "shutting-down") for inst in instances)
    except Exception as e:
        logger.debug("Error checking instance states for '%s': %s", instance_ids, e)
        return False


def _find_instances(asg_client, ec2, asg_name: str) -> list[str]:
    asg_error, ec2_error = None, None

    try:
        ids = _find_via_asg_api(asg_client, asg_name)
    except Exception as e:
        asg_error = e
        ids = []

    try:
        ids += _find_via_ec2_tags(ec2, asg_name, exclude=set(ids))
    except Exception as e:
        ec2_error = e

    if not ids and (asg_error or ec2_error):
        logger.warning(
            "Instance discovery failed for ASG '%s': ASG API: %s, EC2 tags: %s",
            asg_name,
            asg_error,
            ec2_error,
        )
    return ids


def _find_via_asg_api(asg_client, asg_name: str) -> list[str]:
    return [
        inst["InstanceId"]
        for group in asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name]).get(
            "AutoScalingGroups", []
        )
        for inst in group.get("Instances", [])
    ]


def _find_via_ec2_tags(ec2, asg_name: str, *, exclude: set[str]) -> list[str]:
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:aws:autoscaling:groupName", "Values": [asg_name]},
            {
                "Name": "instance-state-name",
                "Values": ["running", "pending", "stopping", "shutting-down"],
            },
        ]
    )
    return [
        inst["InstanceId"]
        for res in resp.get("Reservations", [])
        for inst in res.get("Instances", [])
        if inst["InstanceId"] not in exclude
    ]
