"""S3 bucket cleanup handler.

A bucket can't be deleted while non-empty, so the prepare step empties it
(all object versions + delete markers) before CloudFormation/CCAPI removes it.
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
_NO_POLICY_CODES = ("NoSuchBucketPolicy", *_NOT_FOUND_CODES)


def _remove_bucket_policy(client: BaseClient, bucket: str) -> None:
    """Best-effort delete the bucket policy so a resource-based Deny can't block teardown."""
    try:
        client.delete_bucket_policy(Bucket=bucket)
        logger.debug("Removed bucket policy on '%s'", bucket[:LOG_TRUNCATE_MEDIUM])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in _NO_POLICY_CODES:
            return
        logger.warning(
            "Could not remove bucket policy on '%s': %s", bucket[:LOG_TRUNCATE_MEDIUM], e
        )
    except BotoCoreError as e:
        logger.warning(
            "Could not remove bucket policy on '%s': %s", bucket[:LOG_TRUNCATE_MEDIUM], e
        )


@resource_handler("AWS::S3::Bucket", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Empty the bucket (all versions + delete markers) before stack deletion."""
    client = build_client(session, "s3")
    _remove_bucket_policy(client, resource.identifier)
    total = 0
    errors = 0
    try:
        for page in client.get_paginator("list_object_versions").paginate(
            Bucket=resource.identifier
        ):
            objects = [
                {"Key": obj["Key"], "VersionId": obj["VersionId"]}
                for obj in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            for batch_start in range(0, len(objects), _DELETE_BATCH_SIZE):
                batch = objects[batch_start : batch_start + _DELETE_BATCH_SIZE]
                resp = client.delete_objects(
                    Bucket=resource.identifier, Delete={"Objects": batch, "Quiet": True}
                )
                errors += len(resp.get("Errors", []))
                total += len(batch)
                if total % _LOG_INTERVAL == 0:
                    logger.debug(
                        "Emptying '%s': %d objects so far",
                        resource.identifier[:LOG_TRUNCATE_MEDIUM],
                        total,
                    )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="Bucket not found",
            )
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed after deleting {total} objects: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Connection error after deleting {total} objects: {e}",
        )
    except Exception as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Failed after deleting {total} objects: {e}",
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


@resource_handler("AWS::S3::Bucket", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the S3 bucket after it has been emptied."""
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
                message="Bucket already gone",
            )
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete bucket: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Connection error deleting bucket: {e}",
        )
    logger.debug("Deleted S3 bucket '%s'", resource.identifier[:LOG_TRUNCATE_MEDIUM])
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
