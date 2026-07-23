"""Tests for the AWS Lambda function cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.lambda_ import _delete
from aws_bench.resource_management.cleanup.models import HandlerStatus
from aws_bench.resource_management.deferred import deferred_scope, is_deferred

_FN = "cf-edge-headers-json"
_TYPE = "AWS::Lambda::Function"


def _resource() -> Resource:
    return Resource(type=_TYPE, identifier=_FN)


def _session() -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    return session, client


def _replicated_error() -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "InvalidParameterValueException",
                "Message": (
                    f"Lambda was unable to delete arn:aws:lambda:us-east-1:1:function:{_FN} "
                    "because it is a replicated function. Please see our documentation for "
                    "Deleting Lambda@Edge Functions and Replicas."
                ),
            }
        },
        "DeleteFunction",
    )


def test_delete_success():
    session, client = _session()
    result = _delete(_resource(), session)
    session.client.assert_called_with("lambda")
    client.delete_function.assert_called_once_with(FunctionName=_FN)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_already_gone_is_skipped():
    session, client = _session()
    client.delete_function.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DeleteFunction"
    )
    result = _delete(_resource(), session)
    assert result.status == HandlerStatus.SKIPPED
    assert "already gone" in result.message


def test_delete_replicated_function_is_deferred_and_skipped():
    session, client = _session()
    client.delete_function.side_effect = _replicated_error()
    with deferred_scope():
        result = _delete(_resource(), session)
        assert is_deferred(_TYPE, _FN)
    assert result.status == HandlerStatus.SKIPPED
    assert "deferred" in result.message.lower()


def test_delete_replicated_function_without_scope_still_skips():
    """Outside a deferred scope the mark is a no-op, but the delete must not fail."""
    session, client = _session()
    client.delete_function.side_effect = _replicated_error()
    result = _delete(_resource(), session)
    assert result.status == HandlerStatus.SKIPPED
    assert not is_deferred(_TYPE, _FN)


def test_delete_other_invalid_parameter_error_fails():
    """An InvalidParameterValueException that is not the replicated-function case fails."""
    session, client = _session()
    client.delete_function.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValueException", "Message": "bad runtime"}},
        "DeleteFunction",
    )
    with deferred_scope():
        result = _delete(_resource(), session)
        assert not is_deferred(_TYPE, _FN)
    assert result.status == HandlerStatus.FAILED
    assert "Failed to delete function" in result.message


def test_delete_other_client_error_fails():
    session, client = _session()
    client.delete_function.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "DeleteFunction"
    )
    result = _delete(_resource(), session)
    assert result.status == HandlerStatus.FAILED


def test_delete_botocore_error_fails():
    session, client = _session()
    client.delete_function.side_effect = BotoCoreError()
    result = _delete(_resource(), session)
    assert result.status == HandlerStatus.FAILED
    assert "Connection error" in result.message
