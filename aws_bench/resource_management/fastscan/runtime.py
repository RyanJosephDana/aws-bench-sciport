"""Shared lister runtime helpers: the ``collect`` paginate-and-pull helper + ``RETRY_CONFIG``."""

from __future__ import annotations

from typing import Any

from botocore.config import Config

from aws_bench.logging.logger import get_logger

logger = get_logger(__name__)

# Bounded retries + a tight CONNECT timeout so a lister whose service has no endpoint in the
# region (cloudhsm, importexport, …) fails in ~2s instead of stalling the sweep for ~35s —
# these set the wall-clock under the concurrent sweep. 2s is coverage-safe: live endpoints
# handshake in ~150-220ms (measured), a ~10x margin, so a real service is never false-killed;
# only the data transfer is governed by the generous read_timeout. (connect_timeout caps the
# TCP+TLS handshake only, NOT a slow-paginating real API.)
RETRY_CONFIG = Config(
    retries={"max_attempts": 4, "mode": "adaptive"},
    connect_timeout=2,
    read_timeout=20,
    max_pool_connections=1,
)

# Hard cap on pages pulled per lister. Without it a lister against a very large service does an
# unbounded ``list(paginate())``; since a lister in-flight at the sweep deadline drains at
# pool-exit (running threads can't be cancelled), one such lister can push the whole scan past
# its wall-clock budget (in-Lambda, past the function timeout, losing the entire result). At the
# default page size this covers
# tens of thousands of resources per lister; hitting it is logged, never silently truncated.
MAX_PAGES_PER_LISTER = 10


def walk_path(page: dict[str, Any], path: str) -> list[Any]:
    """Resolve a dotted result path to a flat list of items (missing keys yield an empty list)."""
    nodes: list[Any] = [page]
    for segment in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            value = node.get(segment)
            if value is None:
                continue
            if isinstance(value, list):
                nxt.extend(value)
            else:
                nxt.append(value)
        nodes = nxt
    return nodes


def _resolve_field(item: dict[str, Any], field: str) -> Any:
    """Resolve a possibly-nested (dotted) field to a scalar value."""
    value: Any = item
    for segment in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


def _paginate(client: Any, op: str) -> list[dict[str, Any]]:
    """Return pages for ``op`` (paginator when available), capped at MAX_PAGES_PER_LISTER."""
    if not client.can_paginate(op):
        return [getattr(client, op)()]
    pages: list[dict[str, Any]] = []
    for page in getattr(client, "get_paginator")(op).paginate():
        pages.append(page)
        if len(pages) >= MAX_PAGES_PER_LISTER:
            logger.debug(
                "Lister %s hit the %d-page cap; results truncated for this scan",
                op,
                MAX_PAGES_PER_LISTER,
            )
            break
    return pages


def collect(
    client: Any,
    op: str,
    path: str,
    id_field: str | None,
    status_field: str | None = None,
    status_filter: list[str] | None = None,
    status_exclude: list[str] | None = None,
) -> list[str]:
    """Paginate ``op`` and pull ``id_field`` from each item under ``path`` (optional status).

    ``status_field`` supports dotted paths (e.g. ``State.Name``) so nested fields
    like ``ec2.Instance.State.Name`` can be filtered without a custom lister.

    Two mutually-independent status filters are supported (both need ``status_field``):

    - ``status_filter`` is an *allowlist* — keep only items whose status is in it.
    - ``status_exclude`` is a *blocklist* — drop only items whose status is in it.

    If both are given, an item must pass both (in the allowlist AND not in the blocklist).
    """
    out: list[str] = []
    for page in _paginate(client, op):
        for item in walk_path(page, path):
            if status_field and (status_filter is not None or status_exclude is not None):
                status = _resolve_field(item, status_field) if isinstance(item, dict) else None
                if status_filter is not None and status not in status_filter:
                    continue
                if status_exclude is not None and status in status_exclude:
                    continue
            if id_field is None:
                value = item
            elif isinstance(item, dict):
                value = item.get(id_field)
            else:
                value = item
            if value is not None:
                out.append(value if isinstance(value, str) else str(value))
    return out
