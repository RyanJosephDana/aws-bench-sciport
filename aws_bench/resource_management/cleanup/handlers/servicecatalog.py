"""Service Catalog cleanup handlers.

CloudControl exposes ``AWS::ServiceCatalog::CloudFormationProduct`` but cannot delete a product that
still has provisioning artifacts, and fast-scan's ``SearchProductsAsAdmin`` lister emits the product
ARN (``arn:aws:catalog:region:acct:product/prod-…``) while ``DeleteProduct`` wants the bare product
``Id`` (``prod-…``). This handler bridges both: extract the id from the ARN and delete via the
service API.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)


def _product_id(identifier: str) -> str:
    """The bare product id (``prod-…``) from a product ARN or a bare id."""
    # arn:aws:catalog:region:acct:product/prod-xxxx -> prod-xxxx ; a bare id passes through.
    return identifier.rsplit("/", 1)[-1]


@resource_handler("AWS::ServiceCatalog::CloudFormationProduct", role="delete")
def _delete_product(resource: Resource, session: boto3.Session) -> HandlerResult:
    try:
        build_client(session, "servicecatalog").delete_product(Id=_product_id(resource.identifier))
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete Service Catalog product: {e}",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
