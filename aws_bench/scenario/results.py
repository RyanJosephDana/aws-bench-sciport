"""Scenario job/trial *output* models, plus the lifecycle hook payload.

``ScenarioHookEvent`` lives here — defined last — because it references both
a trial config (input) and a trial result (output). Defining it after every
model it embeds means the annotations resolve at class-creation time, so no
``model_rebuild()`` is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from harbor.models.trial.result import ExceptionInfo, TimingInfo
from pydantic import BaseModel, ConfigDict, Field

from aws_bench.resource_management.reset.models import ResetResult
from aws_bench.resource_management.verify.models import AccountVerifyResult
from aws_bench.scenario.events import ScenarioEvent, ScenarioPhase
from aws_bench.scenario.job_config import ScenarioTrialConfig


class ScenarioTrialResult(BaseModel):
    """Outcome of a single scenario trial."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: UUID = Field(default_factory=uuid4)
    scenario_name: str
    trial_name: str
    phase: ScenarioPhase | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    environment_setup: TimingInfo | None = None
    execute: TimingInfo | None = None
    exit_code: int | None = None
    exception_info: ExceptionInfo | None = None
    """Serialized exception if the trial failed."""

    account_mapping: dict[str, str] = Field(default_factory=dict)
    """Account tag → account ID mapping for this trial."""
    regions: list[str] = Field(default_factory=list)
    """AWS regions this trial deployed to."""

    resource_results: dict[str, list] | None = None
    """Results from resource management operations (snapshot, verify, reset, cleanup)."""

    @property
    def success(self) -> bool:
        """True iff the trial completed without exception and exited 0 or left unset."""
        return self.exception_info is None and self.exit_code in (0, None)


class ScenarioJobResult(BaseModel):
    """Aggregated result of all scenario trials in one job."""

    id: UUID = Field(default_factory=uuid4)
    job_name: str
    job_dir: Path
    started_at: datetime
    finished_at: datetime | None = None
    n_total: int
    n_succeeded: int = 0
    n_failed: int = 0
    trial_results: list[ScenarioTrialResult] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """True iff every trial succeeded."""
        return self.n_failed == 0 and self.n_succeeded == self.n_total

    def get_verify_results(self) -> list:
        """Extract verify results from trial resource_results.

        Returns:
            Flattened list of AccountVerifyResult from all trials.
        """
        results: list[AccountVerifyResult] = []
        for trial_result in self.trial_results:
            if trial_result.resource_results and "verify" in trial_result.resource_results:
                results.extend(trial_result.resource_results["verify"])
        return results

    def get_reset_results(self) -> list:
        """Extract reset results from trial resource_results.

        Returns:
            Flattened list of ResetResult from all trials.
        """
        results: list[ResetResult] = []
        for trial_result in self.trial_results:
            if trial_result.resource_results and "reset" in trial_result.resource_results:
                results.extend(trial_result.resource_results["reset"])
        return results


class ScenarioHookEvent(BaseModel):
    """Event payload passed to scenario lifecycle hooks.

    ``config`` carries the trial config so observers can inspect scenario
    metadata, account mapping, etc. ``result`` is populated only for
    ``END`` events. ``exit_code`` and the textual ``error`` are derivable
    from ``result`` but kept as conveniences for terse observers.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event: ScenarioEvent
    scenario_name: str
    trial_name: str
    phase: ScenarioPhase
    config: ScenarioTrialConfig | None = None
    result: ScenarioTrialResult | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
