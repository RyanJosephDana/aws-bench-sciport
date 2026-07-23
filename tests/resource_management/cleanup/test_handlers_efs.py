"""Tests for the EFS file system cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

import aws_bench.resource_management.cleanup.handlers.efs as efs_handler
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import PREPARE_REGISTRY
from aws_bench.resource_management.cleanup.models import HandlerStatus


def _session_with_efs() -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    efs = MagicMock()
    session.client.return_value = efs
    return session, efs


def _resource(fs_id: str = "fs-0367f8d26e80c9c6a") -> Resource:
    return Resource(type="AWS::EFS::FileSystem", identifier=fs_id)


def _run(session):
    return PREPARE_REGISTRY["AWS::EFS::FileSystem"](_resource(), session)


def test_handler_is_registered():
    assert "AWS::EFS::FileSystem" in PREPARE_REGISTRY


def test_deletes_mount_targets_then_succeeds():
    """Deletes every mount target and waits until they clear, then reports SUCCESS."""
    session, efs = _session_with_efs()
    # First call lists 2 mount targets; the wait predicate then sees them cleared.
    efs.describe_mount_targets.side_effect = [
        {"MountTargets": [{"MountTargetId": "fsmt-1"}, {"MountTargetId": "fsmt-2"}]},
        {"MountTargets": []},
    ]
    result = _run(session)
    assert efs.delete_mount_target.call_count == 2
    efs.delete_mount_target.assert_any_call(MountTargetId="fsmt-1")
    efs.delete_mount_target.assert_any_call(MountTargetId="fsmt-2")
    assert result.status == HandlerStatus.SUCCESS


def test_no_mount_targets_is_success_noop():
    session, efs = _session_with_efs()
    efs.describe_mount_targets.return_value = {"MountTargets": []}
    result = _run(session)
    efs.delete_mount_target.assert_not_called()
    assert result.status == HandlerStatus.SUCCESS


def test_file_system_not_found_is_skipped():
    session, efs = _session_with_efs()
    efs.describe_mount_targets.side_effect = ClientError(
        {"Error": {"Code": "FileSystemNotFound", "Message": "gone"}}, "DescribeMountTargets"
    )
    result = _run(session)
    assert result.status == HandlerStatus.SKIPPED


def test_fails_closed_when_mount_targets_do_not_clear():
    """If mount targets never clear within the timeout, report FAILED (don't hang)."""
    session, efs = _session_with_efs()
    efs.describe_mount_targets.return_value = {"MountTargets": [{"MountTargetId": "fsmt-1"}]}
    with (
        patch.object(efs_handler, "_MOUNT_TARGET_DELETE_TIMEOUT", 0),
        patch.object(efs_handler, "_MOUNT_TARGET_POLL_INTERVAL", 0),
    ):
        result = _run(session)
    efs.delete_mount_target.assert_called_once_with(MountTargetId="fsmt-1")
    assert result.status == HandlerStatus.FAILED
