"""Scan-result model + the sweep's worker cap for fast service-API-native discovery."""

from __future__ import annotations

from dataclasses import dataclass, field

# Concurrency cap for the lister sweep. Listers are I/O-bound (one AWS API call each), so a
# wide pool is fine; capped to avoid exhausting client/socket limits with 1500+ listers.
MAX_LISTER_WORKERS = 24


@dataclass(frozen=True)
class FastScanResult:
    """Raw scan of one account/region, keyed by ``"<service>:<Op>"``: discovered/failed/empty."""

    discovered: dict[str, list[str]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    empty: set[str] = field(default_factory=set)

    @property
    def total_resources(self) -> int:
        """Total discovered resource ids across all listers."""
        return sum(len(v) for v in self.discovered.values())
