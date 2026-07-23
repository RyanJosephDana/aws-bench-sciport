"""Tests for the S3 Tables table-bucket cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.s3tables import (
    _delete,
    _empty_table_bucket,
    _prepare,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_BUCKET = "arn:aws:s3tables:us-east-1:111122223333:bucket/demo"


def _paginator(pages: list[dict]) -> MagicMock:
    paginator = MagicMock()
    paginator.paginate.return_value = pages
    return paginator


def _client_with(namespaces: list[dict], tables_by_ns: dict[str, list[dict]]) -> MagicMock:
    """Build a MagicMock s3tables client whose paginators return the given data."""
    client = MagicMock()

    def get_paginator(op: str) -> MagicMock:
        if op == "list_namespaces":
            return _paginator([{"namespaces": namespaces}])
        if op == "list_tables":

            def paginate(*, tableBucketARN: str, namespace: str):  # noqa: N803
                return [{"tables": tables_by_ns.get(namespace, [])}]

            paginator = MagicMock()
            paginator.paginate.side_effect = paginate
            return paginator
        raise AssertionError(f"unexpected paginator {op}")

    client.get_paginator.side_effect = get_paginator
    return client


# -- _empty_table_bucket --


def test_empty_table_bucket_deletes_tables_then_namespaces():
    client = _client_with(
        namespaces=[{"namespace": ["ns1"]}, {"namespace": ["ns2"]}],
        tables_by_ns={"ns1": [{"name": "t1"}, {"name": "t2"}], "ns2": [{"name": "t3"}]},
    )
    _empty_table_bucket(client, _BUCKET)

    assert client.delete_table.call_count == 3
    client.delete_table.assert_any_call(tableBucketARN=_BUCKET, namespace="ns1", name="t1")
    client.delete_table.assert_any_call(tableBucketARN=_BUCKET, namespace="ns2", name="t3")
    assert client.delete_namespace.call_count == 2
    client.delete_namespace.assert_any_call(tableBucketARN=_BUCKET, namespace="ns2")


def test_empty_table_bucket_skips_namespace_without_name():
    client = _client_with(namespaces=[{"namespace": []}], tables_by_ns={})
    _empty_table_bucket(client, _BUCKET)
    client.delete_namespace.assert_not_called()


def test_empty_table_bucket_tolerates_already_gone_table():
    """A NotFoundException on one table must not abort emptying the rest."""
    client = _client_with(
        namespaces=[{"namespace": ["ns1"]}],
        tables_by_ns={"ns1": [{"name": "t1"}, {"name": "t2"}]},
    )
    client.delete_table.side_effect = [
        ClientError({"Error": {"Code": "NotFoundException"}}, "DeleteTable"),
        None,
    ]
    _empty_table_bucket(client, _BUCKET)
    # Both tables were attempted, and the namespace was still deleted afterwards.
    assert client.delete_table.call_count == 2
    client.delete_namespace.assert_called_once_with(tableBucketARN=_BUCKET, namespace="ns1")


def test_empty_table_bucket_reraises_unexpected_table_error():
    """A non-NotFound error still propagates (mapped to FAILED by the handler)."""
    client = _client_with(
        namespaces=[{"namespace": ["ns1"]}],
        tables_by_ns={"ns1": [{"name": "t1"}]},
    )
    client.delete_table.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "DeleteTable"
    )
    with pytest.raises(ClientError):
        _empty_table_bucket(client, _BUCKET)


# -- _prepare --


def test_prepare_empties_and_succeeds():
    session = MagicMock()
    session.client.return_value = _client_with(
        namespaces=[{"namespace": ["ns1"]}], tables_by_ns={"ns1": [{"name": "t1"}]}
    )
    result = _prepare(Resource(type="AWS::S3Tables::TableBucket", identifier=_BUCKET), session)
    session.client.assert_called_with("s3tables")
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_skips_when_bucket_not_found():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "NotFoundException"}}, "ListNamespaces"
    )
    result = _prepare(Resource(type="AWS::S3Tables::TableBucket", identifier=_BUCKET), session)
    assert result.status == HandlerStatus.SKIPPED


def test_prepare_fails_on_other_client_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListNamespaces"
    )
    result = _prepare(Resource(type="AWS::S3Tables::TableBucket", identifier=_BUCKET), session)
    assert result.status == HandlerStatus.FAILED


# -- _delete --


def test_delete_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete(Resource(type="AWS::S3Tables::TableBucket", identifier=_BUCKET), session)
    client.delete_table_bucket.assert_called_once_with(tableBucketARN=_BUCKET)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_already_gone_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_table_bucket.side_effect = ClientError(
        {"Error": {"Code": "NotFoundException"}}, "DeleteTableBucket"
    )
    result = _delete(Resource(type="AWS::S3Tables::TableBucket", identifier=_BUCKET), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_failure_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_table_bucket.side_effect = ClientError(
        {"Error": {"Code": "ConflictException"}}, "DeleteTableBucket"
    )
    result = _delete(Resource(type="AWS::S3Tables::TableBucket", identifier=_BUCKET), session)
    assert result.status == HandlerStatus.FAILED
