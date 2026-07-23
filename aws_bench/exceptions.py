"""Top-level exceptions for aws_bench."""


class AWSBenchError(Exception):
    """Base exception for all aws-bench errors."""


class CredentialError(AWSBenchError):
    """Failed to resolve or assume AWS credentials."""


class AccountContaminatedError(AWSBenchError):
    """One or more of a scenario's accounts are flagged contaminated.

    Raised at trial entry (benchmark run, env setup) to refuse work on accounts
    whose baseline is not trustworthy (a prior reset failed to restore them),
    instead of scoring against dirty state. Carries every contaminated account so
    the operator sees them all in one pass rather than fix-rerun-fix.
    """

    def __init__(self, account_ids: list[str], scenario_id: str) -> None:
        """Build the error from the scenario and its contaminated account id(s)."""
        self.account_ids = account_ids
        self.scenario_id = scenario_id
        joined = ", ".join(account_ids)
        super().__init__(
            f"Scenario '{scenario_id}' has {len(account_ids)} contaminated account(s): "
            f"{joined}. A prior reset failed to restore baseline. Run "
            f"'aws-bench env cleanup' to clean them and clear the flag."
        )


class OperationCancelled(BaseException):
    """Raised at a cooperative checkpoint after a shutdown signal (Ctrl+C / SIGTERM).

    A ``BaseException`` (not ``Exception``) so it bypasses the per-account
    ``except Exception`` handlers instead of being reclassified as a failed
    result. Not ``asyncio.CancelledError``: asyncio special-cases that type, so
    reusing it would misattribute a worker-thread shutdown as a task cancel.
    """
