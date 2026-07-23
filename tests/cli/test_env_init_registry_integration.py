"""Integration: ``aws-bench env init -d <name>@<version>`` happy path.

Mocks at IO boundaries:
  * ``AwsBenchRegistry.from_path`` → fake registry (registry-mode entry)
  * ``TaskClient.download_tasks`` → synthesized download results
  * ``AccountManager`` / ``CredentialProvider`` → no AWS calls fire
  * ``provision_scenarios`` / ``provision_scenarios_with_progress`` →
    successful summary (no real AWS Organizations work)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from harbor.models.task.id import GitTaskId
from harbor.tasks.client import BatchDownloadResult, TaskDownloadResult

from aws_bench.cli.env import init as env_init
from aws_bench.dataset.registry import (
    AwsBenchDatasetSpec,
    AwsBenchRegistry,
    RegistryScenarioId,
)


@pytest.fixture
def fake_registry():
    return AwsBenchRegistry(
        datasets=[
            AwsBenchDatasetSpec(
                name="test-dataset",
                version="1.0.0",
                description="",
                tasks=[],
                scenarios=[
                    RegistryScenarioId(
                        name="ec2-small",
                        git_url="https://github.com/x/y",
                        git_commit_id="abc",
                        path=Path("scenarios/ec2-small"),
                    ),
                    RegistryScenarioId(
                        name="rds-mysql",
                        git_url="https://github.com/x/y",
                        git_commit_id="abc",
                        path=Path("scenarios/rds-mysql"),
                    ),
                ],
            ),
        ],
    )


@pytest.fixture
def materialized_scenarios(tmp_path, fake_registry, scenario_layout_factory):
    """Create on-disk scenario directories that the TaskClient mock returns."""
    cache = tmp_path / "cache"
    paths: dict[str, Path] = {}
    for s in fake_registry.datasets[0].scenarios:
        paths[s.name] = scenario_layout_factory(cache / s.name, s.name)
    return paths


def _patch_task_client_download(mocker, paths_by_name):
    async def fake_download_tasks(self, task_ids, **kwargs):
        results = []
        for tid in task_ids:
            name = tid.path.name
            results.append(
                TaskDownloadResult(
                    path=paths_by_name[name],
                    download_time_sec=0.0,
                    cached=False,
                    resolved_git_commit_id=tid.git_commit_id,
                )
            )
        return BatchDownloadResult(results=results, total_time_sec=0.0)

    return mocker.patch(
        "aws_bench.dataset.config.TaskClient.download_tasks",
        new=fake_download_tasks,
    )


@pytest.fixture
def stub_env_deps(mocker, fake_registry):
    """Stub every external dep of ``env init``: registry, AWS, provisioning."""
    ns = MagicMock()

    mocker.patch(
        "aws_bench.dataset.config.AwsBenchRegistry.from_path",
        return_value=fake_registry,
    )

    mocker.patch("aws_bench.cli.env.preflight_aws_credentials")
    mocker.patch(
        "aws_bench.cli.env.CredentialProvider.get",
        classmethod(lambda cls: MagicMock()),
    )

    account_manager_instance = MagicMock()
    ns.account_manager_cls = mocker.patch(
        "aws_bench.cli.env.AccountManager",
        return_value=account_manager_instance,
    )
    ns.account_manager = account_manager_instance

    fake_summary = MagicMock()
    fake_summary.all_succeeded = True
    ns.provision_with_progress = mocker.patch(
        "aws_bench.cli.env.provision_scenarios_with_progress",
        new=AsyncMock(return_value=fake_summary),
    )
    ns.provision_quiet = mocker.patch(
        "aws_bench.cli.env.provision_scenarios",
        new=AsyncMock(return_value=fake_summary),
    )

    mocker.patch("aws_bench.cli.env.display_provisioning_summary")

    return ns


def test_env_init_registry_mode_happy_path(
    materialized_scenarios,
    stub_env_deps,
    mocker,
    tmp_path,
):
    _patch_task_client_download(mocker, materialized_scenarios)

    fake_registry_path = tmp_path / "registry.json"
    fake_registry_path.write_text("[]")  # contents irrelevant — from_path is mocked.

    env_init(
        ctx=MagicMock(meta={}),
        ou_name="awsbench-ou",
        scenario_path=None,
        dataset="test-dataset@1.0.0",
        registry_url=None,
        registry_path=fake_registry_path,
        include_scenarios=None,
        exclude_scenarios=None,
        n_concurrent=4,
        quiet=True,
        wait_for_quotas=False,
        quota_timeout=60,
        poll_interval=10,
    )

    # provision_scenarios was called (quiet=True path) with the resolved set.
    stub_env_deps.provision_quiet.assert_called_once()
    scenarios = stub_env_deps.provision_quiet.call_args.args[0]
    scenario_names = {s.name for s in scenarios}
    assert scenario_names == {"ec2-small", "rds-mysql"}

    # AccountManager.init_organization fired with the OU name.
    stub_env_deps.account_manager.init_organization.assert_called_once_with("awsbench-ou")


def test_env_init_registry_mode_fetch_failure_aborts(
    materialized_scenarios,
    stub_env_deps,
    mocker,
    tmp_path,
):
    """One scenario fetch fails; env init aborts (fail-fast).

    The batched scenario fetch raises ScenarioFetchError on a clone failure rather
    than continuing with the survivors. env init catches it in its provisioning
    try/except and exits non-zero (typer.Exit), never reaching provision_scenarios.
    """

    async def partial_fail_download(self, task_ids, **kwargs):
        for tid in task_ids:
            assert isinstance(tid, GitTaskId)
            if tid.path.name == "rds-mysql":
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["git"],
                    stderr=b"clone failed",
                )
        return BatchDownloadResult(
            results=[
                TaskDownloadResult(
                    path=materialized_scenarios["ec2-small"],
                    download_time_sec=0.0,
                    cached=False,
                    resolved_git_commit_id=tid.git_commit_id,
                )
                for tid in task_ids
            ],
            total_time_sec=0.0,
        )

    mocker.patch(
        "aws_bench.dataset.config.TaskClient.download_tasks",
        new=partial_fail_download,
    )

    fake_registry_path = tmp_path / "registry.json"
    fake_registry_path.write_text("[]")

    with pytest.raises(typer.Exit):
        env_init(
            ctx=MagicMock(meta={}),
            ou_name="awsbench-ou",
            scenario_path=None,
            dataset="test-dataset@1.0.0",
            registry_url=None,
            registry_path=fake_registry_path,
            include_scenarios=None,
            exclude_scenarios=None,
            n_concurrent=4,
            quiet=True,
            wait_for_quotas=False,
            quota_timeout=60,
            poll_interval=10,
        )

    # Fail-fast: provision_scenarios is never reached.
    stub_env_deps.provision_quiet.assert_not_called()
