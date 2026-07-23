"""Scenario-related exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aws_bench.exceptions import AWSBenchError
from aws_bench.resource_management.models import QuotaIncreaseResult


class ScenarioError(AWSBenchError):
    """Base for scenario-related errors."""


class NonZeroExitCodeError(ScenarioError):
    """A phase script exited with a non-zero status code.

    Carries the phase, its exit code, and the tail of the script's
    combined stdout+stderr so the operator sees both the failing phase
    and its last output in the raised message.
    """

    _STDOUT_TAIL_CHARS = 2000

    def __init__(self, *, phase: str, exit_code: int, stdout: str) -> None:
        """Build a message from the failing phase, its code, and stdout tail."""
        self.phase = phase
        self.exit_code = exit_code
        self.stdout = stdout
        tail = stdout[-self._STDOUT_TAIL_CHARS :].strip()
        message = f"{phase} phase exited {exit_code}"
        if tail:
            message = f"{message}\nstdout tail:\n{tail}"
        super().__init__(message)


class PhaseTimeoutError(ScenarioError):
    """A phase script ran past its ``timeout_sec`` budget.

    Carries the phase and the budget so the operator sees which phase timed
    out and after how long, instead of a bare ``asyncio.TimeoutError``.
    """

    def __init__(self, *, phase: str, timeout_sec: float) -> None:
        """Build a message from the phase and its elapsed timeout budget."""
        self.phase = phase
        self.timeout_sec = timeout_sec
        super().__init__(f"{phase} phase timed out after {timeout_sec:.0f}s")


class SnapshotFailedError(ScenarioError):
    """One or more accounts failed post-setup baseline capture.

    Without a saved baseline, verify/reset have nothing to diff against, so the
    setup trial fails rather than deploy "succeeding" with a missing baseline.
    """

    def __init__(self, *, failures: list[str]) -> None:
        """Build a message from the failing ``account: reason`` pairs."""
        self.failures = failures
        super().__init__(
            f"post setup snapshot failed for {len(failures)} account(s): {'; '.join(failures)}"
        )


class VerifyFailedError(ScenarioError):
    """One or more accounts failed post-setup baseline verification.

    Includes a missing/unsaved baseline: nothing was checked, so verify fails.
    """

    def __init__(self, *, failures: list[str]) -> None:
        """Build a message from the failing ``account: reason`` pairs."""
        self.failures = failures
        super().__init__(f"env verify failed for {len(failures)} account(s): {'; '.join(failures)}")


class ResetFailedError(ScenarioError):
    """One or more accounts failed to reset to their post-setup baseline."""

    def __init__(self, *, failures: list[str]) -> None:
        """Build a message from the failing reason(s)."""
        self.failures = failures
        super().__init__(f"env reset failed for {len(failures)} account(s): {'; '.join(failures)}")


class CleanupFailedError(ScenarioError):
    """One or more accounts failed cleanup of their scenario stacks.

    Remaining orphaned resources also fail here, not just stack-delete errors.
    """

    def __init__(self, *, failures: list[str]) -> None:
        """Build a message from the failing ``account: reason`` pairs."""
        self.failures = failures
        super().__init__(
            f"env cleanup failed for {len(failures)} account(s): {'; '.join(failures)}"
        )


class DuplicateScenarioNameError(ScenarioError):
    """Two scenarios in a discovery scan declared the same name."""


class ScenarioDiscoveryError(ScenarioError):
    """One or more scenarios failed validation during discovery.

    Carries every failure (``list[tuple[Path, Exception]]``) so the
    operator sees them all in one pass instead of the fix-rerun-fix-rerun
    cycle that fail-fast produces.
    """

    def __init__(self, errors: list[tuple["Path", Exception]]) -> None:
        """Build a single aggregated message from per-scenario failures."""
        self.errors = errors
        joined = "\n".join(f"  {p}: {e}" for p, e in errors)
        super().__init__(f"{len(errors)} scenario(s) failed validation:\n{joined}")


@dataclass(frozen=True)
class UnmetQuota:
    """One scenario x account x region x quota whose current value is below the required value."""

    scenario_name: str
    account_id: str
    region: str
    result: QuotaIncreaseResult


class InsufficientQuotaError(ScenarioError):
    """One or more required quotas have insufficient current values.

    Carries every unmet quota (``list[UnmetQuota]``) so the operator
    sees all of them in one pass instead of fixing them one at a time
    across multiple runs.
    """

    def __init__(self, failures: list[UnmetQuota]) -> None:
        """Build a single aggregated message from per-quota failures."""
        self.failures = failures
        joined = "\n".join(
            f"  {f.scenario_name}/{f.account_id}/{f.region}: "
            f"{f.result.service_code}/{f.result.quota_code} "
            f"-> {f.result.status.value} ({f.result.error_message or ''})"
            for f in failures
        )
        super().__init__(f"{len(failures)} quota(s) below desired value:\n{joined}")


class SetupValidationError(ScenarioError):
    """Raised when setup pre-conditions are not met (e.g., init snapshot missing)."""

    def __init__(self, message: str):
        """Initialize with a descriptive error message."""
        super().__init__(message)


class AccountLimitExceededError(ScenarioError):
    """AWS Organizations hit its maximum-accounts limit during provisioning.

    Raised reactively when account creation fails with ACCOUNT_LIMIT_EXCEEDED.
    The ``detail`` reports whether a Service Quotas increase was just filed or
    one was already pending, and the message points the operator at re-running
    after the increase is approved.
    """

    def __init__(self, detail: str) -> None:
        """Build the message from a detail describing the quota-request outcome."""
        self.detail = detail
        super().__init__(
            "AWS Organizations account limit exceeded while creating accounts. "
            f"{detail} Wait for the increase to be approved, then re-run "
            "'aws-bench env init'."
        )
