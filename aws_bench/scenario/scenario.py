"""The Scenario class — bundles scenario_dir + paths + parsed manifest."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harbor.tasks.client import TaskClient

from aws_bench.constants import SCENARIO_CACHE_DIR
from aws_bench.scenario.config import ScenarioManifest
from aws_bench.scenario.events import ScenarioPhase
from aws_bench.scenario.hashing import compute_scenario_hash
from aws_bench.scenario.locator import ScenarioConfig
from aws_bench.scenario.paths import ScenarioPaths


class Scenario:
    """Represent a containerized scenario with the following layout.

    ::

    ├── scenario.toml
    ├── scenario/
    │   └── Dockerfile
    ├── deploy/
    │   └── deploy.sh
    ├── verify/                 (optional)
    │   └── verify.sh
    ├── cleanup/                (optional)
    │   └── cleanup.sh
    └── reset/                  (optional)
        └── reset.sh

    Holds the parsed manifest and validated on-disk layout. Where the directory
    came from (local path vs git ref) is the descriptor's concern
    (``ScenarioConfig``).
    """

    def __init__(self, scenario_dir: Path | str) -> None:
        """Parse scenario.toml and validate the on-disk layout.

        Raises:
            FileNotFoundError: A required artifact is missing (Dockerfile,
                deploy.sh) or an optional phase directory is half-built
                (verify/ exists but verify.sh missing — same for cleanup/).
            pydantic.ValidationError: scenario.toml schema violation.
            tomllib.TOMLDecodeError: scenario.toml is not valid TOML.
        """
        self._scenario_dir = Path(scenario_dir).resolve()
        self.paths = ScenarioPaths(self._scenario_dir)
        self.manifest = ScenarioManifest.model_validate_toml(self.paths.config_path.read_bytes())
        self.name = self.manifest.scenario.name
        self._validate_layout()

    @classmethod
    async def from_config(cls, config: ScenarioConfig) -> Scenario:
        """Construct a Scenario from a git or local scenario reference.

        A git scenario is downloaded under ``SCENARIO_CACHE_DIR`` (or the
        descriptor's ``download_dir`` override) and read from the resulting
        path; a local scenario is read from its literal path. ``download_tasks``
        is idempotent, so a prior batched ``cache_scenarios`` is a cache hit and
        this works without one.
        """
        if config.is_git():
            scenario_dir = (
                await TaskClient().download_tasks(
                    task_ids=[config.to_scenario_id()],
                    overwrite=config.overwrite,
                    output_dir=config.download_dir or SCENARIO_CACHE_DIR,
                )
            ).paths[0]
            return cls(scenario_dir)
        return cls(config.path)

    @staticmethod
    def is_valid_dir(scenario_dir: Path | str) -> bool:
        """Structural discovery filter: True iff a scenario.toml exists.

        Does not validate contents — full validation (layout, TOML parse,
        schema) runs at construction, so a scenario.toml-bearing dir that is
        otherwise malformed fails loud and named during listing.
        """
        return ScenarioPaths(scenario_dir).config_path.is_file()

    def has_phase_script(self, phase: ScenarioPhase) -> bool:
        """Return whether a phase's entry script exists on disk."""
        return self.paths.has_phase_script(phase)

    @property
    def scenario_dir(self) -> Path:
        """Resolved scenario directory."""
        return self._scenario_dir

    @property
    def hash(self) -> str:
        """SHA256 hash of scenario directory contents."""
        return compute_scenario_hash(self._scenario_dir)

    def _validate_layout(self) -> None:
        """Validate the on-disk scenario layout at construction time.

        A scenario must be buildable and deployable: a Dockerfile build context
        and a deploy phase script. The verify and cleanup phases are optional —
        whether each runs is answered by ``has_phase_script`` at run time, so a
        scenario with no verify.sh / cleanup.sh is valid (that phase is skipped).
        """
        if not self.paths.dockerfile_path.is_file():
            raise FileNotFoundError(f"Missing Dockerfile: {self.paths.dockerfile_path}")
        if not self.has_phase_script(ScenarioPhase.DEPLOY):
            raise FileNotFoundError(
                f"Missing deploy.sh: {self.paths.phase_script_path(ScenarioPhase.DEPLOY)}"
            )


@dataclass(frozen=True)
class ScenarioRegionMap:
    """Maps each scenario name to its declared regions."""

    _by_name: dict[str, list[str]]

    @classmethod
    def from_scenarios(cls, scenarios: Iterable[Scenario]) -> ScenarioRegionMap:
        """Build the map from scenario manifests, keyed by scenario name."""
        return cls({s.name: list(s.manifest.scenario.regions) for s in scenarios})

    def regions_for(self, scenario_name: str) -> list[str]:
        """Return the declared regions for a scenario, or raise if unknown."""
        try:
            return self._by_name[scenario_name]
        except KeyError:
            raise KeyError(f"No declared regions for scenario {scenario_name!r}") from None
