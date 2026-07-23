"""AWS Batch compute-environment cleanup handler.

A **managed** compute environment cannot be deleted directly: it must first be
set to ``DISABLED`` (``UpdateComputeEnvironment``) and disassociated from every
job queue (``UpdateJobQueue`` / ``DeleteJobQueue``); Fargate environments must
also drain their jobs. If this ordering isn't followed the environment enters an
``INVALID`` state and delete never stabilizes. Crucially, Batch tears the
environment down by *assuming its service role*, so that role must still exist —
if it is deleted first, the environment goes ``INVALID`` permanently and even a
plain ``DeleteComputeEnvironment`` (or the generic Cloud Control API path) reports
"did not stabilize".

The prepare step disassociates+deletes referencing job queues, disables the
environment, and submits the delete — all while the service role is still alive.
The delete step polls until the environment is gone. Modeled on the EMR handler.
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

# Fargate teardown reaps the backing ECS cluster asynchronously (minutes).
_CE_DELETE_TIMEOUT = 900
_CE_POLL_INTERVAL = 15
# A job queue's disable/delete flips it through UPDATING/DELETING before it clears.
_JQ_DELETE_TIMEOUT = 300
_JQ_POLL_INTERVAL = 10
# Bound the wait for a state change (DISABLED) to settle before the next call.
_STATE_SETTLE_TIMEOUT = 180

# describe_compute_environments.status values that mean an in-flight state change.
_CE_TRANSIENT_STATUSES = frozenset({"CREATING", "UPDATING"})


def _region_from_arn(arn: str) -> str | None:
    """Extract the region from a Batch ARN (``arn:aws:batch:<region>:...``).

    The compute-environment / job-queue identifiers are full ARNs, so the region
    is authoritative there — more reliable than the session's default region.
    Returns None for a non-ARN identifier (falls back to the session region).
    """
    parts = arn.split(":")
    if len(parts) >= 4 and parts[0] == "arn" and parts[3]:
        return parts[3]
    return None


def _describe_ce(batch: object, ce_arn: str) -> dict | None:
    """Return the compute environment's description, or None if it is gone."""
    resp = batch.describe_compute_environments(computeEnvironments=[ce_arn])  # type: ignore[attr-defined]
    envs = resp.get("computeEnvironments", [])
    return envs[0] if envs else None


def _queues_referencing(batch: object, ce_arn: str) -> list[dict]:
    """Return job queues whose compute-environment order includes ``ce_arn``."""
    queues: list[dict] = []
    paginator = batch.get_paginator("describe_job_queues")  # type: ignore[attr-defined]
    for page in paginator.paginate():
        for queue in page.get("jobQueues", []):
            order = queue.get("computeEnvironmentOrder", [])
            if any(entry.get("computeEnvironment") == ce_arn for entry in order):
                queues.append(queue)
    return queues


def _delete_referencing_job_queues(batch: object, ce_arn: str) -> None:
    """Disable + delete every job queue bound to ``ce_arn`` and wait until gone.

    A compute environment cannot be deleted while a job queue still references it,
    so the queues must go first. Best-effort per queue: a failure is logged and the
    others still proceed (the CE delete surfaces any remaining association).
    """
    for queue in _queues_referencing(batch, ce_arn):
        queue_arn = queue["jobQueueArn"]
        try:
            if queue.get("state") != "DISABLED":
                batch.update_job_queue(jobQueue=queue_arn, state="DISABLED")  # type: ignore[attr-defined]
                _wait_job_queue_settled(batch, queue_arn)
            batch.delete_job_queue(jobQueue=queue_arn)  # type: ignore[attr-defined]
            logger.debug("Deleting Batch job queue '%s'", queue_arn[:LOG_TRUNCATE_MEDIUM])
        except (ClientError, BotoCoreError) as e:
            logger.warning(
                "Failed to tear down Batch job queue '%s': %s", queue_arn[:LOG_TRUNCATE_MEDIUM], e
            )
    wait_until(
        lambda: not _queues_referencing(batch, ce_arn),
        timeout=_JQ_DELETE_TIMEOUT,
        interval=_JQ_POLL_INTERVAL,
    )


def _wait_job_queue_settled(batch: object, queue_arn: str) -> None:
    """Wait until the job queue is no longer UPDATING (so it can be deleted)."""

    def _settled() -> bool:
        resp = batch.describe_job_queues(jobQueues=[queue_arn])  # type: ignore[attr-defined]
        queues = resp.get("jobQueues", [])
        return not queues or queues[0].get("status") != "UPDATING"

    wait_until(_settled, timeout=_STATE_SETTLE_TIMEOUT, interval=_JQ_POLL_INTERVAL)


@resource_handler("AWS::Batch::ComputeEnvironment", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Disassociate job queues, disable the compute environment, and submit delete.

    Runs before the generic stuck-resource handling that could delete the service
    role, so the environment tears down while Batch can still assume that role.
    """
    ce_arn = resource.identifier
    batch = build_client(session, "batch", region_name=_region_from_arn(ce_arn))

    try:
        ce = _describe_ce(batch, ce_arn)
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=ce_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to describe compute environment: {e}",
        )

    if ce is None:
        return HandlerResult(
            resource_id=ce_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message="Compute environment already gone",
        )

    # Idempotent: the delete handler re-runs prepare, so a CE already deleting must
    # not be re-submitted (a second delete on a DELETING CE raises ClientException).
    if ce.get("status") == "DELETING":
        return HandlerResult(
            resource_id=ce_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message="Compute environment already deleting",
        )

    try:
        _delete_referencing_job_queues(batch, ce_arn)

        if ce.get("state") != "DISABLED":
            batch.update_compute_environment(computeEnvironment=ce_arn, state="DISABLED")
            wait_until(
                lambda: (
                    (_describe_ce(batch, ce_arn) or {}).get("status") not in _CE_TRANSIENT_STATUSES
                ),
                timeout=_STATE_SETTLE_TIMEOUT,
                interval=_CE_POLL_INTERVAL,
            )

        batch.delete_compute_environment(computeEnvironment=ce_arn)
        logger.debug("Initiated deletion of Batch compute env '%s'", ce_arn[:LOG_TRUNCATE_MEDIUM])
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=ce_arn,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to disable/delete compute environment: {e}",
        )

    return HandlerResult(
        resource_id=ce_arn,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message="Job queues removed, compute environment disabled and deletion submitted",
    )


@resource_handler("AWS::Batch::ComputeEnvironment", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Poll until the compute environment is fully gone."""
    ce_arn = resource.identifier
    batch = build_client(session, "batch", region_name=_region_from_arn(ce_arn))

    gone = wait_until(
        lambda: _describe_ce(batch, ce_arn) is None,
        timeout=_CE_DELETE_TIMEOUT,
        interval=_CE_POLL_INTERVAL,
    )
    if gone:
        logger.debug("Batch compute environment '%s' deleted", ce_arn[:LOG_TRUNCATE_MEDIUM])
        return HandlerResult(
            resource_id=ce_arn,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.SUCCESS,
            message="Compute environment deleted",
        )

    # Surface the CE's own statusReason — an INVALID environment (e.g. a
    # deleted service role) explains itself here far better than a bare timeout.
    reason = ""
    try:
        ce = _describe_ce(batch, ce_arn)
        if ce is not None:
            reason = f" (status={ce.get('status')}, reason={ce.get('statusReason')})"
    except (ClientError, BotoCoreError):
        pass
    return HandlerResult(
        resource_id=ce_arn,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.FAILED,
        message=f"Compute environment deletion did not complete in {_CE_DELETE_TIMEOUT}s{reason}",
    )
