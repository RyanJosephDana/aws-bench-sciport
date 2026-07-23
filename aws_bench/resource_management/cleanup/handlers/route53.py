"""Route53 HostedZone cleanup handler.

Purges all non-mandatory record sets (everything except NS and SOA at the zone
apex) before deletion, preventing HostedZoneNotEmptyException.
"""

from __future__ import annotations

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_MANDATORY_TYPES = {"NS", "SOA"}


@resource_handler("AWS::Route53::HostedZone", role="prepare")
def _prepare_hosted_zone(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete all non-NS/SOA record sets so the zone can be deleted."""
    client = build_client(session, "route53")
    zone_id = resource.identifier

    try:
        changes: list[dict] = []
        zone_name = _get_zone_name(client, zone_id)
        paginator = client.get_paginator("list_resource_record_sets")
        for page in paginator.paginate(HostedZoneId=zone_id):
            for rr in page["ResourceRecordSets"]:
                if rr["Type"] in _MANDATORY_TYPES and rr["Name"].rstrip(".") == zone_name:
                    continue
                changes.append({"Action": "DELETE", "ResourceRecordSet": rr})

        if not changes:
            return HandlerResult(
                resource_id=zone_id,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SUCCESS,
                message="Zone already empty (only NS/SOA)",
            )

        # ChangeResourceRecordSets accepts max 1000 changes per batch
        for i in range(0, len(changes), 1000):
            client.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={"Changes": changes[i : i + 1000]},
            )

        return HandlerResult(
            resource_id=zone_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SUCCESS,
            message=f"Deleted {len(changes)} record set(s)",
        )
    except ClientError as e:
        return HandlerResult(
            resource_id=zone_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=str(e),
        )


def _get_zone_name(client: BaseClient, zone_id: str) -> str:
    """Get the zone name (without trailing dot) for apex record detection."""
    resp = client.get_hosted_zone(Id=zone_id)
    return resp["HostedZone"]["Name"].rstrip(".")
