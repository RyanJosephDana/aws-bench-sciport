"""S3Express DirectoryBucket cleanup handler.

DirectoryBuckets don't support versioning or bucket policies, so the prepare
step only needs to delete all objects (ListObjectsV2 + DeleteObjects).
"""

from __future__ import annotations

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_DELETE_BATCH_SIZE = 1000
_LOG_INTERVAL = 5000
_NOT_FOUND_CODES = ("NoSuchBucket", "404")


def _empty_directory_bucket(client: BaseClient, bucket: str) -> tuple[int, int]:
    """Delete all objects in a directory bucket. Returns (total_deleted, errors)."""
    total = 0
    errors = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not objects:
            continue
        for batch_start in range(0, len(objects), _DELETE_BATCH_SIZE):
            batch = objects[batch_start : batch_start + _DELETE_BATCH_SIZE]
            resp = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            errors += len(resp.get("Errors", []))
            total += len(batch)
            if total % _LOG_INTERVAL == 0:
                logger.debug(
                    "Emptying directory bucket '%s': %d objects so far",
                    bucket[:LOG_TRUNCATE_MEDIUM],
                    total,
                )
    return total, errors


@resource_handler("AWS::S3Express::DirectoryBucket", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Empty the directory bucket before deletion (no versioning)."""
    client = build_client(session, "s3")
    try:
        total, errors = _empty_directory_bucket(client, resource.identifier)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="Directory bucket not found",
            )
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed to empty directory bucket: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Connection error emptying directory bucket: {e}",
        )
    if errors:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Deleted {total} objects with {errors} partial failures",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message=f"Emptied {total} objects",
    )


@resource_handler("AWS::S3Express::DirectoryBucket", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the directory bucket after it has been emptied."""
    client = build_client(session, "s3")
    try:
        client.delete_bucket(Bucket=resource.identifier)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SUCCESS,
                message="Directory bucket already gone",
            )
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete directory bucket: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Connection error deleting directory bucket: {e}",
        )
    logger.debug("Deleted directory bucket '%s'", resource.identifier[:LOG_TRUNCATE_MEDIUM])
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
