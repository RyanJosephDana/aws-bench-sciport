"""Tests for ResourceManager.snapshot_scenarios and build_contexts_from_provisioning."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aws_bench.resource_management.manager import ResourceManager
from aws_bench.resource_management.reset.models import ResetResult
from aws_bench.resource_management.snapshot.models import (
    SnapshotContext,
    SnapshotResult,
    SnapshotStage,
)

VALID_TOML = """\
schema_version = "1.0"

[scenario]
name = "{name}"
account_tags = ["PRIMARY"]
regions = ["us-east-1", "us-west-2"]
"""


def _make_scenario_dir(root: Path, name: str) -> Path:
    """Create a valid scenario directory."""
    sd = root / name
    sd.mkdir()
    (sd / "scenario.toml").write_text(VALID_TOML.format(name=name))
    (sd / "scenario").mkdir()
    (sd / "scenario" / "Dockerfile").write_text("FROM alpine\n")
    (sd / "deploy").mkdir()
    (sd / "deploy" / "deploy.sh").write_text("#!/bin/sh\n")
    return sd


def _make_context(
    scenario_name: str,
    account_ids: list[str],
    stage: SnapshotStage = SnapshotStage.POST_SETUP,
) -> SnapshotContext:
    """Create a SnapshotContext for testing."""
    return SnapshotContext(
        scenario_id=scenario_name,
        scenario_hash="abc123",
        regions=["us-east-1", "us-west-2"],
        stage=stage,
        account_ids=account_ids,
    )


@pytest.mark.asyncio
async def test_snapshot_scenarios_delegates_to_snapshot_manager():
    """Test snapshot_scenarios delegates to SnapshotManager.snapshot_scenarios."""
    ctx = _make_context("test-scenario", ["111111111111"])
    expected_results = {"test-scenario": [SnapshotResult(account_id="111111111111", success=True)]}

    mock_snapshot_mgr = Mock()
    mock_snapshot_mgr.snapshot_scenarios.return_value = expected_results

    with patch(
        "aws_bench.resource_management.manager.SnapshotManager", return_value=mock_snapshot_mgr
    ):
        results = await ResourceManager.snapshot_scenarios([ctx])

    mock_snapshot_mgr.snapshot_scenarios.assert_called_once_with([ctx])
    assert results == expected_results


@pytest.mark.asyncio
async def test_snapshot_scenarios_passes_multiple_contexts():
    """Test snapshot_scenarios passes all contexts to SnapshotManager."""
    contexts = [
        _make_context("scenario-a", ["111111111111"]),
        _make_context("scenario-b", ["222222222222"]),
    ]
    expected_results = {
        "scenario-a": [SnapshotResult(account_id="111111111111", success=True)],
        "scenario-b": [SnapshotResult(account_id="222222222222", success=True)],
    }

    mock_snapshot_mgr = Mock()
    mock_snapshot_mgr.snapshot_scenarios.return_value = expected_results

    with patch(
        "aws_bench.resource_management.manager.SnapshotManager", return_value=mock_snapshot_mgr
    ):
        results = await ResourceManager.snapshot_scenarios(contexts)

    mock_snapshot_mgr.snapshot_scenarios.assert_called_once_with(contexts)
    assert results == expected_results


@pytest.mark.asyncio
async def test_verify_scenario(tmp_path):
    """Test verify_scenario verifies accounts against baseline."""
    scenario_dir = _make_scenario_dir(tmp_path, "test-verify")

    account_mapping = {"PRIMARY": "111111111111"}

    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    from aws_bench.resource_management.verify.models import AccountVerifyResult

    mock_verify_result = AccountVerifyResult(
        account_id="111111111111",
        environment_id="test-verify",
        success=True,
        region_results=[],
    )

    mock_verify_mgr = Mock()
    mock_verify_mgr.verify_account_multiregion.return_value = mock_verify_result

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.VerifyManager",
            return_value=mock_verify_mgr,
        ),
    ):
        results = await ResourceManager.verify_scenario(
            scenario_name="test-verify",
            scenario_dir=scenario_dir,
            account_mapping=account_mapping,
            region=None,
        )

    assert len(results) == 1
    assert results[0].success is True


@pytest.mark.asyncio
async def test_reset_scenarios(tmp_path):
    """Test reset_scenarios resets accounts to baseline."""
    scenario_dir = _make_scenario_dir(tmp_path, "test-reset")

    account_mapping = {"PRIMARY": "111111111111"}

    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    mock_reset_result = ResetResult(
        success=True,
        reason="Reset successful",
        needs_redeploy=False,
    )

    mock_reset_mgr = Mock()
    mock_reset_mgr.reset_account = AsyncMock(return_value=mock_reset_result)

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.ResetManager",
            return_value=mock_reset_mgr,
        ),
    ):
        results = await ResourceManager.reset_scenarios(
            scenario_name="test-reset",
            scenario_dir=scenario_dir,
            account_mapping=account_mapping,
            max_concurrent=10,
        )

    assert len(results) == 1
    assert results[0].success is True


@pytest.mark.asyncio
async def test_cleanup_scenarios_by_name():
    """Test cleanup_scenarios_by_name removes resources."""
    account_mapping = {"PRIMARY": "111111111111"}

    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    mock_cleanup_mgr = Mock()
    mock_cleanup_mgr.cleanup_all_stacks = AsyncMock(return_value=Mock())

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.CleanupManager",
            return_value=mock_cleanup_mgr,
        ),
    ):
        results = await ResourceManager.cleanup_scenarios_by_name(
            scenario_name="test-cleanup",
            account_mapping=account_mapping,
            max_concurrent=10,
            all_regions=False,
        )

    assert len(results) == 1
    assert results[0].error is None
    # Baseline path must forward all_regions=False (not an empty regions list, which
    # CleanupManager rejects with "regions list cannot be empty"). regions defaults to None
    # here (no scenario regions passed).
    mock_cleanup_mgr.cleanup_all_stacks.assert_awaited_once_with(
        regions=None, all_regions=False, sweep_post_setup=True
    )


@pytest.mark.asyncio
async def test_cleanup_scenarios_by_name_forwards_scenario_regions():
    """Scenario regions are forwarded to cleanup_all_stacks (the no-baseline fallback floor)."""
    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    mock_cleanup_mgr = Mock()
    mock_cleanup_mgr.cleanup_all_stacks = AsyncMock(return_value=Mock())

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.CleanupManager",
            return_value=mock_cleanup_mgr,
        ),
    ):
        await ResourceManager.cleanup_scenarios_by_name(
            scenario_name="test-cleanup",
            account_mapping={"PRIMARY": "111111111111"},
            max_concurrent=10,
            all_regions=False,
            regions=["us-east-1", "us-east-2", "ap-southeast-1"],
        )

    mock_cleanup_mgr.cleanup_all_stacks.assert_awaited_once_with(
        regions=["us-east-1", "us-east-2", "ap-southeast-1"],
        all_regions=False,
        sweep_post_setup=True,
    )


@pytest.mark.asyncio
async def test_cleanup_scenarios_by_name_nests_output_dir_per_account(tmp_path):
    """Each account's cleanup writes under output_dir/<account_tag>, not the shared root.

    Concurrent accounts otherwise clobber each other's summary.json / run_metadata.json /
    per-region manifests in the one phase dir; nesting per tag keeps them separate.
    """
    account_mapping = {"PRIMARY": "111111111111", "SECONDARY": "222222222222"}

    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    mock_cleanup_mgr = Mock()
    mock_cleanup_mgr.cleanup_all_stacks = AsyncMock(return_value=Mock())

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.CleanupManager",
            return_value=mock_cleanup_mgr,
        ) as cleanup_cls,
    ):
        await ResourceManager.cleanup_scenarios_by_name(
            scenario_name="test-cleanup",
            account_mapping=account_mapping,
            max_concurrent=10,
            all_regions=False,
            output_dir=tmp_path,
        )

    output_dirs = {c.kwargs["output_dir"] for c in cleanup_cls.call_args_list}
    assert output_dirs == {tmp_path / "PRIMARY", tmp_path / "SECONDARY"}


@pytest.mark.asyncio
async def test_reset_scenarios_nests_output_dir_per_account(tmp_path):
    """Each account's reset writes under output_dir/<account_tag>, not the shared root."""
    account_mapping = {"PRIMARY": "111111111111", "SECONDARY": "222222222222"}

    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    mock_reset_mgr = Mock()
    mock_reset_mgr.reset_account = AsyncMock(
        return_value=ResetResult(success=True, reason="ok", needs_redeploy=False)
    )

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.ResetManager",
            return_value=mock_reset_mgr,
        ) as reset_cls,
    ):
        await ResourceManager.reset_scenarios(
            scenario_name="test-reset",
            scenario_dir=None,
            account_mapping=account_mapping,
            max_concurrent=10,
            output_dir=tmp_path,
        )

    output_dirs = {c.kwargs["output_dir"] for c in reset_cls.call_args_list}
    assert output_dirs == {tmp_path / "PRIMARY", tmp_path / "SECONDARY"}


@pytest.mark.asyncio
async def test_cleanup_scenarios_by_name_all_regions_true():
    """Test cleanup_scenarios_by_name forwards all_regions=True to CleanupManager."""
    account_mapping = {"PRIMARY": "111111111111"}

    mock_cred_provider = Mock()
    mock_cred_provider.get_session_for_account.return_value = Mock()

    mock_cleanup_mgr = Mock()
    mock_cleanup_mgr.cleanup_all_stacks = AsyncMock(return_value=Mock())

    with (
        patch(
            "aws_bench.resource_management.manager.CredentialProvider.get",
            return_value=mock_cred_provider,
        ),
        patch(
            "aws_bench.resource_management.manager.CleanupManager",
            return_value=mock_cleanup_mgr,
        ),
    ):
        results = await ResourceManager.cleanup_scenarios_by_name(
            scenario_name="test-cleanup",
            account_mapping=account_mapping,
            max_concurrent=10,
            all_regions=True,
        )

    assert len(results) == 1
    assert results[0].error is None
    mock_cleanup_mgr.cleanup_all_stacks.assert_awaited_once_with(
        regions=None, all_regions=True, sweep_post_setup=True
    )
