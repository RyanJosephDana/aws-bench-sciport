"""Athena workgroup cleanup handler."""

from __future__ import annotations

import boto3

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)


@resource_handler("AWS::Athena::WorkGroup", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    client = build_client(session, "athena")
    wg = resource.identifier
    try:
        client.get_work_group(WorkGroup=wg)
    except client.exceptions.InvalidRequestException:
        return HandlerResult(
            resource_id=wg,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message="Workgroup not found",
        )
    for page in client.get_paginator("list_named_queries").paginate(WorkGroup=wg):
        for qid in page.get("NamedQueryIds", []):
            client.delete_named_query(NamedQueryId=qid)
    kwargs = {"WorkGroup": wg}
    while True:
        resp = client.list_prepared_statements(**kwargs)
        for ps in resp.get("PreparedStatements", []):
            client.delete_prepared_statement(StatementName=ps["StatementName"], WorkGroup=wg)
        if "NextToken" not in resp:
            break
        kwargs["NextToken"] = resp["NextToken"]
    return HandlerResult(
        resource_id=wg,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message="Emptied workgroup",
    )


@resource_handler("AWS::Athena::WorkGroup", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    build_client(session, "athena").delete_work_group(
        WorkGroup=resource.identifier, RecursiveDeleteOption=True
    )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
