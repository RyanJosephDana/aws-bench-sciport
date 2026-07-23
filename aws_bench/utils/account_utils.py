"""Utility functions for account operations."""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatch
from typing import TypeVar

from aws_bench.account_management.models import ScenarioAccount

T = TypeVar("T")


def group_accounts_by_scenario(accounts: list[ScenarioAccount]) -> dict[str, list[ScenarioAccount]]:
    """Group accounts by scenario name.

    Args:
        accounts: List of ScenarioAccount objects

    Returns:
        Dictionary mapping scenario name to list of accounts in that scenario
    """
    by_scenario: dict[str, list[ScenarioAccount]] = {}
    for acct in accounts:
        by_scenario.setdefault(acct.scenario_name, []).append(acct)
    return by_scenario


def filter_by_scenario_name(
    items: list[T],
    name_of: Callable[[T], str],
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[T]:
    """Filter items by scenario-name fnmatch globs.

    Include patterns gate first (item kept only if at least one matches), then
    exclude patterns remove any remaining matches. Used by both the dataset's
    scenario filter and the env command's account filter so both sides honor
    the same glob semantics.

    Args:
        items: Items to filter (e.g. ``ScenarioConfig``s or ``ScenarioAccount``s).
        name_of: Extracts the scenario name from each item.
        include: Optional fnmatch globs; item kept only if its name matches one.
        exclude: Optional fnmatch globs; item dropped if its name matches one.

    Returns:
        Filtered list (input order preserved).
    """
    if include:
        items = [it for it in items if any(fnmatch(name_of(it), p) for p in include)]
    if exclude:
        items = [it for it in items if not any(fnmatch(name_of(it), p) for p in exclude)]
    return items
