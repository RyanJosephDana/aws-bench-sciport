"""Tests for S3 bucket cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.s3 import (
    _delete as _delete_s3_bucket,
)
from aws_bench.resource_management.cleanup.handlers.s3 import (
    _prepare as _prepare_s3_bucket,
)
from aws_bench.resource_management.cleanup.handlers.s3 import (
    _remove_bucket_policy,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_BUCKET = "my-test-bucket"


def _resource(name: str = _BUCKET) -> Resource:
    return Resource(type="AWS::S3::Bucket", identifier=name)


def _session_with_paginator(pages: list[dict]) -> tuple[MagicMock, MagicMock]:
    """Create a mock session whose S3 client returns given pages."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = pages
    client.delete_objects.return_value = {"Errors": []}
    return session, client


# -- _prepare --


class TestPrepare:
    def test_deletes_all_versions_and_delete_markers(self):
        session, client = _session_with_paginator(
            [
                {
                    "Versions": [{"Key": "a", "VersionId": "v1"}],
                    "DeleteMarkers": [{"Key": "b", "VersionId": "v2"}],
                }
            ]
        )
        result = _prepare_s3_bucket(_resource(), session)

        client.delete_objects.assert_called_once()
        objects = client.delete_objects.call_args[1]["Delete"]["Objects"]
        assert len(objects) == 2
        assert result.status == HandlerStatus.SUCCESS
        assert "Emptied 2 objects" in result.message

    def test_noop_on_empty_bucket(self):
        session, client = _session_with_paginator([{"Versions": [], "DeleteMarkers": []}])
        result = _prepare_s3_bucket(_resource("empty-bucket"), session)

        client.delete_objects.assert_not_called()
        assert result.status == HandlerStatus.SUCCESS
        assert "Emptied 0 objects" in result.message

    def test_batches_large_object_lists(self):
        """Objects are batched in groups of 1000."""
        objects = [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(5001)]
        session, client = _session_with_paginator([{"Versions": objects, "DeleteMarkers": []}])
        result = _prepare_s3_bucket(_resource("big-bucket"), session)

        # 5001 objects / 1000 batch size = 6 calls
        assert client.delete_objects.call_count == 6
        assert result.status == HandlerStatus.SUCCESS

    def test_skips_when_bucket_not_found(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}}, "ListObjectVersions"
        )
        result = _prepare_s3_bucket(_resource("gone-bucket"), session)

        assert result.status == HandlerStatus.SKIPPED
        assert "Bucket not found" in result.message

    def test_skips_on_404_error_code(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "ListObjectVersions"
        )
        result = _prepare_s3_bucket(_resource("gone-bucket"), session)

        assert result.status == HandlerStatus.SKIPPED

    def test_fails_on_access_denied(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "ListObjectVersions"
        )
        result = _prepare_s3_bucket(_resource("denied-bucket"), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed after deleting 0 objects" in result.message

    def test_fails_on_botocore_error(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = BotoCoreError()
        result = _prepare_s3_bucket(_resource("error-bucket"), session)

        assert result.status == HandlerStatus.FAILED

    def test_fails_on_generic_exception(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = Exception("Network error")
        result = _prepare_s3_bucket(_resource("error-bucket"), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed after deleting 0 objects" in result.message

    def test_reports_partial_failures(self):
        session, client = _session_with_paginator(
            [{"Versions": [{"Key": "file1", "VersionId": "v1"}], "DeleteMarkers": []}]
        )
        client.delete_objects.return_value = {"Errors": [{"Key": "file1", "Code": "AccessDenied"}]}
        result = _prepare_s3_bucket(_resource("partial-fail-bucket"), session)

        assert result.status == HandlerStatus.FAILED
        assert "1 partial failures" in result.message

    def test_handles_multiple_pages(self):
        pages = [
            {"Versions": [{"Key": "a", "VersionId": "v1"}], "DeleteMarkers": []},
            {"Versions": [{"Key": "b", "VersionId": "v2"}], "DeleteMarkers": []},
        ]
        session, client = _session_with_paginator(pages)
        result = _prepare_s3_bucket(_resource(), session)

        assert client.delete_objects.call_count == 2
        assert result.status == HandlerStatus.SUCCESS
        assert "Emptied 2 objects" in result.message


# -- _delete --


class TestDelete:
    def test_deletes_bucket(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client

        result = _delete_s3_bucket(_resource(), session)

        client.delete_bucket.assert_called_once_with(Bucket=_BUCKET)
        assert result.status == HandlerStatus.SUCCESS

    def test_success_when_bucket_already_gone(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.delete_bucket.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}}, "DeleteBucket"
        )
        result = _delete_s3_bucket(_resource(), session)

        assert result.status == HandlerStatus.SUCCESS
        assert "already gone" in result.message

    def test_fails_on_bucket_not_empty(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.delete_bucket.side_effect = ClientError(
            {"Error": {"Code": "BucketNotEmpty"}}, "DeleteBucket"
        )
        result = _delete_s3_bucket(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed to delete bucket" in result.message

    def test_fails_on_botocore_error(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.delete_bucket.side_effect = BotoCoreError()
        result = _delete_s3_bucket(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Connection error" in result.message


# -- _remove_bucket_policy (unblocks buckets whose policy denies DeleteBucket) --


class TestRemoveBucketPolicy:
    def test_prepare_strips_bucket_policy_before_emptying(self):
        """Prepare removes any bucket policy so a resource-based Deny can't wedge delete."""
        session, client = _session_with_paginator([{"Versions": [], "DeleteMarkers": []}])
        result = _prepare_s3_bucket(_resource(), session)

        client.delete_bucket_policy.assert_called_once_with(Bucket=_BUCKET)
        assert result.status == HandlerStatus.SUCCESS

    def test_ignores_missing_policy(self):
        client = MagicMock()
        client.delete_bucket_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucketPolicy"}}, "DeleteBucketPolicy"
        )
        # The common case (no policy) must be swallowed silently, not raised.
        _remove_bucket_policy(client, _BUCKET)

    def test_ignores_bucket_not_found(self):
        client = MagicMock()
        client.delete_bucket_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}}, "DeleteBucketPolicy"
        )
        _remove_bucket_policy(client, _BUCKET)  # should not raise

    def test_prepare_continues_when_policy_removal_denied(self):
        """Policy removal is best-effort: a denial is logged, and emptying still runs."""
        session, client = _session_with_paginator([{"Versions": [], "DeleteMarkers": []}])
        client.delete_bucket_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "DeleteBucketPolicy"
        )
        result = _prepare_s3_bucket(_resource(), session)

        client.delete_bucket_policy.assert_called_once_with(Bucket=_BUCKET)
        assert result.status == HandlerStatus.SUCCESS

    def test_ignores_botocore_error(self):
        client = MagicMock()
        client.delete_bucket_policy.side_effect = BotoCoreError()
        _remove_bucket_policy(client, _BUCKET)  # should not raise
