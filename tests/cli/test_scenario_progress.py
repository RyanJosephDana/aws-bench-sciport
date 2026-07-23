"""Tests for run_phase_with_progress."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aws_bench.account_management.exceptions import AccountResolutionError
from aws_bench.account_management.models import ScenarioAccount
from aws_bench.cli.scenario_progress import (
    _build_on_event,
    provision_scenarios_with_progress,
    run_phase_with_progress,
    snapshot_env_with_progress,
)
from aws_bench.resource_management.models import (
    QuotaIncreaseResult,
    QuotaStatus,
)
from aws_bench.resource_management.snapshot.models import SnapshotResult, SnapshotStage
from aws_bench.scenario.events import ScenarioEvent, ScenarioPhase
from aws_bench.scenario.exceptions import InsufficientQuotaError, UnmetQuota
from aws_bench.scenario.provisioning import (
    ProvisionedAccount,
    ProvisionEvent,
    ProvisionHookEvent,
    ProvisioningSummary,
)
from aws_bench.scenario.results import ScenarioJobResult
from aws_bench.scenario.scenario import Scenario


def _empty_result() -> ScenarioJobResult:
    return ScenarioJobResult(
        job_name="test-job",
        job_dir=Path("/tmp/test-job"),
        started_at=datetime.now(timezone.utc),
        n_total=0,
    )


def _fake_job(result: ScenarioJobResult) -> MagicMock:
    job = MagicMock()
    job.config.job_name = result.job_name
    # Match ScenarioJob's _scenarios list (private attribute the wrapper reads
    # for n_total).
    job._scenarios = []
    job.run = AsyncMock(return_value=result)
    job.add_hook = MagicMock(return_value=job)
    return job


@pytest.mark.asyncio
async def test_run_phase_with_progress_calls_job_run_with_phase():
    expected_result = _empty_result()
    job = _fake_job(expected_result)

    result = await run_phase_with_progress(job, ScenarioPhase.DEPLOY)

    job.run.assert_awaited_once_with(ScenarioPhase.DEPLOY)
    assert result is expected_result


@pytest.mark.asyncio
async def test_run_phase_with_progress_registers_all_lifecycle_hooks():
    """Register one on_* per ScenarioEvent the wrapper cares about.

    Events: START, ENVIRONMENT_START, PHASE_START, CANCEL, END.
    """
    job = _fake_job(_empty_result())

    await run_phase_with_progress(job, ScenarioPhase.DEPLOY)

    registered_events = {call.args[0] for call in job.add_hook.call_args_list}
    expected = {
        ScenarioEvent.START,
        ScenarioEvent.ENVIRONMENT_START,
        ScenarioEvent.PHASE_START,
        ScenarioEvent.CANCEL,
        ScenarioEvent.END,
    }
    assert registered_events == expected


def _end_event(succeeded: bool) -> ProvisionHookEvent:
    return ProvisionHookEvent(
        event=ProvisionEvent.END,
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        succeeded=succeeded,
    )


@pytest.mark.asyncio
async def test_on_event_advances_overall_only_on_successful_end():
    """The overall bar counts successful provisions; a failed END does not advance."""
    overall = MagicMock()
    per_account = MagicMock()
    overall_id = MagicMock()
    on_event = _build_on_event(per_account, overall, overall_id)

    await on_event(_end_event(succeeded=True))
    await on_event(_end_event(succeeded=False))

    overall.advance.assert_called_once_with(overall_id)


# -- provision_scenarios_with_progress wait pass -----------------------------
# The wrapper runs its own approval-wait pass (separate from provision_scenarios'
# quiet path); it must honor the same waited/unmet_quotas contract so the exit
# code and summary tables match between `env init` and `env init -q`.


def _pending_unmet() -> UnmetQuota:
    return UnmetQuota(
        scenario_name="sc",
        account_id="111111111111",
        region="us-east-1",
        result=QuotaIncreaseResult(
            service_code="ec2",
            quota_code="L-1",
            desired_value=10.0,
            status=QuotaStatus.ALREADY_PENDING,
        ),
    )


def _summary_with_pending() -> ProvisioningSummary:
    """What provision_scenarios returns to the wrapper: seeded pending, not yet waited."""
    summary = ProvisioningSummary(
        accounts=[ProvisionedAccount("sc", "PRIMARY", account_id="111111111111")],
        unmet_quotas=[_pending_unmet()],
        waited=False,
    )
    return summary


@pytest.fixture()
def _wrapper_deps():
    """Keyword deps for provision_scenarios_with_progress (all mocked)."""
    return {
        "account_manager": MagicMock(),
        "quota_manager": MagicMock(),
        "cred_provider": MagicMock(),
        "n_concurrent": 1,
        "quota_timeout": 0,
        "poll_interval": 0,
    }


@pytest.mark.asyncio
async def test_wrapper_wait_timeout_marks_waited_so_exit_is_nonzero(_wrapper_deps):
    """A timed-out wait must set waited=True so all_succeeded is False (exit 1).

    Regression: the wrapper set unmet_quotas on timeout but not waited, so
    all_succeeded returned True and `env init --wait-for-quotas` exited 0 with a
    still-pending quota — the dishonest exit this feature exists to prevent.
    """
    sc = MagicMock()
    sc.manifest.scenario.account_tags = ["PRIMARY"]
    summary = _summary_with_pending()

    async def _timeout(*_a, **_kw):
        raise InsufficientQuotaError([_pending_unmet()])

    with (
        patch(
            "aws_bench.cli.scenario_progress.provision_scenarios",
            new=AsyncMock(return_value=summary),
        ),
        patch("aws_bench.scenario.provisioning._await_quota_approvals", new=_timeout),
    ):
        result = await provision_scenarios_with_progress(
            [sc], "ou", wait_for_quotas=True, **_wrapper_deps
        )

    assert result.waited is True
    assert result.unmet_quotas  # the pending quota is still recorded
    assert result.all_succeeded is False


@pytest.mark.asyncio
async def test_wrapper_wait_success_clears_unmet_and_marks_waited(_wrapper_deps):
    """A successful wait clears the seeded pending quotas and marks waited.

    Regression: the wrapper never cleared unmet_quotas on success, so a
    fully-granted run showed a false 'not yet granted' table and understated
    'met' counts.
    """
    sc = MagicMock()
    sc.manifest.scenario.account_tags = ["PRIMARY"]
    summary = _summary_with_pending()

    with (
        patch(
            "aws_bench.cli.scenario_progress.provision_scenarios",
            new=AsyncMock(return_value=summary),
        ),
        patch(
            "aws_bench.scenario.provisioning._await_quota_approvals",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await provision_scenarios_with_progress(
            [sc], "ou", wait_for_quotas=True, **_wrapper_deps
        )

    assert result.waited is True
    assert result.unmet_quotas == []
    assert result.all_succeeded is True


# -- snapshot_env_with_progress ----------------------------------------------
# Resolves the OU to exactly the source scenarios' accounts, then snapshots each
# scenario across only its declared regions (one snapshot_scenarios call per
# scenario so the display advances per scenario).


def _snapshot_scenario(
    name: str, regions: list[str], tags: tuple[str, ...] = ("PRIMARY",)
) -> Scenario:
    sc = MagicMock()
    sc.name = name
    sc.manifest.scenario.regions = list(regions)
    sc.manifest.scenario.account_tags = list(tags)
    return cast(Scenario, sc)


def _scenario_account(account_id: str, scenario_name: str, tag: str = "PRIMARY") -> ScenarioAccount:
    return ScenarioAccount(
        account_id=account_id,
        email=f"{account_id}@example.com",
        scenario_name=scenario_name,
        account_tag=tag,
    )


@pytest.mark.asyncio
async def test_snapshot_env_with_progress_scopes_each_scenario_to_declared_regions():
    """One snapshot_scenarios call per scenario, each context scoped to declared regions."""
    scenarios = [
        _snapshot_scenario("scenario-a", ["us-east-1", "us-west-2"]),
        _snapshot_scenario("scenario-b", ["eu-west-1"]),
    ]
    fake_env = SimpleNamespace(
        accounts={
            "scenario-a": {"PRIMARY": _scenario_account("111111111111", "scenario-a")},
            "scenario-b": {"PRIMARY": _scenario_account("222222222222", "scenario-b")},
        }
    )

    def _snap_side(contexts):
        ctx = contexts[0]
        return {
            ctx.scenario_id: [
                SnapshotResult(
                    account_id=ctx.account_ids[0],
                    success=True,
                    regions_captured=ctx.regions,
                )
            ]
        }

    with (
        patch("aws_bench.cli.scenario_progress.AccountManager") as mock_am,
        patch(
            "aws_bench.cli.scenario_progress.ResourceManager.snapshot_scenarios",
            new_callable=AsyncMock,
            side_effect=_snap_side,
        ) as mock_snap,
    ):
        mock_am.return_value.resolve_test_environment.return_value = fake_env
        results = await snapshot_env_with_progress(
            scenarios, "awsbench-ou", Path("/tmp/aws-bench-snapshots"), n_concurrent=2
        )

    # resolve_test_environment was keyed by each scenario's declared account_tags.
    _, required = mock_am.return_value.resolve_test_environment.call_args.args
    assert required == {"scenario-a": {"PRIMARY"}, "scenario-b": {"PRIMARY"}}

    # One snapshot_scenarios call per scenario, each with a single context.
    by_scenario = {
        call.args[0][0].scenario_id: call.args[0][0] for call in mock_snap.call_args_list
    }
    assert set(by_scenario) == {"scenario-a", "scenario-b"}
    for ctx in by_scenario.values():
        assert ctx.stage == SnapshotStage.OBSERVABILITY
        assert ctx.scenario_hash == ""
        assert ctx.output_dir == Path("/tmp/aws-bench-snapshots")
    assert by_scenario["scenario-a"].regions == ["us-east-1", "us-west-2"]
    assert by_scenario["scenario-a"].account_ids == ["111111111111"]
    assert by_scenario["scenario-b"].regions == ["eu-west-1"]
    assert by_scenario["scenario-b"].account_ids == ["222222222222"]

    # Per-scenario result dicts are merged.
    assert set(results) == {"scenario-a", "scenario-b"}
    assert results["scenario-a"][0].regions_captured == ["us-east-1", "us-west-2"]


@pytest.mark.asyncio
async def test_snapshot_env_with_progress_empty_scenarios_is_noop():
    """No scenarios → returns empty and never touches account resolution or scanning."""
    with (
        patch("aws_bench.cli.scenario_progress.AccountManager") as mock_am,
        patch(
            "aws_bench.cli.scenario_progress.ResourceManager.snapshot_scenarios",
            new_callable=AsyncMock,
        ) as mock_snap,
    ):
        results = await snapshot_env_with_progress(
            [], "awsbench-ou", Path("/tmp/aws-bench-snapshots"), n_concurrent=1
        )

    assert results == {}
    mock_am.return_value.resolve_test_environment.assert_not_called()
    mock_snap.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_env_with_progress_propagates_account_resolution_error():
    """A source scenario with no provisioned account fails loud (resolver raises)."""
    scenarios = [_snapshot_scenario("scenario-a", ["us-east-1"])]

    with (
        patch("aws_bench.cli.scenario_progress.AccountManager") as mock_am,
        patch(
            "aws_bench.cli.scenario_progress.ResourceManager.snapshot_scenarios",
            new_callable=AsyncMock,
        ) as mock_snap,
    ):
        mock_am.return_value.resolve_test_environment.side_effect = AccountResolutionError(
            "no account for scenario-a"
        )
        with pytest.raises(AccountResolutionError):
            await snapshot_env_with_progress(
                scenarios, "awsbench-ou", Path("/tmp/aws-bench-snapshots"), n_concurrent=1
            )

    mock_snap.assert_not_awaited()
