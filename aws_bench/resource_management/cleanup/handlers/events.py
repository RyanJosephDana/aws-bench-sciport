"""EventBridge (CloudWatch Events) rule cleanup handler.

A managed rule — created by an AWS service, e.g. EKS Auto Mode's
``EKSComputeManagedRule`` for EC2 capacity/health events — cannot be removed by a
plain ``DeleteRule``: the API rejects it with "... is a managed rule. Set 'force'
parameter to true to override" (verified live). Such rules are not
part of any CloudFormation stack (the owning service creates them out-of-band) and
outlive the cluster, so nothing else garbage-collects them. Raw CCAPI does not pass
``Force``, so the delete fails every run and the rule lingers as an orphan.

This handler deletes any ``AWS::Events::Rule`` by first removing its targets
(``Force=True``) and then deleting the rule (``Force=True``). Force is harmless for
an unmanaged rule and required for a managed one, so a single path covers both.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_NOT_FOUND_CODES = ("ResourceNotFoundException",)
_DEFAULT_EVENT_BUS = "default"


def _parse_rule_identifier(identifier: str) -> tuple[str, str]:
    """Return ``(event_bus_name, rule_name)`` from a rule identifier.

    Accepts the three shapes the scanner / CCAPI can surface:
    * ARN, default bus:  ``arn:aws:events:<region>:<acct>:rule/<rule>``
    * ARN, custom bus:   ``arn:aws:events:<region>:<acct>:rule/<bus>/<rule>``
    * CCAPI composite:   ``<bus>|<rule>`` (or a bare ``<rule>`` for the default bus)
    """
    if identifier.startswith("arn:"):
        suffix = identifier.split(":rule/", 1)[-1]
        if "/" in suffix:
            bus, name = suffix.split("/", 1)
            return bus, name
        return _DEFAULT_EVENT_BUS, suffix
    if "|" in identifier:
        bus, name = identifier.split("|", 1)
        return bus, name
    return _DEFAULT_EVENT_BUS, identifier


def _failed(resource: Resource, message: str) -> HandlerResult:
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.FAILED,
        message=message,
    )


@resource_handler("AWS::Events::Rule", role="delete")
def _delete_rule(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Remove the rule's targets, then delete the rule — both with ``Force=True``."""
    bus, name = _parse_rule_identifier(resource.identifier)
    try:
        client = build_client(session, "events")

        # Targets must be removed before a rule can be deleted; Force is required
        # for a managed rule's targets too.
        target_ids: list[str] = []
        paginator = client.get_paginator("list_targets_by_rule")
        for page in paginator.paginate(Rule=name, EventBusName=bus):
            target_ids.extend(t["Id"] for t in page.get("Targets", []) if t.get("Id"))
        if target_ids:
            client.remove_targets(Rule=name, EventBusName=bus, Ids=target_ids, Force=True)

        client.delete_rule(Name=name, EventBusName=bus, Force=True)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SUCCESS,
                message="Rule already gone",
            )
        return _failed(resource, f"Failed to delete rule: {e}")
    except BotoCoreError as e:
        return _failed(resource, f"Connection error deleting rule: {e}")

    logger.debug("Deleted EventBridge rule '%s'", name[:LOG_TRUNCATE_MEDIUM])
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
