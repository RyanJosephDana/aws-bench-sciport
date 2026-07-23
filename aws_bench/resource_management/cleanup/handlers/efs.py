"""EFS file system cleanup handler.

An ``AWS::EFS::FileSystem`` cannot be deleted while it still has mount targets:
CCAPI/CloudFormation ``DeleteFileSystem`` fails with ``FileSystemInUse``. Each
mount target also owns a *requester-managed* ENI in a VPC subnet, so a lingering
EFS file system — one a force-deleted stack abandoned, or that a managed service
created inside the stack's VPC and did not remove — pins the subnet/VPC and
stalls the teardown.

This prepare handler deletes every mount target and waits for them to clear, so
the subsequent CCAPI ``DeleteFileSystem`` succeeds and the mount-target ENIs are
released (which also unpins the subnet/VPC). The ENI reaper does the same release
from the VPC side during stack deletion (``reap_vpc_enis``); this handler covers
the case where the orphaned file system itself is swept.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.resource_management.utils.polling import wait_until
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

# Mount targets delete asynchronously (~30-90s). Bounded so a stuck mount target
# cannot hang the sweep forever — if it does not clear the file-system delete is
# left to fail loudly rather than blocking indefinitely.
_MOUNT_TARGET_DELETE_TIMEOUT = 180
_MOUNT_TARGET_POLL_INTERVAL = 10


@resource_handler("AWS::EFS::FileSystem", role="prepare")
def _prepare(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the file system's mount targets so it becomes deletable.

    Returns SUCCESS once no mount targets remain (or none existed); FAILED if they
    do not clear within the timeout; SKIPPED if the file system is already gone.
    """
    fs_id = resource.identifier
    efs = build_client(session, "efs")
    try:
        mount_targets = efs.describe_mount_targets(FileSystemId=fs_id).get("MountTargets", [])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "FileSystemNotFound":
            return HandlerResult(
                resource_id=fs_id,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="File system not found",
            )
        logger.warning("Could not list mount targets for EFS '%s': %s", fs_id, e)
        return HandlerResult(
            resource_id=fs_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"describe_mount_targets failed: {e}",
        )
    except BotoCoreError as e:
        logger.warning("Could not list mount targets for EFS '%s': %s", fs_id, e)
        return HandlerResult(
            resource_id=fs_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"describe_mount_targets failed: {e}",
        )

    if not mount_targets:
        return HandlerResult(
            resource_id=fs_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.SUCCESS,
            message="No mount targets to delete",
        )

    for mount_target in mount_targets:
        mt_id = mount_target.get("MountTargetId", "")
        if not mt_id:
            continue
        try:
            efs.delete_mount_target(MountTargetId=mt_id)
            logger.debug("Deleted EFS mount target %s (fs %s)", mt_id, fs_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "MountTargetNotFound":
                logger.debug("Could not delete EFS mount target %s: %s", mt_id, e)
        except BotoCoreError as e:
            logger.debug("Could not delete EFS mount target %s: %s", mt_id, e)

    # DeleteFileSystem fails with FileSystemInUse until every mount target is gone,
    # so wait for them to clear before letting the CCAPI delete proceed.
    def _mount_targets_cleared() -> bool:
        return not efs.describe_mount_targets(FileSystemId=fs_id).get("MountTargets", [])

    if not wait_until(
        _mount_targets_cleared,
        timeout=_MOUNT_TARGET_DELETE_TIMEOUT,
        interval=_MOUNT_TARGET_POLL_INTERVAL,
    ):
        return HandlerResult(
            resource_id=fs_id,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message="Mount targets did not finish deleting within timeout",
        )

    return HandlerResult(
        resource_id=fs_id,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message=f"Deleted {len(mount_targets)} mount target(s)",
    )
