"""Tests for the Service Catalog product cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.servicecatalog import _delete_product
from aws_bench.resource_management.cleanup.models import HandlerStatus


def test_delete_product_extracts_id_from_arn():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    arn = "arn:aws:catalog:us-east-1:111122223333:product/prod-abc123"
    r = Resource(type="AWS::ServiceCatalog::CloudFormationProduct", identifier=arn)
    result = _delete_product(r, session)
    assert result.status == HandlerStatus.SUCCESS
    client.delete_product.assert_called_once_with(Id="prod-abc123")


def test_delete_product_accepts_bare_id():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    r = Resource(type="AWS::ServiceCatalog::CloudFormationProduct", identifier="prod-xyz")
    _delete_product(r, session)
    client.delete_product.assert_called_once_with(Id="prod-xyz")
