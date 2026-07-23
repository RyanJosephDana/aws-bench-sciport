"""Concurrency-bounded queue for scenario trials.

The queue owns trial construction, hook wiring, and retry. Callers
register per-event callbacks once on the queue (``on_trial_started``,
``on_environment_started``, etc.) and every trial the queue creates
inherits them.

Retry policy is provided via :class:`RetryConfig`. By default
``max_retries=0`` so behavior matches a single-attempt queue; opt in by
passing a configured ``RetryConfig``. Trial outcomes that record an
``exception_info`` (i.e. ``trial.run`` returned a result rather than
raising) are inspected against ``include_exceptions``/
``exclude_exceptions`` to decide whether to retry.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Coroutine, Sequence
from typing import Any  # for Coroutine[Any, Any, T]

from harbor.models.trial.result import ExceptionInfo

from aws_bench.exceptions import OperationCancelled
from aws_bench.logging.logger import get_logger
from aws_bench.scenario.events import ScenarioEvent, ScenarioPhase
from aws_bench.scenario.job_config import RetryConfig, ScenarioTrialConfig
from aws_bench.scenario.results import ScenarioTrialResult
from aws_bench.scenario.trial import HookCallback, ScenarioTrial
from aws_bench.scenario.trial_paths import ScenarioJobPaths
from aws_bench.utils.credentials_provider import CredentialProvider

logger = get_logger(__name__)


class ScenarioQueue:
    """Bounded-concurrency dispatcher for scenario trials.

    The queue creates one ``ScenarioTrial`` per submitted config, wires
    the queue-level hooks, and runs the requested phase under a
    semaphore. Trial construction is async (``ScenarioTrial.create``) so
    setup work that needs the loop can grow here without breaking
    callers.
    """

    def __init__(
        self,
        n_concurrent: int,
        cred_provider: CredentialProvider,
        phase: ScenarioPhase,
        *,
        hooks: dict[ScenarioEvent, list[HookCallback]] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize the queue.

        Args:
            n_concurrent: Maximum simultaneous in-flight trials.
            cred_provider: Source of management STS credentials.
            phase: Scenario phase to run on every trial (deploy / verify /
                cleanup).
            hooks: Optional initial hook map. Additional hooks can be added
                via :meth:`add_hook` or one of the ``on_*`` helpers.
            retry_config: Per-trial retry policy. Defaults to ``max_retries=0``
                (no retries); opt in by passing a configured ``RetryConfig``.
        """
        if n_concurrent < 1:
            raise ValueError(f"n_concurrent must be >= 1, got {n_concurrent}")
        self._semaphore = asyncio.Semaphore(n_concurrent)
        self._cred_provider = cred_provider
        self._phase = phase
        self._retry_config = retry_config or RetryConfig()
        self._hooks: dict[ScenarioEvent, list[HookCallback]] = (
            {e: list(hooks.get(e, [])) for e in ScenarioEvent}
            if hooks is not None
            else {e: [] for e in ScenarioEvent}
        )

    # ── hook registration ────────────────────────────────────────────────

    def add_hook(self, event: ScenarioEvent, callback: HookCallback) -> ScenarioQueue:
        """Register a callback for a trial lifecycle event."""
        self._hooks[event].append(callback)
        return self

    def on_trial_started(self, callback: HookCallback) -> ScenarioQueue:
        """Register a callback that runs when a queued trial starts."""
        return self.add_hook(ScenarioEvent.START, callback)

    def on_environment_started(self, callback: HookCallback) -> ScenarioQueue:
        """Register a callback that runs when the trial's container starts building."""
        return self.add_hook(ScenarioEvent.ENVIRONMENT_START, callback)

    def on_phase_started(self, callback: HookCallback) -> ScenarioQueue:
        """Register a callback that runs when the phase script starts executing."""
        return self.add_hook(ScenarioEvent.PHASE_START, callback)

    def on_trial_ended(self, callback: HookCallback) -> ScenarioQueue:
        """Register a callback that runs when a queued trial ends."""
        return self.add_hook(ScenarioEvent.END, callback)

    def on_trial_cancelled(self, callback: HookCallback) -> ScenarioQueue:
        """Register a callback that runs when a queued trial is cancelled."""
        return self.add_hook(ScenarioEvent.CANCEL, callback)

    # ── submission ──────────────────────────────────────────────────────

    def submit(
        self,
        config: ScenarioTrialConfig,
    ) -> Coroutine[Any, Any, ScenarioTrialResult]:
        """Return a coroutine that runs one trial under the semaphore."""
        return self._run(config)

    def submit_batch(
        self,
        configs: Sequence[ScenarioTrialConfig],
    ) -> list[Coroutine[Any, Any, ScenarioTrialResult]]:
        """Return one coroutine per config, ordered to match input."""
        return [self.submit(c) for c in configs]

    # ── execution ───────────────────────────────────────────────────────

    async def _run(
        self,
        config: ScenarioTrialConfig,
    ) -> ScenarioTrialResult:
        async with self._semaphore:
            return await self._execute_with_retries(config)

    async def _execute_with_retries(
        self,
        config: ScenarioTrialConfig,
    ) -> ScenarioTrialResult:
        """Run one trial with retry on retryable exceptions."""
        last_result: ScenarioTrialResult | None = None
        for attempt in range(self._retry_config.max_retries + 1):
            result = await self._execute_once(config)
            last_result = result

            if result.exception_info is None:
                return result

            if not self._should_retry(result.exception_info.exception_type):
                return result

            if attempt == self._retry_config.max_retries:
                return result

            # Remove the failed attempt's output dir so the next attempt
            # starts clean and the surviving on-disk record is the last
            # attempt's alone (no stale result.json / exception.txt).
            trial_dir = ScenarioJobPaths(config.output_dir).trial_paths(config.trial_name).trial_dir
            shutil.rmtree(trial_dir, ignore_errors=True)

            delay = self._backoff_delay(attempt)
            logger.info(
                "Trial %s/%s failed with %s; retrying in %.2fs (attempt %d/%d)",
                config.scenario.name,
                config.trial_name,
                result.exception_info.exception_type,
                delay,
                attempt + 2,
                self._retry_config.max_retries + 1,
            )
            await asyncio.sleep(delay)

        # Loop always exits via one of the returns above; defensive fallback.
        assert last_result is not None
        return last_result

    async def _execute_once(
        self,
        config: ScenarioTrialConfig,
    ) -> ScenarioTrialResult:
        trial: ScenarioTrial | None = None
        try:
            trial = await ScenarioTrial.create(
                config=config,
                cred_provider=self._cred_provider,
            )
            self._wire_hooks(trial)
            return await trial.run(self._phase)
        except (asyncio.CancelledError, OperationCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Trial setup failed for %s/%s: %s",
                config.scenario.name,
                config.trial_name,
                exc,
            )
            if trial is not None:
                trial.result.exception_info = ExceptionInfo.from_exception(exc)
                return trial.result
            return ScenarioTrialResult(
                scenario_name=config.scenario.name,
                trial_name=config.trial_name,
                phase=self._phase,
                exception_info=ExceptionInfo.from_exception(exc),
            )

    def _should_retry(self, exception_type: str) -> bool:
        """Apply the retry policy to one exception type name."""
        cfg = self._retry_config
        if cfg.exclude_exceptions and exception_type in cfg.exclude_exceptions:
            return False
        if cfg.include_exceptions is not None and exception_type not in cfg.include_exceptions:
            return False
        return True

    def _backoff_delay(self, attempt: int) -> float:
        cfg = self._retry_config
        delay = cfg.min_wait_sec * (cfg.wait_multiplier**attempt)
        return min(delay, cfg.max_wait_sec)

    def _wire_hooks(self, trial: ScenarioTrial) -> None:
        for event, callbacks in self._hooks.items():
            for callback in callbacks:
                trial.add_hook(event, callback)
