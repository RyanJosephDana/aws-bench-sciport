"""``ScenarioConfig`` — the source descriptor for a scenario.

A small, pure descriptor of *where a scenario comes from* (local path or git
reference), distinct from the materialized
:class:`~aws_bench.scenario.scenario.Scenario` that holds the parsed manifest.
It depends only on ``pydantic`` and the branded source ids.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from aws_bench.dataset.source_ids import GitScenarioId, LocalScenarioId, ScenarioIdType


class ScenarioConfig(BaseModel):
    """Source descriptor for a scenario.

    One shape covers local-only scenarios (no git fields) and git-addressable
    scenarios (registry-sourced). The materialized ``Scenario`` object holds
    the parsed manifest at runtime; this type only describes where the
    scenario directory comes from.

    Frozen so it is hashable: ``get_scenarios`` keys its result by the
    descriptor, and ``ScenarioTrialConfig`` persists it verbatim.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    git_url: str | None = None
    git_commit_id: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _check_git_commit_requires_url(self) -> ScenarioConfig:
        if self.git_commit_id is not None and self.git_url is None:
            raise ValueError("'git_commit_id' requires 'git_url' to be set.")
        return self

    def is_git(self) -> bool:
        """Whether this is a git-addressable scenario.

        A scenario with no ``git_url`` is local.
        """
        return self.git_url is not None

    def to_scenario_id(self) -> ScenarioIdType:
        """Convert to a branded scenario id for cache-path resolution and fetching."""
        if self.is_git():
            # Re-guard for the type checker (is_git() guarantees this); an
            # explicit raise keeps the invariant under ``python -O`` where
            # assert vanishes.
            if self.git_url is None:
                raise ValueError("git_url must be set for a git scenario.")
            return GitScenarioId(
                git_url=self.git_url,
                git_commit_id=self.git_commit_id,
                path=self.path,
            )
        return LocalScenarioId(path=self.path)

    def get_local_path(self) -> Path:
        """Resolve the on-disk path.

        Local descriptors return the literal path; remote descriptors return
        the cache path from ``GitScenarioId.get_local_path()``, which points
        under aws-bench's ``SCENARIO_CACHE_DIR``.
        """
        return self.to_scenario_id().get_local_path()
