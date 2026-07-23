"""AwsBenchTrialQueue — per-scenario admission gate + the retry loop that builds AwsBenchTrial.

The queue admits a trial through its scenario's readers-writer gate *before* the
trial takes a global concurrency slot:

- trials of different scenarios run in parallel (each scenario is one AWS account);
- ``read-only`` trials of one scenario co-run on that account;
- ``mutating`` (or undeclared) trials of one scenario run exclusively, so a trial
  that resets the account never overlaps another trial on it.

The global ``-n`` semaphore then caps how many of the eligible trials run at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import AsyncIterator

from harbor.models.job.config import RetryConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult
from harbor.trial.hooks import HookCallback, TrialEvent
from harbor.trial.queue import TrialQueue

from aws_bench.dataset.task_config import ConcurrencyMode
from aws_bench.task.aws_trial import AwsBenchTrial
from aws_bench.task.trial_config import AwsBenchTrialConfig


class _ScenarioAdmissionGate:
    """Readers-writer admission for one scenario's account.

    A ``READ_ONLY`` trial is admitted when no writer holds; a ``MUTATING`` trial is
    admitted only when no writer holds and no readers are in flight. Releasing wakes
    everyone waiting so the next eligible set proceeds.

    Hold the scenario only via ``async with gate.hold(mode)``: the hold is recorded
    on entry and dropped on exit (including on exception or cancellation), so the
    release always matches the acquire and a hold can never leak.

    Reader-preferring: a continuous stream of ``READ_ONLY`` trials can keep a waiting
    ``MUTATING`` trial parked. A run is finite, so the readers drain and the writer
    then proceeds — liveness holds without fairness queuing.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer_held = False

    @contextlib.asynccontextmanager
    async def hold(self, mode: ConcurrencyMode) -> AsyncIterator[None]:
        """Block until this trial may hold the scenario, hold for the body, then release."""
        await self._acquire(mode)
        try:
            yield
        finally:
            await self._release(mode)

    async def _acquire(self, mode: ConcurrencyMode) -> None:
        async with self._cond:
            if mode is ConcurrencyMode.READ_ONLY:
                await self._cond.wait_for(lambda: not self._writer_held)
                self._readers += 1
            else:
                await self._cond.wait_for(lambda: not self._writer_held and self._readers == 0)
                self._writer_held = True

    async def _release(self, mode: ConcurrencyMode) -> None:
        async with self._cond:
            if mode is ConcurrencyMode.READ_ONLY:
                self._readers -= 1
            else:
                self._writer_held = False
            self._cond.notify_all()


class AwsBenchTrialQueue(TrialQueue):
    """TrialQueue that gates each trial on its scenario, then builds an AwsBenchTrial.

    Each trial config carries its own ``scenario_id`` + ``concurrency_mode``, so
    the queue admits a trial through its scenario's gate (created on first use)
    before the global semaphore.
    """

    def __init__(
        self,
        n_concurrent: int,
        retry_config: RetryConfig | None = None,
        hooks: dict[TrialEvent, list[HookCallback]] | None = None,
    ) -> None:
        """Build the queue. Scenario gates are created lazily, one per scenario_id."""
        if n_concurrent < 1:
            raise ValueError(f"n_concurrent must be >= 1, got {n_concurrent}")
        super().__init__(n_concurrent, retry_config=retry_config, hooks=hooks)
        self._gates: dict[str, _ScenarioAdmissionGate] = {}

    async def _run_trial(self, trial_config: TrialConfig) -> TrialResult:
        """Admit the trial through its scenario gate, then run it under the semaphore.

        The gate is held across the whole trial (pre-invoke through post-invoke), so
        a scenario's reset completes before the next exclusive trial on it starts.
        """
        if not isinstance(trial_config, AwsBenchTrialConfig):
            raise TypeError(
                f"AwsBenchTrialQueue requires AwsBenchTrialConfig; got "
                f"{type(trial_config).__name__} for trial {trial_config.trial_name}."
            )
        # Single-statement setdefault: concurrent first-touches on one scenario
        # all get the same gate. Do not split into check-then-set — that race
        # would hand two trials separate gates for one account, breaking the
        # exclusive-writer guarantee.
        gate = self._gates.setdefault(trial_config.scenario_id, _ScenarioAdmissionGate())
        async with gate.hold(trial_config.concurrency_mode), self._semaphore:
            return await self._execute_trial_with_retries(trial_config)

    async def _execute_trial_with_retries(self, trial_config: TrialConfig) -> TrialResult:
        """Run the retry loop, rebuilding ``AwsBenchTrial`` for each attempt."""
        for attempt in range(self._retry_config.max_retries + 1):
            trial = await AwsBenchTrial.create(trial_config)
            self._setup_hooks(trial)
            result = await trial.run()

            if result.exception_info is None:
                return result

            if not self._should_retry_exception(result.exception_info.exception_type):
                self._logger.debug(
                    "Not retrying trial because the exception is not in "
                    "include_exceptions or the maximum number of retries has been reached"
                )
                return result
            if attempt == self._retry_config.max_retries:
                self._logger.debug(
                    "Not retrying trial because the maximum number of retries has been reached"
                )
                return result

            shutil.rmtree(trial.paths.trial_dir, ignore_errors=True)
            delay_sec = self._calculate_backoff_delay_sec(attempt)
            self._logger.debug(
                f"Trial {trial_config.trial_name} failed with exception "
                f"{result.exception_info.exception_type}. Retrying in {delay_sec:.2f} seconds..."
            )
            await asyncio.sleep(delay_sec)

        raise RuntimeError(
            f"Trial {trial_config.trial_name} produced no result. This should never happen."
        )
