"""EMR Cluster cleanup handler.

A cluster must be terminated before deletion: the prepare step disables
termination protection (auto-enabled on multi-primary/HA clusters, where it makes
terminate_job_flows a no-op) and calls terminate_job_flows. The delete step polls
until the cluster reaches a terminal state, then waits for EMR to reap the managed
EC2 fleet (instances, volumes, ENIs, placement group) so it is not left as orphans.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.resource_management.utils.polling import wait_until
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_NOT_FOUND_CODES = ("ClusterNotFound", "InvalidRequestException")
_TERMINAL_STATES = {"TERMINATED", "TERMINATED_WITH_ERRORS"}

_TERMINATION_TIMEOUT = 900  # 15 minutes
_TERMINATION_POLL_INTERVAL = 15

# After the cluster reaches a terminal state, Amazon EMR reaps the managed EC2
# fleet (instances, EBS volumes, ENIs, and the multi-primary placement group)
# asynchronously. Wait for the instances to clear so the post-run scan does not
# report them as orphaned resources.
_FLEET_REAP_TIMEOUT = 300  # 5 minutes
_FLEET_REAP_POLL_INTERVAL = 15
# EMR tags every managed instance with the cluster (job flow) id.
_JOB_FLOW_TAG = "aws:elasticmapreduce:job-flow-id"
# Instance states that still count as "present"; terminated instances linger in
# DescribeInstances for ~1h but are already gone, so they are excluded here.
_LIVE_INSTANCE_STATES = ("pending", "running", "shutting-down", "stopping", "stopped")


def _wait_for_fleet_reaped(session: boto3.Session, cluster_id: str) -> bool:
    """Poll until the cluster's EMR-managed EC2 instances are gone (best-effort)."""
    ec2 = build_client(session, "ec2")

    def _fleet_clear() -> bool:
        resp = ec2.describe_instances(
            Filters=[
                {"Name": f"tag:{_JOB_FLOW_TAG}", "Values": [cluster_id]},
                {"Name": "instance-state-name", "Values": list(_LIVE_INSTANCE_STATES)},
            ]
        )
        return not any(reservation.get("Instances") for reservation in resp.get("Reservations", []))

    return wait_until(
        _fleet_clear,
        timeout=_FLEET_REAP_TIMEOUT,
        interval=_FLEET_REAP_POLL_INTERVAL,
    )


@resource_handler("AWS::EMR::Cluster", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Disable termination protection, then terminate the EMR cluster if running."""
    client = build_client(session, "emr")
    cluster_id = resource.identifier
    try:
        resp = client.describe_cluster(ClusterId=cluster_id)
        state = resp["Cluster"]["Status"]["State"]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=cluster_id,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="Cluster not found",
            )
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to describe cluster: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Connection error describing cluster: {e}",
        )

    if state in _TERMINAL_STATES:
        # Already terminal: nothing to terminate. The EMR lister surfaces only live clusters, and
        # any EC2 fleet a failed teardown left behind is caught by the EC2 instance lister, so
        # there is nothing to do here.
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message=f"Cluster already terminated (state={state})",
        )

    # Multi-primary (HA) clusters have termination protection enabled automatically, disable first
    try:
        client.set_termination_protection(JobFlowIds=[cluster_id], TerminationProtected=False)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=cluster_id,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="Cluster not found while disabling termination protection",
            )
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to disable termination protection: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Connection error disabling termination protection: {e}",
        )

    try:
        client.terminate_job_flows(JobFlowIds=[cluster_id])
        logger.debug(
            "Initiated termination of EMR cluster '%s'",
            cluster_id[:LOG_TRUNCATE_MEDIUM],
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=cluster_id,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="Cluster not found during termination",
            )
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to terminate cluster: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Connection error terminating cluster: {e}",
        )

    return HandlerResult(
        resource_id=cluster_id,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message=f"Initiated termination (was in state={state})",
    )


@resource_handler("AWS::EMR::Cluster", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Wait for the cluster to terminate, then for its managed EC2 fleet to be reaped."""
    client = build_client(session, "emr")
    cluster_id = resource.identifier

    # wait_until swallows predicate exceptions, so capture the last error and
    # surface it on timeout instead of burning the full timeout on a generic message.
    last_error: Exception | None = None

    def _is_terminated() -> bool:
        nonlocal last_error
        try:
            resp = client.describe_cluster(ClusterId=cluster_id)
            state = resp["Cluster"]["Status"]["State"]
            last_error = None
            return state in _TERMINAL_STATES
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                return True  # Gone is as good as terminated
            last_error = e
            return False
        except BotoCoreError as e:
            last_error = e
            return False

    terminated = wait_until(
        _is_terminated,
        timeout=_TERMINATION_TIMEOUT,
        interval=_TERMINATION_POLL_INTERVAL,
    )

    if terminated:
        logger.debug("EMR cluster '%s' terminated", cluster_id[:LOG_TRUNCATE_MEDIUM])
        # Wait for EMR's managed EC2 fleet to clear so they are not reported as orphans by the
        # post-run scan.
        fleet_reaped = _wait_for_fleet_reaped(session, cluster_id)
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.SUCCESS,
            message=(
                "Cluster terminated and managed EC2 fleet reaped"
                if fleet_reaped
                else "Cluster terminated; managed EC2 fleet still reaping"
            ),
        )

    if last_error is not None:
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Error polling cluster termination: {last_error}",
        )

    return HandlerResult(
        resource_id=cluster_id,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.FAILED,
        message=f"Cluster termination timed out after {_TERMINATION_TIMEOUT}s",
    )
