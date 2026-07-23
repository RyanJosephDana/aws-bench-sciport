"""Shared delete/prepare scaffolding for custom cleanup handlers.

Several service handlers (bedrock, elbv2, s3tables, imagebuilder) hand-rolled the
same error-mapping plumbing around a single SDK delete op:

* run the delete op; a *not-found* ``ClientError`` means the resource is already
  gone → ``SUCCESS``;
* any other ``ClientError`` → ``FAILED``;
* a ``BotoCoreError`` → ``FAILED``;
* log ``Deleted ...`` and return ``SUCCESS`` on the happy path.

The prepare handlers duplicated the mirror mapping (*not-found* → ``SKIPPED``,
other ``ClientError`` / ``BotoCoreError`` → ``FAILED``). Both scaffolds live here as
the single source of truth so each handler only owns the work and its messages.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import LOG_TRUNCATE_MEDIUM, Resource
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)


def _error_code(error: ClientError) -> str:
    """Return the AWS error code from a ClientError (empty string if absent)."""
    return error.response.get("Error", {}).get("Code", "")


def service_delete(
    resource: Resource,
    session: boto3.Session,
    *,
    client_name: str,
    op_name: str,
    id_param: str,
    not_found_codes: Sequence[str],
    already_gone_message: str,
    log_label: str,
    post_delete: Callable[[BaseClient], None] | None = None,
) -> HandlerResult:
    """Run a single SDK delete op with the shared already-gone/error mapping.

    Args:
        resource: The resource whose identifier is deleted.
        session: The boto3 session to build the service client from.
        client_name: The boto3 client name (e.g. ``"elbv2"``).
        op_name: The delete op method on that client (e.g. ``"delete_load_balancer"``).
        id_param: The keyword arg the delete op takes the identifier under.
        not_found_codes: AWS error codes that mean the resource is already gone.
        already_gone_message: Success message when the resource was already gone.
        log_label: Human label for the success log line (e.g. ``"load balancer"``).
        post_delete: Optional callback run with the client after the delete op is
            accepted (e.g. an elbv2 terminal-deletion waiter). It may raise
            ``ClientError`` / ``BotoCoreError`` to signal a still-in-flight delete.

    Returns:
        A ``delete``-action HandlerResult: SUCCESS when the resource is gone (or
        was already gone), FAILED otherwise.
    """
    try:
        client = build_client(session, client_name)
        getattr(client, op_name)(**{id_param: resource.identifier})
        if post_delete is not None:
            post_delete(client)
    except ClientError as e:
        if _error_code(e) in not_found_codes:
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SUCCESS,
                message=already_gone_message,
            )
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete {log_label}: {e}",
        )
    except BotoCoreError as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Connection error deleting {log_label}: {e}",
        )
    logger.debug(f"Deleted {log_label} '{resource.identifier[:LOG_TRUNCATE_MEDIUM]}'")
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


def prepare_error_result(
    error: ClientError | BotoCoreError,
    resource: Resource,
    *,
    not_found_codes: Sequence[str],
    not_found_message: str,
    failed_message_prefix: str,
) -> HandlerResult:
    """Map a prepare-handler AWS exception to a HandlerResult.

    A *not-found* ``ClientError`` becomes ``SKIPPED`` (the resource is already
    gone, so there is nothing to prepare); any other ``ClientError`` or a
    ``BotoCoreError`` becomes ``FAILED``.

    Args:
        error: The caught AWS exception.
        resource: The resource being prepared.
        not_found_codes: AWS error codes that mean the resource is already gone.
        not_found_message: SKIPPED message for the already-gone case.
        failed_message_prefix: Prefix for the FAILED message (the exception is
            appended, e.g. ``"Failed to empty table bucket"``).

    Returns:
        A ``prepare``-action HandlerResult (SKIPPED or FAILED).
    """
    if isinstance(error, ClientError) and _error_code(error) in not_found_codes:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message=not_found_message,
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.FAILED,
        message=f"{failed_message_prefix}: {error}",
    )
