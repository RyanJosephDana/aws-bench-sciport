"""Scenario lifecycle vocabulary: phases and trial events.

Pure enums with no model dependencies — the leaf of the scenario
orchestration model graph. ``job_config`` and ``results`` build on these.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class ScenarioPhase(StrEnum):
    """The script phases authored inside a scenario.

    Each value is also the on-disk directory name (``deploy/``, ``verify/``,
    ``cleanup/``, ``reset/``), the entry script stem (``deploy.sh``), and the
    in-container log subdir (``/logs/<phase>/``). Subclassing ``str`` lets the
    value flow into ``Path`` joins and shell command strings without explicit
    conversion.
    """

    DEPLOY = "deploy"
    VERIFY = "verify"
    CLEANUP = "cleanup"
    RESET = "reset"

    @property
    def gerund(self) -> str:
        """Human-facing present-participle form for log lines."""
        return {
            ScenarioPhase.DEPLOY: "Deploying...",
            ScenarioPhase.VERIFY: "Verifying...",
            ScenarioPhase.CLEANUP: "Cleaning up...",
            ScenarioPhase.RESET: "Resetting...",
        }[self]


class ScenarioEvent(Enum):
    """Lifecycle events for a single scenario trial.

    Phase-transition events are named ``<RESOURCE>_START`` so a generic
    UI can render any step without knowing the resource type.
    ``ENVIRONMENT_START`` fires before container build/start,
    ``PHASE_START`` before the phase script invocation. ``END`` and
    ``CANCEL`` close the lifecycle.
    """

    START = "start"
    ENVIRONMENT_START = "environment_start"
    PHASE_START = "phase_start"
    END = "end"
    CANCEL = "cancel"
