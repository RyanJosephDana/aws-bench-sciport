"""LakeFormation Resource cleanup handler.

An agent-created ``AWS::LakeFormation::Resource`` registration is metadata that
associates an S3/S3Tables ARN with an IAM role for Lake Formation permissions.
It is not stack-managed when the agent registers it via ``RegisterResource``
directly.

Deregistration is a single API call — no dependencies to unwind first.
"""

from __future__ import annotations

import boto3

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import service_delete
from aws_bench.resource_management.cleanup.models import HandlerResult

_NOT_FOUND_CODES = (
    "EntityNotFoundException",
    "InvalidInputException",
    "OperationTimeoutException",
)


@resource_handler("AWS::LakeFormation::Resource", role="delete")
def _delete(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Deregister the LakeFormation resource."""
    return service_delete(
        resource,
        session,
        client_name="lakeformation",
        op_name="deregister_resource",
        id_param="ResourceArn",
        not_found_codes=_NOT_FOUND_CODES,
        already_gone_message="LakeFormation resource already deregistered",
        log_label="LakeFormation resource",
    )
