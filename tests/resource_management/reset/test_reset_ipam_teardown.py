"""Prove the IPAM child-pool drain fix reaches the RESET teardown path.

Reset deletes new resources via ``ResourceCleaner.cleanup(custom_delete=True,
ccapi_fallback=True)`` (not ``StackDeleter``). These tests exercise that entry point
to show ``AWS::EC2::IPAMPool`` is routed to ``deprovision_and_delete_pool`` and the
CCAPI fallback batches ``IPAMPool`` after ``VPC``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.deleter import Deleter
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import CUSTOM_DELETION_REGISTRY
from aws_bench.resource_management.cleanup.handlers.ipam import deprovision_and_delete_pool
from aws_bench.resource_management.cleanup.models import StackResource
from aws_bench.resource_management.cleanup.resource_cleaner import ResourceCleaner

_IPAM_POOL_TYPE = "AWS::EC2::IPAMPool"
_POOL_ID = "ipam-pool-abc123"
_POOL_CIDR = "10.0.0.0/16"


def _session_with_ec2(ec2: MagicMock) -> MagicMock:
    """A boto3-like session whose ``client("ec2")`` yields ``ec2`` (build_client path)."""
    session = MagicMock()
    session.client.return_value = ec2
    return session


def _paginator_returning(*pages) -> MagicMock:
    """Build a paginator that returns the given GetIpamPoolCidrs pages."""
    paginator = MagicMock()
    paginator.paginate.return_value = pages
    return paginator


def _cidrs_page(state: str, cidr: str = _POOL_CIDR) -> dict:
    """A single-CIDR GetIpamPoolCidrs page in the given state."""
    return {"IpamPoolCidrs": [{"Cidr": cidr, "State": state}]}


def _wire_ec2_cidr_paginators(ec2: MagicMock, *cidr_paginators: MagicMock) -> None:
    """Route get_ipam_pool_cidrs to the scripted paginators; allocations always empty.

    Each poll reads CIDRs then allocations, so a plain list side_effect on
    ``get_paginator`` would misalign; dispatch by operation instead. The last CIDR
    paginator is reused once the script is exhausted (fixed steady state).
    """
    cidrs = list(cidr_paginators)
    empty_alloc = MagicMock()
    empty_alloc.paginate.return_value = [{"IpamPoolAllocations": []}]

    def get_paginator(operation: str) -> MagicMock:
        if operation == "get_ipam_pool_allocations":
            return empty_alloc
        return cidrs.pop(0) if len(cidrs) > 1 else cidrs[0]

    ec2.get_paginator.side_effect = get_paginator


@contextmanager
def _fast_drain():
    """Shrink the drain budget/interval and neutralize sleeps so tests run instantly."""
    module = "aws_bench.resource_management.cleanup.handlers.ipam"
    with (
        patch(f"{module}._DEPROVISION_BUDGET_SEC", 0.05),
        patch(f"{module}._DEPROVISION_POLL_INTERVAL_SEC", 0.001),
        patch(f"{module}.time.sleep"),
    ):
        yield


# -- Reset path: ResourceCleaner drives the child pool to DELETED via the custom handler --


def test_reset_cleanup_drains_and_deletes_ipam_child_pool():
    """A failed-deprovision CIDR that clears mid-drain -> pool DELETED, no failures.

    Via the reset entry point (``ResourceCleaner.cleanup`` with an IPAMPool
    StackResource): the delete must go through the drain handler, not raw CCAPI.
    """
    ec2 = MagicMock()
    _wire_ec2_cidr_paginators(
        ec2,
        _paginator_returning(_cidrs_page("failed-deprovision")),
        _paginator_returning(_cidrs_page("deprovisioned")),
    )
    ec2.delete_ipam_pool.return_value = {"IpamPool": {"State": "delete-complete"}}
    # Confirm-gone poll: describe reports the pool absent so the delete confirms DELETED.
    ec2.describe_ipam_pools.return_value = {"IpamPools": []}

    resources = [StackResource(_POOL_ID, _POOL_ID, _IPAM_POOL_TYPE, "")]
    cleaner = ResourceCleaner(_session_with_ec2(ec2), "us-east-1")

    with (
        _fast_drain(),
        patch(
            "aws_bench.resource_management.cleanup.resource_cleaner.CloudControlManager"
        ) as mock_ccm_cls,
    ):
        mock_ccm_cls.return_value.delete_resources.return_value = {}
        failures = asyncio.run(
            cleaner.cleanup(
                resources,
                prepare=True,
                custom_delete=True,
                ccapi_fallback=True,
            )
        )

    # The custom drain handler ran: failed-deprovision CIDR was re-issued, then delete.
    ec2.deprovision_ipam_pool_cidr.assert_called_with(IpamPoolId=_POOL_ID, Cidr=_POOL_CIDR)
    ec2.delete_ipam_pool.assert_called_once_with(IpamPoolId=_POOL_ID)

    # Pool ended DELETED via the handler, so CCAPI fallback never received it.
    ccapi_arg = mock_ccm_cls.return_value.delete_resources.call_args.args[0]
    assert all(r.type != _IPAM_POOL_TYPE for r in ccapi_arg)

    # No failures returned -> reset's _delete_resource_set logs nothing to worry about.
    assert failures == {}


def test_reset_cleanup_stuck_ipam_child_pool_surfaces_as_failure():
    """A CIDR stuck failed-deprovision past budget -> pool FAILED, surfaced to reset.

    Confirms the handler's non-self-healing branch reaches reset as a failure the
    caller can act on (reset's final verify then re-scans and gates), rather than a
    silent success.
    """
    ec2 = MagicMock()
    _wire_ec2_cidr_paginators(ec2, _paginator_returning(_cidrs_page("failed-deprovision")))
    ec2.delete_ipam_pool.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValue", "Message": "Cannot delete pool: CIDR in use"}},
        "DeleteIpamPool",
    )
    # Pool never vanishes -> confirm-gone poll never succeeds -> FAILED at budget.
    ec2.describe_ipam_pools.return_value = {"IpamPools": [{"IpamPoolId": _POOL_ID}]}

    resources = [StackResource(_POOL_ID, _POOL_ID, _IPAM_POOL_TYPE, "")]
    cleaner = ResourceCleaner(_session_with_ec2(ec2), "us-east-1")

    with (
        _fast_drain(),
        patch(
            "aws_bench.resource_management.cleanup.resource_cleaner.CloudControlManager"
        ) as mock_ccm_cls,
    ):
        mock_ccm_cls.return_value.delete_resources.return_value = {}
        failures = asyncio.run(
            cleaner.cleanup(
                resources,
                prepare=True,
                custom_delete=True,
                ccapi_fallback=True,
            )
        )

    # The handler was exercised (drain re-issued deprovision), and the stuck pool
    # failed rather than being handed to raw CCAPI.
    ec2.deprovision_ipam_pool_cidr.assert_called_with(IpamPoolId=_POOL_ID, Cidr=_POOL_CIDR)
    ccapi_arg = mock_ccm_cls.return_value.delete_resources.call_args.args[0]
    assert all(r.type != _IPAM_POOL_TYPE for r in ccapi_arg)

    assert any(r.type == _IPAM_POOL_TYPE for r in failures)


# -- Wiring guards: the reset path routes IPAMPool to the drain handler and orders it --


def test_ipam_pool_registered_to_drain_handler():
    """``AWS::EC2::IPAMPool`` maps in CUSTOM_DELETION_REGISTRY to the drain handler.

    ResourceCleaner._custom_delete dispatches through this registry, proving the
    reset custom_delete step routes the pool to ``deprovision_and_delete_pool``.
    """
    assert _IPAM_POOL_TYPE in CUSTOM_DELETION_REGISTRY

    delete_fn = CUSTOM_DELETION_REGISTRY[_IPAM_POOL_TYPE]
    ec2 = MagicMock()
    ec2.get_paginator.return_value = _paginator_returning({"IpamPoolCidrs": []})
    ec2.delete_ipam_pool.return_value = {"IpamPool": {"State": "delete-complete"}}
    ec2.describe_ipam_pools.return_value = {"IpamPools": []}

    with (
        _fast_drain(),
        patch(
            "aws_bench.resource_management.cleanup.handlers.ipam.deprovision_and_delete_pool",
            wraps=deprovision_and_delete_pool,
        ) as spy_drain,
    ):
        delete_fn(Resource(type=_IPAM_POOL_TYPE, identifier=_POOL_ID), _session_with_ec2(ec2))

    # The registered handler delegates to the shared drain function for this pool.
    spy_drain.assert_called_once()
    assert spy_drain.call_args.args[1] == _POOL_ID
    ec2.delete_ipam_pool.assert_called_once_with(IpamPoolId=_POOL_ID)


def test_ccapi_fallback_orders_ipam_pool_after_vpc():
    """In the CCAPI deleter used by the reset fallback, IPAMPool batches after VPC.

    A pool with a live IPAM allocation cannot delete; the VPC must be torn down
    first to free the allocation. The Deleter orders batches highest-level-first,
    with VPC=10 and IPAMPool=5, so VPC's batch precedes the pool's.
    """
    deleter = Deleter(MagicMock(), resource_exists_fn=lambda _r: True)
    vpc = Resource(type="AWS::EC2::VPC", identifier="vpc-1")
    pool = Resource(type=_IPAM_POOL_TYPE, identifier=_POOL_ID)

    # Pool listed first to prove ordering is by level, not input order.
    batches = deleter._order_batches([pool, vpc])

    flat_types = [r.type for batch in batches for r in batch]
    assert flat_types.index("AWS::EC2::VPC") < flat_types.index(_IPAM_POOL_TYPE)
