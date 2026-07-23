"""Tests for cleanup manager orchestration."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aws_bench.resource_management.cleanup.manager import CleanupManager
from aws_bench.resource_management.cleanup.models import (
    CleanupSummary,
    RegionResult,
    StackDeletionResult,
    StackDeletionStatus,
)


def test_manager_initialization():
    """Test CleanupManager initialization with session."""
    session = Mock()
    manager = CleanupManager(session)
    assert manager._session == session
    assert manager._account_id is None


def test_cleanup_summary_properties():
    """Test CleanupSummary calculated properties."""
    summary = CleanupSummary(
        regions=[
            RegionResult(
                region="us-east-1", stacks_found=5, stacks_deleted=4, stacks_failed=["failed-stack"]
            ),
            RegionResult(region="us-west-2", stacks_found=3, stacks_deleted=3),
        ],
        orphaned_resources={"AWS::S3::Bucket": ["bucket-1", "bucket-2"]},
    )

    assert summary.total_stacks == 8
    assert summary.total_deleted == 7
    assert summary.total_failed == 1
    assert summary.total_orphaned == 2
    assert not summary.all_stacks_succeeded


def test_cleanup_summary_all_stacks_succeeded():
    """Test CleanupSummary all_stacks_succeeded property."""
    summary = CleanupSummary(
        regions=[
            RegionResult(region="us-east-1", stacks_found=5, stacks_deleted=5),
        ]
    )
    assert summary.all_stacks_succeeded


def test_cleanup_summary_all_stacks_succeeded_false_on_region_error():
    """Test CleanupSummary all_stacks_succeeded is False if a region has an error."""
    summary = CleanupSummary(
        regions=[
            RegionResult(region="us-east-1", stacks_found=5, stacks_deleted=5),
            RegionResult(region="us-west-2", error="Connection timeout"),
        ]
    )
    assert not summary.all_stacks_succeeded


def test_cleanup_summary_all_stacks_succeeded_ignores_orphaned_resources():
    """Test all_stacks_succeeded only checks stack status, not orphaned resources.

    This weaker, orphan-agnostic verdict is reused by reset and the untag/terminate
    gate, so it must stay orphan-agnostic; cleanup uses the stricter is_clean instead.
    """
    summary = CleanupSummary(
        regions=[
            RegionResult(region="us-east-1", stacks_found=5, stacks_deleted=5),
        ],
        orphaned_resources={"AWS::S3::Bucket": ["bucket-1", "bucket-2", "bucket-3"]},
    )
    # Stacks succeeded but orphaned resources remain
    assert summary.all_stacks_succeeded is True
    assert summary.total_orphaned == 3


def test_cleanup_summary_is_clean_true_when_all_deleted_and_no_orphans():
    """is_clean is True when every stack was deleted and no orphans remain."""
    summary = CleanupSummary(
        regions=[RegionResult(region="us-east-1", stacks_found=5, stacks_deleted=5)],
    )
    assert summary.all_stacks_succeeded is True
    assert summary.is_clean is True


def test_cleanup_summary_is_clean_false_when_orphans_remain():
    """is_clean is False when orphans remain, even though all stacks were deleted."""
    summary = CleanupSummary(
        regions=[RegionResult(region="us-east-1", stacks_found=5, stacks_deleted=5)],
        orphaned_resources={"AWS::S3::Bucket": ["bucket-1", "bucket-2"]},
    )
    # all_stacks_succeeded stays orphan-agnostic; is_clean does not.
    assert summary.all_stacks_succeeded is True
    assert summary.is_clean is False


def test_cleanup_summary_is_clean_false_when_stack_failed():
    """is_clean is False when a stack failed to delete, regardless of orphans."""
    summary = CleanupSummary(
        regions=[
            RegionResult(
                region="us-east-1", stacks_found=5, stacks_deleted=4, stacks_failed=["stuck-stack"]
            ),
        ],
    )
    assert summary.all_stacks_succeeded is False
    assert summary.is_clean is False


def test_region_result_defaults():
    """Test RegionResult default values."""
    result = RegionResult(region="us-east-1")
    assert result.stacks_found == 0
    assert result.stacks_deleted == 0
    assert result.stacks_failed == []
    assert result.stacks_failed_count == 0
    assert result.error == ""


def test_region_result_stacks_failed_count_derived_from_list():
    """Test stacks_failed_count is always derived from stacks_failed list."""
    result = RegionResult(region="us-east-1", stacks_failed=["stack1", "stack2", "stack3"])
    assert result.stacks_failed_count == 3
    assert len(result.stacks_failed) == 3

    # Verify it's truly derived, not stored
    result.stacks_failed.append("stack4")
    assert result.stacks_failed_count == 4


def test_get_account_id():
    """Test _get_account_id fetches from STS."""
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client

    manager = CleanupManager(session)
    account_id = manager._get_account_id()

    assert account_id == "123456789012"
    session.client.assert_called_once_with("sts")
    sts_client.get_caller_identity.assert_called_once()


def test_get_account_id_caches_result():
    """Test _get_account_id caches the result."""
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client

    manager = CleanupManager(session)
    account_id1 = manager._get_account_id()
    account_id2 = manager._get_account_id()

    assert account_id1 == account_id2 == "123456789012"
    # Should only call STS once due to caching
    sts_client.get_caller_identity.assert_called_once()


def test_discover_regions():
    """Test region discovery."""
    session = Mock()
    ec2_client = Mock()
    ec2_client.describe_regions.return_value = {
        "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
    }
    session.client.return_value = ec2_client

    manager = CleanupManager(session)
    regions = manager._discover_regions()

    assert regions == ["us-east-1", "us-west-2"]
    session.client.assert_called_once_with("ec2", region_name="us-east-1")


def test_discover_regions_error():
    """Test region discovery handles errors."""
    from botocore.exceptions import ClientError

    session = Mock()
    ec2_client = Mock()
    ec2_client.describe_regions.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "Not authorized"}},
        "DescribeRegions",
    )
    session.client.return_value = ec2_client

    manager = CleanupManager(session)
    with pytest.raises(RuntimeError, match="Failed to list AWS regions"):
        manager._discover_regions()


def test_cleanup_stack_empty_name_raises():
    """Test cleanup_stack raises ValueError for empty stack name."""
    session = Mock()
    manager = CleanupManager(session)
    with pytest.raises(ValueError, match="stack_name cannot be empty"):
        asyncio.run(manager.cleanup_stack(""))


def test_cleanup_stack_whitespace_name_raises():
    """Test cleanup_stack raises ValueError for whitespace-only stack name."""
    session = Mock()
    manager = CleanupManager(session)
    with pytest.raises(ValueError, match="stack_name cannot be empty"):
        asyncio.run(manager.cleanup_stack("   "))


def test_cleanup_stack_delegates_to_stack_deleter_delete_stack():
    """cleanup_stack routes the single-stack delete through StackDeleter.delete_stack.

    This is the hop reset depends on: StackRestorer._delete_for_resetup -> this
    cleanup_stack -> StackDeleter.delete_stack (whose FAILED path runs the IPAM reap).
    A SUCCESS delete must surface as all_stacks_succeeded so reset returns
    DELETED_NEEDS_REDEPLOY.
    """
    session = Mock()
    manager = CleanupManager(session)

    delete_stack = AsyncMock(
        return_value=StackDeletionResult(
            stack_name="ipam-stack", status=StackDeletionStatus.SUCCESS
        )
    )
    with (
        patch.object(manager, "_find_stack_region", new=AsyncMock(return_value="us-east-1")),
        patch.object(manager, "_write_metadata"),
        patch.object(manager, "_save_summary"),
        patch("aws_bench.resource_management.cleanup.manager.StackDeleter") as mock_deleter_cls,
    ):
        mock_deleter_cls.return_value.delete_stack = delete_stack
        summary = asyncio.run(manager.cleanup_stack("ipam-stack"))

    delete_stack.assert_awaited_once_with("ipam-stack")
    assert summary.all_stacks_succeeded
    assert summary.total_deleted == 1


def test_cleanup_all_stacks_handles_scan_failure(tmp_path):
    """Test cleanup_all_stacks continues when orphan scan fails."""
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client

    manager = CleanupManager(session, output_dir=tmp_path)

    with (
        patch.object(manager, "_discover_regions", return_value=["us-east-1"]),
        patch.object(manager, "_write_metadata"),
        patch.object(manager, "_cleanup_single_region") as mock_cleanup,
        patch.object(manager, "_scan_orphaned_resources", side_effect=RuntimeError("Scan failed")),
        patch.object(manager, "_log_summary"),
        patch.object(manager, "_save_summary"),
    ):
        mock_cleanup.return_value = RegionResult(
            region="us-east-1", stacks_found=2, stacks_deleted=2
        )

        summary = asyncio.run(manager.cleanup_all_stacks())

        # Should complete successfully with empty orphan map
        assert summary.total_stacks == 2
        assert summary.total_deleted == 2
        assert summary.total_orphaned == 0
        assert summary.orphaned_resources == {}


def test_limit_to_baseline_regions_empty_intersection():
    """Test _limit_to_baseline_regions handles empty intersection gracefully."""
    from datetime import datetime, timezone

    from aws_bench.resource_management.snapshot.models import Snapshot

    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client

    # Create manager with snapshot manager
    manager = CleanupManager(session, env_name="test-env")

    # Mock snapshot with baseline regions
    mock_snapshot = Snapshot(
        timestamp=datetime.now(timezone.utc),
        account_id="123456789012",
        environment_id="test-env",
        scenario_hash="hash_v1",
        drift_baseline={},
        stack_metadata={},
        resource_ids={},
        regions=["us-east-1", "us-west-2"],  # Baseline regions
    )

    with patch.object(
        manager._snapshot_mgr, "load_snapshot", return_value=mock_snapshot
    ) as mock_load:
        # Request regions with no overlap
        requested_regions = ["eu-west-1", "eu-central-1"]
        result = asyncio.run(manager._limit_to_baseline_regions(requested_regions))

        # Should fall back to requested regions with warning
        assert result == requested_regions
        mock_load.assert_called_once()


def test_limit_to_baseline_regions_partial_intersection():
    """Test _limit_to_baseline_regions with partial intersection."""
    from datetime import datetime, timezone

    from aws_bench.resource_management.snapshot.models import Snapshot

    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client

    manager = CleanupManager(session, env_name="test-env")

    mock_snapshot = Snapshot(
        timestamp=datetime.now(timezone.utc),
        account_id="123456789012",
        environment_id="test-env",
        scenario_hash="hash_v1",
        drift_baseline={},
        stack_metadata={},
        resource_ids={},
        regions=["us-east-1", "us-west-2", "eu-west-1"],
    )

    with patch.object(manager._snapshot_mgr, "load_snapshot", return_value=mock_snapshot):
        # Request regions with partial overlap
        requested_regions = ["us-east-1", "ap-south-1"]
        result = asyncio.run(manager._limit_to_baseline_regions(requested_regions))

        # Should return only the intersection
        assert result is not None
        assert set(result) == {"us-east-1"}


def test_limit_to_baseline_regions_same_set_different_order():
    """Test _limit_to_baseline_regions doesn't log spuriously when sets match."""
    from datetime import datetime, timezone

    from aws_bench.resource_management.snapshot.models import Snapshot

    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client

    manager = CleanupManager(session, env_name="test-env")

    mock_snapshot = Snapshot(
        timestamp=datetime.now(timezone.utc),
        account_id="123456789012",
        environment_id="test-env",
        scenario_hash="hash_v1",
        drift_baseline={},
        stack_metadata={},
        resource_ids={},
        regions=["us-east-1", "us-west-2", "eu-west-1"],
    )

    with (
        patch.object(manager._snapshot_mgr, "load_snapshot", return_value=mock_snapshot),
        patch("aws_bench.resource_management.cleanup.manager.logger") as mock_logger,
    ):
        # Request same regions in different order
        requested_regions = ["eu-west-1", "us-west-2", "us-east-1"]
        result = asyncio.run(manager._limit_to_baseline_regions(requested_regions))

        # Should return intersection (all regions)
        assert result is not None
        assert set(result) == set(requested_regions)

        # Should NOT log "Limiting cleanup to..." since sets are identical
        limiting_calls = [
            call for call in mock_logger.info.call_args_list if "Limiting cleanup" in str(call)
        ]
        assert len(limiting_calls) == 0


def test_sweep_post_setup_residuals_runs_per_region(tmp_path):
    """sweep_post_setup_residuals resolves regions and sweeps each one."""
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client
    manager = CleanupManager(session, output_dir=tmp_path)

    swept: list[str] = []

    async def fake_delete(region, setup):
        swept.append(region)

    with (
        patch.object(
            manager,
            "_resolve_regions",
            new_callable=AsyncMock,
            return_value=["us-east-1", "us-west-2"],
        ),
        patch.object(manager, "_load_snapshot_resource_ids", return_value=None),
        patch.object(manager, "_delete_resources_created_after_setup", side_effect=fake_delete),
    ):
        asyncio.run(manager.sweep_post_setup_residuals())

    assert sorted(swept) == ["us-east-1", "us-west-2"]


def test_sweep_post_setup_residuals_swallows_generic_error(tmp_path):
    """A generic per-region sweep error is swallowed (best-effort)."""
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client
    manager = CleanupManager(session, output_dir=tmp_path)

    async def boom(region, setup):
        raise RuntimeError("scan failed")

    with (
        patch.object(
            manager, "_resolve_regions", new_callable=AsyncMock, return_value=["us-east-1"]
        ),
        patch.object(manager, "_load_snapshot_resource_ids", return_value=None),
        patch.object(manager, "_delete_resources_created_after_setup", side_effect=boom),
    ):
        # Must not raise.
        asyncio.run(manager.sweep_post_setup_residuals())


def test_sweep_post_setup_residuals_propagates_cancellation(tmp_path):
    """A cooperative shutdown during a sweep re-raises to unwind the run."""
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client
    manager = CleanupManager(session, output_dir=tmp_path)

    async def cancel(region, setup):
        raise asyncio.CancelledError()

    with (
        patch.object(
            manager, "_resolve_regions", new_callable=AsyncMock, return_value=["us-east-1"]
        ),
        patch.object(manager, "_load_snapshot_resource_ids", return_value=None),
        patch.object(manager, "_delete_resources_created_after_setup", side_effect=cancel),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(manager.sweep_post_setup_residuals())


def test_cleanup_all_stacks_skips_phase1_when_sweep_post_setup_false(tmp_path):
    """cleanup_all_stacks(sweep_post_setup=False) skips the in-cleanup Phase 1 sweep.

    Patches the two per-phase deletion coroutines directly (not ``_run_sweep_phase``)
    so the real ``_run_sweep_phase`` awaits them — asserting Phase 1's post-setup
    deletion never runs while Phase 3's init-snapshot sweep still does.
    """
    session = Mock()
    sts_client = Mock()
    sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts_client
    manager = CleanupManager(session, output_dir=tmp_path)

    with (
        patch.object(manager, "_discover_regions", return_value=["us-east-1"]),
        patch.object(manager, "_write_metadata"),
        patch.object(
            manager, "_delete_resources_created_after_setup", new_callable=AsyncMock
        ) as mock_phase1,
        patch.object(
            manager, "_delete_resources_not_in_init_snapshot", new_callable=AsyncMock
        ) as mock_phase3,
        patch.object(
            manager,
            "_cleanup_single_region",
            new_callable=AsyncMock,
            return_value=RegionResult(region="us-east-1"),
        ),
        patch.object(
            manager,
            "_scan_orphaned_resources",
            new_callable=AsyncMock,
            side_effect=RuntimeError("skip scan"),
        ),
        patch.object(manager, "_log_summary"),
        patch.object(manager, "_save_summary"),
        patch.object(manager, "_load_snapshot_resource_ids", return_value=None),
    ):
        asyncio.run(manager.cleanup_all_stacks(sweep_post_setup=False))

    # Phase 1 (post-setup) skipped; Phase 3 (init-snapshot sweep) still runs.
    mock_phase1.assert_not_called()
    mock_phase3.assert_called_once()
