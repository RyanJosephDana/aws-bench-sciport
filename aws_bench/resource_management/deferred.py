"""Per-run registry of resources whose deletion is *deferred* (eventually consistent).

Some resources cannot be deleted immediately even though a delete was issued.
A cleanup handler records such a resource here via :func:`mark_deferred` when it
detects that condition; reset and cleanup verification then exclude the recorded
identifiers for the current run (:func:`exclude_deferred`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

# (cfn_type, identifier) pairs deferred in the active run; None outside any scope.
_deferred: ContextVar[set[tuple[str, str]] | None] = ContextVar(
    "aws_bench_deferred_deletions", default=None
)


@contextlib.contextmanager
def deferred_scope() -> Iterator[set[tuple[str, str]]]:
    """Establish a fresh deferred-deletion set for the duration of a run.

    Tasks/threads spawned within the scope share the same set object, so a handler
    marking a resource in a worker thread and the verifier reading it later observe
    the same entries; concurrent runs in sibling asyncio tasks get independent sets.
    """
    entries: set[tuple[str, str]] = set()
    token = _deferred.set(entries)
    try:
        yield entries
    finally:
        _deferred.reset(token)


def mark_deferred(resource_type: str, identifier: str) -> None:
    """Record a resource whose deletion is deferred (no-op outside a deferred scope)."""
    entries = _deferred.get()
    if entries is not None:
        entries.add((resource_type, identifier))


def is_deferred(resource_type: str, identifier: str) -> bool:
    """Whether ``(resource_type, identifier)`` was deferred in the active run."""
    entries = _deferred.get()
    return entries is not None and (resource_type, identifier) in entries


def deferred_snapshot() -> frozenset[tuple[str, str]]:
    """Return an immutable copy of the active run's deferred pairs (empty if none)."""
    entries = _deferred.get()
    return frozenset(entries) if entries else frozenset()


def exclude_deferred(resources: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Return resources without entries deferred in the active run."""
    entries = _deferred.get()
    if not entries:
        return resources
    kept: dict[str, list[dict]] = {}
    for rtype, items in resources.items():
        remaining = [item for item in items if (rtype, item.get("Identifier", "")) not in entries]
        if remaining:
            kept[rtype] = remaining
    return kept
