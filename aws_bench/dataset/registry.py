"""aws-bench-extended registry models.

Each dataset entry in ``registry.json`` carries:
- ``tasks`` — task descriptors.
- ``scenarios`` — aws-bench-specific extension, structured the same way.

The :class:`AwsBenchRegistry` loader handles both local files and remote
URLs, and :class:`RegistryValidator` enforces schema, SemVer-shape,
per-dataset uniqueness, cross-dataset consistency, and task-to-scenario
reference checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import requests
from harbor.models.registry import DatasetSpec, RegistryTaskId
from pydantic import BaseModel, Field, ValidationError

from aws_bench.constants import REGISTRY_CACHE_DIR
from aws_bench.dataset.exceptions import RegistryValidationError
from aws_bench.dataset.source_ids import (
    GitInstructionId,
    GitScenarioId,
    InstructionIdType,
    LocalInstructionId,
    LocalScenarioId,
)
from aws_bench.logging.logger import get_logger

logger = get_logger(__name__)

# Canonical SemVer 2.0.0 regular expression. Source: https://semver.org/.
# Validation enforces the shape only.
SEMVER_REGEX = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

# Matches MAJOR.MINOR (no patch). Used by AwsBenchRegistry.get_dataset_spec
# to expand "1.0" into "highest patch within 1.0.x".
PARTIAL_VERSION_REGEX = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)$")


_REGISTRY_CACHE_TTL_SECONDS = 3600


def _registry_cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode()).hexdigest()[:12]
    return REGISTRY_CACHE_DIR / f"registry-{h}.json"


class RegistryScenarioId(RegistryTaskId):
    """Scenario descriptor in the aws-bench-extended registry.

    Has a ``{name, git_url, git_commit_id, path}`` shape and adds
    ``to_source_id()``, which returns branded scenario fetch ids so the
    scenario domain stays distinct from task types.
    """

    def to_source_id(self) -> GitScenarioId | LocalScenarioId:
        """Map this scenario to its branded fetch primitive.

        Scenarios with a ``git_url`` become ``GitScenarioId`` so they can be
        fed directly into ``TaskClient.download_tasks`` (which dispatches on
        ``isinstance(GitTaskId)``). Scenarios without a ``git_url`` are treated
        as local-only and become ``LocalScenarioId``.
        """
        if self.git_url is not None:
            return GitScenarioId(
                git_url=self.git_url,
                git_commit_id=self.git_commit_id,
                path=self.path,
            )
        return LocalScenarioId(path=self.path)


class RegistryExtraInstructionPathId(BaseModel):
    """A registry-declared extra-instruction file (local or git-fetched).

    Referenced purely by file ``path`` (no ``name``). ``path`` names the file;
    the fetch unit is its parent directory (the unit
    ``TaskClient.download_tasks`` understands). That the path resolves to a
    real file is checked at resolve time (``resolve_instruction_paths``),
    against the fetched filesystem — not guessed at parse time.
    """

    path: Path
    git_url: str | None = None
    git_commit_id: str | None = None

    def to_source_id(self) -> InstructionIdType:
        """Map to a branded fetch primitive, keyed on the file's parent dir.

        The fetch unit is ``path.parent`` (``download_tasks`` copies a
        directory tree), so the named file is indexed out of the fetched
        directory afterward. A registry instruction may be git-referenced or
        a local path.
        """
        target = self.path.parent
        if self.git_url is not None:
            return GitInstructionId(
                git_url=self.git_url,
                git_commit_id=self.git_commit_id,
                path=target,
            )
        return LocalInstructionId(path=target)


class AwsBenchDatasetSpec(DatasetSpec):
    """A single dataset entry in the aws-bench-extended registry.

    Carries ``name``, ``version``, ``description``, ``tasks``, and ``metrics``,
    plus ``scenarios`` and ``extra_instruction_paths``. ``description`` is
    required: every dataset entry must declare one.
    """

    scenarios: list[RegistryScenarioId] = Field(default_factory=list)
    extra_instruction_paths: list[RegistryExtraInstructionPathId] = Field(default_factory=list)


class AwsBenchRegistry(BaseModel):
    """Top-level registry container loaded from a ``registry.json`` file.

    Use ``from_path`` or ``from_url`` to construct. Either raises
    ``RegistryValidationError`` on JSON/schema problems, presenting one
    consistent error type to callers.
    """

    path: Path | None = None
    url: str | None = None
    datasets: list[AwsBenchDatasetSpec]

    @classmethod
    def from_path(cls, path: Path) -> "AwsBenchRegistry":
        """Load and validate a registry from a local JSON file."""
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise RegistryValidationError(f"Invalid JSON in registry file {path}: {e}") from e

        try:
            datasets = [AwsBenchDatasetSpec.model_validate(row) for row in data]
        except ValidationError as e:
            raise RegistryValidationError(f"Invalid registry schema in {path}:\n{e}") from e

        return cls(path=path, datasets=datasets)

    @classmethod
    def from_url(cls, url: str) -> "AwsBenchRegistry":
        """Fetch ``registry.json`` from *url* with a 1-hour disk-cache TTL.

        Cache lives at ``REGISTRY_CACHE_DIR``. Cache hits are re-validated
        via Pydantic. Invalid responses are NOT written to the cache,
        so a transient bad response doesn't poison the next hour.
        """
        cache_path = _registry_cache_path(url)
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < _REGISTRY_CACHE_TTL_SECONDS:
                try:
                    datasets = [
                        AwsBenchDatasetSpec.model_validate(row)
                        for row in json.loads(cache_path.read_text())
                    ]
                except (json.JSONDecodeError, ValidationError) as e:
                    raise RegistryValidationError(
                        f"Invalid cached registry at {cache_path}: {e}"
                    ) from e
                return cls(url=url, path=None, datasets=datasets)

        session = requests.Session()
        try:
            resp = session.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RegistryValidationError(f"Failed to fetch registry from {url}: {e}") from e
        try:
            datasets = [AwsBenchDatasetSpec.model_validate(row) for row in json.loads(resp.text)]
        except (json.JSONDecodeError, ValidationError) as e:
            raise RegistryValidationError(f"Invalid registry from {url}: {e}") from e
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(resp.text)
        return cls(url=url, path=None, datasets=datasets)

    def get_dataset_spec(self, name: str, version: str | None = None) -> AwsBenchDatasetSpec:
        """Look up a dataset by name and version.

        - ``version=None``: pick highest version overall via
          ``resolve_version``.
        - Three-part SemVer (``"1.0.3"``): exact string match.
        - Two-part partial (``"1.0"``): pick the highest ``1.0.x`` patch
          within the band (uses ``startswith("1.0.")`` so ``"1.10.0"`` is
          correctly excluded).

        Raises ``RegistryValidationError`` with the list of available
        datasets or versions on miss. Partial-miss errors list versions
        in the same band, not the whole registry.
        """
        from harbor.registry.client.base import resolve_version

        by_version: dict[str, AwsBenchDatasetSpec] = {
            ds.version: ds for ds in self.datasets if ds.name == name
        }
        if not by_version:
            available = sorted({ds.name for ds in self.datasets})
            raise RegistryValidationError(
                f"Dataset {name!r} not found in registry. Available: {available}"
            )

        if version is None:
            resolved = resolve_version(list(by_version.keys()))
        elif PARTIAL_VERSION_REGEX.match(version):
            prefix = f"{version}."
            candidates = [v for v in by_version if v.startswith(prefix)]
            if not candidates:
                raise RegistryValidationError(
                    f"Dataset {name!r} has no version matching {version!r}. "
                    f"Available versions: {sorted(by_version.keys())}"
                )
            resolved = resolve_version(candidates)
        else:
            resolved = version  # exact three-part SemVer

        if resolved not in by_version:
            raise RegistryValidationError(
                f"Dataset {name!r} version {resolved!r} not found. "
                f"Available versions: {sorted(by_version.keys())}"
            )
        return by_version[resolved]


class RegistryValidator:
    """Validates an ``AwsBenchRegistry`` at load time and dataset-select time.

    Load-time checks enforced by ``validate()``:
      - SemVer 2.0.0 shape on every dataset ``version`` (strict).
      - Unique scenario ``name`` within each dataset (strict).
      - Unique task ``name`` within each dataset (strict).
      - Cross-dataset scenario consistency — same name, same origin
        (warning, not raise).

    Task-to-scenario reference checking happens post-fetch — after each
    scenario's manifest has been parsed and its canonical name is known
    — and is therefore the responsibility of the run-flow, not this
    validator. Registry-shape errors (parse, uniqueness, version) still
    belong here.

    Pydantic schema validation already ran during ``AwsBenchRegistry``
    construction; this class layers the cross-row checks on top.
    """

    def __init__(self, registry: AwsBenchRegistry) -> None:
        """Initialize with the registry to validate."""
        self._registry = registry

    # --- Load-time validations (run once when the registry is loaded) -------

    def validate(self) -> None:
        """Run the load-time SemVer + uniqueness (strict) + consistency (warning) checks.

        Meant to be called once, right after registry load and before
        any dataset lookup.
        """
        self._check_semver_versions()
        self._check_unique_scenario_names_per_dataset()
        self._check_unique_task_names_per_dataset()
        self._warn_cross_dataset_scenario_consistency()

    def _check_semver_versions(self) -> None:
        """Every dataset's ``version`` must follow the SemVer 2.0.0 shape.

        aws-bench requires strict three-part SemVer: PEP 440 shorthand
        (e.g. ``"1.0"``) and the literal ``"head"`` are both rejected at
        this layer. Downstream version lookup
        (``AwsBenchRegistry.get_dataset_spec``) does exact string match
        on three-part versions; partial ``MAJOR.MINOR`` is supported as
        a query shape only.
        """
        invalid: list[tuple[str, str]] = []
        for ds in self._registry.datasets:
            if not SEMVER_REGEX.match(ds.version):
                invalid.append((ds.name, ds.version))
        if invalid:
            entries = ", ".join(f"{n}@{v!r}" for n, v in invalid)
            raise RegistryValidationError(
                f"Dataset version(s) are not valid SemVer 2.0.0: {entries}. "
                f"See https://semver.org/ — expected MAJOR.MINOR.PATCH "
                f"(e.g. '0.1.0')."
            )

    def _check_unique_scenario_names_per_dataset(self) -> None:
        for ds in self._registry.datasets:
            counts: dict[str, int] = defaultdict(int)
            for s in ds.scenarios:
                counts[s.name] += 1
            dupes = sorted(n for n, c in counts.items() if c > 1)
            if dupes:
                raise RegistryValidationError(
                    f"Dataset {ds.name}@{ds.version}: duplicate scenario name(s): {dupes}"
                )

    def _check_unique_task_names_per_dataset(self) -> None:
        for ds in self._registry.datasets:
            counts: dict[str, int] = defaultdict(int)
            for t in ds.tasks:
                counts[t.name] += 1
            dupes = sorted(n for n, c in counts.items() if c > 1)
            if dupes:
                raise RegistryValidationError(
                    f"Dataset {ds.name}@{ds.version}: duplicate task name(s): {dupes}"
                )

    def _warn_cross_dataset_scenario_consistency(self) -> None:
        """Same scenario name across datasets SHOULD have the same origin.

        Emits a ``logger.warning`` (does not raise) when scenario names
        appearing in multiple datasets point to divergent
        ``(git_url, git_commit_id, path)`` tuples. Divergence is allowed
        but usually unintentional.
        """
        by_name: dict[str, list[tuple[str, RegistryScenarioId]]] = defaultdict(list)
        for ds in self._registry.datasets:
            for s in ds.scenarios:
                by_name[s.name].append((f"{ds.name}@{ds.version}", s))

        for name, occurrences in by_name.items():
            if len(occurrences) < 2:
                continue
            first_ref, first_spec = occurrences[0]
            first_origin = (
                first_spec.git_url,
                first_spec.git_commit_id,
                str(first_spec.path),
            )
            divergent: list[str] = []
            for ds_ref, s in occurrences[1:]:
                origin = (s.git_url, s.git_commit_id, str(s.path))
                if origin != first_origin:
                    divergent.append(ds_ref)
            if divergent:
                logger.warning(
                    "Scenario %r has divergent definitions across datasets: "
                    "first seen in %s, divergent in %s. "
                    "Allowed but likely unintentional.",
                    name,
                    first_ref,
                    divergent,
                )
