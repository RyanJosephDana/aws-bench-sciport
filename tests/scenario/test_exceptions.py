"""Tests for scenario exception classes."""

from __future__ import annotations

from pathlib import Path

from aws_bench.resource_management.models import QuotaIncreaseResult, QuotaStatus
from aws_bench.scenario.exceptions import (
    InsufficientQuotaError,
    NonZeroExitCodeError,
    ScenarioDiscoveryError,
    UnmetQuota,
)


def test_discovery_error_includes_count_and_each_path():
    errs = [
        (Path("/tmp/a"), ValueError("bad toml")),
        (Path("/tmp/b"), FileNotFoundError("missing dockerfile")),
    ]
    exc = ScenarioDiscoveryError(errs)
    s = str(exc)
    assert "2 scenario(s) failed validation" in s
    assert "/tmp/a" in s
    assert "/tmp/b" in s
    assert "bad toml" in s
    assert "missing dockerfile" in s


def test_discovery_error_preserves_errors_list():
    errs: list[tuple[Path, Exception]] = [(Path("/tmp/a"), ValueError("x"))]
    exc = ScenarioDiscoveryError(errs)
    assert exc.errors == errs


def _make_unmet_quota(
    scenario_name: str = "scenario-a",
    account_id: str = "111111111111",
    region: str = "us-east-1",
    service_code: str = "ec2",
    quota_code: str = "L-1216C47A",
    desired: float = 50.0,
    status: QuotaStatus = QuotaStatus.ALREADY_PENDING,
    error_message: str = "current=8.0, required=50.0 (PENDING)",
) -> UnmetQuota:
    return UnmetQuota(
        scenario_name=scenario_name,
        account_id=account_id,
        region=region,
        result=QuotaIncreaseResult(
            service_code=service_code,
            quota_code=quota_code,
            desired_value=desired,
            status=status,
            error_message=error_message,
        ),
    )


def test_insufficient_quota_error_includes_count_and_each_failure():
    failures = [
        _make_unmet_quota(scenario_name="alpha", account_id="111111111111"),
        _make_unmet_quota(scenario_name="beta", account_id="222222222222"),
    ]
    exc = InsufficientQuotaError(failures)
    s = str(exc)
    assert "2 quota(s) below desired value" in s
    assert "alpha" in s
    assert "beta" in s
    assert "111111111111" in s
    assert "222222222222" in s
    assert "us-east-1" in s
    assert "L-1216C47A" in s
    assert "ALREADY_PENDING" in s


def test_insufficient_quota_error_preserves_failures_list():
    failures = [_make_unmet_quota()]
    exc = InsufficientQuotaError(failures)
    assert exc.failures == failures


def test_nonzero_exit_code_error_carries_phase_code_and_stdout_tail():
    exc = NonZeroExitCodeError(phase="deploy", exit_code=7, stdout="line1\nline2\nboom\n")
    assert exc.phase == "deploy"
    assert exc.exit_code == 7
    assert exc.stdout == "line1\nline2\nboom\n"
    s = str(exc)
    assert "deploy phase exited 7" in s
    assert "boom" in s


def test_nonzero_exit_code_error_omits_tail_when_stdout_empty():
    exc = NonZeroExitCodeError(phase="verify", exit_code=1, stdout="")
    s = str(exc)
    assert s == "verify phase exited 1"


def test_nonzero_exit_code_error_truncates_long_stdout():
    big = "x" * 10_000
    exc = NonZeroExitCodeError(phase="cleanup", exit_code=2, stdout=big)
    s = str(exc)
    assert "cleanup phase exited 2" in s
    # Only the tail is included, not the full 10k chars.
    assert len(s) < 3_000
