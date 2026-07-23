"""Fast-scan manager: run the native-API sweep and project it onto CFN types.

Sits between the raw :class:`~.engine.FastResourceScanner` sweep (``service:op`` →
ids) and the CloudControl-shaped :class:`~..ccapi.models.ScanResult` the rest of
resource-management consumes. Lives in the ``fastscan`` package so the Lambda-side
:class:`~.lambda_scanner.LambdaScanner` and the host-side ``make_scanner`` factory both
import it as a sibling — neither has to reach back into ``resource_management.scanner``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import boto3

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import ScanResult
from aws_bench.resource_management.ccapi.type_registry import TypeRegistry
from aws_bench.resource_management.fastscan.engine import FastResourceScanner
from aws_bench.resource_management.fastscan.listers import cfn_type_pins
from aws_bench.resource_management.fastscan.models import FastScanResult
from aws_bench.resource_management.fastscan.type_map import (
    cfn_type_resource_nouns,
    cfn_type_service_endpoint,
    collides_with_sibling_type,
    lister_op_noun,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class _PinnedByType:
    """Discoveries/failures/empties for listers pinning an exact CFN type, keyed by that type."""

    detected: dict[str, list[str]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    empty: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _NounIndex:
    """Non-overridden discoveries/failures/empties indexed by ``(service, lister_op_noun)``."""

    discovered: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    failed: dict[tuple[str, str], str] = field(default_factory=dict)
    empty: set[tuple[str, str]] = field(default_factory=set)
    services: set[str] = field(default_factory=set)
    by_endpoint: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)
    # ``(service, noun)`` keys consumed by a requested type, so the catch-all skips them.
    attributed: set[tuple[str, str]] = field(default_factory=set)
    attributed_failed: set[tuple[str, str]] = field(default_factory=set)


def _unused_scan_fn(_types: list[str], _max_workers: int) -> ScanResult:
    """Registry ``scan_fn`` that must never fire — fast-scan uses only ``list_types``."""
    raise RuntimeError(
        "fast-scan must not generate the CCAPI skip-list; it uses the full CFN registry"
    )


class FastScanManager:
    """Drop-in for :class:`CloudControlManager`: native-API discovery over the full CFN registry."""

    def __init__(self, session: boto3.Session, region_name: str | None = None) -> None:
        """Initialize with a boto3 session and optional region."""
        self._session = session
        self._region_name = region_name or session.region_name
        self._scanner = FastResourceScanner(session)
        # Resolves the type universe from the CFN registry only (list_types — no per-type scan,
        # no skip-list generation). Region-pinned so list_types runs in the scan region.
        self._registry = TypeRegistry(session, scan_fn=_unused_scan_fn, region_name=region_name)
        self._scannable_types: list[str] | None = None

    def get_scannable_types(self) -> list[str]:
        """Full CFN registry type universe (cheap ``list_types``; no CCAPI skip-list scan)."""
        if self._scannable_types is None:
            self._scannable_types = list(self._registry.list_all_resource_types())
        return list(self._scannable_types)

    def scan_resources(
        self,
        resource_types: list[str] | None = None,
        region: str | None = None,
    ) -> ScanResult:
        """Scan ``region`` and project onto ``resource_types`` (None = full CFN registry)."""
        region = region or self._region_name or "us-east-1"
        scannable_types = (
            resource_types if resource_types is not None else self.get_scannable_types()
        )
        raw = self._scanner.scan(region)
        return self.project(raw, scannable_types)

    @staticmethod
    def project(raw: FastScanResult, scannable_types: list[str]) -> ScanResult:
        """Project a raw ``service:op`` :class:`FastScanResult` onto CFN types (CCAPI shape)."""
        pins = cfn_type_pins()
        pinned = FastScanManager._partition_overrides(raw, pins)
        by_noun = FastScanManager._index_by_service_noun(raw, pins)

        result = FastScanManager._attribute_types_by_noun(scannable_types, by_noun)
        FastScanManager._apply_type_overrides(result, pinned, set(scannable_types))
        FastScanManager._bucket_unattributed(result, by_noun)

        logger.debug(
            f"Fast-scan: {len(result.detected)} type(s) detected, "
            f"{len(raw.failed)} lister(s) failed"
        )
        return result

    @staticmethod
    def _partition_overrides(raw: FastScanResult, overrides: dict[str, str]) -> _PinnedByType:
        """Collect the raw keys that pin an exact CFN type, re-keyed by that CFN type."""
        pinned = _PinnedByType()
        for key, ids in raw.discovered.items():
            if key in overrides:
                pinned.detected.setdefault(overrides[key], []).extend(ids)
        for key, err in raw.failed.items():
            if key in overrides:
                pinned.failed.setdefault(overrides[key], err)
        for key in raw.empty:
            if key in overrides:
                pinned.empty.add(overrides[key])
        return pinned

    @staticmethod
    def _index_by_service_noun(raw: FastScanResult, overrides: dict[str, str]) -> _NounIndex:
        """Index the non-overridden raw keys by ``(service, lister_op_noun)`` for attribution."""
        index = _NounIndex()
        for key, ids in raw.discovered.items():
            if key in overrides:
                continue
            service, op = key.split(":", 1)
            index.services.add(service)
            index.discovered.setdefault((service, lister_op_noun(op)), []).extend(ids)
        for key, err in raw.failed.items():
            if key in overrides:
                continue
            service, op = key.split(":", 1)
            index.services.add(service)
            index.failed[(service, lister_op_noun(op))] = err
        for key in raw.empty:
            if key in overrides:
                continue
            service, op = key.split(":", 1)
            index.services.add(service)
            index.empty.add((service, lister_op_noun(op)))
        # Group discoveries by service so type attribution iterates only the matched service's
        # nouns rather than every discovery.
        for (service, noun), ids in index.discovered.items():
            index.by_endpoint.setdefault(service, []).append((noun, ids))
        return index

    @staticmethod
    def _attribute_types_by_noun(scannable_types: list[str], by_noun: _NounIndex) -> ScanResult:
        """Attribute noun-indexed discoveries/failures/empties to the requested CFN types."""
        detected: dict[str, list[dict]] = {}
        failed: dict[str, str] = {}
        empty: set[str] = set()
        for cfn_type in scannable_types:
            # Skip types whose only reachable lister is a sibling's (same ids, same noun):
            # attributing here would double-type the sibling's resources onto this type.
            if collides_with_sibling_type(cfn_type):
                continue
            endpoint = cfn_type_service_endpoint(cfn_type, by_noun.services)
            if endpoint is None:
                continue
            nouns = cfn_type_resource_nouns(cfn_type)
            ids: list[str] = []
            scanned = False
            for noun, id_list in by_noun.by_endpoint.get(endpoint, []):
                if noun in nouns:
                    ids.extend(id_list)
                    by_noun.attributed.add((endpoint, noun))
                    scanned = True
            type_error: str | None = None
            for (service, noun), err in by_noun.failed.items():
                if service == endpoint and noun in nouns:
                    type_error = err
                    by_noun.attributed_failed.add((service, noun))
            if any(service == endpoint and noun in nouns for (service, noun) in by_noun.empty):
                scanned = True
            if ids:
                detected[cfn_type] = [{"Identifier": i} for i in dict.fromkeys(ids)]
            elif type_error is not None:
                failed[cfn_type] = type_error
            elif scanned:
                empty.add(cfn_type)
        return ScanResult(detected=detected, failed=failed, empty=empty)

    @staticmethod
    def _apply_type_overrides(
        result: ScanResult, pinned: _PinnedByType, requested: set[str]
    ) -> None:
        """Fold direct-type overrides into requested types (detected wins > failed > empty)."""
        for cfn_type, ids in pinned.detected.items():
            if cfn_type in requested and ids:
                merged = [d["Identifier"] for d in result.detected.get(cfn_type, [])] + ids
                result.detected[cfn_type] = [{"Identifier": i} for i in dict.fromkeys(merged)]
                result.failed.pop(cfn_type, None)
                result.empty.discard(cfn_type)
        for cfn_type, err in pinned.failed.items():
            if cfn_type in requested and cfn_type not in result.detected:
                result.failed[cfn_type] = err
                result.empty.discard(cfn_type)
        for cfn_type in pinned.empty:
            if (
                cfn_type in requested
                and cfn_type not in result.detected
                and cfn_type not in result.failed
            ):
                result.empty.add(cfn_type)

    @staticmethod
    def _bucket_unattributed(result: ScanResult, by_noun: _NounIndex) -> None:
        """Route discoveries/failures no requested type claimed into per-service ``*`` buckets."""
        for (service, noun), id_list in by_noun.discovered.items():
            if (service, noun) not in by_noun.attributed and id_list:
                bucket = f"AWS::{service}::*"
                result.detected.setdefault(bucket, []).extend({"Identifier": i} for i in id_list)
        # A failure no requested type claimed must still surface (it would otherwise read as
        # clean), unless the same service already produced a discovery bucket (reachable).
        for (service, noun), err in by_noun.failed.items():
            bucket = f"AWS::{service}::*"
            if (service, noun) not in by_noun.attributed_failed and bucket not in result.detected:
                result.failed.setdefault(bucket, err)
