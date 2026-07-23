"""S3 Tables table-bucket cleanup handler.

A task-created ``AWS::S3Tables::TableBucket`` cannot be deleted by raw CCAPI
while it still holds namespaces/tables — ``DeleteTableBucket`` returns "The
bucket that you tried to delete is not empty". The prepare step empties the
bucket (tables, then namespaces) so the delete can succeed.

This is distinct from the streaming-and-iot scenario ``reset.sh``, which empties
the *pre-deployed* ``bench-tables-*`` bucket (a stack resource it must not
delete). Here the bucket itself is a task-created NEW resource that reset must
delete outright, so the handler both empties and deletes it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import (
    prepare_error_result,
    service_delete,
)
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

_NOT_FOUND_CODES = ("NotFoundException", "NoSuchBucket")


def _delete_if_present(op: Callable[..., Any], **kwargs: str) -> None:
    """Run a single s3tables delete, ignoring an already-gone item.

    Keeps ``_empty_table_bucket`` best-effort per item: a table/namespace removed
    concurrently (or on a retried empty) must not abort the whole empty. Any other
    error propagates to the caller, which maps it to a FAILED HandlerResult.
    """
    try:
        op(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") not in _NOT_FOUND_CODES:
            raise


def _empty_table_bucket(client: BaseClient, bucket_arn: str) -> None:
    """Delete every table then every namespace in the bucket.

    Order matters: a namespace cannot be deleted while it still holds tables.
    Best-effort per item — a single already-gone table/namespace must not abort
    the whole empty, since the goal is only that the bucket ends up empty.
    """
    for ns_page in client.get_paginator("list_namespaces").paginate(tableBucketARN=bucket_arn):
        for ns in ns_page.get("namespaces", []):
            # The API models ``namespace`` as a single-element list.
            namespace = ns["namespace"][0] if ns.get("namespace") else None
            if not namespace:
                continue
            for tbl_page in client.get_paginator("list_tables").paginate(
                tableBucketARN=bucket_arn, namespace=namespace
            ):
                for tbl in tbl_page.get("tables", []):
                    _delete_if_present(
                        client.delete_table,
                        tableBucketARN=bucket_arn,
                        namespace=namespace,
                        name=tbl["name"],
                    )
            _delete_if_present(
                client.delete_namespace, tableBucketARN=bucket_arn, namespace=namespace
            )


@resource_handler("AWS::S3Tables::TableBucket", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Empty the table bucket (tables → namespaces) so it can be deleted."""
    client = build_client(session, "s3tables")
    try:
        _empty_table_bucket(client, resource.identifier)
    except (ClientError, BotoCoreError) as e:
        return prepare_error_result(
            e,
            resource,
            not_found_codes=_NOT_FOUND_CODES,
            not_found_message="Table bucket not found",
            failed_message_prefix="Failed to empty table bucket",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message="Emptied table bucket (namespaces and tables)",
    )


@resource_handler("AWS::S3Tables::TableBucket", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the (now-empty) table bucket via the s3tables API."""
    return service_delete(
        resource,
        session,
        client_name="s3tables",
        op_name="delete_table_bucket",
        id_param="tableBucketARN",
        not_found_codes=_NOT_FOUND_CODES,
        already_gone_message="Table bucket already gone",
        log_label="S3 Tables bucket",
    )
