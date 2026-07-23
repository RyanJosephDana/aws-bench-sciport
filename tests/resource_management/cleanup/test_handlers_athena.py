"""Tests for Athena workgroup cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.athena import (
    _delete as _delete_athena_workgroup,
)
from aws_bench.resource_management.cleanup.handlers.athena import (
    _prepare as _prepare_athena_workgroup,
)


def test_prepare_athena_workgroup_empties_queries_and_statements():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [{"NamedQueryIds": ["q1"]}]
    client.list_prepared_statements.return_value = {
        "PreparedStatements": [{"StatementName": "ps1"}]
    }
    r = Resource(type="AWS::Athena::WorkGroup", identifier="wg1")
    _prepare_athena_workgroup(r, session)
    client.delete_named_query.assert_called_once_with(NamedQueryId="q1")
    client.delete_prepared_statement.assert_called_once()


def test_prepare_athena_workgroup_skips_not_found():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.exceptions.InvalidRequestException = type("InvalidRequestException", (Exception,), {})
    client.get_work_group.side_effect = client.exceptions.InvalidRequestException()
    r = Resource(type="AWS::Athena::WorkGroup", identifier="wg1")
    _prepare_athena_workgroup(r, session)
    client.get_paginator.assert_not_called()


def test_prepare_athena_pagination_follows_next_token():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [{"NamedQueryIds": []}]
    client.list_prepared_statements.side_effect = [
        {"PreparedStatements": [{"StatementName": "ps1"}], "NextToken": "tok"},
        {"PreparedStatements": []},
    ]
    r = Resource(type="AWS::Athena::WorkGroup", identifier="wg1")
    _prepare_athena_workgroup(r, session)
    assert client.list_prepared_statements.call_count == 2
    assert client.delete_prepared_statement.call_count == 1


def test_delete_athena_workgroup_calls_delete():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    r = Resource(type="AWS::Athena::WorkGroup", identifier="wg1")
    _delete_athena_workgroup(r, session)
    client.delete_work_group.assert_called_once()
