"""AWS Lambda function cleanup handler.

Primarily used to clear Lambda@Edge function since the removal takes time to
propagate across all regions. Normal Lambda functions are also handled with no
special cleanup handling.

A Lambda@Edge function cannot be deleted while CloudFront still holds its
replicas: ``delete_function`` fails with ``InvalidParameterValueException``
until the replicas are removed, which happens automatically a few hours after
the last distribution association is gone.

So this delete handler issues the delete and, on that specific error, records the
function as a *deferred* deletion and returns ``SKIPPED`` rather than ``FAILED``.
Reset/cleanup verification excludes deferred resources for the current run, so a
lingering Lambda@Edge master no longer fails reset between trials; the final cleanup
(or a later run) deletes it once the replicas clear. All other functions delete normally.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.resource_management.deferred import mark_deferred
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_NOT_FOUND_CODES = ("ResourceNotFoundException",)
# delete_function rejects a Lambda@Edge master whose replicas CloudFront has not
# yet removed with this code; the message names it "a replicated function".
_REPLICATED_ERROR_CODE = "InvalidParameterValueException"
_REPLICATED_HINT = "replicated function"


@resource_handler("AWS::Lambda::Function", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the function; defer it if it is a Lambda@Edge master pending replica teardown."""
    client = build_client(session, "lambda")
    name = resource.identifier
    try:
        client.delete_function(FunctionName=name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        message = e.response.get("Error", {}).get("Message", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=name,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SKIPPED,
                message="Function already gone",
            )
        if code == _REPLICATED_ERROR_CODE and _REPLICATED_HINT in message.lower():
            # Lambda@Edge replica teardown is asynchronous (a few hours); record the
            # deferral so verification does not treat the lingering master as a
            # residual, and do not fail the run.
            mark_deferred(resource.type, name)
            logger.debug(
                "Deferring Lambda@Edge function '%s': replicas still being removed by CloudFront",
                name[:LOG_TRUNCATE_MEDIUM],
            )
            return HandlerResult(
                resource_id=name,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SKIPPED,
                message="Lambda@Edge replica teardown in progress; deletion deferred",
            )
        return HandlerResult(
            resource_id=name,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete function: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=name,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Connection error deleting function: {e}",
        )

    logger.debug("Deleted Lambda function '%s'", name[:LOG_TRUNCATE_MEDIUM])
    return HandlerResult(
        resource_id=name,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
        message="Function deleted",
    )


@resource_handler("AWS::Lambda::LayerVersion", role="delete")
def _delete_layer_version(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete a Lambda layer version (or all versions of a layer).

    The identifier may be either:
    - Full versioned ARN: arn:aws:lambda:<region>:<account>:layer:<name>:<version>
    - Unversioned layer ARN: arn:aws:lambda:<region>:<account>:layer:<name>

    When no version is specified, all versions of the layer are deleted.
    """
    client = build_client(session, "lambda")
    identifier = resource.identifier

    # Parse layer name and optional version from ARN
    # Format: arn:aws:lambda:region:account:layer:name[:version]
    parts = identifier.split(":")
    layer_name: str | None = None
    version: int | None = None

    if "layer" in parts:
        layer_idx = parts.index("layer")
        if layer_idx + 1 < len(parts):
            layer_name = parts[layer_idx + 1]
        if layer_idx + 2 < len(parts):
            try:
                version = int(parts[layer_idx + 2])
            except ValueError:
                version = None

    if not layer_name:
        return HandlerResult(
            resource_id=identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Cannot parse layer name from identifier: {identifier}",
        )

    try:
        if version is not None:
            # Delete a specific version
            client.delete_layer_version(LayerName=layer_name, VersionNumber=version)
        else:
            # No version specified — delete all versions
            versions = []
            paginator = client.get_paginator("list_layer_versions")
            for page in paginator.paginate(LayerName=layer_name):
                versions.extend(v["Version"] for v in page.get("LayerVersions", []))
            if not versions:
                return HandlerResult(
                    resource_id=identifier,
                    resource_type=resource.type,
                    action="delete",
                    status=HandlerStatus.SUCCESS,
                    message="Layer has no versions (already gone)",
                )
            for v in versions:
                client.delete_layer_version(LayerName=layer_name, VersionNumber=v)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _NOT_FOUND_CODES:
            return HandlerResult(
                resource_id=identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SUCCESS,
                message="Layer version already gone",
            )
        return HandlerResult(
            resource_id=identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete layer version: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Connection error deleting layer version: {e}",
        )

    logger.debug("Deleted Lambda layer version '%s'", identifier[:LOG_TRUNCATE_MEDIUM])
    return HandlerResult(
        resource_id=identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
        message="Layer version deleted",
    )
