"""Tests for CLI display utilities."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from harbor.models.trial.result import ExceptionInfo

from aws_bench.account_management.models import (
    ScenarioAccount,
)
from aws_bench.cli.display import (
    display_cleanup_results,
    display_provisioning_summary,
    display_reset_results,
    display_setup_summary,
    display_snapshot_results,
    display_verify_results,
)
from aws_bench.cli.ui import TeeConsole
from aws_bench.resource_management.cleanup.models import (
    AccountCleanupResult,
    CleanupSummary,
    RegionResult,
)
from aws_bench.resource_management.models import (
    QuotaConfiguration,
    QuotaIncreaseRequest,
    QuotaIncreaseResult,
    QuotaStatus,
)
from aws_bench.resource_management.reset.models import ResetResult
from aws_bench.resource_management.snapshot.models import SnapshotResult
from aws_bench.resource_management.verify.models import AccountVerifyResult, RegionVerifyResult
from aws_bench.scenario.events import ScenarioPhase
from aws_bench.scenario.exceptions import UnmetQuota
from aws_bench.scenario.provisioning import (
    ProvisionedAccount,
    ProvisioningSummary,
    SubmittedQuotaBatch,
)
from aws_bench.scenario.results import ScenarioJobResult, ScenarioTrialResult

# ===========================================================================
# display_cleanup_results — cleanup result formatting
# ===========================================================================


def _clean_summary(region: str = "us-east-1", found: int = 5) -> CleanupSummary:
    """A CleanupSummary whose teardown is fully clean."""
    return CleanupSummary(
        regions=[
            RegionResult(region=region, stacks_found=found, stacks_deleted=found, stacks_failed=[])
        ],
        orphaned_resources={},
        run_dir="/tmp/test",
    )


def _cleanup_job(*trials: ScenarioTrialResult) -> ScenarioJobResult:
    """Wrap trials in a ScenarioJobResult for display_cleanup_results."""
    return ScenarioJobResult(
        job_name="cleanup",
        job_dir=Path("/tmp/job"),
        started_at=datetime.now(timezone.utc),
        n_total=len(trials),
        trial_results=list(trials),
    )


def _cleanup_trial(
    *,
    scenario_name: str,
    tag: str = "PRIMARY",
    account_id: str = "123456789012",
    summary: CleanupSummary | None = None,
    account_error: str | None = None,
    script_error: str | None = None,
) -> ScenarioTrialResult:
    """A cleanup trial: framework teardown result plus optional script error."""
    return ScenarioTrialResult(
        scenario_name=scenario_name,
        trial_name=f"{scenario_name}__t",
        phase=ScenarioPhase.CLEANUP,
        account_mapping={tag: account_id},
        exception_info=(
            ExceptionInfo(
                exception_type="PhaseTimeoutError",
                exception_message=script_error,
                exception_traceback="",
                occurred_at=datetime.now(timezone.utc),
            )
            if script_error
            else None
        ),
        resource_results={
            "cleanup": [
                AccountCleanupResult(account_id=account_id, summary=summary, error=account_error)
            ]
        },
    )


def test_display_cleanup_results_shows_success():
    """A clean teardown with no script error is OK."""
    result = _cleanup_job(_cleanup_trial(scenario_name="serverless-apps", summary=_clean_summary()))

    has_failures = display_cleanup_results(result)

    assert has_failures is False


def test_display_cleanup_results_script_error_with_clean_teardown_fails(capsys):
    """cleanup.sh timing out is FAILED even when the framework teardown is clean.

    Regression for the contradictory display where an 8/8-Clean teardown printed
    "cleaned successfully" under a cleanup-timeout ERROR. The script failure now
    drives the row status, and the detail names the script error — not the count.
    """
    result = _cleanup_job(
        _cleanup_trial(
            scenario_name="serverless-apps",
            account_id="123456789012",
            summary=_clean_summary(found=8),
            script_error="cleanup phase timed out after 3600s",
        )
    )

    has_failures = display_cleanup_results(result)

    assert has_failures is True
    out = capsys.readouterr().out
    assert "serverless-apps" in out
    assert "FAILED" in out
    # Detail names the script error. Assert on single tokens: Rich word-wraps the
    # cell, so the full phrase "timed out after 3600s" is not contiguous in stdout.
    assert "timed out" in out
    assert "3600s" in out
    # The reassuring stack count must NOT appear on a script-failure row.
    assert "8/8" not in out
    assert "cleaned successfully" not in out


def test_display_cleanup_results_orphans_count_as_failure(capsys):
    """An account that deleted all stacks but left orphans is reported as FAILED."""
    summary = CleanupSummary(
        regions=[
            RegionResult(region="us-east-1", stacks_found=5, stacks_deleted=5, stacks_failed=[])
        ],
        orphaned_resources={"AWS::S3::Bucket": ["bucket-1", "bucket-2"]},
        run_dir="/tmp/test",
    )
    result = _cleanup_job(_cleanup_trial(scenario_name="databases-and-storage", summary=summary))

    has_failures = display_cleanup_results(result)

    assert has_failures is True
    out = capsys.readouterr().out
    assert "FAILED" in out
    # Single token: Rich word-wraps "2 orphaned resource(s) remain" across lines.
    assert "orphaned" in out


def test_display_cleanup_results_shows_failures(capsys):
    """A teardown error (no summary) is FAILED, detail carries the error."""
    result = _cleanup_job(
        _cleanup_trial(
            scenario_name="iam-and-connectivity", summary=None, account_error="Connection timeout"
        )
    )

    has_failures = display_cleanup_results(result)

    assert has_failures is True
    out = capsys.readouterr().out
    assert "Connection timeout" in out


def test_display_cleanup_results_shows_mixed(capsys):
    """One clean trial + one failed trial reports a failure and lists both scenarios."""
    result = _cleanup_job(
        _cleanup_trial(
            scenario_name="compute-and-data",
            account_id="111111111111",
            summary=_clean_summary(region="us-west-2", found=3),
        ),
        _cleanup_trial(
            scenario_name="streaming-and-iot",
            account_id="222222222222",
            summary=None,
            account_error="Failed",
        ),
    )

    has_failures = display_cleanup_results(result)

    assert has_failures is True
    out = capsys.readouterr().out
    assert "compute-and-data" in out
    assert "streaming-and-iot" in out


# ===========================================================================
# display_verify_results — verification result formatting
# ===========================================================================


def test_display_verify_results_all_passed():
    """Displays all passed verification results."""
    results = [
        AccountVerifyResult(
            account_id="123456789012",
            environment_id="env-1",
            success=True,
            region_results=[
                RegionVerifyResult(region="us-east-1", success=True),
            ],
        )
    ]

    has_failures = display_verify_results(results)

    assert has_failures is False


def test_display_verify_results_with_error_message():
    """Displays verification with error message."""
    results = [
        AccountVerifyResult(
            account_id="123456789012",
            environment_id="env-1",
            success=False,
            region_results=[],
            error_message="Snapshot not found",
        )
    ]

    has_failures = display_verify_results(results)

    assert has_failures is True


def test_display_verify_results_with_region_failures():
    """Displays verification with region-level failures."""
    results = [
        AccountVerifyResult(
            account_id="123456789012",
            environment_id="env-1",
            success=False,
            region_results=[
                RegionVerifyResult(
                    region="us-east-1",
                    success=False,
                    error_message="New resources found",
                    suggestion="Run reset",
                ),
            ],
        )
    ]

    has_failures = display_verify_results(results)

    assert has_failures is True


def test_display_verify_results_preserves_bracketed_error_text(capsys):
    """A region error containing brackets renders verbatim, not eaten as markup.

    AWS/botocore error strings routinely contain a regex character class like
    ``[a-zA-Z0-9-]+``. Since printed strings are markup-active, the interpolated
    error must be escaped or Rich swallows the bracketed span.
    """
    results = [
        AccountVerifyResult(
            account_id="123456789012",
            environment_id="env-1",
            success=False,
            region_results=[
                RegionVerifyResult(
                    region="us-east-1",
                    success=False,
                    error_message="Member must satisfy pattern: [a-zA-Z0-9-]+",
                ),
            ],
        )
    ]

    display_verify_results(results)

    out = capsys.readouterr().out
    assert "[a-zA-Z0-9-]+" in out


def test_display_reset_results_survives_closing_tag_in_exception(capsys):
    """An exception whose text has a Rich closing tag must not crash the render.

    ``str(exception)`` can contain something like ``[/mnt]``; parsed as markup
    that is an unbalanced closing tag and raises ``MarkupError``, aborting the
    command. The interpolated exception text must be escaped.
    """
    results: list[ResetResult | Exception] = [RuntimeError("cleanup failed at [/prod] path")]
    accounts = [
        ScenarioAccount(
            account_id="123456789012",
            email="a@example.com",
            scenario_name="s",
            account_tag="t",
            status="ACTIVE",
        ),
    ]

    # Must not raise MarkupError.
    display_reset_results(results, accounts)

    out = capsys.readouterr().out
    assert "[/prod]" in out


def test_display_verify_results_failure_with_no_details():
    """Displays generic message when verification fails with no details."""
    results = [
        AccountVerifyResult(
            account_id="123456789012",
            environment_id="env-1",
            success=False,
            region_results=[],
            error_message=None,
        )
    ]

    has_failures = display_verify_results(results)

    assert has_failures is True


# ===========================================================================
# display_reset_results — reset result formatting
# ===========================================================================


def test_display_reset_results_all_success():
    """Displays all successful reset results."""
    accounts = [
        ScenarioAccount(
            account_id="123456789012",
            email="test1@example.com",
            scenario_name="env-1",
            account_tag="tag",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="123456789013",
            email="test2@example.com",
            scenario_name="env-2",
            account_tag="tag",
            status="ACTIVE",
        ),
    ]
    results: list[ResetResult | Exception] = [
        ResetResult(success=True, reason="Reset complete"),
        ResetResult(success=True, reason="Reset complete"),
    ]

    has_failures = display_reset_results(results, accounts)

    assert has_failures is False


def test_display_reset_results_with_failures():
    """Displays reset results with failures."""
    accounts = [
        ScenarioAccount(
            account_id="123456789012",
            email="test1@example.com",
            scenario_name="env-1",
            account_tag="tag",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="123456789013",
            email="test2@example.com",
            scenario_name="env-2",
            account_tag="tag",
            status="ACTIVE",
        ),
    ]
    results: list[ResetResult | Exception] = [
        ResetResult(success=True, reason="Reset complete"),
        ResetResult(success=False, reason="Dataset mismatch"),
    ]

    has_failures = display_reset_results(results, accounts)

    assert has_failures is True


def test_display_reset_results_with_exceptions():
    """Displays reset results with exceptions."""
    accounts = [
        ScenarioAccount(
            account_id="123456789012",
            email="test@example.com",
            scenario_name="env-1",
            account_tag="tag",
            status="ACTIVE",
        ),
    ]
    results: list[ResetResult | Exception] = [
        Exception("Connection timeout"),
    ]

    has_failures = display_reset_results(results, accounts)

    assert has_failures is True


def test_display_reset_results_length_mismatch_raises():
    """Raises ValueError when results and accounts length mismatch."""
    accounts = [
        ScenarioAccount(
            account_id="123456789012",
            email="test@example.com",
            scenario_name="env-1",
            account_tag="tag",
            status="ACTIVE",
        ),
    ]
    results: list[ResetResult | Exception] = [
        ResetResult(success=True, reason="Reset complete"),
        ResetResult(success=True, reason="Reset complete"),
    ]

    with pytest.raises(ValueError):
        display_reset_results(results, accounts)


# ===========================================================================
# display_setup_summary — env setup per-trial summary table
# ===========================================================================


def test_display_setup_summary_shows_all_passed(capsys, tmp_path):
    started = datetime.now(timezone.utc)
    finished = started + timedelta(seconds=42)
    job_dir = tmp_path / "job-x"
    result = ScenarioJobResult(
        job_name="job-x",
        job_dir=job_dir,
        started_at=started,
        finished_at=finished,
        n_total=2,
        n_succeeded=2,
        n_failed=0,
        trial_results=[
            ScenarioTrialResult(
                scenario_name="scenario-a",
                trial_name="scenario-a__abc",
                phase=ScenarioPhase.DEPLOY,
                started_at=started,
                finished_at=finished,
                exit_code=0,
            ),
            ScenarioTrialResult(
                scenario_name="scenario-b",
                trial_name="scenario-b__def",
                phase=ScenarioPhase.DEPLOY,
                started_at=started,
                finished_at=finished,
                exit_code=0,
            ),
        ],
    )
    display_setup_summary(result)
    captured = capsys.readouterr()
    out = captured.out.replace("\n", "")
    assert "scenario-a" in captured.out
    assert "scenario-b" in captured.out
    assert "Deployed 2/2 scenario(s)" in captured.out
    assert f"Output: {job_dir}" in out


def test_display_setup_summary_shows_failures(capsys, tmp_path):
    started = datetime.now(timezone.utc)
    finished = started + timedelta(seconds=10)
    job_dir = tmp_path / "job-x"
    result = ScenarioJobResult(
        job_name="job-x",
        job_dir=job_dir,
        started_at=started,
        finished_at=finished,
        n_total=1,
        n_succeeded=0,
        n_failed=1,
        trial_results=[
            ScenarioTrialResult(
                scenario_name="bad",
                trial_name="bad__123",
                phase=ScenarioPhase.DEPLOY,
                started_at=started,
                finished_at=finished,
                exit_code=1,
            ),
        ],
    )
    display_setup_summary(result)
    captured = capsys.readouterr()
    out = captured.out.replace("\n", "")
    assert "1 failed" in captured.out
    assert "bad" in captured.out
    expected_log = job_dir / "bad__123" / "trial.log"
    assert str(expected_log) in out


# ===========================================================================
# display_provisioning_summary — env init per-account provisioning summary
# ===========================================================================


def _batch(scenario, tag, account_id, region, *quota_codes):
    """Build a SubmittedQuotaBatch with one QuotaIncreaseRequest per code."""
    return SubmittedQuotaBatch(
        scenario_name=scenario,
        account_tag=tag,
        account_id=account_id,
        config=QuotaConfiguration(
            region=region,
            increases=[
                QuotaIncreaseRequest(service_code="ec2", quota_code=qc, desired_value=10.0)
                for qc in quota_codes
            ],
        ),
    )


def _unmet(scenario, account_id, region, quota_code, status, error="msg"):
    """Build an UnmetQuota for a single scenario x account x region x quota."""
    return UnmetQuota(
        scenario_name=scenario,
        account_id=account_id,
        region=region,
        result=QuotaIncreaseResult(
            service_code="ec2",
            quota_code=quota_code,
            desired_value=10.0,
            status=status,
            error_message=error,
        ),
    )


def test_provisioning_summary_met_never_negative_with_multi_quota_batch(capsys):
    """met/submitted counts individual quotas, not region batches.

    Regression: a single region batch held 3 quotas; 2 were unmet. The old
    code did ``len(submitted_quotas) - unmet`` = ``1 - 2`` = ``-1``. met must
    count individual quotas (3) minus unmet quotas (2) = 1.
    """
    acct = ProvisionedAccount(
        scenario_name="multi",
        account_tag="PRIMARY",
        account_id="111111111111",
        submitted_quotas=[
            _batch("multi", "PRIMARY", "111111111111", "us-west-2", "L-A", "L-B", "L-C"),
        ],
    )
    summary = ProvisioningSummary(accounts=[acct])
    summary.unmet_quotas = [
        _unmet("multi", "111111111111", "us-west-2", "L-A", QuotaStatus.ALREADY_PENDING),
        _unmet("multi", "111111111111", "us-west-2", "L-B", QuotaStatus.FAILED),
    ]

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "1/3" in out
    assert "-1/3" not in out
    assert "-2/3" not in out


def test_provisioning_summary_met_equals_submitted_quota_count(capsys):
    """With no wait failures, met == total individual quotas across batches."""
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        submitted_quotas=[
            _batch("sc", "PRIMARY", "111111111111", "us-east-1", "L-A", "L-B"),
            _batch("sc", "PRIMARY", "111111111111", "us-west-2", "L-C"),
        ],
    )
    summary = ProvisioningSummary(accounts=[acct])

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "3/3" in out


def test_provisioning_summary_pending_separated_from_failures(capsys):
    """Not-yet-granted quotas appear in their own table, not under failures."""
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        submitted_quotas=[_batch("sc", "PRIMARY", "111111111111", "us-west-2", "L-A", "L-B")],
    )
    summary = ProvisioningSummary(accounts=[acct])
    summary.unmet_quotas = [
        _unmet("sc", "111111111111", "us-west-2", "L-A", QuotaStatus.ALREADY_PENDING),
        _unmet("sc", "111111111111", "us-east-1", "L-B", QuotaStatus.DENIED),
    ]

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    # Both a not-yet-granted table and a failures table render.
    assert "not yet granted" in out.lower()
    assert "Quota approval failures" in out
    # The hint nudges the operator toward env show.
    assert "env show" in out


def test_provisioning_summary_case_closed_is_not_a_failure(capsys):
    """A CASE_CLOSED quota lands in 'not yet granted', never the failures table.

    Regression: CASE_CLOSED was mislabeled FAILED. AWS reports CASE_CLOSED for
    an approved increase whose value may still be propagating.
    """
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        submitted_quotas=[_batch("sc", "PRIMARY", "111111111111", "us-east-1", "L-A")],
    )
    summary = ProvisioningSummary(accounts=[acct])
    summary.unmet_quotas = [
        _unmet(
            "sc",
            "111111111111",
            "us-east-1",
            "L-A",
            QuotaStatus.CASE_CLOSED,
            "case closed — increase may still be propagating",
        ),
    ]

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Quota approval failures" not in out
    assert "not yet granted" in out.lower()
    assert "env show" in out


def test_provisioning_summary_pending_only_no_failure_table(capsys):
    """When all unmet quotas are pending, no 'failures' table is shown."""
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        submitted_quotas=[_batch("sc", "PRIMARY", "111111111111", "us-west-2", "L-A")],
    )
    summary = ProvisioningSummary(accounts=[acct])
    summary.unmet_quotas = [
        _unmet("sc", "111111111111", "us-west-2", "L-A", QuotaStatus.ALREADY_PENDING),
    ]

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Quota approval failures" not in out
    assert "env show" in out


def test_provisioning_summary_all_failed_no_pending_table(capsys):
    """When all unmet quotas are genuine failures, no 'not yet granted' table."""
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        submitted_quotas=[_batch("sc", "PRIMARY", "111111111111", "us-east-1", "L-A", "L-B")],
    )
    summary = ProvisioningSummary(accounts=[acct])
    summary.unmet_quotas = [
        _unmet("sc", "111111111111", "us-east-1", "L-A", QuotaStatus.DENIED),
        _unmet("sc", "111111111111", "us-east-1", "L-B", QuotaStatus.FAILED),
    ]

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Quota approval failures" in out
    assert "not yet granted" not in out.lower()
    assert "env show" not in out


def test_provisioning_summary_snapshot_success_shows_footer(capsys):
    """A captured baseline adds a Snapshotted count to the footer, no failures table."""
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        snapshot_result=SnapshotResult(account_id="111111111111", success=True),
    )
    summary = ProvisioningSummary(accounts=[acct])

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Snapshotted 1/1" in out
    assert "Post init snapshot failures" not in out


def _wide_console(monkeypatch):
    """Pin the display console wide so the Status column isn't truncated.

    The default Console width ellipsizes long status cells in the narrow test
    terminal; asserting on that cell's text needs a fixed width. Kept a
    ``TeeConsole`` so the console still mirrors into run.log as in production.
    """
    monkeypatch.setattr("aws_bench.cli.display.console", TeeConsole(width=200))


def test_provisioning_summary_snapshot_failure_marks_row_and_lists_failure(capsys, monkeypatch):
    """A failed baseline flips the row to FAILED and renders the failures table + hint."""
    _wide_console(monkeypatch)
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        snapshot_result=SnapshotResult(
            account_id="111111111111", success=False, error_message="scan blew up"
        ),
    )
    summary = ProvisioningSummary(accounts=[acct])

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Snapshotted 0/1" in out
    assert "FAILED" in out
    assert "Post init snapshot" in out
    assert "Post init snapshot failures" in out
    assert "scan blew up" in out
    assert "env init" in out


def test_provisioning_summary_snapshot_failure_outranks_quota_partial(capsys, monkeypatch):
    """A failed snapshot outranks a quota submit failure in the row's status.

    An account with both a quota submit failure and a failed snapshot reports the
    snapshot failure, confirming snapshot precedence over PARTIAL.
    """
    _wide_console(monkeypatch)
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        submit_failures=[RuntimeError("quota denied")],
        snapshot_result=SnapshotResult(
            account_id="111111111111", success=False, error_message="scan blew up"
        ),
    )
    summary = ProvisioningSummary(accounts=[acct])

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Post init snapshot" in out
    assert "PARTIAL" not in out


def test_provisioning_summary_snapshot_dash_when_capture_never_ran(capsys):
    """An account that failed before capture shows no Snapshotted count and no table."""
    acct = ProvisionedAccount(
        scenario_name="sc",
        account_tag="PRIMARY",
        account_id="111111111111",
        error=RuntimeError("account create failed"),
    )
    summary = ProvisioningSummary(accounts=[acct])

    display_provisioning_summary(summary)
    out = capsys.readouterr().out

    assert "Snapshotted" not in out
    assert "Post init snapshot failures" not in out


# ===========================================================================
# display_snapshot_results — snapshot result formatting
# ===========================================================================


def test_display_snapshot_results_all_success():
    """Displays all successful snapshot results."""
    results = {
        "scenario-1": [
            SnapshotResult(
                account_id="123456789012",
                success=True,
                regions_captured=["us-east-1", "us-west-2"],
            ),
            SnapshotResult(
                account_id="123456789013",
                success=True,
                regions_captured=["eu-west-1"],
            ),
        ],
        "scenario-2": [
            SnapshotResult(
                account_id="123456789014",
                success=True,
                regions_captured=["ap-south-1"],
            ),
        ],
    }

    has_failures = display_snapshot_results(results)

    assert has_failures is False


def test_display_snapshot_results_shows_output_path(capsys):
    """Renders the local file path for observability captures."""
    results = {
        "scenario-1": [
            SnapshotResult(
                account_id="123456789012",
                success=True,
                regions_captured=["us-east-1"],
                output_path="/tmp/snaps/scenario-1/123456789012.json",
            ),
        ],
    }

    has_failures = display_snapshot_results(results)

    assert has_failures is False
    assert "Written to: /tmp/snaps/scenario-1/123456789012.json" in capsys.readouterr().out


def test_display_snapshot_results_with_failures():
    """Displays snapshot results with failures and error messages."""
    results = {
        "scenario-1": [
            SnapshotResult(
                account_id="123456789012",
                success=True,
                regions_captured=["us-east-1"],
            ),
            SnapshotResult(
                account_id="123456789013",
                success=False,
                regions_captured=[],
                error_message="Access denied",
            ),
        ],
    }

    has_failures = display_snapshot_results(results)

    assert has_failures is True


def test_display_snapshot_results_empty_regions():
    """Displays snapshot with no regions captured."""
    results = {
        "scenario-1": [
            SnapshotResult(
                account_id="123456789012",
                success=True,
                regions_captured=[],
            ),
        ],
    }

    has_failures = display_snapshot_results(results)

    assert has_failures is False


def test_display_snapshot_results_multiple_failures():
    """Displays multiple snapshot failures across scenarios."""
    results = {
        "scenario-1": [
            SnapshotResult(
                account_id="123456789012",
                success=False,
                regions_captured=[],
                error_message="Network timeout",
            ),
        ],
        "scenario-2": [
            SnapshotResult(
                account_id="123456789013",
                success=False,
                regions_captured=["us-east-1"],
                error_message="Permission denied",
            ),
        ],
    }

    has_failures = display_snapshot_results(results)

    assert has_failures is True
