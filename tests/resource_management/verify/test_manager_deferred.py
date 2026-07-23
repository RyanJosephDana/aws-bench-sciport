"""Tests that new-resource verification excludes deferred deletions (Lambda@Edge)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aws_bench.resource_management.deferred import deferred_scope, mark_deferred
from aws_bench.resource_management.verify.manager import VerifyManager

_TYPE = "AWS::Lambda::Function"
_FN = "cf-edge-headers-json"


def _manager() -> VerifyManager:
    """A VerifyManager with its heavy __init__ bypassed and the scanner stubbed."""
    mgr = object.__new__(VerifyManager)
    mgr._session = MagicMock()
    mgr._region_name = "us-east-1"
    mgr._scan_mgr = MagicMock()
    mgr._scan_mgr.scan_resources.return_value = MagicMock(detected={}, failed={})
    return mgr


def test_check_new_resources_excludes_deferred():
    """The only 'new' resource was deferred this run, so verification passes."""
    mgr = _manager()
    with (
        patch(
            "aws_bench.resource_management.verify.manager.find_new_resources",
            return_value={_TYPE: [{"Identifier": _FN}]},
        ),
        patch("aws_bench.resource_management.verify.manager.AwsManagedOwnershipProbe") as probe,
    ):
        probe.return_value.exclude_aws_managed.side_effect = lambda r: r
        with deferred_scope():
            mark_deferred(_TYPE, _FN)
            result = mgr._check_new_resources({}, {}, set())
    assert result is None


def test_check_new_resources_reports_undeferred():
    """Without a deferral the same resource is reported as new (verification fails)."""
    mgr = _manager()
    with (
        patch(
            "aws_bench.resource_management.verify.manager.find_new_resources",
            return_value={_TYPE: [{"Identifier": _FN}]},
        ),
        patch("aws_bench.resource_management.verify.manager.AwsManagedOwnershipProbe") as probe,
    ):
        probe.return_value.exclude_aws_managed.side_effect = lambda r: r
        with deferred_scope():
            result = mgr._check_new_resources({}, {}, set())
    assert result is not None
    assert result.success is False
    assert result.new_resources == {_TYPE: [{"Identifier": _FN}]}
