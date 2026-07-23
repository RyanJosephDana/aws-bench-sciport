"""AWS EC2 IPAM pool cleanup handler.

A pool can't delete while a CIDR still backs an allocation; a just-deleted VPC
frees its allocation asynchronously (live-measured 15-31 min) and can't be force-released.
The VPC has no custom handler, so it is only torn down in the later CCAPI step — waiting
for it here would deadlock. So ``deprovision_and_delete_pool`` deletes the blocking VPC(s)
itself to start the release clock in-phase, waits for the allocation to release,
deprovisions, deletes, then confirms the pool is gone; if not gone within the budget it
returns FAILED. Contract: CONFIRMED gone (DELETED/ALREADY_GONE) or FAILED, never an
unconfirmed defer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client, raise_if_shutdown

logger = get_logger(__name__)

_NOT_FOUND_CODES = ("InvalidIpamPoolId.NotFound",)
_DEPROVISION_POLL_INTERVAL_SEC = 30
_DEPROVISION_BUDGET_SEC = 1800


class PoolDeleteOutcome(Enum):
    """Result of :func:`deprovision_and_delete_pool`; each caller maps it to its own status."""

    DELETED = "deleted"
    """The pool was deleted and confirmed gone (polled to not-found)."""
    ALREADY_GONE = "already-gone"
    """The pool did not exist (a prior pass already removed it); treated as gone."""
    FAILED = "failed"
    """The pool could not be confirmed gone within budget (never a silent leak)."""


@dataclass
class PoolDeleteResult:
    """Outcome plus a human message from :func:`deprovision_and_delete_pool`."""

    outcome: PoolDeleteOutcome
    message: str


def deprovision_and_delete_pool(
    client: BaseClient, pool_id: str, *, deadline: float | None = None
) -> PoolDeleteResult:
    """Drain, delete, and confirm an IPAM pool is gone, or FAIL (never raises).

    Shared by the delete handler and the child-pool reaper. ``deadline`` is a shared
    ``time.monotonic()`` budget (None grants a fresh one) so the reaper can bound a
    whole child tree by ~one budget instead of one per pool. Returns DELETED/ALREADY_GONE
    once the pool is confirmed absent, or FAILED if the budget expires with it present.
    """
    if deadline is None:
        deadline = time.monotonic() + _DEPROVISION_BUDGET_SEC

    drain = _drain_cidrs(client, pool_id, deadline=deadline)
    if drain is not None:
        return drain

    return _delete_and_confirm_gone(client, pool_id, deadline=deadline)


def _delete_and_confirm_gone(
    client: BaseClient, pool_id: str, *, deadline: float
) -> PoolDeleteResult:
    """Issue delete, then poll until the pool is not-found, retrying delete within budget.

    Returns DELETED once ``describe_ipam_pools`` confirms the pool is gone. A rejected
    delete (CIDR still deprovisioning) is folded into the poll loop — keep retrying until
    gone or the budget expires, at which point it is FAILED (no optimistic accept).
    """
    while True:
        raise_if_shutdown()
        try:
            client.delete_ipam_pool(IpamPoolId=pool_id)
        except ClientError as e:
            if _is_not_found(e):
                return PoolDeleteResult(PoolDeleteOutcome.ALREADY_GONE, "IPAM pool already gone")
            # A delete rejected while CIDRs are still deprovisioning is transient: keep
            # polling/retrying until the pool is gone or the budget expires.
            logger.debug(f"DeleteIpamPool for '{pool_id}' rejected; will re-check: {e}")
        except BotoCoreError as e:
            return PoolDeleteResult(
                PoolDeleteOutcome.FAILED, f"Connection error deleting IPAM pool: {e}"
            )

        if not _pool_exists(client, pool_id):
            logger.debug(f"Deleted IPAM pool '{pool_id}'")
            return PoolDeleteResult(PoolDeleteOutcome.DELETED, "IPAM pool deleted")

        if time.monotonic() >= deadline:
            return PoolDeleteResult(
                PoolDeleteOutcome.FAILED,
                f"IPAM pool '{pool_id}' still present after deletion budget expired",
            )
        time.sleep(_DEPROVISION_POLL_INTERVAL_SEC)


def _drain_cidrs(client: BaseClient, pool_id: str, *, deadline: float) -> PoolDeleteResult | None:
    """Wait for allocations to release, then deprovision the CIDRs, until drained or budget hit.

    Deprovision only churns while an allocation lingers, so wait for release first.
    Returns ``None`` once drained (caller deletes); a terminal ``PoolDeleteResult``
    (ALREADY_GONE, or FAILED if still held at the budget) otherwise.
    """
    # VPCs whose delete we already issued, so the poll loop never re-deletes them.
    deleted_vpcs: set[str] = set()
    while True:
        raise_if_shutdown()
        try:
            cidrs = _get_provisioned_cidrs(client, pool_id)
        except ClientError as e:
            if _is_not_found(e):
                return PoolDeleteResult(PoolDeleteOutcome.ALREADY_GONE, "IPAM pool already gone")
            return PoolDeleteResult(PoolDeleteOutcome.FAILED, f"Failed to read pool state: {e}")

        if not cidrs:
            return None

        try:
            allocations = _get_allocations(client, pool_id)
        except ClientError as e:
            if _is_not_found(e):
                return PoolDeleteResult(PoolDeleteOutcome.ALREADY_GONE, "IPAM pool already gone")
            return PoolDeleteResult(PoolDeleteOutcome.FAILED, f"Failed to read pool state: {e}")

        if allocations:
            # A lingering allocation blocks deprovision. If it is a VPC, delete it now to
            # start AWS's async release clock (the VPC is not torn down until the later
            # CCAPI step, so waiting alone would deadlock); then poll for the release.
            _delete_blocking_vpcs(client, allocations, deleted_vpcs)
            if time.monotonic() >= deadline:
                return PoolDeleteResult(
                    PoolDeleteOutcome.FAILED,
                    f"IPAM pool '{pool_id}' allocation not released within budget",
                )
            logger.debug(f"Waiting for IPAM pool '{pool_id}' allocation to release")
            time.sleep(_DEPROVISION_POLL_INTERVAL_SEC)
            continue

        # Allocations clear: issue deprovision once per not-yet-deprovisioned CIDR.
        logger.debug(f"Deprovisioning {len(cidrs)} CIDR(s) from IPAM pool '{pool_id}'")
        gone = _deprovision_cidrs(client, pool_id, cidrs)
        if gone is not None:
            return gone

        if time.monotonic() >= deadline:
            return None
        time.sleep(_DEPROVISION_POLL_INTERVAL_SEC)


def _deprovision_cidrs(
    client: BaseClient, pool_id: str, cidrs: list[dict]
) -> PoolDeleteResult | None:
    """Issue deprovision for each CIDR; return ``ALREADY_GONE`` if the pool vanished."""
    for cidr_info in cidrs:
        cidr = cidr_info.get("Cidr", "")
        try:
            client.deprovision_ipam_pool_cidr(IpamPoolId=pool_id, Cidr=cidr)
            logger.debug(f"Deprovisioning CIDR '{cidr}' from pool '{pool_id}'")
        except ClientError as e:
            if _is_not_found(e):
                return PoolDeleteResult(PoolDeleteOutcome.ALREADY_GONE, "IPAM pool already gone")
            # A CIDR already mid-deprovision rejects with InvalidParameterValue; that
            # is not an error, so keep polling the remaining CIDRs.
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidParameterValue":
                continue
            logger.warning(f"Failed to deprovision CIDR '{cidr}' from pool '{pool_id}': {e}")
    return None


@resource_handler("AWS::EC2::IPAMPool", role="delete")
def _delete_ipam_pool(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete an IPAM pool, draining its CIDRs then confirming it is gone.

    Contract: the pool is either confirmed gone (SUCCESS/SKIPPED) or FAILED — no defer.
    """
    client = build_client(session, "ec2")
    pool_id = resource.identifier
    result = deprovision_and_delete_pool(client, pool_id)

    if result.outcome == PoolDeleteOutcome.ALREADY_GONE:
        return _already_gone(pool_id, resource.type)
    if result.outcome == PoolDeleteOutcome.DELETED:
        logger.debug(f"Deleted IPAM pool '{pool_id[:LOG_TRUNCATE_MEDIUM]}'")
        return HandlerResult(
            resource_id=pool_id,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.SUCCESS,
            message=result.message,
        )
    return _failed(pool_id, resource.type, result.message)


def _get_provisioned_cidrs(client: BaseClient, pool_id: str) -> list[dict]:
    """Return provisioned CIDRs for a pool (those not already deprovisioned)."""
    cidrs: list[dict] = []
    paginator = client.get_paginator("get_ipam_pool_cidrs")
    for page in paginator.paginate(IpamPoolId=pool_id):
        for cidr in page.get("IpamPoolCidrs", []):
            if cidr.get("State") != "deprovisioned":
                cidrs.append(cidr)
    return cidrs


def _get_allocations(client: BaseClient, pool_id: str) -> list[dict]:
    """Return the pool's IPAM allocations (each a not-yet-released resource, e.g. a VPC)."""
    allocations: list[dict] = []
    paginator = client.get_paginator("get_ipam_pool_allocations")
    for page in paginator.paginate(IpamPoolId=pool_id):
        allocations.extend(page.get("IpamPoolAllocations", []))
    return allocations


def _delete_blocking_vpcs(
    client: BaseClient, allocations: list[dict], deleted_vpcs: set[str]
) -> None:
    """Best-effort delete each VPC still holding an allocation, once (never raises).

    Only VPC allocations are actioned; a VPC in a different region than the client, or
    one that fails to delete (e.g. DependencyViolation), is logged and left to the wait
    loop. ``deleted_vpcs`` tracks issued deletes so the poll loop deletes each VPC once.
    """
    for allocation in allocations:
        if allocation.get("ResourceType") != "vpc":
            continue
        vpc_id = allocation.get("ResourceId")
        if not vpc_id or vpc_id in deleted_vpcs:
            continue
        region = allocation.get("ResourceRegion")
        if region and region != client.meta.region_name:
            # Cross-region VPC: we only hold this region's client, so we cannot safely
            # delete it — leave it to the wait loop / budget rather than guess a client.
            logger.warning(
                f"IPAM allocation VPC '{vpc_id}' is in region '{region}', not "
                f"'{client.meta.region_name}'; leaving it for the release wait"
            )
            continue
        deleted_vpcs.add(vpc_id)
        _try_delete_vpc(client, vpc_id)


def _try_delete_vpc(client: BaseClient, vpc_id: str) -> None:
    """Delete a VPC, treating not-found as done and any other fault as a logged no-op."""
    try:
        client.delete_vpc(VpcId=vpc_id)
        logger.debug(f"Deleted VPC '{vpc_id}' holding an IPAM allocation")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "InvalidVpcID.NotFound":
            return  # Already gone (reset's CCAPI step may have deleted it); no-op.
        logger.warning(f"Could not delete IPAM-allocated VPC '{vpc_id}': {e}")
    except BotoCoreError as e:
        logger.warning(f"Could not delete IPAM-allocated VPC '{vpc_id}': {e}")


def _pool_exists(client: BaseClient, pool_id: str) -> bool:
    """Return True if the pool is still present; False on not-found (confirm-gone poll).

    A read error other than not-found is conservatively treated as "still present" so a
    transient fault never falsely confirms deletion.
    """
    try:
        resp = client.describe_ipam_pools(IpamPoolIds=[pool_id])
    except ClientError as e:
        if _is_not_found(e):
            return False
        logger.debug(f"Could not confirm IPAM pool '{pool_id}' state: {e}")
        return True
    except BotoCoreError as e:
        logger.debug(f"Could not confirm IPAM pool '{pool_id}' state: {e}")
        return True
    return bool(resp.get("IpamPools"))


def _is_not_found(e: ClientError) -> bool:
    """Return True if the error indicates the pool no longer exists."""
    code = e.response.get("Error", {}).get("Code", "")
    msg = e.response.get("Error", {}).get("Message", "")
    if code in _NOT_FOUND_CODES:
        return True
    # InvalidParameterValue with a "not found" message means the pool is gone;
    # other InvalidParameterValue errors (e.g. CIDR already deprovisioning) are not
    # "not found" and should be handled by the caller.
    if code == "InvalidParameterValue" and "not found" in msg.lower():
        return True
    return "not found" in msg.lower()


def _already_gone(pool_id: str, resource_type: str) -> HandlerResult:
    """Return a SKIPPED result for a pool that no longer exists."""
    return HandlerResult(
        resource_id=pool_id,
        resource_type=resource_type,
        action="delete",
        status=HandlerStatus.SKIPPED,
        message="IPAM pool already gone",
    )


def _failed(pool_id: str, resource_type: str, message: str) -> HandlerResult:
    """Return a FAILED result."""
    return HandlerResult(
        resource_id=pool_id,
        resource_type=resource_type,
        action="delete",
        status=HandlerStatus.FAILED,
        message=message,
    )
