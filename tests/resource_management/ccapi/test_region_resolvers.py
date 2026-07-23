"""Tests for aws_bench.resource_management.ccapi.region_resolvers."""

from __future__ import annotations

from unittest.mock import MagicMock

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.ccapi.region_resolvers import RegionResolver

# -- RegionResolver.get_resource_region --


def test_get_resource_region_returns_none_for_unregistered_type():
    session = MagicMock()
    resolver = RegionResolver(session)
    resource = Resource(type="AWS::Lambda::Function", identifier="fn")
    assert resolver.get_resource_region(resource) is None


def test_get_resource_region_returns_region_for_s3_bucket():
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {"BucketRegion": "eu-west-1"}

    resolver = RegionResolver(session)
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    assert resolver.get_resource_region(resource) == "eu-west-1"


def test_get_resource_region_falls_back_to_client_region():
    session = MagicMock()
    s3_client = MagicMock()
    s3_client.meta.region_name = "ap-southeast-1"
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {}

    resolver = RegionResolver(session)
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    assert resolver.get_resource_region(resource) == "ap-southeast-1"


def test_get_resource_region_returns_none_when_no_region_available():
    session = MagicMock()
    s3_client = MagicMock()
    s3_client.meta.region_name = None
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {}

    resolver = RegionResolver(session)
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    # None means "no opinion" — filter_resources_by_region keeps the resource
    assert resolver.get_resource_region(resource) is None


def test_get_resource_region_returns_none_on_resolver_error():
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    s3_client.head_bucket.side_effect = Exception("access denied")

    resolver = RegionResolver(session)
    resource = Resource(type="AWS::S3::Bucket", identifier="private-bucket")
    assert resolver.get_resource_region(resource) is None


def test_get_resource_region_memoizes_per_identifier():
    """A bucket's region is resolved once (memoized), not once per region scan."""
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {"BucketRegion": "eu-west-1"}

    resolver = RegionResolver(session)
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")

    for _ in range(5):
        assert resolver.get_resource_region(resource) == "eu-west-1"
    s3_client.head_bucket.assert_called_once()


def test_get_resource_region_builds_service_client_once():
    """The service client is created once (shared-Session client creation isn't thread-safe)."""
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {"BucketRegion": "eu-west-1"}

    resolver = RegionResolver(session)
    # Distinct buckets each miss the cache but must share one client.
    for i in range(5):
        resolver.get_resource_region(Resource(type="AWS::S3::Bucket", identifier=f"b{i}"))
    session.client.assert_called_once_with("s3")


def test_get_resource_region_does_not_cache_error_result():
    """A failed lookup is NOT cached, so the next region scan retries it.

    Caching a transient failure would stick the resource into every region's
    results; only successes are memoized, so a later lookup re-attempts.
    """
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    # First lookup fails, second succeeds — the retry must reach head_bucket again.
    s3_client.head_bucket.side_effect = [
        Exception("transient throttle"),
        {"BucketRegion": "eu-west-1"},
    ]

    resolver = RegionResolver(session)
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")

    assert resolver.get_resource_region(resource) is None
    assert resolver.get_resource_region(resource) == "eu-west-1"
    assert s3_client.head_bucket.call_count == 2


# -- RegionResolver.filter_resources_by_region --


def test_filter_keeps_resources_in_correct_region():
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {"BucketRegion": "us-east-1"}

    resolver = RegionResolver(session)
    resources = [Resource(type="AWS::S3::Bucket", identifier="b1")]
    result = resolver.filter_resources_by_region("us-east-1", resources)
    assert len(result) == 1


def test_filter_removes_resources_in_wrong_region():
    session = MagicMock()
    s3_client = MagicMock()
    session.client.return_value = s3_client
    s3_client.head_bucket.return_value = {"BucketRegion": "eu-west-1"}

    resolver = RegionResolver(session)
    resources = [Resource(type="AWS::S3::Bucket", identifier="b1")]
    result = resolver.filter_resources_by_region("us-east-1", resources)
    assert len(result) == 0


def test_filter_keeps_resources_without_resolver():
    session = MagicMock()
    resolver = RegionResolver(session)
    resources = [Resource(type="AWS::Lambda::Function", identifier="fn1")]
    result = resolver.filter_resources_by_region("us-east-1", resources)
    assert len(result) == 1
