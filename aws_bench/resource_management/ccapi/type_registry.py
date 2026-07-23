"""CCAPI type registry — discover, cache, classify, and skip resource types."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Callable

import boto3
import tenacity

from aws_bench.constants import CCAPI_CACHE_DIR
from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import (
    MAX_WORKERS_HEAVY,
    UNSUPPORTED_CCAPI_ERROR_CODES,
    ScanResult,
)
from aws_bench.resource_management.utils.file_io import write_json
from aws_bench.utils.concurrent import build_client
from aws_bench.utils.retry import is_fresh_account_transient

logger = get_logger(__name__)

# On a freshly-vended account, list_types can fail with a not-yet-converged STS/IAM error
# (InvalidClientTokenId etc.) before the account is known to the region's control plane. This
# is host-side (type_registry is not in the Lambda closure), so retry with tenacity.
_LIST_TYPES_RETRY_ATTEMPTS = 5

_SKIP_TYPES_FILE = "ccapi_skip_types.json"
_SKIP_TYPES_PATH = CCAPI_CACHE_DIR / _SKIP_TYPES_FILE
_SKIP_TYPES_MAX_AGE_DAYS = 30

_SECONDS_PER_DAY = 86400
_ERROR_RE = re.compile(r"An error occurred \(([a-zA-Z]+)\)")

ScanFn = Callable[[list[str], int], ScanResult]


class TypeRegistry:
    """Manages the cache of CCAPI resource types that should be skipped."""

    def __init__(
        self, session: boto3.Session, *, scan_fn: ScanFn, region_name: str | None = None
    ) -> None:
        """Initialize with a boto3 session and a scan callable."""
        self._session = session
        self._scan_fn = scan_fn
        self._region_name = region_name or session.region_name

    @tenacity.retry(
        retry=tenacity.retry_if_exception(is_fresh_account_transient),
        wait=tenacity.wait_exponential(min=5, max=60) + tenacity.wait_random(0, 10),
        stop=tenacity.stop_after_attempt(_LIST_TYPES_RETRY_ATTEMPTS),
        reraise=True,
    )
    def _paginate_resource_types(self) -> list[str]:
        """Paginate ``list_types``, retrying fresh-account STS/IAM convergence errors."""
        cfn = build_client(self._session, "cloudformation", region_name=self._region_name)
        all_types: list[str] = []
        for page in cfn.get_paginator("list_types").paginate(
            Type="RESOURCE",
            Visibility="PUBLIC",
            Filters={"Category": "AWS_TYPES"},
        ):
            all_types.extend(ts["TypeName"] for ts in page.get("TypeSummaries", []))
        return all_types

    def list_all_resource_types(self) -> list[str]:
        """List every AWS resource type from the CloudFormation registry."""
        try:
            all_types = self._paginate_resource_types()
        except Exception:
            logger.exception("Failed to list resource types")
            raise
        logger.debug("Total resource types: %d", len(all_types))
        return all_types

    def load_skip_types(self) -> set[str]:
        """Load the skip list, regenerating if stale (30 days) or missing."""
        cached = self._read_cached_skip_types()
        if cached is not None:
            return cached
        return self.generate_skip_types()

    def generate_skip_types(self, *, max_workers: int = MAX_WORKERS_HEAVY) -> set[str]:
        """Scan all AWS resource types and persist those that should be skipped."""
        all_types = self.list_all_resource_types()
        scan_result = self._scan_fn(all_types, max_workers)
        logger.info("Failed types: %d", len(scan_result.failed))

        skip = self._classify_skip_types(scan_result.failed)
        logger.info(
            "Skip list: %d types (from %d total failed)", len(skip), len(scan_result.failed)
        )

        write_json({"types_to_skip": sorted(skip)}, _SKIP_TYPES_PATH)
        return skip

    def _read_cached_skip_types(
        self,
    ) -> set[str] | None:
        """Read skip types from the cache file. Returns None if stale, missing, or corrupt."""
        try:
            if not _SKIP_TYPES_PATH.exists():
                logger.info("Skip types list not found, generating...")
                return None
            age_days = (time.time() - _SKIP_TYPES_PATH.stat().st_mtime) / _SECONDS_PER_DAY
            if age_days > _SKIP_TYPES_MAX_AGE_DAYS:
                logger.info(
                    "Skip types list is %.0f days old (max %d), regenerating...",
                    age_days,
                    _SKIP_TYPES_MAX_AGE_DAYS,
                )
                return None
            with open(_SKIP_TYPES_PATH) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning("Skip types file has unexpected format — regenerating")
                return None
            skip = set(data.get("types_to_skip", []))
            logger.info("Loaded %d skip types (%.0f days old)", len(skip), age_days)
            return skip
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load skip types from %s: %s — regenerating", _SKIP_TYPES_PATH, exc
            )
            return None

    def _classify_skip_types(self, failed_types: dict[str, str]) -> set[str]:
        """Determine which failed types should be permanently skipped."""
        errors_by_type: dict[str, set[str]] = defaultdict(set)
        for resource_type, error_msg in failed_types.items():
            match = _ERROR_RE.search(error_msg)
            if match:
                errors_by_type[match.group(1)].add(resource_type)

        skip_types: set[str] = set()
        for error_type in UNSUPPORTED_CCAPI_ERROR_CODES:
            skip_types.update(errors_by_type.get(error_type, set()))
        return skip_types
