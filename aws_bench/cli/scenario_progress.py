"""Rich progress UI for ``aws-bench env init`` and ``env setup``.

Wraps ``provision_scenarios`` with a live two-pane display: an overall
progress bar plus a per-account spinner that mirrors lifecycle events.
The optional approval-wait pass renders its own bar afterward.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from aws_bench.account_management.manager import AccountManager
from aws_bench.resource_management.manager import ResourceManager
from aws_bench.resource_management.quota_manager import QuotaManager
from aws_bench.resource_management.snapshot.models import (
    SnapshotContext,
    SnapshotResult,
    SnapshotStage,
)
from aws_bench.scenario.events import ScenarioEvent, ScenarioPhase
from aws_bench.scenario.exceptions import UnmetQuota
from aws_bench.scenario.job import ScenarioJob
from aws_bench.scenario.provisioning import (
    ProvisionEvent,
    ProvisionHookEvent,
    ProvisioningSummary,
    _build_account_mappings,
    _count_quotas_to_verify,
    provision_scenarios,
    run_quota_wait_pass,
)
from aws_bench.scenario.results import ScenarioHookEvent, ScenarioJobResult
from aws_bench.scenario.scenario import Scenario
from aws_bench.utils.credentials_provider import CredentialProvider


async def provision_scenarios_with_progress(
    scenarios: list[Scenario],
    ou_name: str,
    *,
    account_manager: AccountManager,
    n_concurrent: int,
    wait_for_quotas: bool,
    quota_timeout: int,
    poll_interval: int,
    cred_provider: CredentialProvider,
    quota_manager: QuotaManager,
) -> ProvisioningSummary:
    """Run ``provision_scenarios`` with a rich progress display."""
    n_total = sum(len(sc.manifest.scenario.account_tags) for sc in scenarios)

    overall = Progress(
        SpinnerColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
    )
    per_account = Progress(
        SpinnerColumn(),
        TimeElapsedColumn(),
        TextColumn("[progress.description]{task.description}"),
    )

    with Live(Group(overall, per_account), refresh_per_second=10):
        overall_id = overall.add_task("Provisioning accounts", total=n_total)
        on_event = _build_on_event(per_account, overall, overall_id)
        # Two passes — provision then approval-wait — so each gets its own bar.
        result = await provision_scenarios(
            scenarios,
            ou_name,
            n_concurrent=n_concurrent,
            wait_for_quotas=False,
            poll_interval=poll_interval,
            account_manager=account_manager,
            quota_manager=quota_manager,
            cred_provider=cred_provider,
            on_event=on_event,
        )

    if not wait_for_quotas:
        return result

    n_total = _count_quotas_to_verify(scenarios, _build_account_mappings(result.accounts))
    overall_quota = Progress(
        SpinnerColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
    )
    with Live(overall_quota, refresh_per_second=10):
        overall_id = overall_quota.add_task(
            f"Waiting on {n_total} quota(s)",
            total=n_total,
        )

        def _on_poll(met: int, _total: int, _unmet: list[UnmetQuota]) -> None:
            overall_quota.update(overall_id, completed=met)

        await run_quota_wait_pass(
            result,
            scenarios,
            cred_provider=cred_provider,
            quota_timeout=quota_timeout,
            poll_interval=poll_interval,
            n_concurrent=n_concurrent,
            on_poll=_on_poll,
        )
    return result


def _build_on_event(per_account: Progress, overall: Progress, overall_id: TaskID):
    """Return a single ``on_event`` callback that drives the rich widgets."""
    task_ids: dict[tuple[str, str], TaskID] = {}

    def _key(event: ProvisionHookEvent) -> tuple[str, str]:
        return (event.scenario_name, event.account_tag)

    def _label(event: ProvisionHookEvent, action: str) -> str:
        suffix = f" ({event.account_id})" if event.account_id else ""
        return f"{event.scenario_name}/{event.account_tag}: {action}{suffix}"

    async def on_event(event: ProvisionHookEvent) -> None:
        match event.event:
            case ProvisionEvent.START:
                task_ids[_key(event)] = per_account.add_task(_label(event, "starting"), total=None)
            case ProvisionEvent.ACCOUNT_START:
                tid = task_ids.get(_key(event))
                if tid is not None:
                    per_account.update(tid, description=_label(event, "provisioning account"))
            case ProvisionEvent.ROLE_START:
                tid = task_ids.get(_key(event))
                if tid is not None:
                    per_account.update(tid, description=_label(event, "waiting for role"))
            case ProvisionEvent.QUOTAS_START:
                tid = task_ids.get(_key(event))
                if tid is not None:
                    per_account.update(tid, description=_label(event, "submitting quotas"))
            case ProvisionEvent.SNAPSHOT_START:
                tid = task_ids.get(_key(event))
                if tid is not None:
                    per_account.update(
                        tid, description=_label(event, "capturing post init snapshot")
                    )
            case ProvisionEvent.CANCEL:
                tid = task_ids.get(_key(event))
                if tid is not None:
                    per_account.update(tid, description=_label(event, "cancelling"))
            case ProvisionEvent.END:
                tid = task_ids.pop(_key(event), None)
                if tid is not None:
                    per_account.remove_task(tid)
                # Count successes, not attempts, so a failed/cancelled account
                # leaves the bar below its total.
                if event.succeeded:
                    overall.advance(overall_id)

    return on_event


async def run_phase_with_progress(
    job: ScenarioJob,
    phase: ScenarioPhase,
) -> ScenarioJobResult:
    """Run ``job.run(phase)`` under a rich.Live overall + per-trial display."""
    n_total = len(job._scenarios)  # type: ignore[attr-defined]

    overall = Progress(
        SpinnerColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
    )
    per_trial = Progress(
        SpinnerColumn(),
        TimeElapsedColumn(),
        TextColumn("[progress.description]{task.description}"),
    )

    task_ids: dict[str, TaskID] = {}
    overall_id: TaskID | None = None

    def _label(event: ScenarioHookEvent, action: str) -> str:
        return f"{event.trial_name}: {action}"

    async def on_start(event: ScenarioHookEvent) -> None:
        task_ids[event.trial_name] = per_trial.add_task(
            _label(event, "starting"),
            total=None,
        )

    async def on_env_start(event: ScenarioHookEvent) -> None:
        tid = task_ids.get(event.trial_name)
        if tid is not None:
            per_trial.update(tid, description=_label(event, "building image"))

    async def on_phase_start(event: ScenarioHookEvent) -> None:
        tid = task_ids.get(event.trial_name)
        if tid is not None:
            per_trial.update(tid, description=_label(event, phase.gerund.lower()))

    async def on_cancel(event: ScenarioHookEvent) -> None:
        tid = task_ids.get(event.trial_name)
        if tid is not None:
            per_trial.update(tid, description=_label(event, "cancelling"))

    async def on_end(event: ScenarioHookEvent) -> None:
        tid = task_ids.pop(event.trial_name, None)
        if tid is not None:
            per_trial.remove_task(tid)
        if overall_id is not None:
            overall.advance(overall_id)

    job.add_hook(ScenarioEvent.START, on_start)
    job.add_hook(ScenarioEvent.ENVIRONMENT_START, on_env_start)
    job.add_hook(ScenarioEvent.PHASE_START, on_phase_start)
    job.add_hook(ScenarioEvent.CANCEL, on_cancel)
    job.add_hook(ScenarioEvent.END, on_end)

    with Live(Group(overall, per_trial), refresh_per_second=10):
        overall_id = overall.add_task(phase.gerund, total=n_total)
        return await job.run(phase)


async def snapshot_env_with_progress(
    scenarios: list[Scenario],
    ou_name: str,
    output_dir: Path,
    *,
    n_concurrent: int,
) -> dict[str, list[SnapshotResult]]:
    """Capture an on-demand OBSERVABILITY snapshot per scenario, with a live display.

    Resolves ``ou_name`` to exactly ``scenarios``' accounts (keyed by each scenario's
    declared ``account_tags``) via ``AccountManager.resolve_test_environment`` — a
    source scenario with no provisioned account fails loud there. Each scenario is
    then snapshotted across only its declared regions (never every enabled region:
    scenario accounts sit under a region-guardrail SCP that denies CloudFormation
    outside them), one scenario per ``ResourceManager.snapshot_scenarios`` call so the
    display advances per scenario. Scenarios run under a ``n_concurrent`` bound.

    Args:
        scenarios: The (already include/exclude-filtered) source scenarios.
        ou_name: Testing environment (OU) name.
        output_dir: Directory the per-account JSON files are written under.
        n_concurrent: Max scenarios captured concurrently.

    Returns:
        Dict mapping scenario name to list of SnapshotResult per account.

    Raises:
        AccountResolutionError: A source scenario has no provisioned account in the OU.
    """
    results: dict[str, list[SnapshotResult]] = {}
    if not scenarios:
        return results

    required_by_scenario = {
        scenario.name: set(scenario.manifest.scenario.account_tags) for scenario in scenarios
    }
    test_environment = AccountManager().resolve_test_environment(ou_name, required_by_scenario)

    overall = Progress(
        SpinnerColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
    )
    per_scenario = Progress(
        SpinnerColumn(),
        TimeElapsedColumn(),
        TextColumn("[progress.description]{task.description}"),
    )
    gate = asyncio.Semaphore(n_concurrent)
    overall_id: TaskID | None = None

    async def _capture(scenario: Scenario) -> dict[str, list[SnapshotResult]]:
        async with gate:
            tid = per_scenario.add_task(f"{scenario.name}: capturing", total=None)
            try:
                ctx = SnapshotContext(
                    scenario_id=scenario.name,
                    scenario_hash="",
                    regions=list(scenario.manifest.scenario.regions),
                    stage=SnapshotStage.OBSERVABILITY,
                    account_ids=[
                        account.account_id
                        for account in test_environment.accounts[scenario.name].values()
                    ],
                    output_dir=output_dir,
                )
                # One scenario per batch call: snapshot_scenarios fans out that
                # scenario's account(s) internally and folds failures into results,
                # so a bad account never cancels sibling scenarios.
                return await ResourceManager.snapshot_scenarios([ctx])
            finally:
                per_scenario.remove_task(tid)
                # Advance on completion (success or failure) — the bar tracks progress,
                # not success; display_snapshot_results reports failures afterward.
                if overall_id is not None:
                    overall.advance(overall_id)

    with Live(Group(overall, per_scenario), refresh_per_second=10):
        overall_id = overall.add_task("Capturing snapshots", total=len(scenarios))
        parts = await asyncio.gather(*(_capture(scenario) for scenario in scenarios))

    # Scenario names are unique, so per-scenario result dicts merge without collision.
    for part in parts:
        results.update(part)
    return results
