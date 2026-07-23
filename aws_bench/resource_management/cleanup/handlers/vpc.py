"""VPC-network pre-delete hooks.

Three hooks run before CloudControl (CCAPI) deletes VPC-network resources:

* ``AWS::EC2::VPC`` — discover ENIs, EFS mount targets, and security groups
  attached to the VPC so they are deleted alongside it.
* ``AWS::EC2::InternetGateway`` — detach the IGW from its VPC(s). CCAPI's
  ``DeleteResource`` maps to ``DeleteInternetGateway``, which raises
  ``DependencyViolation`` while the gateway is still attached, so it must be
  detached first. An IGW attached to the account's **default VPC** is skipped so
  the default VPC's connectivity is preserved (and it is thus never deleted).
* ``AWS::EC2::VPNGateway`` — the same detach-before-delete requirement, via
  ``DetachVpnGateway`` before ``DeleteVpnGateway``. (No default-VPC guard: AWS
  does not attach a VPN gateway to the default VPC.)

The two detach hooks return no new resources: the gateways are already in the
deletion set; the hooks only make them deletable (like emptying a bucket before
deleting it). They run only in the CCAPI sweep/reset path (``_resolve_hooks`` is
reached only when ``custom_delete`` or ``ccapi_fallback`` is set), not in the
stack-deleter prepare-only pass, so they never pre-empt CloudFormation's own
gateway teardown during a normal stack delete.

Both hooks are best-effort: EC2 calls are wrapped so a missing gateway or a
transient detach failure logs a warning and lets the subsequent CCAPI delete (or
the next sweep) proceed rather than aborting the whole cleanup.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import pre_delete_hook
from aws_bench.resource_management.cleanup.handlers.cross_service import (
    discover_vpc_dynamic_resources,
)
from aws_bench.resource_management.cleanup.models import StackResource
from aws_bench.resource_management.fastscan.listers.custom_listers import default_vpc_ids
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

# Gateway↔VPC attachment states that still hold the gateway to a VPC and thus
# block deletion. An attachment in any other state (notably ``detaching`` /
# ``detached``) needs no action.
_ATTACHED_STATES = {"available", "attached", "attaching"}


@pre_delete_hook("AWS::EC2::VPC")
def _discover_vpc_dynamic_resources(
    resources: list[StackResource], session: boto3.Session
) -> list[Resource]:
    """Find ENIs, EFS mount targets, and security groups attached to VPCs."""
    vpc_ids = [
        resource.physical_id
        for resource in resources
        if resource.resource_type == "AWS::EC2::VPC" and resource.physical_id
    ]
    if not vpc_ids:
        return []
    return discover_vpc_dynamic_resources(vpc_ids, session)


@pre_delete_hook("AWS::EC2::InternetGateway")
def _detach_internet_gateways(
    resources: list[StackResource], session: boto3.Session
) -> list[Resource]:
    """Detach Internet Gateways from their VPCs so CCAPI can delete them.

    ``DeleteInternetGateway`` (CCAPI's ``DeleteResource`` target) requires the
    IGW to be detached from every VPC first, otherwise it raises
    ``DependencyViolation``. Returns no new resources — the IGWs are already
    queued for deletion; this hook only makes them deletable.

    An IGW attached to the account's **default VPC** is left untouched: detaching
    it would break the default VPC's internet connectivity, and the default VPC
    is a pre-existing account resource, not scenario state. Skipping the detach
    also leaves that IGW attached, so the subsequent CCAPI delete fails (a
    no-op), preserving it.
    """
    igw_ids = [
        resource.physical_id
        for resource in resources
        if resource.resource_type == "AWS::EC2::InternetGateway" and resource.physical_id
    ]
    if not igw_ids:
        return []
    ec2 = build_client(session, "ec2")
    # Shared with the fast-scan listers; best-effort here — an empty guard set
    # must not abort the detach (a wedged scenario stack still gets unblocked).
    try:
        protected_vpc_ids = default_vpc_ids(ec2)
    except (ClientError, BotoCoreError) as e:
        logger.warning("DescribeVpcs (is-default) failed; not protecting default-VPC IGWs: %s", e)
        protected_vpc_ids = set()
    for igw_id in igw_ids:
        _detach_one_internet_gateway(ec2, igw_id, protected_vpc_ids)
    return []


def _detach_one_internet_gateway(ec2: Any, igw_id: str, protected_vpc_ids: set[str]) -> None:
    """Detach a single IGW from every non-default VPC it is attached to (best-effort).

    Attachments to a default VPC are skipped so the default VPC's gateway is
    neither detached nor deleted.
    """
    try:
        resp = ec2.describe_internet_gateways(InternetGatewayIds=[igw_id])
    except ClientError as e:
        if not _is_not_found(e):
            logger.warning("DescribeInternetGateways failed for %s: %s", igw_id, e)
        return
    for igw in resp.get("InternetGateways", []):
        for attachment in igw.get("Attachments", []):
            vpc_id = attachment.get("VpcId")
            if not vpc_id or attachment.get("State") not in _ATTACHED_STATES:
                continue
            if vpc_id in protected_vpc_ids:
                logger.debug(
                    "Skipping detach of IGW %s from default VPC %s (preserving default VPC)",
                    igw_id,
                    vpc_id,
                )
                continue
            try:
                ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                logger.debug("Detached IGW %s from VPC %s", igw_id, vpc_id)
            except ClientError as e:
                if not _is_not_found(e):
                    logger.warning("DetachInternetGateway %s from %s failed: %s", igw_id, vpc_id, e)


@pre_delete_hook("AWS::EC2::VPNGateway")
def _detach_vpn_gateways(resources: list[StackResource], session: boto3.Session) -> list[Resource]:
    """Detach VPN Gateways from their VPCs so CCAPI can delete them.

    ``DeleteVpnGateway`` shares the IGW detach-before-delete requirement: a VGW
    still attached to a VPC cannot be deleted. Returns no new resources.
    """
    vgw_ids = [
        resource.physical_id
        for resource in resources
        if resource.resource_type == "AWS::EC2::VPNGateway" and resource.physical_id
    ]
    if not vgw_ids:
        return []
    ec2 = build_client(session, "ec2")
    for vgw_id in vgw_ids:
        _detach_one_vpn_gateway(ec2, vgw_id)
    return []


def _detach_one_vpn_gateway(ec2: Any, vgw_id: str) -> None:
    """Detach a single VPN gateway from every VPC it is attached to (best-effort)."""
    try:
        resp = ec2.describe_vpn_gateways(VpnGatewayIds=[vgw_id])
    except ClientError as e:
        if not _is_not_found(e):
            logger.warning("DescribeVpnGateways failed for %s: %s", vgw_id, e)
        return
    for vgw in resp.get("VpnGateways", []):
        for attachment in vgw.get("VpcAttachments", []):
            vpc_id = attachment.get("VpcId")
            if not vpc_id or attachment.get("State") not in _ATTACHED_STATES:
                continue
            try:
                ec2.detach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=vpc_id)
                logger.debug("Detached VPN gateway %s from VPC %s", vgw_id, vpc_id)
            except ClientError as e:
                if not _is_not_found(e):
                    logger.warning("DetachVpnGateway %s from %s failed: %s", vgw_id, vpc_id, e)


def _is_not_found(error: ClientError) -> bool:
    """Return True for an EC2 ``Invalid...ID.NotFound`` error (gateway already gone)."""
    code = error.response.get("Error", {}).get("Code", "")
    return code.endswith(".NotFound")
