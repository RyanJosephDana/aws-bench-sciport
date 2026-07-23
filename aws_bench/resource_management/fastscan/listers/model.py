"""The one lister model: a ``(service, op)`` identity + a ``run`` that returns resource ids."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from aws_bench.resource_management.fastscan.runtime import RETRY_CONFIG, collect


class SessionLike(Protocol):
    """The subset of ``boto3.Session`` a lister uses: a ``client(service, ...)`` method."""

    def client(self, *args: Any, **kwargs: Any) -> Any:
        """Return a service client (first arg is the service name)."""
        ...


# A lister returns a flat list of resource ids/ARNs (or dict records the scanner normalizes).
ListerRun = Callable[[SessionLike], list]


def _paginate_and_pull(
    service: str,
    method: str,
    result_path: str,
    id_field: str | None,
    status_field: str | None,
    status_filter: tuple[str, ...] | None,
    status_exclude: tuple[str, ...] | None,
    session: SessionLike,
) -> list[str]:
    """Paginate ``method``, walk ``result_path``, pull ``id_field`` (the data-lister body)."""
    return collect(
        session.client(service, config=RETRY_CONFIG),
        method,
        result_path,
        id_field,
        status_field,
        list(status_filter) if status_filter is not None else None,
        list(status_exclude) if status_exclude is not None else None,
    )


@dataclass(frozen=True)
class Lister:
    """One native-API lister: ``service``, CamelCase ``op``, ``run``, optional ``cfn_type`` pin."""

    service: str
    op: str
    run: ListerRun
    cfn_type: str | None = None

    @classmethod
    def from_row(
        cls,
        *,
        service: str,
        op: str,
        method: str,
        result_path: str,
        id_field: str | None,
        cfn_type: str | None = None,
        status_field: str | None = None,
        status_filter: tuple[str, ...] | None = None,
        status_exclude: tuple[str, ...] | None = None,
    ) -> Lister:
        """Build a data-defined lister (one table row) whose ``run`` paginates ``method``."""
        run = partial(
            _paginate_and_pull,
            service,
            method,
            result_path,
            id_field,
            status_field,
            status_filter,
            status_exclude,
        )
        return cls(service=service, op=op, run=run, cfn_type=cfn_type)
