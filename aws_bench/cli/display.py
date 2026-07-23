"""Display utilities for CLI output."""

from collections import defaultdict

from rich import box
from rich.console import Console, Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aws_bench.account_management.models import ScenarioAccount
from aws_bench.cli.ui import console
from aws_bench.resource_management.cleanup.models import (
    AccountCleanupResult,
)
from aws_bench.resource_management.models import QuotaEntry
from aws_bench.resource_management.reset.models import ResetResult
from aws_bench.resource_management.snapshot.models import SnapshotResult
from aws_bench.resource_management.verify.models import AccountVerifyResult
from aws_bench.scenario.exceptions import UnmetQuota
from aws_bench.scenario.provisioning import ProvisioningSummary
from aws_bench.scenario.results import ScenarioJobResult


def display_provisioning_summary(summary: ProvisioningSummary) -> None:
    """Render a final table summarizing per-account provisioning outcomes.

    One row per (scenario, account_tag). When ``--wait-for-quotas`` is set,
    unmet quotas are split into two tables underneath: those not yet granted
    (still pending, or approved but not yet propagated) and those that were
    denied or errored.
    """
    unmet_by_account = _unmet_quotas_by_account(summary)

    table = Table(title="Provisioning summary", show_lines=False)
    table.add_column("Scenario / Account Tag", no_wrap=True)
    table.add_column("Account ID")
    table.add_column("Quotas submitted", justify="right")
    table.add_column("Quotas met", justify="right")
    table.add_column("Snapshot", justify="center")
    table.add_column("Status")

    n_snapshotted = 0
    n_snapshot_attempted = 0
    for acct in summary.accounts:
        snapshot_failed = acct.snapshot_result is not None and not acct.snapshot_result.success
        if acct.error is not None:
            status = f"[red]FAILED[/red] — {escape(str(acct.error))}"
        elif snapshot_failed:
            status = "[red]FAILED[/red] — post init snapshot"
        elif acct.submit_failures:
            status = (
                f"[yellow]PARTIAL[/yellow] — {len(acct.submit_failures)} quota submit(s) failed"
            )
        else:
            status = "[green]OK[/green]"

        if acct.snapshot_result is None:
            snapshot_cell = "[dim]—[/dim]"
        elif acct.snapshot_result.success:
            snapshot_cell = "[green]✓[/green]"
            n_snapshot_attempted += 1
            n_snapshotted += 1
        else:
            snapshot_cell = "[red]✗[/red]"
            n_snapshot_attempted += 1

        # Each SubmittedQuotaBatch is one region holding several quotas; count
        # the quotas so the unit matches the per-quota unmet count below.
        submitted = sum(len(b.config.increases) for b in acct.submitted_quotas)
        unmet = unmet_by_account.get((acct.scenario_name, acct.account_tag), 0)
        met = max(0, submitted - unmet)
        met_cell = f"{met}/{submitted}" if submitted else "-"

        table.add_row(
            f"{acct.scenario_name}/{acct.account_tag}",
            acct.account_id or "-",
            str(submitted) if submitted else "-",
            met_cell,
            snapshot_cell,
            status,
        )
    console.print(table)
    footer = f"Provisioned {summary.n_provisioned}/{summary.n_total} account(s)"
    if n_snapshot_attempted:
        footer += f" · Snapshotted {n_snapshotted}/{n_snapshot_attempted}"
    footer += f" · {summary.n_failed} failed."
    console.print(footer)

    _display_snapshot_failures(summary)
    _display_quota_wait_outcomes(summary)

    # Warn about quota propagation delay when any quotas were submitted.
    total_submitted = sum(
        len(b.config.increases) for acct in summary.accounts for b in acct.submitted_quotas
    )
    if total_submitted:
        console.print()
        console.print(
            "[yellow]Note:[/yellow] Quota increases have been approved, but it can take "
            "up to roughly 30 minutes for the new values to propagate to each service's API. "
            "If `aws-bench env setup` failed with unmet quota, wait and retry."
        )


def _display_snapshot_failures(summary: ProvisioningSummary) -> None:
    """List accounts whose post init snapshot capture failed, with a re-run hint.

    Capture is idempotent, so re-running ``env init`` retries only the accounts
    still missing a snapshot.
    """
    failed = [
        (acct, acct.snapshot_result)
        for acct in summary.accounts
        if acct.snapshot_result is not None and not acct.snapshot_result.success
    ]
    if not failed:
        return

    console.print()
    table = Table(title="Post init snapshot failures")
    table.add_column("Scenario / Account Tag", no_wrap=True)
    table.add_column("Account ID")
    table.add_column("Error")
    for acct, snapshot in failed:
        table.add_row(
            f"{acct.scenario_name}/{acct.account_tag}",
            acct.account_id or "-",
            escape(snapshot.error_message or "unknown"),
        )
    console.print(table)
    console.print(
        "[dim]'env cleanup' needs this snapshot. Re-run [/dim][bold]aws-bench env init[/bold]"
        "[dim] to retry.[/dim]"
    )


def _display_quota_wait_outcomes(summary: ProvisioningSummary) -> None:
    """Render unmet quotas, split into 'not yet granted' and 'failed' tables.

    Quotas that are still pending, or approved/case-closed but whose new value
    hasn't propagated yet, are not failures (``not status.is_failure``); they
    get their own table plus a hint to re-check with ``env show``. Denied and
    errored quotas go in a separate failures table.
    """
    not_granted = [q for q in summary.unmet_quotas if not q.result.status.is_failure]
    failed = [q for q in summary.unmet_quotas if q.result.status.is_failure]

    if not_granted:
        console.print()
        console.print(_build_quota_table("Quotas not yet granted", not_granted))
        console.print(
            "[dim]Each is awaiting AWS approval, or approved with the new value still "
            "propagating. Re-check with [/dim][bold]aws-bench env show[/bold][dim].[/dim]"
        )

    if failed:
        console.print()
        console.print(_build_quota_table("Quota approval failures", failed))


def _build_quota_table(title: str, rows: list[UnmetQuota]) -> Table:
    """Build a per-quota table from ``UnmetQuota`` rows."""
    table = Table(title=title)
    table.add_column("Scenario")
    table.add_column("Account ID")
    table.add_column("Region")
    table.add_column("Quota Code")
    table.add_column("Status")
    table.add_column("Error")
    for q in rows:
        table.add_row(
            q.scenario_name,
            q.account_id,
            q.region,
            q.result.quota_code,
            q.result.status.value,
            escape(q.result.error_message or "None"),
        )
    return table


def _unmet_quotas_by_account(summary: ProvisioningSummary) -> dict[tuple[str, str], int]:
    """Return ``{(scenario_name, account_tag): unmet_quota_count}``.

    Counts individual quotas from ``summary.unmet_quotas``, matching the unit of
    the per-account submitted-quota count. Accounts absent here have zero unmet.
    """
    tag_for_account_id: dict[str, str] = {
        a.account_id: a.account_tag for a in summary.accounts if a.account_id
    }
    unmet_per_account: dict[tuple[str, str], int] = defaultdict(int)
    for quota in summary.unmet_quotas:
        tag = tag_for_account_id.get(quota.account_id)
        if tag is None:
            continue
        unmet_per_account[(quota.scenario_name, tag)] += 1
    return dict(unmet_per_account)


def _cleanup_row_detail(
    script_error: str | None,
    account_result: AccountCleanupResult | None,
) -> tuple[str, bool]:
    """Resolve one cleanup row's ``(detail, is_failed)``.

    Fails if the script OR the teardown failed. Precedence puts the script error
    first so a clean teardown never masks a failed script:
    script error > teardown error > not clean > clean.
    """
    if script_error:
        # First line is the verdict; NonZeroExitCodeError appends a stdout tail.
        return escape(script_error.splitlines()[0])[:80], True
    if account_result is None:
        return "no teardown result recorded", True
    if account_result.error or account_result.summary is None:
        return escape(account_result.error or "Unknown error")[:80], True
    if not account_result.summary.is_clean:
        return escape(account_result.summary.failure_reason)[:80], True
    return "Clean", False


def display_cleanup_results(result: ScenarioJobResult) -> bool:
    """Display cleanup results as a summary table; return True iff any row failed.

    One row per (scenario, account tag), FAILED if the teardown was not clean OR
    the trial's cleanup.sh errored. Takes the job result (not the flattened
    per-account list) because only its trials carry the scenario/tag/script error.
    """
    table = Table(title="Cleanup summary", show_lines=False)
    table.add_column("Scenario", no_wrap=True)
    table.add_column("Account", no_wrap=True)
    table.add_column("Status")
    table.add_column("Detail")

    succeeded = 0
    failed = 0
    for trial in result.trial_results:
        # One script per trial, so its error applies to every account cleaned.
        script_error = trial.exception_info.exception_message if trial.exception_info else None
        tag_by_account = {acct_id: tag for tag, acct_id in trial.account_mapping.items()}
        cleanup_results = (trial.resource_results or {}).get("cleanup", [])

        # A script that errored before any teardown result (e.g. a timeout) still
        # failed the phase — synthesize a row per account so it stays visible.
        if not cleanup_results and script_error:
            cleanup_results = [
                AccountCleanupResult(account_id=acct_id, summary=None, error=None)
                for acct_id in (trial.account_mapping.values() or ["-"])
            ]

        for account_result in cleanup_results:
            account_id = account_result.account_id
            tag = tag_by_account.get(account_id)
            account_cell = f"{tag} {account_id}" if tag else account_id
            detail, is_failed = _cleanup_row_detail(script_error, account_result)
            status = "[red]FAILED[/red]" if is_failed else "[green]OK[/green]"
            table.add_row(trial.scenario_name, account_cell, status, detail)
            if is_failed:
                failed += 1
            else:
                succeeded += 1

    console.print(table)
    if failed:
        console.print(f"Cleanup completed with errors: {succeeded} succeeded, {failed} failed.")
        return True

    console.print(f"Cleanup complete: {succeeded} account(s) cleaned successfully.")
    return False


def display_verify_results(results: list[AccountVerifyResult]) -> bool:
    """Display verification results; return True iff any account failed."""
    all_passed = True

    for result in results:
        console.print(f"\nAccount {result.account_id} ({result.environment_id}):")

        if result.error_message:
            console.print(f"  ✗ {escape(result.error_message)}")
            all_passed = False
            continue

        if result.region_results:
            region_list = ", ".join(r.region for r in result.region_results)
            console.print(f"  Scanning {len(result.region_results)} region(s): {region_list}")

            for region_result in result.region_results:
                if not region_result.success:
                    console.print(
                        f"  ✗ {region_result.region}: {escape(region_result.error_message or '')}"
                    )
                    if region_result.suggestion:
                        console.print(f"    Suggestion: {escape(region_result.suggestion)}")
                    all_passed = False

        account_passed = result.success and all(r.success for r in (result.region_results or []))
        if account_passed:
            console.print("  ✓ Account state matches post-setup baseline")
        else:
            if not result.region_results and not result.error_message:
                console.print("  ✗ Verification failed (no details available)")
            all_passed = False

    if not all_passed:
        console.print("\nVerification FAILED for one or more accounts.")
        return True

    console.print("\nAll accounts verified successfully!")
    return False


def display_snapshot_results(results: dict[str, list[SnapshotResult]]) -> bool:
    """Display snapshot results; return True iff any account failed.

    Args:
        results: Dict mapping scenario name to list of SnapshotResult per account.

    Returns:
        True if any snapshot failed, False otherwise.
    """
    all_failed = False
    for scenario_name, snapshot_results in results.items():
        console.print(f"\n{scenario_name}:")
        for result in snapshot_results:
            status = "✓" if result.success else "✗"
            regions = ", ".join(result.regions_captured) if result.regions_captured else "none"
            console.print(f"  {status} {result.account_id} (regions: {regions})")
            if result.output_path:
                console.print(f"    Written to: {result.output_path}")
            if result.error_message:
                console.print(f"    Error: {escape(result.error_message)}")
            if not result.success:
                all_failed = True
    return all_failed


def display_reset_results(
    results: list[ResetResult | Exception], accounts: list[ScenarioAccount] | None = None
) -> bool:
    """Display reset results as a summary table; return True iff any account failed.

    Args:
        results: List of ResetResult or Exception from reset operations
        accounts: Optional list of accounts for detailed display. If None, shows simple output.

    Returns:
        True if any reset failed, False otherwise
    """
    succeeded = sum(1 for r in results if isinstance(r, ResetResult) and r.success)
    failed = len(results) - succeeded

    table = Table(title="Reset summary", show_lines=False)
    table.add_column("Scenario", no_wrap=True)
    table.add_column("Account", no_wrap=True)
    table.add_column("Status")
    table.add_column("Detail")

    if accounts:
        for result, account in zip(results, accounts, strict=True):
            if isinstance(result, Exception):
                status = "[red]FAILED[/red]"
                detail = escape(str(result))[:80]
            elif result.success:
                status = "[green]OK[/green]"
                detail = result.reason or "Reset to baseline"
                if result.needs_redeploy and result.redeploy_succeeded:
                    detail = "Stacks recreated via setup"
            else:
                status = "[red]FAILED[/red]"
                detail = escape(result.reason)[:80]
            table.add_row(account.scenario_name, account.account_id, status, detail)
    else:
        for result in results:
            if isinstance(result, Exception):
                status = "[red]FAILED[/red]"
                detail = escape(str(result))[:80]
                scenario = "-"
                account = "-"
            elif result.success:
                status = "[green]OK[/green]"
                detail = result.reason or "Reset to baseline"
                if result.needs_redeploy and result.redeploy_succeeded:
                    detail = "Stacks recreated via setup"
                scenario = result.scenario_name or "-"
                account = result.account_id or "-"
            else:
                status = "[red]FAILED[/red]"
                detail = escape(result.reason)[:80]
                scenario = result.scenario_name or "-"
                account = result.account_id or "-"
            table.add_row(scenario, account, status, detail)

    console.print(table)
    console.print(f"Reset complete: {succeeded} succeeded, {failed} failed")
    return failed > 0


def display_setup_summary(result: ScenarioJobResult) -> None:
    """Render a final table summarizing per-trial outcomes for env setup.

    Columns: Scenario | Account | Regions | Duration | Status.
    Footer: 'Deployed N/M scenario(s); K failed.'
    """
    table = Table(title=f"Setup summary — {result.job_name}", show_lines=False)
    table.add_column("Scenario", no_wrap=True)
    table.add_column("Account", no_wrap=True)
    table.add_column("Regions")
    table.add_column("Duration")
    table.add_column("Status")

    for tr in result.trial_results:
        if tr.started_at and tr.finished_at:
            duration = f"{(tr.finished_at - tr.started_at).total_seconds():.1f}s"
        else:
            duration = "-"
        if tr.exception_info is not None:
            status = f"[red]EXCEPTION[/red] {tr.exception_info.exception_type}"
        elif tr.exit_code == 0:
            status = "[green]OK[/green]"
        else:
            status = f"[red]FAILED[/red] (exit {tr.exit_code})"

        # Show account(s) and regions
        accounts = ", ".join(tr.account_mapping.values()) if tr.account_mapping else "-"
        regions = ", ".join(tr.regions) if tr.regions else "-"

        table.add_row(
            tr.scenario_name,
            accounts,
            regions,
            duration,
            status,
        )

    console.print(table)
    console.print(
        f"Deployed {result.n_succeeded}/{result.n_total} scenario(s); {result.n_failed} failed."
    )
    console.print(f"Output: {result.job_dir}")

    if result.n_failed:
        console.print()
        console.print("[yellow]For failure logs, see:[/yellow]")
        for tr in result.trial_results:
            if tr.success:
                continue
            console.print(f"  {result.job_dir / tr.trial_name / 'trial.log'}")


def _quota_table(quota_entries: list[QuotaEntry]) -> Table:
    """Compact per-account quota table: region, id, name, requested → current."""
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=False)
    table.add_column("", width=1)  # status mark
    table.add_column("Region", style="cyan", no_wrap=True)
    table.add_column("Quota ID", no_wrap=True)
    table.add_column("Name", max_width=42, no_wrap=True, overflow="ellipsis")
    table.add_column("Req", justify="right")
    table.add_column("Cur", justify="right")
    for e in quota_entries:
        mark = Text("✓", style="green") if e.is_met else Text("✗", style="red")
        table.add_row(
            mark,
            e.region,
            e.quota_id,
            e.name or "[dim]—[/dim]",
            str(e.requested),
            "?" if e.current is None else str(e.current),
        )
    return table


def _stacks_renderable(stacks: list[dict[str, str]]) -> RenderableType:
    """Compact per-account stack table, or a dim 'none' line."""
    if not stacks:
        return Text("Stacks: none", style="dim")
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=False)
    table.add_column("Stack", no_wrap=True)
    table.add_column("Status")
    table.add_column("Region", style="cyan", no_wrap=True)
    for s in stacks:
        table.add_row(s["name"], s.get("status", ""), s.get("region", ""))
    return table


def render_account_state(
    account: ScenarioAccount,
    quota_entries: list[QuotaEntry],
    quota_error: str | None,
    stacks: list[dict[str, str]],
) -> Panel:
    """Build a bordered panel of one account's quota + stack state (no I/O)."""
    title = (
        f"[bold]{account.scenario_name} / {account.account_tag}[/bold]  ·  "
        f"{account.account_id}  ·  [dim]{account.status}[/dim]"
    )

    body: list[RenderableType] = []
    if quota_error:
        body.append(Text(f"Quotas: {quota_error}", style="red"))
    elif quota_entries:
        unmet = sum(1 for e in quota_entries if not e.is_met)
        heading = f"Quotas ({len(quota_entries)})"
        if unmet:
            heading += f"  [red]· {unmet} unmet[/red]"
        body.append(Text.from_markup(heading))
        body.append(_quota_table(quota_entries))
    else:
        body.append(Text("Quotas: none requested", style="dim"))

    body.append(Text())  # blank line between quotas and stacks
    body.append(_stacks_renderable(stacks))

    border = "red" if (quota_error or any(not e.is_met for e in quota_entries)) else "green"
    return Panel(Group(*body), title=title, title_align="left", border_style=border)


def render_env_header(
    console: Console,
    mgmt_account_id: str,
    org_id: str,
    env_name: str,
    ou_id: str,
    account_count: int | None = None,
    account_quota: str | None = None,
) -> None:
    """Print the ``env show`` heading panel (environment name + org/OU/account ids)."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column()
    grid.add_row("Organizational unit", ou_id)
    grid.add_row("Organization", org_id)
    grid.add_row("Management account", mgmt_account_id)
    if account_count is not None:
        grid.add_row("Scenario accounts", f"[bold]{account_count}[/bold]")
    if account_quota is not None:
        grid.add_row("Account-limit increase (management account)", account_quota)
    console.print(
        Panel(
            grid,
            title=Text(env_name, style="bold cyan"),
            title_align="left",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, 1),
        )
    )
    console.print()


def render_env_list(
    console: Console,
    org_id: str,
    management_account_id: str,
    environments: list[dict],
) -> None:
    """Render the ``env list`` table of test environments.

    Each entry in ``environments`` has keys: ``name``, ``ou_id``,
    ``account_count`` (int or None if lookup failed).
    """
    table = Table(
        title=f"Test Environments (org: {org_id}, management: {management_account_id})",
        show_lines=False,
    )
    table.add_column("Name", no_wrap=True)
    table.add_column("OU ID")
    table.add_column("Accounts", justify="right")

    for env in environments:
        count = (
            str(env["account_count"]) if env["account_count"] is not None else "[yellow]?[/yellow]"
        )
        table.add_row(env["name"], env["ou_id"], count)

    console.print(table)
