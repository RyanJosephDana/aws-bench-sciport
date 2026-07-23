"""Domain-branded id types for registry descriptors.

A registry descriptor (scenario / extra-instruction / uv-script metric) is
not a "task", so each gets its own branded id type rather than a raw
``GitTaskId`` / ``LocalTaskId``. The branded ids remain ``isinstance`` of the
task-id primitives the download client dispatches on, so they feed through the
fetch machinery unchanged while naming the type for its own domain.

Scenarios (``to_scenario_id``) and extra-instructions (``to_source_id``) each
convert through a ``Git* | Local*`` union, so both arms are branded — a
scenario must never degrade into a raw ``LocalTaskId``. Each ``Git*`` id
overrides ``get_local_path()`` to resolve under its own cache directory; each
``Local*`` id resolves to its literal on-disk path (no override needed).

Metrics have no local branded type: only git uv-script metrics need a fetch
id (``GitUVScriptId``). Declarative metrics and local uv-scripts stay plain
``MetricConfig`` — a local uv-script's ``script_path`` is read directly and
never becomes an id.
"""

from pathlib import Path

import shortuuid
from harbor.models.task.id import GitTaskId, LocalTaskId

from aws_bench.constants import INSTRUCTION_CACHE_DIR, METRIC_CACHE_DIR, SCENARIO_CACHE_DIR


class GitScenarioId(GitTaskId):
    """A git-referenced scenario descriptor, cached under SCENARIO_CACHE_DIR."""

    def get_local_path(self) -> Path:
        """Resolve the cache path for this scenario under SCENARIO_CACHE_DIR."""
        return SCENARIO_CACHE_DIR / shortuuid.uuid(str(self)) / self.path.name


class LocalScenarioId(LocalTaskId):
    """A local scenario descriptor; resolves to its literal on-disk path."""


ScenarioIdType = GitScenarioId | LocalScenarioId
"""A scenario reference: git-addressable or local. Mirrors Harbor's ``TaskIdType``."""


class GitInstructionId(GitTaskId):
    """A git-referenced extra-instruction descriptor, cached under INSTRUCTION_CACHE_DIR."""

    def get_local_path(self) -> Path:
        """Resolve the cache path for this instruction under INSTRUCTION_CACHE_DIR."""
        return INSTRUCTION_CACHE_DIR / shortuuid.uuid(str(self)) / self.path.name


class LocalInstructionId(LocalTaskId):
    """A local extra-instruction descriptor; resolves to its literal on-disk path."""


InstructionIdType = GitInstructionId | LocalInstructionId
"""An instruction reference: git-addressable or local. Mirrors ``ScenarioIdType``."""


class GitUVScriptId(GitTaskId):
    """A git-referenced uv-script metric descriptor, cached under METRIC_CACHE_DIR.

    Only git uv-script metrics ever get a branded id; declarative metrics and
    local uv-scripts stay plain ``MetricConfig`` (a local uv-script's
    ``script_path`` is read directly).
    """

    def get_local_path(self) -> Path:
        """Resolve the cache path for this metric script under METRIC_CACHE_DIR."""
        return METRIC_CACHE_DIR / shortuuid.uuid(str(self)) / self.path.name
