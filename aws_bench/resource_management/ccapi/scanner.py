"""CCAPI resource scanning — enumerate resources via Cloud Control API."""

from __future__ import annotations

import functools
import time
from concurrent.futures import as_completed

import boto3
from botocore.exceptions import ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import (
    MAX_WORKERS_HEAVY,
    ScanResult,
)
from aws_bench.resource_management.ccapi.type_registry import TypeRegistry
from aws_bench.utils.concurrent import (
    interruptible_executor,
    raise_if_shutdown,
    shutdown_requested,
)

logger = get_logger(__name__)

_SCAN_LOG_INTERVAL = 50
_MAX_RETRIES = 3
_RETRY_DELAY_BASE = 1  # seconds

_RETRYABLE_ERROR_CODES = {
    "ServiceUnavailableException",
    "InternalServiceException",
}


class Scanner:
    """Scans AWS resources via Cloud Control API."""

    def __init__(self, client, *, session: boto3.Session, region_name: str | None = None) -> None:
        """Initialize with a cloudcontrol client and session.

        Args:
            client: Pre-built cloudcontrol client (shared with CloudControlManager)
            session: boto3 Session (needed for TypeRegistry discovery only)
            region_name: AWS region name (optional, uses session's region if not provided)
        """
        self._client = client
        self._session = session
        self._region_name = region_name or session.region_name
        self._scannable_types: list[str] | None = None

    def get_scannable_types(self) -> list[str]:
        """Get all CCAPI-supported resource types, excluding known problematic ones."""
        if self._scannable_types is None:
            logger.debug("Discovering CCAPI resource types")
            registry = TypeRegistry(
                self._session, scan_fn=self.scan_resources, region_name=self._region_name
            )
            skip = registry.load_skip_types()
            all_types = registry.list_all_resource_types()
            self._scannable_types = [name for name in all_types if name not in skip]
            logger.info(
                "Found %d scannable resource types (%d skipped)",
                len(self._scannable_types),
                len(skip),
            )
        return list(self._scannable_types)

    def scan_resources(
        self,
        resource_types: list[str] | None = None,
        max_workers: int = MAX_WORKERS_HEAVY,
    ) -> ScanResult:
        """Scan account for resources via CCAPI list_resources.

        If *resource_types* is ``None``, auto-discovers all scannable types.
        Retries failed resource types up to _MAX_RETRIES times.
        """
        if resource_types is None:
            resource_types = self.get_scannable_types()

        logger.info(f"Scanning {len(resource_types)} resource type(s) via CCAPI")
        logger.debug(f"Max workers: {max_workers}, retry attempts: {_MAX_RETRIES}")

        detected: dict[str, list[dict]] = {}
        failed: dict[str, str] = {}
        empty: set[str] = set()

        list_fn = functools.partial(self._list_resources_by_type_with_retry)
        with interruptible_executor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(list_fn, resource_type): resource_type
                for resource_type in resource_types
            }
            for future in as_completed(futures):
                resource_type = futures[future]
                try:
                    resources = future.result()
                    if resources:
                        detected[resource_type] = resources
                        logger.debug(f"Found: {resource_type} ({len(resources)} resources)")
                    else:
                        empty.add(resource_type)
                        logger.debug(f"Empty: {resource_type}")
                except Exception as e:
                    failed[resource_type] = str(e)
                    logger.debug(f"Failed to scan {resource_type}: {e}")

        # Backstop: a shutdown flagged with no in-flight worker left to raise would
        # otherwise return a partial `detected`; abort instead.
        raise_if_shutdown()

        total_resources = sum(len(items) for items in detected.values())
        logger.debug(
            f"CCAPI resource scan: {total_resources} resource(s) found across "
            f"{len(detected)} type(s), {len(empty)} type(s) empty, {len(failed)} type(s) failed"
        )

        if failed:
            logger.debug(f"Failed resource types: {list(failed.keys())[:10]}")

        return ScanResult(detected=detected, failed=failed, empty=empty)

    def _list_resources_by_type_with_retry(self, resource_type: str) -> list[dict]:
        """List all resources of a given type via CCAPI with retry logic.

        Retries up to _MAX_RETRIES times with exponential backoff on transient errors only.
        Non-transient errors (permissions, unsupported actions) fail fast.
        """
        # Worker entry: queued workers bail here instead of each running its AWS call first.
        raise_if_shutdown()

        last_exception = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._list_resources_by_type(resource_type)
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code not in _RETRYABLE_ERROR_CODES:
                    raise
                last_exception = e
                if attempt < _MAX_RETRIES - 1 and not shutdown_requested():
                    delay = _RETRY_DELAY_BASE * (2**attempt)
                    logger.debug(
                        f"Retry {attempt + 1}/{_MAX_RETRIES} for {resource_type} "
                        f"after {error_code}. Waiting {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.debug(
                        f"Failed to scan {resource_type} after {_MAX_RETRIES} attempts: {e}"
                    )
            except Exception:
                # Non-ClientError exceptions (e.g., network issues) fail immediately
                raise

        # If we get here, all retries failed
        raise last_exception  # type: ignore

    def _list_resources_by_type(self, resource_type: str) -> list[dict]:
        """List all resources of a given type via CCAPI."""
        resources: list[dict] = []
        for page in self._client.get_paginator("list_resources").paginate(TypeName=resource_type):
            resources.extend(page.get("ResourceDescriptions", []))
        return resources
