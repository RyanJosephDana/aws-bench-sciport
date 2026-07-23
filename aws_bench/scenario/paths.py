"""On-disk paths for a containerized scenario."""

from __future__ import annotations

from pathlib import Path

from aws_bench.scenario.events import ScenarioPhase


class ScenarioPaths:
    """File paths for a scenario directory.

    Expected layout:
        ├── scenario.toml
        ├── scenario/
        │   ├── Dockerfile
        │   └── ...
        ├── deploy/
        │   └── deploy.sh
        ├── verify/        (optional)
        │   └── verify.sh
        ├── cleanup/       (optional)
        │   └── cleanup.sh
        └── reset/         (optional)
            └── reset.sh

    Each phase directory follows the ``<phase>/<phase>.sh`` convention, so a
    phase's script is located by name via ``phase_script_path``.
    """

    CONFIG_FILENAME = "scenario.toml"

    def __init__(self, scenario_dir: Path | str):
        """Initialize from a scenario directory path."""
        self.scenario_dir = Path(scenario_dir).resolve()

    @property
    def config_path(self) -> Path:
        """Path to scenario.toml."""
        return self.scenario_dir / self.CONFIG_FILENAME

    @property
    def build_context_dir(self) -> Path:
        """Path to the Docker build-context directory."""
        return self.scenario_dir / "scenario"

    @property
    def dockerfile_path(self) -> Path:
        """Path to the Dockerfile."""
        return self.build_context_dir / "Dockerfile"

    def phase_dir(self, phase: ScenarioPhase) -> Path:
        """Path to a phase's directory (``<scenario_dir>/<phase>/``)."""
        return self.scenario_dir / phase

    def phase_script_path(self, phase: ScenarioPhase) -> Path:
        """Path to a phase's entry script (``<phase>/<phase>.sh``).

        Returns the conventional location; the file need not exist. Callers
        test existence via ``has_phase_script``.
        """
        return self.phase_dir(phase) / f"{phase}.sh"

    def has_phase_script(self, phase: ScenarioPhase) -> bool:
        """Return whether a phase's entry script exists on disk."""
        return self.phase_script_path(phase).is_file()
