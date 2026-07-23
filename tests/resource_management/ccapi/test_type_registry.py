"""Tests for aws_bench.resource_management.ccapi.type_registry."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import tenacity
from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.type_registry import (
    TypeRegistry,
)


def _fresh_account_error() -> ClientError:
    """The not-yet-converged STS/IAM error a freshly-vended account returns from list_types."""
    return ClientError(
        {"Error": {"Code": "InvalidClientTokenId", "Message": "token not yet known"}},
        "ListTypes",
    )


def _paginator_yielding(*type_names: str) -> MagicMock:
    """A cfn client mock whose list_types paginator yields one page with ``type_names``."""
    cfn = MagicMock()
    cfn.get_paginator.return_value.paginate.return_value = [
        {"TypeSummaries": [{"TypeName": n} for n in type_names]}
    ]
    return cfn


# -- TypeRegistry._read_cached_skip_types --


def test_read_cached_skip_types_returns_none_when_file_missing(tmp_path):
    with patch(
        "aws_bench.resource_management.ccapi.type_registry._SKIP_TYPES_PATH",
        tmp_path / "nope.json",
    ):
        assert TypeRegistry(MagicMock(), scan_fn=MagicMock())._read_cached_skip_types() is None


def test_read_cached_skip_types_returns_types_from_valid_cache(tmp_path):
    cache_file = tmp_path / "skip.json"
    cache_file.write_text(json.dumps({"types_to_skip": ["AWS::A::B", "AWS::C::D"]}))
    with patch("aws_bench.resource_management.ccapi.type_registry._SKIP_TYPES_PATH", cache_file):
        result = TypeRegistry(MagicMock(), scan_fn=MagicMock())._read_cached_skip_types()
    assert result == {"AWS::A::B", "AWS::C::D"}


def test_read_cached_skip_types_returns_none_when_cache_stale(tmp_path):
    cache_file = tmp_path / "skip.json"
    cache_file.write_text(json.dumps({"types_to_skip": ["AWS::A::B"]}))
    with (
        patch("aws_bench.resource_management.ccapi.type_registry._SKIP_TYPES_PATH", cache_file),
        patch("aws_bench.resource_management.ccapi.type_registry._SKIP_TYPES_MAX_AGE_DAYS", -1),
    ):
        assert TypeRegistry(MagicMock(), scan_fn=MagicMock())._read_cached_skip_types() is None


def test_read_cached_skip_types_returns_none_on_corrupt_json(tmp_path):
    cache_file = tmp_path / "skip.json"
    cache_file.write_text("not json")
    with patch("aws_bench.resource_management.ccapi.type_registry._SKIP_TYPES_PATH", cache_file):
        assert TypeRegistry(MagicMock(), scan_fn=MagicMock())._read_cached_skip_types() is None


def test_read_cached_skip_types_returns_none_on_non_dict_json(tmp_path):
    cache_file = tmp_path / "skip.json"
    cache_file.write_text(json.dumps(["AWS::A::B"]))
    with patch("aws_bench.resource_management.ccapi.type_registry._SKIP_TYPES_PATH", cache_file):
        assert TypeRegistry(MagicMock(), scan_fn=MagicMock())._read_cached_skip_types() is None


# -- TypeRegistry.load --


@patch("aws_bench.resource_management.ccapi.type_registry.TypeRegistry._read_cached_skip_types")
def test_load_returns_cached_when_available(mock_read):
    mock_read.return_value = {"AWS::A::B"}
    result = TypeRegistry(MagicMock(), scan_fn=MagicMock()).load_skip_types()
    assert result == {"AWS::A::B"}


@patch.object(TypeRegistry, "generate_skip_types")
@patch("aws_bench.resource_management.ccapi.type_registry.TypeRegistry._read_cached_skip_types")
def test_load_regenerates_when_cache_missing(mock_read, mock_generate):
    mock_read.return_value = None
    mock_generate.return_value = {"AWS::X::Y"}
    result = TypeRegistry(MagicMock(), scan_fn=MagicMock()).load_skip_types()
    assert result == {"AWS::X::Y"}
    mock_generate.assert_called_once()


# -- TypeRegistry._classify_skip_types --


def test_classify_skip_types_skips_unsupported_error_types():
    failed = {
        "AWS::A::B": "An error occurred (UnsupportedActionException)",
        "AWS::C::D": "An error occurred (TypeNotFoundException)",
    }
    result = TypeRegistry(MagicMock(), scan_fn=MagicMock())._classify_skip_types(failed)
    assert result == {"AWS::A::B", "AWS::C::D"}


def test_classify_skip_types_keeps_non_skip_error_types():
    failed = {
        "AWS::A::B": "An error occurred (AccessDeniedException)",
    }
    result = TypeRegistry(MagicMock(), scan_fn=MagicMock())._classify_skip_types(failed)
    assert result == set()


def test_classify_skip_types_mixed_skip_and_keep():
    failed = {
        "AWS::A::B": "An error occurred (UnsupportedActionException)",
        "AWS::C::D": "An error occurred (AccessDeniedException)",
    }
    result = TypeRegistry(MagicMock(), scan_fn=MagicMock())._classify_skip_types(failed)
    assert result == {"AWS::A::B"}


def test_classify_skip_types_empty_input():
    assert TypeRegistry(MagicMock(), scan_fn=MagicMock())._classify_skip_types({}) == set()


# -- TypeRegistry.generate_skip_types --


@patch("aws_bench.resource_management.ccapi.type_registry.write_json")
def test_generate_persists_to_disk(mock_write):
    from aws_bench.resource_management.ccapi.models import ScanResult

    session = MagicMock()
    cfn = MagicMock()
    session.client.return_value = cfn
    cfn.get_paginator.return_value.paginate.return_value = [
        {"TypeSummaries": [{"TypeName": "AWS::A::B"}, {"TypeName": "AWS::C::D"}]}
    ]
    mock_scan = MagicMock(
        return_value=ScanResult(
            detected={},
            failed={"AWS::A::B": "An error occurred (UnsupportedActionException)"},
        )
    )

    result = TypeRegistry(session, scan_fn=mock_scan).generate_skip_types()

    assert result == {"AWS::A::B"}
    mock_write.assert_called_once()
    data, path = mock_write.call_args[0]
    assert data == {"types_to_skip": ["AWS::A::B"]}


# -- TypeRegistry.list_all_resource_types (fresh-account retry) --


@patch.object(
    TypeRegistry._paginate_resource_types.retry,  # type: ignore[attr-defined]
    "wait",
    tenacity.wait_none(),
)
@patch("aws_bench.resource_management.ccapi.type_registry.build_client")
def test_list_all_resource_types_retries_fresh_account_error(mock_build_client):
    """A not-yet-converged list_types on a fresh account is retried, then succeeds."""
    good = _paginator_yielding("AWS::A::B", "AWS::C::D")
    bad = MagicMock()
    bad.get_paginator.return_value.paginate.side_effect = _fresh_account_error()
    # First build → failing client, second build (retry) → succeeding client.
    mock_build_client.side_effect = [bad, good]

    result = TypeRegistry(MagicMock(), scan_fn=MagicMock()).list_all_resource_types()

    assert result == ["AWS::A::B", "AWS::C::D"]
    assert mock_build_client.call_count == 2


@patch.object(
    TypeRegistry._paginate_resource_types.retry,  # type: ignore[attr-defined]
    "wait",
    tenacity.wait_none(),
)
@patch("aws_bench.resource_management.ccapi.type_registry.build_client")
def test_list_all_resource_types_does_not_retry_other_errors(mock_build_client):
    """A non-fresh-account error is not retried — it propagates on the first attempt."""
    cfn = MagicMock()
    cfn.get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "ListTypes"
    )
    mock_build_client.return_value = cfn

    reg = TypeRegistry(MagicMock(), scan_fn=MagicMock())
    try:
        reg.list_all_resource_types()
        raise AssertionError("expected ClientError")
    except ClientError as exc:
        assert exc.response["Error"]["Code"] == "AccessDeniedException"
    assert mock_build_client.call_count == 1  # no retry
