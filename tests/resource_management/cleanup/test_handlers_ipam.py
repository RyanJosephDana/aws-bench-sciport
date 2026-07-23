"""Tests for the IPAM Pool cleanup handler."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.ipam import (
    PoolDeleteOutcome,
    _delete_ipam_pool,
    _pool_exists,
    deprovision_and_delete_pool,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus


def _resource(pool_id: str = "ipam-pool-abc123") -> Resource:
    return Resource(type="AWS::EC2::IPAMPool", identifier=pool_id)


def _session_with_ec2(ec2: MagicMock) -> MagicMock:
    session = MagicMock()
    session.client.return_value = ec2
    return session


def _cidrs_page(state: str, cidr: str = "10.0.0.0/16") -> dict:
    """A single-CIDR GetIpamPoolCidrs page in the given state."""
    return {"IpamPoolCidrs": [{"Cidr": cidr, "State": state}]}


def _alloc_page(present: bool) -> dict:
    """A GetIpamPoolAllocations page: one VPC allocation when ``present``, else empty."""
    if present:
        return {
            "IpamPoolAllocations": [
                {"ResourceType": "vpc", "ResourceId": "vpc-1", "ResourceRegion": "us-east-1"}
            ]
        }
    return {"IpamPoolAllocations": []}


def _wire_paginators(
    ec2: MagicMock,
    *,
    cidr_pagelists: list[dict] | list[list[dict]],
    alloc_pagelists: list[dict] | None = None,
) -> None:
    """Wire ``ec2.get_paginator`` for both CIDR and allocation lookups.

    Each poll reads CIDRs then allocations; ``cidr_pagelists``/``alloc_pagelists`` are
    the per-call pages, popped in order (the last is reused once exhausted so a fixed
    steady state can be polled indefinitely).
    """
    cidrs = list(cidr_pagelists)
    allocs = list(alloc_pagelists if alloc_pagelists is not None else [_alloc_page(False)])

    def get_paginator(operation: str) -> MagicMock:
        paginator = MagicMock()
        if operation == "get_ipam_pool_cidrs":
            page = cidrs.pop(0) if len(cidrs) > 1 else cidrs[0]
            paginator.paginate.return_value = [page]
        elif operation == "get_ipam_pool_allocations":
            page = allocs.pop(0) if len(allocs) > 1 else allocs[0]
            paginator.paginate.return_value = [page]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected paginator {operation}")
        return paginator

    ec2.get_paginator.side_effect = get_paginator


def _wire_pool_gone(ec2: MagicMock) -> None:
    """Make ``describe_ipam_pools`` report the pool gone (the confirm-gone poll)."""
    ec2.describe_ipam_pools.return_value = {"IpamPools": []}


def _ec2_in_region(region: str = "us-east-1") -> MagicMock:
    """A mock EC2 client whose ``meta.region_name`` matches the allocation's region."""
    ec2 = MagicMock()
    ec2.meta.region_name = region
    return ec2


_MODULE = "aws_bench.resource_management.cleanup.handlers.ipam"


@contextmanager
def _fast_drain():
    """Neutralize sleeps and grant a generous budget so a within-budget drain runs instantly.

    Sleep is a no-op, so a drain that reaches a terminal CIDR/allocation state completes
    at once; the budget is large so it never expires while the scripted pages are polled.
    """
    with (
        patch(f"{_MODULE}._DEPROVISION_BUDGET_SEC", 3600),
        patch(f"{_MODULE}._DEPROVISION_POLL_INTERVAL_SEC", 0.001),
        patch(f"{_MODULE}.time.sleep"),
    ):
        yield


@contextmanager
def _expired_budget(within_budget_polls: int = 0):
    """Deterministically expire the drain budget via an injected clock (no real wall-clock).

    ``time.monotonic`` returns 0.0 for the deadline computation, then ``within_budget_polls``
    values before the deadline (each lets one more poll run), then a value far past the
    deadline — so budget expiry is exact and independent of load or a tiny real sleep.
    """
    before = [float(i + 1) for i in range(within_budget_polls)]
    clock = iter([0.0, *before])

    def fake_monotonic() -> float:
        return next(clock, 1e9)

    with (
        patch(f"{_MODULE}._DEPROVISION_POLL_INTERVAL_SEC", 0.001),
        patch(f"{_MODULE}.time.sleep"),
        patch(f"{_MODULE}.time.monotonic", side_effect=fake_monotonic),
    ):
        yield


# -- allocation-aware drain: wait for release before deprovision --


def test_deprovision_and_delete_pool_waits_for_allocation_then_deletes():
    """Allocation present for the first polls then clears -> wait, deprovision, DELETE-confirmed.

    The converge case: the handler deletes the blocking VPC, whose IPAM allocation is
    still releasing for two polls (no deprovision issued), then clears, so the CIDR is
    deprovisioned, the pool deleted, and confirmed gone in one pass.
    """
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    # Allocation present, present, then cleared. CIDR provisioned until deprovisioned.
    _wire_paginators(
        ec2,
        cidr_pagelists=[
            _cidrs_page("provisioned"),
            _cidrs_page("provisioned"),
            _cidrs_page("provisioned"),
            _cidrs_page("deprovisioned"),
        ],
        alloc_pagelists=[
            _alloc_page(True),
            _alloc_page(True),
            _alloc_page(False),
            _alloc_page(False),
        ],
    )
    _wire_pool_gone(ec2)

    # Record whether deprovision was ever called while an allocation was still present.
    calls_while_allocated: list[str] = []
    allocation_cleared = {"value": False}
    orig_get_paginator = ec2.get_paginator.side_effect

    def tracking_get_paginator(operation: str) -> MagicMock:
        paginator = orig_get_paginator(operation)
        if operation == "get_ipam_pool_allocations":
            pages = paginator.paginate.return_value
            has_alloc = any(p.get("IpamPoolAllocations") for p in pages)
            allocation_cleared["value"] = not has_alloc
        return paginator

    ec2.get_paginator.side_effect = tracking_get_paginator

    def deprovision(**kwargs):
        if not allocation_cleared["value"]:
            calls_while_allocated.append(kwargs["Cidr"])

    ec2.deprovision_ipam_pool_cidr.side_effect = deprovision

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    # The blocking VPC was deleted exactly once to start the async release clock.
    ec2.delete_vpc.assert_called_once_with(VpcId="vpc-1")
    # Deprovision was NOT issued while an allocation was present, only after it cleared.
    assert calls_while_allocated == []
    ec2.deprovision_ipam_pool_cidr.assert_called_with(IpamPoolId=pool_id, Cidr="10.0.0.0/16")
    ec2.delete_ipam_pool.assert_called_once_with(IpamPoolId=pool_id)
    # DELETED only after describe confirmed the pool gone.
    ec2.describe_ipam_pools.assert_called_with(IpamPoolIds=[pool_id])
    assert result.outcome == PoolDeleteOutcome.DELETED
    assert "deleted" in result.message.lower()


def test_deprovision_and_delete_pool_no_deprovision_while_allocation_present():
    """Allocation never clears -> deprovision is NEVER issued (would only churn/fail)."""
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[_cidrs_page("provisioned")],
        alloc_pagelists=[_alloc_page(True)],
    )

    with _expired_budget():
        deprovision_and_delete_pool(ec2, pool_id)

    ec2.deprovision_ipam_pool_cidr.assert_not_called()


def test_deprovision_and_delete_pool_allocation_never_clears_fails():
    """Allocation still present at budget -> FAILED (no defer, delete never attempted)."""
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[_cidrs_page("provisioned")],
        alloc_pagelists=[_alloc_page(True)],
    )

    with _expired_budget():
        result = deprovision_and_delete_pool(ec2, pool_id)

    # Budget expired with the pool still allocated: FAILED, never a silent leak.
    assert result.outcome == PoolDeleteOutcome.FAILED
    # Delete never attempted while allocated.
    ec2.delete_ipam_pool.assert_not_called()


# -- blocking-VPC deletion: the handler frees its own allocation --


def test_deprovision_and_delete_pool_deletes_blocking_vpc_once():
    """The blocking VPC is deleted exactly once even across many wait polls before it clears.

    The allocation lingers for several polls, so the poll loop runs repeatedly; delete_vpc
    must be issued only on the first sighting, not re-issued every poll.
    """
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[
            _cidrs_page("provisioned"),
            _cidrs_page("provisioned"),
            _cidrs_page("provisioned"),
            _cidrs_page("provisioned"),
            _cidrs_page("deprovisioned"),
        ],
        alloc_pagelists=[
            _alloc_page(True),
            _alloc_page(True),
            _alloc_page(True),
            _alloc_page(False),
            _alloc_page(False),
        ],
    )
    _wire_pool_gone(ec2)

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    # Deleted once across the three allocated polls, not re-issued each poll.
    ec2.delete_vpc.assert_called_once_with(VpcId="vpc-1")
    assert result.outcome == PoolDeleteOutcome.DELETED


def test_deprovision_and_delete_pool_vpc_already_gone_is_noop():
    """delete_vpc InvalidVpcID.NotFound (reset's CCAPI step deleted it) -> treated as no-op.

    The handler does not raise; it proceeds to wait for the allocation to release, then
    deprovisions and deletes the pool as usual.
    """
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[
            _cidrs_page("provisioned"),
            _cidrs_page("provisioned"),
            _cidrs_page("deprovisioned"),
        ],
        alloc_pagelists=[
            _alloc_page(True),
            _alloc_page(False),
            _alloc_page(False),
        ],
    )
    _wire_pool_gone(ec2)
    ec2.delete_vpc.side_effect = ClientError(
        {"Error": {"Code": "InvalidVpcID.NotFound", "Message": "not found"}}, "DeleteVpc"
    )

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    ec2.delete_vpc.assert_called_once_with(VpcId="vpc-1")
    assert result.outcome == PoolDeleteOutcome.DELETED


def test_deprovision_and_delete_pool_vpc_dependency_violation_falls_through_to_wait():
    """delete_vpc DependencyViolation -> logged, handler falls through to waiting, no raise.

    An unexpectedly non-bare VPC we cannot delete degrades to the current wait-for-release
    behavior; the allocation never clears, so it still FAILs at budget (never wedges/raises).
    """
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[_cidrs_page("provisioned")],
        alloc_pagelists=[_alloc_page(True)],
    )
    ec2.delete_vpc.side_effect = ClientError(
        {"Error": {"Code": "DependencyViolation", "Message": "has dependencies"}}, "DeleteVpc"
    )

    with _expired_budget():
        result = deprovision_and_delete_pool(ec2, pool_id)

    # Delete was attempted (and swallowed); handler did not raise and FAILed at budget.
    ec2.delete_vpc.assert_called_once_with(VpcId="vpc-1")
    assert result.outcome == PoolDeleteOutcome.FAILED
    ec2.delete_ipam_pool.assert_not_called()


def test_deprovision_and_delete_pool_non_vpc_allocation_not_deleted():
    """A non-VPC allocation is never passed to delete_vpc; the handler just waits as before."""
    ec2 = _ec2_in_region()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[_cidrs_page("provisioned")],
        alloc_pagelists=[
            {
                "IpamPoolAllocations": [
                    {
                        "ResourceType": "subnet",
                        "ResourceId": "subnet-1",
                        "ResourceRegion": "us-east-1",
                    }
                ]
            }
        ],
    )

    with _expired_budget():
        result = deprovision_and_delete_pool(ec2, pool_id)

    ec2.delete_vpc.assert_not_called()
    assert result.outcome == PoolDeleteOutcome.FAILED


def test_deprovision_and_delete_pool_pool_gone_mid_wait():
    """Pool vanishes mid-wait (list raises NotFound) -> ALREADY_GONE."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    ec2.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "InvalidIpamPoolId.NotFound", "Message": "not found"}},
        "GetIpamPoolCidrs",
    )

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    assert result.outcome == PoolDeleteOutcome.ALREADY_GONE
    assert "already gone" in result.message.lower()


# -- deprovision_and_delete_pool: delete then confirm-gone --


def test_deprovision_and_delete_pool_failed_deprovision_clears_within_budget():
    """failed-deprovision that clears during the drain -> re-deprovisioned -> DELETED.

    Allocation is already clear; a CIDR that first reads failed-deprovision reads
    deprovisioned on the next poll, the pool deletes, and describe confirms it gone.
    """
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[
            _cidrs_page("failed-deprovision"),
            _cidrs_page("deprovisioned"),
        ],
        alloc_pagelists=[_alloc_page(False)],
    )
    _wire_pool_gone(ec2)

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    ec2.deprovision_ipam_pool_cidr.assert_called_with(IpamPoolId=pool_id, Cidr="10.0.0.0/16")
    ec2.delete_ipam_pool.assert_called_once_with(IpamPoolId=pool_id)
    assert result.outcome == PoolDeleteOutcome.DELETED
    assert "deleted" in result.message.lower()


def test_deprovision_and_delete_pool_delete_rejected_then_confirmed_gone():
    """Delete rejected (CIDR still deprovisioning), then describe confirms gone -> DELETED.

    The delete-accept is no longer trusted: a rejected delete is retried and the pool is
    only DELETED once describe reports it absent.
    """
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[{"IpamPoolCidrs": []}],
        alloc_pagelists=[_alloc_page(False)],
    )
    ec2.delete_ipam_pool.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValue", "Message": "CIDR still deprovisioning"}},
        "DeleteIpamPool",
    )
    # First existence check: still present; then gone on the next poll.
    ec2.describe_ipam_pools.side_effect = [
        {"IpamPools": [{"IpamPoolId": pool_id}]},
        {"IpamPools": []},
    ]

    with _expired_budget(within_budget_polls=2):
        result = deprovision_and_delete_pool(ec2, pool_id)

    assert result.outcome == PoolDeleteOutcome.DELETED
    # Delete retried at least twice while the pool lingered.
    assert ec2.delete_ipam_pool.call_count >= 2


def test_deprovision_and_delete_pool_still_present_past_budget_fails():
    """Delete keeps being rejected and the pool never vanishes -> FAILED (no optimistic accept)."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[{"IpamPoolCidrs": []}],
        alloc_pagelists=[_alloc_page(False)],
    )
    ec2.delete_ipam_pool.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValue", "Message": "Cannot delete pool: CIDR in use"}},
        "DeleteIpamPool",
    )
    ec2.describe_ipam_pools.return_value = {"IpamPools": [{"IpamPoolId": pool_id}]}

    with _expired_budget():
        result = deprovision_and_delete_pool(ec2, pool_id)

    assert result.outcome == PoolDeleteOutcome.FAILED
    assert "still present" in result.message.lower()


def test_deprovision_and_delete_pool_delete_accepted_but_not_gone_fails():
    """DeleteIpamPool accepted, but describe still shows the pool past budget -> FAILED.

    Regression for the old optimistic-accept: an accepted delete no longer returns DELETED;
    only a confirmed absence does.
    """
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[{"IpamPoolCidrs": []}],
        alloc_pagelists=[_alloc_page(False)],
    )
    ec2.delete_ipam_pool.return_value = {"IpamPool": {"State": "delete-in-progress"}}
    ec2.describe_ipam_pools.return_value = {"IpamPools": [{"IpamPoolId": pool_id}]}

    with _expired_budget():
        result = deprovision_and_delete_pool(ec2, pool_id)

    assert result.outcome == PoolDeleteOutcome.FAILED


def test_deprovision_and_delete_pool_already_gone():
    """Pool not found while listing CIDRs -> ALREADY_GONE with 'already gone'."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    ec2.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "InvalidIpamPoolId.NotFound", "Message": "not found"}},
        "GetIpamPoolCidrs",
    )

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    assert result.outcome == PoolDeleteOutcome.ALREADY_GONE
    assert "already gone" in result.message.lower()


def test_deprovision_and_delete_pool_delete_notfound_is_already_gone():
    """DeleteIpamPool itself returns NotFound (already gone) -> ALREADY_GONE."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[{"IpamPoolCidrs": []}],
        alloc_pagelists=[_alloc_page(False)],
    )
    ec2.delete_ipam_pool.side_effect = ClientError(
        {"Error": {"Code": "InvalidIpamPoolId.NotFound", "Message": "not found"}},
        "DeleteIpamPool",
    )

    with _fast_drain():
        result = deprovision_and_delete_pool(ec2, pool_id)

    assert result.outcome == PoolDeleteOutcome.ALREADY_GONE


# -- _pool_exists confirm-gone helper --


def test_pool_exists_true_when_present():
    ec2 = MagicMock()
    ec2.describe_ipam_pools.return_value = {"IpamPools": [{"IpamPoolId": "ipam-pool-abc123"}]}
    assert _pool_exists(ec2, "ipam-pool-abc123") is True


def test_pool_exists_false_on_not_found():
    ec2 = MagicMock()
    ec2.describe_ipam_pools.side_effect = ClientError(
        {"Error": {"Code": "InvalidIpamPoolId.NotFound", "Message": "not found"}},
        "DescribeIpamPools",
    )
    assert _pool_exists(ec2, "ipam-pool-abc123") is False


def test_pool_exists_true_on_transient_error():
    """A non-not-found read error is treated as 'still present' so it never falsely confirms."""
    ec2 = MagicMock()
    ec2.describe_ipam_pools.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "slow down"}},
        "DescribeIpamPools",
    )
    assert _pool_exists(ec2, "ipam-pool-abc123") is True


# -- _delete_ipam_pool handler mapping --


def test_delete_ipam_pool_allocation_never_clears_fails():
    """Allocation never released within budget -> FAILED (no defer, no deprovision issued)."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[_cidrs_page("provisioned")],
        alloc_pagelists=[_alloc_page(True)],
    )

    with _expired_budget():
        result = _delete_ipam_pool(_resource(pool_id), _session_with_ec2(ec2))

    ec2.deprovision_ipam_pool_cidr.assert_not_called()
    assert result.status == HandlerStatus.FAILED


def test_delete_ipam_pool_retry_failed_deprovision_cidr_then_success():
    """Failed-deprovision CIDR that clears mid-drain -> deprovision + delete + confirmed gone."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[
            _cidrs_page("failed-deprovision"),
            _cidrs_page("deprovisioned"),
        ],
        alloc_pagelists=[_alloc_page(False)],
    )
    _wire_pool_gone(ec2)

    with _fast_drain():
        result = _delete_ipam_pool(_resource(pool_id), _session_with_ec2(ec2))

    ec2.deprovision_ipam_pool_cidr.assert_called_with(IpamPoolId=pool_id, Cidr="10.0.0.0/16")
    ec2.delete_ipam_pool.assert_called_once_with(IpamPoolId=pool_id)

    assert result.status == HandlerStatus.SUCCESS
    assert "deleted" in result.message.lower()


def test_delete_ipam_pool_failed_deprovision_cidr_still_stuck_returns_failed():
    """Failed-deprovision CIDR stuck past budget (pool never vanishes) -> FAILED."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[{"IpamPoolCidrs": []}],
        alloc_pagelists=[_alloc_page(False)],
    )
    ec2.delete_ipam_pool.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValue", "Message": "Cannot delete pool with CIDRs"}},
        "DeleteIpamPool",
    )
    ec2.describe_ipam_pools.return_value = {"IpamPools": [{"IpamPoolId": pool_id}]}

    with _expired_budget():
        result = _delete_ipam_pool(_resource(pool_id), _session_with_ec2(ec2))

    # Stuck pool that never confirms gone must FAIL, not defer.
    assert result.status == HandlerStatus.FAILED
    assert "still present" in result.message.lower()


def test_delete_ipam_pool_delete_accepted_but_not_confirmed_fails():
    """DeleteIpamPool accepted but the pool never confirms gone within budget -> FAILED."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[{"IpamPoolCidrs": []}],
        alloc_pagelists=[_alloc_page(False)],
    )
    ec2.delete_ipam_pool.return_value = {"IpamPool": {"State": "delete-in-progress"}}
    ec2.describe_ipam_pools.return_value = {"IpamPools": [{"IpamPoolId": pool_id}]}

    with _expired_budget():
        result = _delete_ipam_pool(_resource(pool_id), _session_with_ec2(ec2))

    assert result.status == HandlerStatus.FAILED


def test_delete_ipam_pool_already_gone():
    """Pool not found -> SKIPPED 'already gone'."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    ec2.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "InvalidIpamPoolId.NotFound", "Message": "not found"}},
        "GetIpamPoolCidrs",
    )

    with _fast_drain():
        result = _delete_ipam_pool(_resource(pool_id), _session_with_ec2(ec2))

    assert result.status == HandlerStatus.SKIPPED
    assert "already gone" in result.message.lower()


def test_delete_ipam_pool_skip_deprovisioned_cidr():
    """Already-deprovisioned CIDR is skipped (no deprovision call), pool deletes + confirmed."""
    ec2 = MagicMock()
    pool_id = "ipam-pool-abc123"

    _wire_paginators(
        ec2,
        cidr_pagelists=[_cidrs_page("deprovisioned")],
        alloc_pagelists=[_alloc_page(False)],
    )
    _wire_pool_gone(ec2)

    with _fast_drain():
        result = _delete_ipam_pool(_resource(pool_id), _session_with_ec2(ec2))

    ec2.deprovision_ipam_pool_cidr.assert_not_called()
    ec2.delete_ipam_pool.assert_called_once()

    assert result.status == HandlerStatus.SUCCESS
