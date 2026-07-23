"""DMS replication cleanup handlers.

Stops DMS replication tasks before stack deletion: an active task holds RDS
connections open, so CloudFormation deletion fails with DELETE_FAILED. The
endpoint handler is a no-op — endpoints only need the referencing task stopped.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.resource_management.utils.polling import wait_until
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_RUNNING_STATES = frozenset({"running", "starting"})
# Already stopped/stopping — no action needed.
_TERMINAL_STATES = frozenset({"stopped", "deleting", "failed", "ready"})

# Brief timeout: we stop and move on — CFN handles the rest.
_STOP_WAIT_TIMEOUT = 60
_STOP_WAIT_INTERVAL = 5

_NOT_FOUND_CODES = frozenset({"ResourceNotFoundFault", "InvalidResourceStateFault"})

# Sentinel: task genuinely does not exist (empty result or not-found fault).
_TASK_GONE = "__gone__"


def _get_task_status(client, task_arn: str) -> str | None:
    """Return the lowercased task status, or ``_TASK_GONE`` if it doesn't exist.

    Raises on non-not-found errors (throttling, etc.) so callers can tell "gone"
    from "couldn't tell" — collapsing both to None read throttling as stopped.
    """
    try:
        resp = client.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}]
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return _TASK_GONE
        raise
    tasks = resp.get("ReplicationTasks", [])
    if not tasks:
        return _TASK_GONE
    return tasks[0].get("Status", "").lower()


def _is_task_stopped(client, task_arn: str) -> bool:
    """Whether the task is terminal or gone (transient errors propagate)."""
    status = _get_task_status(client, task_arn)
    if status == _TASK_GONE:
        return True
    return status in _TERMINAL_STATES


@resource_handler("AWS::DMS::ReplicationTask", role="prepare")
def _prepare_replication_task(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Stop a running DMS replication task so its dependent resources can be deleted."""
    client = build_client(session, "dms")
    task_arn = resource.identifier

    # A transient describe error must FAIL, not skip as "gone" — else we'd leave
    # a still-running task unstopped.
    try:
        status = _get_task_status(client, task_arn)
    except (ClientError, BotoCoreError) as e:
        logger.warning("Failed to describe DMS task '%s': %s", task_arn, e)
        return HandlerResult(
            resource_id=task_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to describe task: {e}",
        )
    if status == _TASK_GONE:
        logger.debug("DMS task '%s' not found — already deleted", task_arn)
        return HandlerResult(
            resource_id=task_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message="Replication task not found",
        )

    if status in _TERMINAL_STATES:
        logger.debug("DMS task '%s' already in terminal state: %s", task_arn, status)
        return HandlerResult(
            resource_id=task_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SUCCESS,
            message=f"Task already in state: {status}",
        )

    # Unexpected state (creating/modifying/testing/...) -> FAIL, not a false
    # "stopped". Stopping is best-effort (prepare-only handler), but an
    # unconfirmed state belongs in the failure log, not a clean SUCCESS.
    if status not in _RUNNING_STATES and status != "stopping":
        logger.warning("DMS task '%s' in unexpected state: %s", task_arn, status)
        return HandlerResult(
            resource_id=task_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Unexpected task state: {status}",
        )

    # Already-stopping tasks just need the wait.
    if status != "stopping":
        try:
            client.stop_replication_task(ReplicationTaskArn=task_arn)
            logger.debug("Stopped DMS replication task '%s'", task_arn)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                return HandlerResult(
                    resource_id=task_arn,
                    resource_type=resource.type,
                    action="prepare",
                    status=HandlerStatus.SKIPPED,
                    message="Task not found during stop attempt",
                )
            logger.warning("Failed to stop DMS task '%s': %s", task_arn, e)
            return HandlerResult(
                resource_id=task_arn,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.FAILED,
                message=f"Failed to stop task: {e}",
            )
        except BotoCoreError as e:
            logger.warning("Connection error stopping DMS task '%s': %s", task_arn, e)
            return HandlerResult(
                resource_id=task_arn,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.FAILED,
                message=f"Connection error: {e}",
            )

    # wait_until swallows predicate exceptions, so capture the last error and
    # surface it on timeout instead of a misleading "did not stop" (see emr._delete).
    last_error: Exception | None = None

    def _check_stopped() -> bool:
        nonlocal last_error
        try:
            result = _is_task_stopped(client, task_arn)
            last_error = None
            return result
        except (ClientError, BotoCoreError) as e:
            last_error = e
            return False

    stopped = wait_until(_check_stopped, timeout=_STOP_WAIT_TIMEOUT, interval=_STOP_WAIT_INTERVAL)
    if not stopped:
        if last_error is not None:
            logger.warning("Error polling DMS task '%s': %s", task_arn, last_error)
            return HandlerResult(
                resource_id=task_arn,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.FAILED,
                message=f"Error polling task status: {last_error}",
            )
        logger.warning("DMS task '%s' did not stop within %ds", task_arn, _STOP_WAIT_TIMEOUT)
        return HandlerResult(
            resource_id=task_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Task did not stop within {_STOP_WAIT_TIMEOUT}s",
        )

    return HandlerResult(
        resource_id=task_arn,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message="Replication task stopped",
    )


@resource_handler("AWS::DMS::Endpoint", role="prepare")
def _prepare_endpoint(resource: Resource, session: boto3.Session) -> HandlerResult:
    """No-op prepare for DMS endpoints — deletion is handled by CloudFormation."""
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message="No preparation needed for endpoint",
    )
