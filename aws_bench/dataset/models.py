"""Data models for dataset tasks and environment configuration."""

from enum import StrEnum


class RoleType(StrEnum):
    """Role type for a task."""

    AGENT = "agent"
    VERIFIER = "verifier"
    PRE_INVOKE = "pre-invoke"
    POST_INVOKE = "post-invoke"


class ScriptType(StrEnum):
    """Supported phase-script types.

    Values match the convention-based directory and entry-script names:
    ``ScriptType.PRE_INVOKE`` -> ``<task>/pre_invoke/pre_invoke.sh``.
    """

    PRE_INVOKE = "pre_invoke"
    POST_INVOKE = "post_invoke"
