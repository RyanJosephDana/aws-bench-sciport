"""Tests for the Internet Gateway / VPN Gateway detach pre-delete hooks."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.cleanup.handlers.vpc import (
    _detach_internet_gateways,
    _detach_vpn_gateways,
)
from aws_bench.resource_management.cleanup.models import StackResource


def _igw(physical_id: str) -> StackResource:
    return StackResource("L", physical_id, "AWS::EC2::InternetGateway", "CREATE_COMPLETE")


def _vgw(physical_id: str) -> StackResource:
    return StackResource("L", physical_id, "AWS::EC2::VPNGateway", "CREATE_COMPLETE")


def _client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def _session_with(ec2: MagicMock, default_vpc_ids: tuple[str, ...] = ()) -> MagicMock:
    """Wire an ec2 mock into a session and stub the default-VPC lookup.

    The lookup is the shared paginated ``default_vpc_ids`` helper
    (fastscan ``custom_listers``), so the stub feeds the paginator path.
    """
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"Vpcs": [{"VpcId": v} for v in default_vpc_ids]}])
    ec2.get_paginator.return_value = paginator
    session = MagicMock()
    session.client.return_value = ec2
    return session


# ---------------------------------------------------------------------------
# Internet Gateway
# ---------------------------------------------------------------------------


def test_igw_hook_no_matching_resources_returns_empty_without_client():
    session = MagicMock()
    result = _detach_internet_gateways(
        [StackResource("L", "vpc-1", "AWS::EC2::VPC", "CREATE_COMPLETE")], session
    )
    assert result == []
    session.client.assert_not_called()


def test_igw_hook_skips_empty_physical_id():
    session = MagicMock()
    result = _detach_internet_gateways([_igw("")], session)
    assert result == []
    session.client.assert_not_called()


def test_igw_hook_detaches_each_attached_vpc():
    ec2 = MagicMock()
    ec2.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [
                    {"VpcId": "vpc-1", "State": "available"},
                    {"VpcId": "vpc-2", "State": "available"},
                ],
            }
        ]
    }
    session = _session_with(ec2)

    result = _detach_internet_gateways([_igw("igw-1")], session)

    assert result == []
    ec2.describe_internet_gateways.assert_called_once_with(InternetGatewayIds=["igw-1"])
    assert ec2.detach_internet_gateway.call_count == 2
    ec2.detach_internet_gateway.assert_any_call(InternetGatewayId="igw-1", VpcId="vpc-1")
    ec2.detach_internet_gateway.assert_any_call(InternetGatewayId="igw-1", VpcId="vpc-2")


def test_igw_hook_skips_already_detached_attachment():
    ec2 = MagicMock()
    ec2.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {"InternetGatewayId": "igw-1", "Attachments": [{"VpcId": "vpc-1", "State": "detached"}]}
        ]
    }
    session = _session_with(ec2)

    _detach_internet_gateways([_igw("igw-1")], session)

    ec2.detach_internet_gateway.assert_not_called()


def test_igw_hook_skips_default_vpc_attachment():
    """An IGW attached to a default VPC must not be detached (or deleted)."""
    ec2 = MagicMock()
    ec2.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [{"VpcId": "vpc-def", "State": "available"}],
            }
        ]
    }
    session = _session_with(ec2, default_vpc_ids=("vpc-def",))

    result = _detach_internet_gateways([_igw("igw-1")], session)

    assert result == []
    ec2.get_paginator.assert_called_once_with("describe_vpcs")
    ec2.get_paginator.return_value.paginate.assert_called_once_with(
        Filters=[{"Name": "is-default", "Values": ["true"]}]
    )
    ec2.detach_internet_gateway.assert_not_called()


def test_igw_hook_detaches_non_default_but_skips_default():
    """With a mix, only the non-default VPC attachment is detached."""
    ec2 = MagicMock()
    ec2.describe_internet_gateways.side_effect = [
        {
            "InternetGateways": [
                {
                    "InternetGatewayId": "igw-def",
                    "Attachments": [{"VpcId": "vpc-def", "State": "available"}],
                }
            ]
        },
        {
            "InternetGateways": [
                {
                    "InternetGatewayId": "igw-app",
                    "Attachments": [{"VpcId": "vpc-app", "State": "available"}],
                }
            ]
        },
    ]
    session = _session_with(ec2, default_vpc_ids=("vpc-def",))

    result = _detach_internet_gateways([_igw("igw-def"), _igw("igw-app")], session)

    assert result == []
    ec2.detach_internet_gateway.assert_called_once_with(
        InternetGatewayId="igw-app", VpcId="vpc-app"
    )


def test_igw_hook_detaches_when_default_vpc_lookup_fails():
    """If DescribeVpcs fails, fall back to detaching (best-effort, logged).

    The shared ``default_vpc_ids`` helper propagates the error; the hook wraps
    it fail-open, so the detach still runs with no VPCs protected.
    """
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.side_effect = _client_error(
        "UnauthorizedOperation", "DescribeVpcs"
    )
    ec2.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [{"VpcId": "vpc-1", "State": "available"}],
            }
        ]
    }
    session = MagicMock()
    session.client.return_value = ec2

    result = _detach_internet_gateways([_igw("igw-1")], session)

    assert result == []
    ec2.detach_internet_gateway.assert_called_once_with(InternetGatewayId="igw-1", VpcId="vpc-1")


def test_igw_hook_swallows_not_found_on_describe():
    ec2 = MagicMock()
    ec2.describe_internet_gateways.side_effect = _client_error(
        "InvalidInternetGatewayID.NotFound", "DescribeInternetGateways"
    )
    session = _session_with(ec2)

    result = _detach_internet_gateways([_igw("igw-1")], session)

    assert result == []
    ec2.detach_internet_gateway.assert_not_called()


def test_igw_hook_continues_after_a_detach_error():
    ec2 = MagicMock()
    ec2.describe_internet_gateways.return_value = {
        "InternetGateways": [
            {
                "InternetGatewayId": "igw-1",
                "Attachments": [
                    {"VpcId": "vpc-1", "State": "available"},
                    {"VpcId": "vpc-2", "State": "available"},
                ],
            }
        ]
    }
    ec2.detach_internet_gateway.side_effect = [
        _client_error("DependencyViolation", "DetachInternetGateway"),
        None,
    ]
    session = _session_with(ec2)

    # Must not raise; both attachments are attempted.
    result = _detach_internet_gateways([_igw("igw-1")], session)

    assert result == []
    assert ec2.detach_internet_gateway.call_count == 2


# ---------------------------------------------------------------------------
# VPN Gateway (no default-VPC guard — AWS never attaches a VGW to the default VPC)
# ---------------------------------------------------------------------------


def test_vpn_hook_no_matching_resources_returns_empty_without_client():
    session = MagicMock()
    result = _detach_vpn_gateways([_igw("igw-1")], session)
    assert result == []
    session.client.assert_not_called()


def test_vpn_hook_detaches_only_attached_vpc():
    ec2 = MagicMock()
    ec2.describe_vpn_gateways.return_value = {
        "VpnGateways": [
            {
                "VpnGatewayId": "vgw-1",
                "VpcAttachments": [
                    {"VpcId": "vpc-1", "State": "attached"},
                    {"VpcId": "vpc-2", "State": "detached"},
                ],
            }
        ]
    }
    session = MagicMock()
    session.client.return_value = ec2

    result = _detach_vpn_gateways([_vgw("vgw-1")], session)

    assert result == []
    ec2.describe_vpn_gateways.assert_called_once_with(VpnGatewayIds=["vgw-1"])
    ec2.detach_vpn_gateway.assert_called_once_with(VpnGatewayId="vgw-1", VpcId="vpc-1")
    # VPN path must not consult default-VPC status (no paginated describe_vpcs lookup).
    ec2.get_paginator.assert_not_called()


def test_vpn_hook_swallows_not_found_on_describe():
    ec2 = MagicMock()
    ec2.describe_vpn_gateways.side_effect = _client_error(
        "InvalidVpnGatewayID.NotFound", "DescribeVpnGateways"
    )
    session = MagicMock()
    session.client.return_value = ec2

    result = _detach_vpn_gateways([_vgw("vgw-1")], session)

    assert result == []
    ec2.detach_vpn_gateway.assert_not_called()
