"""Region resolvers for global resources that CCAPI lists in every region.

Some resources (e.g. S3 buckets) appear in every region's list_resources but
actually exist in only one. Resolvers determine the actual region so scanning
and deletion target the correct endpoint.
"""

from __future__ import annotations

import threading
from typing import Callable

import boto3
from botocore.client import BaseClient

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

ResolverFn = Callable[[BaseClient, Resource], str | None]
# resource type -> (AWS service name, resolver). The service names the client
# the resolver needs; RegionResolver builds it once and hands it in.
REGION_RESOLVER_REGISTRY: dict[str, tuple[str, ResolverFn]] = {}


def resolves_region_for(resource_type: str, *, service: str):
    """Register a region resolver for a resource type, backed by ``service``'s client."""

    def decorator(func: ResolverFn) -> ResolverFn:
        REGION_RESOLVER_REGISTRY[resource_type] = (service, func)
        return func

    return decorator


class RegionResolver:
    """Resolves and filters resources by their actual AWS region.

    Global resources (e.g. S3 buckets) surface in every region's scan; the region
    lookup is memoized so a shared resolver resolves each identifier once, not
    once per region.
    """

    def __init__(self, session: boto3.Session) -> None:
        """Initialize with a boto3 session."""
        self._session = session
        self._region_cache: dict[tuple[str, str], str | None] = {}
        self._cache_lock = threading.Lock()
        # Creating a client on a shared Session is not thread-safe, so build each
        # service client once (via the shared construction lock) rather than per
        # worker thread.
        self._clients: dict[str, BaseClient] = {}

    def _client_for(self, service: str) -> BaseClient:
        """Return a cached client for ``service``, built once via the shared lock."""
        client = self._clients.get(service)
        if client is not None:
            return client
        with self._cache_lock:
            client = self._clients.get(service)
            if client is None:
                client = build_client(self._session, service)
                self._clients[service] = client
            return client

    def get_resource_region(self, resource: Resource) -> str | None:
        """Determine the actual region of a resource. Returns None if unknown/global.

        Memoized by (type, identifier): the underlying AWS call (e.g.
        ``head_bucket``) runs once per resource, not once per region scan.
        """
        entry = REGION_RESOLVER_REGISTRY.get(resource.type)
        if not entry:
            return None
        service, resolver = entry

        key = (resource.type, resource.identifier)
        with self._cache_lock:
            if key in self._region_cache:
                return self._region_cache[key]

        try:
            region = resolver(self._client_for(service), resource)
        except Exception as e:
            logger.debug(
                "Failed to resolve region for %s '%s': %s",
                resource.type,
                resource.identifier,
                e,
            )
            # Don't cache a failure: the shared resolver would then stick this
            # resource into every region's results. Leave it uncached so the next
            # region retries; only successful resolutions are memoized.
            return None

        with self._cache_lock:
            self._region_cache[key] = region
        return region

    def filter_resources_by_region(
        self,
        region: str,
        resources: list[Resource],
    ) -> list[Resource]:
        """Filter resources to only those belonging to the given region."""
        filtered = []
        for resource in resources:
            resource_region = self.get_resource_region(resource)
            if resource_region is None or resource_region == region:
                filtered.append(resource)
        skipped = len(resources) - len(filtered)
        if skipped:
            logger.debug("Filtered out %d resources not in region %s", skipped, region)
        return filtered


@resolves_region_for("AWS::S3::Bucket", service="s3")
def _resolve_s3_bucket_region(s3: BaseClient, resource: Resource) -> str | None:
    try:
        resp = s3.head_bucket(Bucket=resource.identifier)
        return resp.get("BucketRegion") or s3.meta.region_name
    except Exception as exc:
        logger.debug("Could not resolve region for bucket '%s': %s", resource.identifier, exc)
        raise
