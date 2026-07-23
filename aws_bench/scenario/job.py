"""Scenario job: orchestrates N trials in parallel.

A factory ``create()`` resolves configuration (scenario discovery, account
mapping, quota gate) before any trial is constructed, then ``run(phase)``
submits trials through a bounded queue and aggregates their results.

The job is phase-agnostic: each invocation runs *one* phase across all
discovered scenarios. ``aws-bench env setup`` calls
``run(ScenarioPhase.DEPLOY)``, ``env verify`` calls
``run(ScenarioPhase.VERIFY)``, ``env cleanup`` calls
``run(ScenarioPhase.CLEANUP)``.

Callers register per-event callbacks via ``add_hook(event, cb)`` (or one of
the ``on_*`` helpers). Hooks added to the job are forwarded to every trial
the job's queue creates.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harbor.models.trial.result import ExceptionInfo
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from aws_bench.account_management.constants import ORG_ACCESS_ROLE
from aws_bench.account_management.manager import AccountManager
from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.models import (
    QuotaConfiguration,
    QuotaIncreaseRequest,
    QuotaStatus,
)
from aws_bench.resource_management.quota_manager import QuotaManager
from aws_bench.scenario.events import ScenarioEvent, ScenarioPhase
from aws_bench.scenario.exceptions import (
    InsufficientQuotaError,
    UnmetQuota,
)
from aws_bench.scenario.job_config import (
    ScenarioJobConfig,
    ScenarioTrialConfig,
)
from aws_bench.scenario.locator import ScenarioConfig
from aws_bench.scenario.queue import ScenarioQueue
from aws_bench.scenario.results import (
    ScenarioJobResult,
    ScenarioTrialResult,
)
from aws_bench.scenario.scenario import Scenario
from aws_bench.scenario.trial import HookCallback
from aws_bench.scenario.trial_paths import ScenarioJobPaths
from aws_bench.utils.concurrent import reraise_if_cancelled
from aws_bench.utils.credentials_provider import CredentialProvider

logger = get_logger(__name__)


def _task_outcomes(tasks: list[asyncio.Task]) -> list[object]:
    """Read each done task as the value ``gather(return_exceptions=True)`` would yield.

    A cancelled task's ``.result()`` / ``.exception()`` *raises*, so a cancellation
    is surfaced as a ``CancelledError`` value instead. Used to rebuild the trial
    aggregate from the tasks after a Ctrl+C, where ``gather`` re-raised and dropped
    its own results list. Every task is settled by the time ``gather`` returns or
    raises, so this never touches a pending task.
    """
    outcomes: list[object] = []
    for task in tasks:
        if task.cancelled():
            outcomes.append(asyncio.CancelledError())
        else:
            outcomes.append(task.exception() or task.result())
    return outcomes


# Service Quotas reads are eventually consistent: a freshly-approved quota can
# still read at its old (too-low) value for a short window. The env-setup gate
# retries after this delay to absorb that, so setup run right after
# `env init --wait-for-quotas` does not abort on a stale read. Increased from
# 60s to 120s as defense-in-depth against the enforcement propagation gap.
_QUOTA_STALE_READ_RETRY_DELAY_SEC = 120
_QUOTA_STALE_READ_MAX_ATTEMPTS = 3


class ScenarioJob:
    """Orchestrates parallel scenario trials for one phase."""

    def __init__(
        self,
        config: ScenarioJobConfig,
        cred_provider: CredentialProvider,
        *,
        scenarios: dict[ScenarioConfig, Scenario],
        account_manager: AccountManager | None = None,
    ) -> None:
        """Initialize the job.

        Use :meth:`create` instead of constructing directly; the factory
        resolves descriptors from the dataset, caches, and materializes.
        ``config.test_environment`` is expected to be populated already. The
        retry policy is read from ``config.retry`` and forwarded to the queue.

        ``scenarios`` maps each ``ScenarioConfig`` descriptor to its materialized
        ``Scenario`` — the objects ``create`` already built for the account/quota
        gate, kept so ``run`` reuses them and each trial config is built from the
        real descriptor.
        """
        self._config = config
        self._cred_provider = cred_provider
        self._scenarios = scenarios
        self._account_manager = account_manager or AccountManager()
        self._id: UUID = uuid4()
        self._paths = ScenarioJobPaths(job_dir=config.jobs_dir / config.job_name)
        self._hooks: dict[ScenarioEvent, list[HookCallback]] = {e: [] for e in ScenarioEvent}

    @classmethod
    async def create(
        cls,
        config: ScenarioJobConfig,
        cred_provider: CredentialProvider,
        *,
        account_manager: AccountManager | None = None,
    ) -> ScenarioJob:
        """Resolve descriptors from the dataset, then accounts; return a ready Job.

        The dataset stays on ``config``; the factory resolves it into
        ``ScenarioConfig`` descriptors and materializes each ``Scenario``
        here, because account-mapping and the quota gate both read the
        manifest before any trial runs. The materialized objects are kept
        paired with their descriptors so ``run`` reuses them.

        Account mappings are resolved fresh from the OU and populated on a
        deep copy of ``config`` so the operator's instance is not mutated.
        """
        am = account_manager or AccountManager()
        # Keyed by descriptor; run() reuses these to build each trial config.
        scenarios = await config.dataset.get_scenarios()

        required_by_scenario = {
            s.name: set(s.manifest.scenario.account_tags) for s in scenarios.values()
        }
        test_environment = am.resolve_test_environment(config.ou_name, required_by_scenario)

        await cls._check_quota_sufficiency_with_retry(
            list(scenarios.values()),
            test_environment.to_scenario_account_mappings(),
            cred_provider,
            n_concurrent=config.n_concurrent,
        )

        resolved_config = config.model_copy(deep=True)
        resolved_config.test_environment = test_environment

        return cls(
            resolved_config,
            cred_provider,
            scenarios=scenarios,
            account_manager=am,
        )

    @staticmethod
    async def _check_quota_sufficiency(
        scenarios: list[Scenario],
        account_mappings: dict[str, dict[str, str]],
        cred_provider: CredentialProvider,
        *,
        n_concurrent: int,
    ) -> None:
        """Refuse to start the job if any required quota's current value is too low.

        ``account_mappings`` is ``{scenario_name: {account_tag: account_id}}``,
        the minimal lookup the check needs; both the run-time ``TestEnvironment``
        and the provision-time account list produce it.

        Aggregated across scenarios x account-tags x regions; raises
        ``InsufficientQuotaError`` listing every unmet quota. Wraps
        ``QuotaManager.verify_quotas`` (which already uses
        ``get_session_for_account`` for refreshable creds).
        """
        work_units: list[tuple[str, str, QuotaConfiguration]] = []
        for scenario in scenarios:
            if not scenario.manifest.quotas:
                continue
            grouped: dict[tuple[str, str], list[QuotaIncreaseRequest]] = defaultdict(list)
            for q in scenario.manifest.quotas:
                grouped[(q.account_tag, q.region)].append(
                    QuotaIncreaseRequest(
                        service_code=q.service_code,
                        quota_code=q.quota_code,
                        desired_value=q.desired_value,
                    )
                )
            mapping = account_mappings.get(scenario.name, {})
            for (account_tag, region), increases in grouped.items():
                account_id = mapping.get(account_tag)
                if account_id is None:
                    continue
                work_units.append(
                    (
                        scenario.name,
                        account_id,
                        QuotaConfiguration(increases=increases, region=region),
                    )
                )

        if not work_units:
            return

        qm = QuotaManager(cred_provider)
        sem = asyncio.Semaphore(n_concurrent)

        async def _verify_one(
            scenario_name: str,
            account_id: str,
            config: QuotaConfiguration,
        ) -> list[UnmetQuota]:
            async with sem:
                results = await asyncio.to_thread(
                    qm.verify_quotas, config, account_id, ORG_ACCESS_ROLE
                )
            return [
                UnmetQuota(
                    scenario_name=scenario_name,
                    account_id=account_id,
                    region=config.region,
                    result=r,
                )
                for r in results
                if r.status != QuotaStatus.ALREADY_MET
            ]

        nested = await asyncio.gather(*(_verify_one(s, a, c) for s, a, c in work_units))
        failures = [sf for group in nested for sf in group]
        if failures:
            raise InsufficientQuotaError(failures)

    @classmethod
    @retry(
        retry=retry_if_exception_type(InsufficientQuotaError),
        stop=stop_after_attempt(_QUOTA_STALE_READ_MAX_ATTEMPTS),
        wait=wait_fixed(_QUOTA_STALE_READ_RETRY_DELAY_SEC),
        reraise=True,
    )
    async def _check_quota_sufficiency_with_retry(
        cls,
        scenarios: list[Scenario],
        account_mappings: dict[str, dict[str, str]],
        cred_provider: CredentialProvider,
        *,
        n_concurrent: int,
    ) -> None:
        """Run the quota gate, retrying on stale eventually-consistent reads.

        For the bare ``env setup`` path (a single gate check, no surrounding poll
        loop). On ``InsufficientQuotaError`` it waits
        ``_QUOTA_STALE_READ_RETRY_DELAY_SEC`` and rechecks up to
        ``_QUOTA_STALE_READ_MAX_ATTEMPTS`` times; a still-unmet quota
        re-raises. The polling caller in ``provisioning.py`` uses the plain
        :meth:`_check_quota_sufficiency` directly — its own loop is the retry.
        """
        await cls._check_quota_sufficiency(
            scenarios, account_mappings, cred_provider, n_concurrent=n_concurrent
        )

    @property
    def id(self) -> UUID:
        """Stable per-job identifier."""
        return self._id

    @property
    def config(self) -> ScenarioJobConfig:
        """Job configuration with resolved account mappings populated."""
        return self._config

    @property
    def paths(self) -> ScenarioJobPaths:
        """Job output paths."""
        return self._paths

    @property
    def n_trials(self) -> int:
        """Number of trials this job runs (one per resolved scenario)."""
        return len(self._scenarios)

    # ── hook registration ────────────────────────────────────────────────

    def add_hook(self, event: ScenarioEvent, callback: HookCallback) -> ScenarioJob:
        """Register a callback for one trial lifecycle event."""
        self._hooks[event].append(callback)
        return self

    def on_trial_started(self, callback: HookCallback) -> ScenarioJob:
        """Register a callback that runs when a queued trial starts."""
        return self.add_hook(ScenarioEvent.START, callback)

    def on_environment_started(self, callback: HookCallback) -> ScenarioJob:
        """Register a callback that runs when a trial container starts building."""
        return self.add_hook(ScenarioEvent.ENVIRONMENT_START, callback)

    def on_phase_started(self, callback: HookCallback) -> ScenarioJob:
        """Register a callback that runs when the phase script starts executing."""
        return self.add_hook(ScenarioEvent.PHASE_START, callback)

    def on_trial_ended(self, callback: HookCallback) -> ScenarioJob:
        """Register a callback that runs when a queued trial ends."""
        return self.add_hook(ScenarioEvent.END, callback)

    def on_trial_cancelled(self, callback: HookCallback) -> ScenarioJob:
        """Register a callback that runs when a queued trial is cancelled."""
        return self.add_hook(ScenarioEvent.CANCEL, callback)

    # ── execution ────────────────────────────────────────────────────────

    async def run(self, phase: ScenarioPhase) -> ScenarioJobResult:
        """Run ``phase`` across all discovered scenarios concurrently.

        The caller (``_run_job_phase``) owns the ``job.log`` file handler so it
        spans ``create`` + ``run``; this method assumes it is already open.
        """
        self._paths.mkdir()
        self._persist_config()

        # Each trial config is built from the real descriptor (its key in the
        # scenarios map), preserving its source / overwrite / git provenance.
        trial_configs: list[ScenarioTrialConfig] = [
            self._build_trial_config(descriptor, scenario)
            for descriptor, scenario in self._scenarios.items()
        ]
        logger.info(
            "%s %d scenario(s) for job %s",
            phase.gerund,
            len(trial_configs),
            self._config.job_name,
        )
        result = ScenarioJobResult(
            job_name=self._config.job_name,
            job_dir=self._paths.job_dir.resolve(),
            started_at=datetime.now(timezone.utc),
            n_total=len(trial_configs),
        )

        queue = self._build_queue(phase)
        # Hold task handles, not just coroutines: a Ctrl+C cancels this job task,
        # making ``gather`` re-raise and discard its results — but the tasks are all
        # ``done``, so the aggregate is rebuilt from them in ``finally``.
        # ``return_exceptions=True`` stops one trial's cancel from erasing siblings
        # on the non-Ctrl+C paths.
        tasks = [asyncio.ensure_future(coro) for coro in queue.submit_batch(trial_configs)]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            # A worker-tripped shutdown surfaces as a trial's OperationCancelled
            # captured here as a value (the job task itself was not cancelled);
            # propagate it so the run unwinds. A Ctrl+C instead cancels this task,
            # so the await above has already raised CancelledError.
            reraise_if_cancelled(_task_outcomes(tasks))
        finally:
            # Coerce from the tasks (all ``done`` here), not gather's lost-on-cancel value.
            result.trial_results = self._coerce_outcomes_to_results(
                _task_outcomes(tasks), trial_configs, phase
            )
            result.n_succeeded = sum(1 for r in result.trial_results if r.success)
            result.n_failed = result.n_total - result.n_succeeded
            result.finished_at = datetime.now(timezone.utc)
            self._persist_result(result)
        return result

    def _build_queue(self, phase: ScenarioPhase) -> ScenarioQueue:
        queue = ScenarioQueue(
            n_concurrent=self._config.n_concurrent,
            cred_provider=self._cred_provider,
            phase=phase,
            retry_config=self._config.retry,
        )
        for event, callbacks in self._hooks.items():
            for cb in callbacks:
                queue.add_hook(event, cb)
        return queue

    def _build_trial_config(
        self, descriptor: ScenarioConfig, scenario: Scenario
    ) -> ScenarioTrialConfig:
        # The account mapping is looked up by the canonical manifest name
        # (scenario.name), which may differ from the descriptor name.
        assert self._config.test_environment is not None
        return ScenarioTrialConfig(
            scenario=descriptor,
            output_dir=self._paths.job_dir,
            environment=self._config.environment.model_copy(deep=True),
            account_mapping=self._config.test_environment.mapping_for(scenario.name),
            timeout_multiplier=self._config.timeout_multiplier,
            ou_name=self._config.ou_name,
        )

    @staticmethod
    def _coerce_outcomes_to_results(
        outcomes: list,
        configs: Sequence[ScenarioTrialConfig],
        phase: ScenarioPhase,
    ) -> list[ScenarioTrialResult]:
        """Convert per-task outcomes (see ``_task_outcomes``) into trial results.

        A genuine result passes through. An exception value (a trial's
        ``CancelledError``, or the ``OperationCancelled`` from a shutdown)
        becomes a synthetic failed result so the job summary still reflects
        every trial's existence. This runs in ``run``'s ``finally``, so the
        aggregate is recorded even when the run unwinds on a shutdown.
        """
        results: list[ScenarioTrialResult] = []
        for outcome, cfg in zip(outcomes, configs, strict=True):
            if isinstance(outcome, ScenarioTrialResult):
                results.append(outcome)
            elif isinstance(outcome, BaseException):
                results.append(
                    ScenarioTrialResult(
                        scenario_name=cfg.scenario.name,
                        trial_name=cfg.trial_name,
                        phase=phase,
                        exception_info=ExceptionInfo.from_exception(outcome),
                    )
                )
        return results

    # ── persistence ──────────────────────────────────────────────────────

    def _persist_config(self) -> None:
        self._paths.config_path.write_text(self._config.model_dump_json(indent=2))

    def _persist_result(self, result: ScenarioJobResult) -> None:
        # Best-effort: runs in run()'s finally while a cancel may be propagating, so
        # a write/serialization error is logged and swallowed rather than replacing
        # the cancel (a BaseException, which isn't caught here). The in-memory result
        # is returned to the caller regardless; only the on-disk artifact is lost.
        try:
            self._paths.result_path.write_text(result.model_dump_json(indent=2))
        except Exception as exc:  # noqa: BLE001 — best-effort artifact; must not mask a cancel
            logger.warning(
                "Could not persist job result to %s for %s (in-memory result is unaffected): %s",
                self._paths.result_path,
                self._config.job_name,
                exc,
            )
