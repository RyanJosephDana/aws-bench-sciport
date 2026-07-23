"""ECR repository cleanup handlers (private + public)."""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from aws_bench.constants import DEFAULT_REGION
from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import service_delete
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_BATCH_DELETE_LIMIT = 100
# Scan lags actual state, so a repo can be gone by cleanup time. Treat not-found as
# already-cleaned success, not a false DELETE_FAILED.
_REPO_NOT_FOUND_CODES = ("RepositoryNotFoundException", "RepositoryPolicyNotFoundException")


def _is_repo_not_found(exc: ClientError) -> bool:
    """True if the error means the repository is already gone."""
    return exc.response.get("Error", {}).get("Code", "") in _REPO_NOT_FOUND_CODES


def _prepare(
    resource: Resource, session: boto3.Session, *, service: str, region: str | None = None
) -> HandlerResult:
    client = (
        build_client(session, service, region_name=region)
        if region
        else build_client(session, service)
    )
    paginator_name = "list_images" if service == "ecr" else "describe_images"
    total = 0
    try:
        for page in client.get_paginator(paginator_name).paginate(
            repositoryName=resource.identifier
        ):
            if service == "ecr":
                ids = page.get("imageIds", [])
            else:
                ids = [{"imageDigest": img["imageDigest"]} for img in page.get("imageDetails", [])]
            for i in range(0, len(ids), _BATCH_DELETE_LIMIT):
                client.batch_delete_image(
                    repositoryName=resource.identifier,
                    imageIds=ids[i : i + _BATCH_DELETE_LIMIT],
                )
            total += len(ids)
    except ClientError as e:
        if _is_repo_not_found(e):
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SUCCESS,
                message="Repository already gone",
            )
        raise
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message=f"Deleted {total} images",
    )


def _delete(
    resource: Resource, session: boto3.Session, *, service: str, region: str | None = None
) -> HandlerResult:
    client = (
        build_client(session, service, region_name=region)
        if region
        else build_client(session, service)
    )
    try:
        client.delete_repository(repositoryName=resource.identifier, force=True)
    except ClientError as e:
        if _is_repo_not_found(e):
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SUCCESS,
                message="Repository already gone",
            )
        raise
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


@resource_handler("AWS::ECR::Repository", role="prepare")
def _prepare_private(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _prepare(resource, session, service="ecr")


@resource_handler("AWS::ECR::Repository", role="delete")
def _delete_private(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _delete(resource, session, service="ecr")


@resource_handler("AWS::ECR::PublicRepository", role="prepare")
def _prepare_public(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _prepare(resource, session, service="ecr-public", region=DEFAULT_REGION)


@resource_handler("AWS::ECR::PublicRepository", role="delete")
def _delete_public(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _delete(resource, session, service="ecr-public", region=DEFAULT_REGION)


@resource_handler("AWS::ECR::PullThroughCacheRule", role="delete")
def _delete_pull_through_cache_rule(resource: Resource, session: boto3.Session) -> HandlerResult:
    # CloudControl cannot delete this type; the fast-scan lister emits the CCAPI primary
    # identifier EcrRepositoryPrefix, which is exactly the arg delete_pull_through_cache_rule wants.
    return service_delete(
        resource,
        session,
        client_name="ecr",
        op_name="delete_pull_through_cache_rule",
        id_param="ecrRepositoryPrefix",
        not_found_codes=("PullThroughCacheRuleNotFoundException",),
        already_gone_message="Pull-through cache rule already gone",
        log_label="pull-through cache rule",
    )


@resource_handler("AWS::ECR::RegistryPolicy", role="delete")
def _delete_registry_policy(resource: Resource, session: boto3.Session) -> HandlerResult:
    # CloudControl cannot delete this type. The registry policy is an account singleton (identifier
    # is the RegistryId); delete_registry_policy takes no argument and removes it outright.
    try:
        build_client(session, "ecr").delete_registry_policy()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") == "RegistryPolicyNotFoundException":
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SUCCESS,
                message="Registry policy already gone",
            )
        raise
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
