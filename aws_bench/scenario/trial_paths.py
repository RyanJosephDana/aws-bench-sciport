"""On-disk paths for a scenario job and its trials."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScenarioTrialPaths:
    """Directory layout for one scenario trial.

    ::

        trial_dir/
        ├── config.json     # ScenarioTrialConfig
        ├── result.json     # ScenarioTrialResult
        ├── trial.log       # per-trial log
        ├── exception.txt   # traceback, written only on failure
        ├── deploy/
        │   └── stdout.txt
        ├── verify/
        │   └── stdout.txt
        ├── cleanup/
        │   └── stdout.txt
        └── reset/
            └── stdout.txt
    """

    trial_dir: Path

    def mkdir(self) -> None:
        """Create the trial directory."""
        self.trial_dir.mkdir(parents=True, exist_ok=True)

    @property
    def config_path(self) -> Path:
        """Persisted ``ScenarioTrialConfig``."""
        return self.trial_dir / "config.json"

    @property
    def result_path(self) -> Path:
        """Persisted ``ScenarioTrialResult``."""
        return self.trial_dir / "result.json"

    @property
    def log_path(self) -> Path:
        """Per-trial log file."""
        return self.trial_dir / "trial.log"

    @property
    def exception_path(self) -> Path:
        """Traceback file, written only when the trial fails."""
        return self.trial_dir / "exception.txt"

    def phase_dir(self, phase: str) -> Path:
        """Host-side output directory for one phase."""
        return self.trial_dir / phase

    def phase_stdout_path(self, phase: str) -> Path:
        """Combined stdout+stderr path for one phase."""
        return self.phase_dir(phase) / "stdout.txt"


@dataclass(frozen=True)
class ScenarioJobPaths:
    """Directory layout for one scenario job.

    ::

        job_dir/
        ├── config.json      # ScenarioJobConfig
        ├── result.json      # ScenarioJobResult
        ├── job.log          # job-level log
        └── <scenario_name>/ # one ScenarioTrialPaths per trial
    """

    job_dir: Path

    def mkdir(self) -> None:
        """Create the job directory."""
        self.job_dir.mkdir(parents=True, exist_ok=True)

    @property
    def config_path(self) -> Path:
        """Persisted ``ScenarioJobConfig``."""
        return self.job_dir / "config.json"

    @property
    def result_path(self) -> Path:
        """Persisted ``ScenarioJobResult``."""
        return self.job_dir / "result.json"

    @property
    def log_path(self) -> Path:
        """Job-level log file."""
        return self.job_dir / "job.log"

    def trial_paths(self, trial_name: str) -> ScenarioTrialPaths:
        """Trial paths under this job, scoped to ``trial_name``."""
        return ScenarioTrialPaths(trial_dir=self.job_dir / trial_name)
