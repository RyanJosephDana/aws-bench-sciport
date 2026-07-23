"""Tests for aws_bench.scenario.queue.ScenarioQueue."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from harbor.models.trial.result import ExceptionInfo

from aws_bench.scenario.events import ScenarioEvent, ScenarioPhase
from aws_bench.scenario.job_config import RetryConfig, ScenarioTrialConfig
from aws_bench.scenario.locator import ScenarioConfig
from aws_bench.scenario.queue import ScenarioQueue
from aws_bench.scenario.results import ScenarioTrialResult


def _make_cfg(
    name: str = "sc",
    trial_name: str = "t-0",
    output_dir: Path | None = None,
) -> ScenarioTrialConfig:
    return ScenarioTrialConfig(
        scenario=ScenarioConfig(name=name, path=Path("/tmp")),
        trial_name=trial_name,
        output_dir=output_dir if output_dir is not None else Path("/tmp"),
        account_mapping={"PRIMARY": "111"},
        ou_name="test-ou",
    )


def _stub_trial(*, exit_code: int = 0, run_side_effect=None) -> MagicMock:
    """Build a MagicMock trial whose run() returns a result or raises."""
    trial = MagicMock()
    result = ScenarioTrialResult(scenario_name="sc", trial_name="t", exit_code=exit_code)
    trial.result = result
    if run_side_effect is not None:
        trial.run = AsyncMock(side_effect=run_side_effect)
    else:
        trial.run = AsyncMock(return_value=result)
    trial.add_hook = MagicMock()
    return trial


def test_n_concurrent_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        ScenarioQueue(
            n_concurrent=0,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )


def test_submit_creates_trial_and_returns_result(tmp_path):
    trial = _stub_trial(exit_code=0)
    with patch("aws_bench.scenario.queue.ScenarioTrial.create", AsyncMock(return_value=trial)):
        q = ScenarioQueue(
            n_concurrent=2,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )
        result = asyncio.run(q.submit(_make_cfg(trial_name="t-1", output_dir=tmp_path)))
    assert result.exit_code == 0
    trial.run.assert_awaited_once_with("deploy")


def test_submit_batch_preserves_order(tmp_path):
    """Ordering of returned results matches input config ordering."""
    expected_order = []

    async def fake_create(config, cred_provider):
        trial = _stub_trial()
        result = ScenarioTrialResult(
            scenario_name=config.scenario.name, trial_name=config.trial_name
        )
        # Sleep proportional to reverse-index so the FIFO semaphore order
        # is the only thing keeping the result list in input order.
        idx = int(config.trial_name.split("-")[1])
        await asyncio.sleep(0.01 * (5 - idx))
        trial.run = AsyncMock(return_value=result)
        return trial

    cfgs = [_make_cfg(trial_name=f"t-{i}", output_dir=tmp_path) for i in range(5)]
    expected_order = [c.trial_name for c in cfgs]

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=5,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )

        async def run_all():
            return await asyncio.gather(*q.submit_batch(cfgs))

        results = asyncio.run(run_all())

    assert [r.trial_name for r in results] == expected_order


def test_semaphore_caps_in_flight(tmp_path):
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_create(config, cred_provider):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return _stub_trial()

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=2,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )
        cfgs = [_make_cfg(trial_name=f"t-{i}", output_dir=tmp_path) for i in range(8)]

        async def run_all():
            return await asyncio.gather(*q.submit_batch(cfgs))

        asyncio.run(run_all())

    assert peak <= 2


def test_trial_setup_failure_returns_failed_result(tmp_path):
    """If ScenarioTrial.create raises, the queue returns a failed result rather than re-raising."""
    with patch(
        "aws_bench.scenario.queue.ScenarioTrial.create",
        AsyncMock(side_effect=RuntimeError("setup failed")),
    ):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )
        result = asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))

    assert result.exception_info is not None
    assert result.exception_info.exception_type == "RuntimeError"
    assert "setup failed" in result.exception_info.exception_message
    assert not result.success


def test_cancellation_propagates(tmp_path):
    """CancelledError must propagate so the caller's gather can react."""
    trial = _stub_trial(run_side_effect=asyncio.CancelledError())
    with patch("aws_bench.scenario.queue.ScenarioTrial.create", AsyncMock(return_value=trial)):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))


def test_retry_until_success(tmp_path):
    """A failing-then-succeeding trial returns the success result after retry."""
    attempts = 0

    async def fake_create(config, cred_provider):
        nonlocal attempts
        attempts += 1
        trial = MagicMock()
        if attempts == 1:
            failed = ScenarioTrialResult(
                scenario_name=config.scenario.name,
                trial_name=config.trial_name,
                exception_info=ExceptionInfo.from_exception(RuntimeError("flaky")),
            )
            trial.result = failed
            trial.run = AsyncMock(return_value=failed)
        else:
            ok = ScenarioTrialResult(
                scenario_name=config.scenario.name,
                trial_name=config.trial_name,
                exit_code=0,
            )
            trial.result = ok
            trial.run = AsyncMock(return_value=ok)
        trial.add_hook = MagicMock()
        return trial

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
            retry_config=RetryConfig(max_retries=2, min_wait_sec=0, max_wait_sec=0),
        )
        result = asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))

    assert attempts == 2
    assert result.exit_code == 0
    assert result.exception_info is None


def test_retry_clears_failed_attempt_output(tmp_path):
    """A retried-then-succeeded trial leaves no stale artifacts from the failed attempt.

    The failed attempt writes an ``exception.txt`` into the trial dir; the
    queue removes that dir before retrying, so it is gone once a later
    attempt succeeds.
    """
    attempts = 0

    async def fake_create(config, cred_provider):
        nonlocal attempts
        attempts += 1
        trial = MagicMock()
        # The queue derives the trial dir as ``output_dir / trial_name``;
        # mirror that here to stage and later assert on the right path.
        trial_dir = config.output_dir / config.trial_name
        if attempts == 1:
            # Simulate the failed attempt persisting an exception file on disk.
            trial_dir.mkdir(parents=True, exist_ok=True)
            (trial_dir / "exception.txt").write_text("Traceback: flaky")
            failed = ScenarioTrialResult(
                scenario_name=config.scenario.name,
                trial_name=config.trial_name,
                exception_info=ExceptionInfo.from_exception(RuntimeError("flaky")),
            )
            trial.result = failed
            trial.run = AsyncMock(return_value=failed)
        else:
            ok = ScenarioTrialResult(
                scenario_name=config.scenario.name,
                trial_name=config.trial_name,
                exit_code=0,
            )
            trial.result = ok
            trial.run = AsyncMock(return_value=ok)
        trial.add_hook = MagicMock()
        return trial

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
            retry_config=RetryConfig(max_retries=2, min_wait_sec=0, max_wait_sec=0),
        )
        cfg = _make_cfg(output_dir=tmp_path)
        result = asyncio.run(q.submit(cfg))

    assert result.exception_info is None
    # The failed attempt's exception.txt was removed before the retry.
    assert not (tmp_path / cfg.trial_name / "exception.txt").exists()


def test_retry_exhausted_returns_last_failure(tmp_path):
    attempts = 0

    async def fake_create(config, cred_provider):
        nonlocal attempts
        attempts += 1
        failed = ScenarioTrialResult(
            scenario_name=config.scenario.name,
            trial_name=config.trial_name,
            exception_info=ExceptionInfo.from_exception(RuntimeError(f"attempt {attempts}")),
        )
        trial = MagicMock()
        trial.result = failed
        trial.run = AsyncMock(return_value=failed)
        trial.add_hook = MagicMock()
        return trial

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
            retry_config=RetryConfig(max_retries=2, min_wait_sec=0, max_wait_sec=0),
        )
        result = asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))

    assert attempts == 3  # 1 initial + 2 retries
    assert result.exception_info is not None
    assert "attempt 3" in result.exception_info.exception_message


def test_retry_skipped_for_excluded_exception(tmp_path):
    """Excluded exceptions abort retry immediately."""
    attempts = 0

    async def fake_create(config, cred_provider):
        nonlocal attempts
        attempts += 1
        failed = ScenarioTrialResult(
            scenario_name=config.scenario.name,
            trial_name=config.trial_name,
            exception_info=ExceptionInfo.from_exception(ValueError("permanent bug")),
        )
        trial = MagicMock()
        trial.result = failed
        trial.run = AsyncMock(return_value=failed)
        trial.add_hook = MagicMock()
        return trial

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
            retry_config=RetryConfig(
                max_retries=5,
                exclude_exceptions={"ValueError"},
                min_wait_sec=0,
                max_wait_sec=0,
            ),
        )
        result = asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))

    assert attempts == 1
    assert result.exception_info is not None
    assert result.exception_info.exception_type == "ValueError"


def test_retry_skipped_when_not_in_include_list(tmp_path):
    """include_exceptions acts as an allowlist; mismatched type does not retry."""
    attempts = 0

    async def fake_create(config, cred_provider):
        nonlocal attempts
        attempts += 1
        failed = ScenarioTrialResult(
            scenario_name=config.scenario.name,
            trial_name=config.trial_name,
            exception_info=ExceptionInfo.from_exception(KeyError("missing")),
        )
        trial = MagicMock()
        trial.result = failed
        trial.run = AsyncMock(return_value=failed)
        trial.add_hook = MagicMock()
        return trial

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", side_effect=fake_create):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
            retry_config=RetryConfig(
                max_retries=5,
                include_exceptions={"TimeoutError"},
                min_wait_sec=0,
                max_wait_sec=0,
            ),
        )
        asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))

    assert attempts == 1


def test_hook_registration_propagates_to_trial(tmp_path):
    """Each registered hook is wired to every trial the queue creates."""
    trial = _stub_trial()

    async def cb(_e) -> None: ...

    with patch("aws_bench.scenario.queue.ScenarioTrial.create", AsyncMock(return_value=trial)):
        q = ScenarioQueue(
            n_concurrent=1,
            cred_provider=MagicMock(),
            phase=ScenarioPhase.DEPLOY,
        )
        q.on_trial_started(cb).on_trial_ended(cb)
        asyncio.run(q.submit(_make_cfg(output_dir=tmp_path)))

    # add_hook was called for every event in the registry; START and END
    # had a callback registered, others had none -> only those calls.
    calls = [c.args for c in trial.add_hook.call_args_list]
    events = [event for event, _ in calls]
    assert ScenarioEvent.START in events
    assert ScenarioEvent.END in events
