"""Tests for S3StorageBackend."""

from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from aws_bench.resource_management.storage.exceptions import (
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
)
from aws_bench.resource_management.storage.s3_backend import S3StorageBackend


@mock_aws
def test_init_auto_creates_bucket():
    """Test S3StorageBackend auto-creates bucket on init."""
    session = boto3.Session()
    backend = S3StorageBackend(
        session=session,
        bucket_name="test-bucket",
        prefix="snapshots/",
        region="us-east-1",
    )

    # Backend should be initialized
    assert backend is not None

    # Bucket should exist
    s3 = session.client("s3", region_name="us-east-1")
    response = s3.head_bucket(Bucket="test-bucket")
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


@mock_aws
def test_init_idempotent_if_bucket_exists():
    """Test init doesn't fail if bucket already exists."""
    session = boto3.Session()
    s3 = session.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")

    # Should not raise
    backend = S3StorageBackend(
        session=session,
        bucket_name="test-bucket",
        prefix="snapshots/",
        region="us-east-1",
    )
    assert backend is not None


@mock_aws
def test_init_configures_versioning():
    """Test init enables versioning on bucket."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Backend should be initialized
    assert backend is not None

    # Check versioning is enabled
    s3 = session.client("s3", region_name="us-east-1")
    versioning = s3.get_bucket_versioning(Bucket="test-bucket")
    assert versioning.get("Status") == "Enabled"


@mock_aws
def test_init_configures_encryption():
    """Test init enables encryption on bucket."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Backend should be initialized
    assert backend is not None

    # Check encryption is enabled
    s3 = session.client("s3", region_name="us-east-1")
    encryption = s3.get_bucket_encryption(Bucket="test-bucket")
    rules = encryption["ServerSideEncryptionConfiguration"]["Rules"]
    assert len(rules) > 0
    assert rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


@mock_aws
def test_init_configures_public_access_block():
    """Test init blocks public access on bucket."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Backend should be initialized
    assert backend is not None

    # Check public access is blocked
    s3 = session.client("s3", region_name="us-east-1")
    public_access_config = s3.get_public_access_block(Bucket="test-bucket")
    config = public_access_config["PublicAccessBlockConfiguration"]
    assert config["BlockPublicAcls"] is True
    assert config["IgnorePublicAcls"] is True
    assert config["BlockPublicPolicy"] is True
    assert config["RestrictPublicBuckets"] is True


@mock_aws
def test_provisions_once_per_process_for_same_bucket():
    """Second backend for the same bucket skips provisioning (once-per-process)."""
    session = boto3.Session()

    with patch.object(S3StorageBackend, "_configure_bucket", autospec=True) as configure_spy:
        S3StorageBackend(session, "test-bucket")
        S3StorageBackend(session, "test-bucket")

    assert configure_spy.call_count == 1


@mock_aws
def test_provisions_each_distinct_bucket_once():
    """A different bucket name still provisions; the cache is keyed per bucket."""
    session = boto3.Session()

    with patch.object(S3StorageBackend, "_configure_bucket", autospec=True) as configure_spy:
        S3StorageBackend(session, "bucket-a")
        S3StorageBackend(session, "bucket-a")
        S3StorageBackend(session, "bucket-b")

    # bucket-a provisioned once (second construction skipped), bucket-b once.
    assert configure_spy.call_count == 2


@mock_aws
def test_concurrent_construction_provisions_once(monkeypatch):
    """Concurrent first-timers for one bucket provision exactly once.

    A slow configure widens the race window so a dropped lock or inverted
    re-check would surface as call_count > 1.
    """
    import threading
    import time

    real_configure = S3StorageBackend._configure_bucket
    call_count = 0
    count_lock = threading.Lock()

    def slow_configure(self):
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.05)  # widen the window between check and add
        return real_configure(self)

    monkeypatch.setattr(S3StorageBackend, "_configure_bucket", slow_configure)

    session = boto3.Session()
    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def build():
        try:
            barrier.wait()  # release all threads at once
            S3StorageBackend(session, "test-bucket")
        except Exception as exc:  # noqa: BLE001 — surface to the assertion
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert call_count == 1


@mock_aws
def test_save_initial_write_no_expected_etag():
    """Test save() with no expected_etag (initial write)."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Save initial data
    data = b"initial content"
    etag = backend.save(key="test-key", data=data, expected_etag=None)

    # Should return an ETag
    assert etag is not None
    assert isinstance(etag, str)
    assert len(etag) > 0

    # Verify object was written
    s3 = session.client("s3", region_name="us-east-1")
    response = s3.get_object(Bucket="test-bucket", Key="snapshots/test-key")
    assert response["Body"].read() == data


@mock_aws
def test_save_update_with_matching_etag():
    """Test save() with matching expected_etag (update)."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Initial write
    initial_data = b"initial content"
    etag1 = backend.save(key="test-key", data=initial_data, expected_etag=None)

    # Update with correct ETag
    updated_data = b"updated content"
    etag2 = backend.save(key="test-key", data=updated_data, expected_etag=etag1)

    # Should return new ETag
    assert etag2 is not None
    assert etag2 != etag1

    # Verify object was updated
    s3 = session.client("s3", region_name="us-east-1")
    response = s3.get_object(Bucket="test-bucket", Key="snapshots/test-key")
    assert response["Body"].read() == updated_data


@mock_aws
def test_save_conflict_with_mismatched_etag():
    """Test save() raises StorageConflictError on ETag mismatch."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Initial write
    initial_data = b"initial content"
    backend.save(key="test-key", data=initial_data, expected_etag=None)

    # Try to update with wrong ETag
    updated_data = b"updated content"
    with pytest.raises(StorageConflictError) as exc_info:
        backend.save(key="test-key", data=updated_data, expected_etag="wrong-etag")

    # Check error details
    error = exc_info.value
    assert error.key == "snapshots/test-key"
    assert error.expected_etag == "wrong-etag"
    assert error.actual_etag is not None


@mock_aws
def test_save_not_found_when_expected_etag_provided_but_key_missing():
    """Test save() raises StorageNotFoundError when expected_etag provided but key doesn't exist."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Try to save with expected_etag but key doesn't exist
    data = b"content"
    with pytest.raises(StorageNotFoundError) as exc_info:
        backend.save(key="missing-key", data=data, expected_etag="some-etag")

    # Check error details
    error = exc_info.value
    assert error.key == "snapshots/missing-key"


@mock_aws
def test_load_existing_key():
    """Test load() returns data and etag for existing key."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create object via save
    original_data = b"test content"
    etag = backend.save(key="test-key", data=original_data, expected_etag=None)

    # Load it back
    loaded_data, loaded_etag = backend.load(key="test-key")

    # Verify data and etag match
    assert loaded_data == original_data
    assert loaded_etag == etag


@mock_aws
def test_load_non_existent_key():
    """Test load() raises StorageNotFoundError for missing key."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Try to load non-existent key
    with pytest.raises(StorageNotFoundError) as exc_info:
        backend.load(key="missing-key")

    # Check error details
    error = exc_info.value
    assert error.key == "snapshots/missing-key"


@mock_aws
def test_round_trip_save_and_load():
    """Test round-trip: save then load preserves data."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Save data
    original_data = b"round trip test data"
    etag1 = backend.save(key="round-trip-key", data=original_data, expected_etag=None)

    # Load it back
    loaded_data, loaded_etag = backend.load(key="round-trip-key")

    # Verify exact match
    assert loaded_data == original_data
    assert loaded_etag == etag1

    # Update and verify again
    updated_data = b"updated round trip data"
    etag2 = backend.save(key="round-trip-key", data=updated_data, expected_etag=etag1)

    # Load updated version
    loaded_data2, loaded_etag2 = backend.load(key="round-trip-key")
    assert loaded_data2 == updated_data
    assert loaded_etag2 == etag2
    assert etag2 != etag1


# ============================================================================
# Task 5: Additional Operations (exists, delete, list_keys, bulk_delete)
# ============================================================================


@mock_aws
def test_exists_returns_true_for_existing_key():
    """Test exists() returns True for existing key."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create an object
    backend.save(key="existing-key", data=b"test data", expected_etag=None)

    # Check it exists
    assert backend.exists(key="existing-key") is True


@mock_aws
def test_exists_returns_false_for_non_existent_key():
    """Test exists() returns False for non-existent key."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Check non-existent key
    assert backend.exists(key="missing-key") is False


@mock_aws
def test_delete_existing_key_succeeds():
    """Test delete() succeeds when deleting existing key."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create an object
    backend.save(key="delete-me", data=b"test data", expected_etag=None)

    # Delete it (should not raise)
    backend.delete(key="delete-me")

    # Verify it's gone
    assert backend.exists(key="delete-me") is False


@mock_aws
def test_delete_non_existent_key_is_idempotent():
    """Test delete() is idempotent when key doesn't exist."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Delete non-existent key (should not raise)
    backend.delete(key="never-existed")


@mock_aws
def test_delete_is_idempotent():
    """Test delete() is idempotent - calling twice works."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create an object
    backend.save(key="delete-twice", data=b"test data", expected_etag=None)

    # Delete it twice (both should succeed)
    backend.delete(key="delete-twice")
    backend.delete(key="delete-twice")


@mock_aws
def test_list_keys_with_empty_prefix():
    """Test list_keys() with no matching objects."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # List keys with no objects
    keys = backend.list_keys(prefix="")

    # Should return empty list
    assert keys == []


@mock_aws
def test_list_keys_with_multiple_objects():
    """Test list_keys() returns all matching keys."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create multiple objects
    backend.save(key="dir1/file1.json", data=b"data1", expected_etag=None)
    backend.save(key="dir1/file2.json", data=b"data2", expected_etag=None)
    backend.save(key="dir2/file3.json", data=b"data3", expected_etag=None)

    # List all keys
    all_keys = backend.list_keys(prefix="")
    assert len(all_keys) == 3
    assert "dir1/file1.json" in all_keys
    assert "dir1/file2.json" in all_keys
    assert "dir2/file3.json" in all_keys

    # List with prefix
    dir1_keys = backend.list_keys(prefix="dir1/")
    assert len(dir1_keys) == 2
    assert "dir1/file1.json" in dir1_keys
    assert "dir1/file2.json" in dir1_keys
    assert "dir2/file3.json" not in dir1_keys


@mock_aws
def test_list_keys_handles_pagination():
    """Test list_keys() handles pagination automatically."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create many objects (more than default page size)
    for i in range(15):
        backend.save(key=f"file{i:03d}.json", data=b"data", expected_etag=None)

    # List all keys - should handle pagination automatically
    keys = backend.list_keys(prefix="")
    assert len(keys) == 15
    assert "file000.json" in keys
    assert "file014.json" in keys


@mock_aws
def test_bulk_delete_all_successful():
    """Test bulk_delete() with all keys successfully deleted."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create objects
    backend.save(key="bulk1.json", data=b"data1", expected_etag=None)
    backend.save(key="bulk2.json", data=b"data2", expected_etag=None)
    backend.save(key="bulk3.json", data=b"data3", expected_etag=None)

    # Bulk delete
    result = backend.bulk_delete(keys=["bulk1.json", "bulk2.json", "bulk3.json"])

    # All should succeed (None means success)
    assert result == {
        "bulk1.json": None,
        "bulk2.json": None,
        "bulk3.json": None,
    }

    # Verify all deleted
    assert backend.exists("bulk1.json") is False
    assert backend.exists("bulk2.json") is False
    assert backend.exists("bulk3.json") is False


@mock_aws
def test_bulk_delete_mixed_existing_and_missing():
    """Test bulk_delete() with mix of existing and non-existent keys."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Create only some objects
    backend.save(key="exists1.json", data=b"data1", expected_etag=None)
    backend.save(key="exists2.json", data=b"data2", expected_etag=None)

    # Bulk delete (including non-existent keys)
    result = backend.bulk_delete(keys=["exists1.json", "missing.json", "exists2.json"])

    # All should succeed (S3 delete is idempotent)
    assert result == {
        "exists1.json": None,
        "missing.json": None,
        "exists2.json": None,
    }


@mock_aws
def test_bulk_delete_empty_list():
    """Test bulk_delete() with empty list."""
    session = boto3.Session()
    backend = S3StorageBackend(session, "test-bucket")

    # Bulk delete with empty list
    result = backend.bulk_delete(keys=[])

    # Should return empty dict
    assert result == {}


# ============================================================================
# PutBucketVersioning retry on OperationAborted
# ============================================================================


def _make_operation_aborted_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "OperationAborted", "Message": "A conflicting operation"}},
        "PutBucketVersioning",
    )


@mock_aws
def test_put_versioning_retries_on_operation_aborted():
    """Test that OperationAborted on put_bucket_versioning is retried and succeeds."""
    session = boto3.Session()
    # Pre-create bucket so __init__ skips _create_bucket
    s3_setup = session.client("s3", region_name="us-east-1")
    s3_setup.create_bucket(Bucket="test-bucket")

    call_count = 0

    original_session_client = session.client

    def patched_client(*args, **kwargs):
        client = original_session_client(*args, **kwargs)
        if args and args[0] == "s3":
            original_put = client.put_bucket_versioning

            def mock_put_versioning(**kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise _make_operation_aborted_error()
                return original_put(**kw)

            # setattr: the botocore client's methods aren't statically typed as
            # writable, so a direct assignment trips the type checker.
            setattr(client, "put_bucket_versioning", mock_put_versioning)  # noqa: B010
        return client

    with patch.object(session, "client", side_effect=patched_client):
        backend = S3StorageBackend(session, "test-bucket")

    assert backend is not None
    assert call_count == 2


@mock_aws
def test_put_versioning_raises_after_retries_exhausted():
    """Test that StorageError is raised after all retry attempts are exhausted."""
    session = boto3.Session()
    # Pre-create bucket so __init__ skips _create_bucket
    s3_setup = session.client("s3", region_name="us-east-1")
    s3_setup.create_bucket(Bucket="test-bucket")

    original_session_client = session.client

    def patched_client(*args, **kwargs):
        client = original_session_client(*args, **kwargs)
        if args and args[0] == "s3":
            # setattr: see the sibling test — the client method is not a
            # statically writable attribute, so assign through setattr.
            setattr(  # noqa: B010
                client,
                "put_bucket_versioning",
                lambda **kw: (_ for _ in ()).throw(_make_operation_aborted_error()),
            )
        return client

    with patch.object(session, "client", side_effect=patched_client):
        with pytest.raises(StorageError, match="Failed to configure S3 bucket"):
            S3StorageBackend(session, "test-bucket")


@mock_aws
def test_put_lifecycle_retries_on_operation_aborted():
    """OperationAborted on put_bucket_lifecycle_configuration is retried.

    Regression: parallel scenario verification configures the same shared state
    bucket concurrently; before all config PUTs were retry-guarded, the lifecycle
    PUT raced and surfaced OperationAborted as a hard verification failure.
    """
    session = boto3.Session()
    s3_setup = session.client("s3", region_name="us-east-1")
    s3_setup.create_bucket(Bucket="test-bucket")

    call_count = 0
    original_session_client = session.client

    def patched_client(*args, **kwargs):
        client = original_session_client(*args, **kwargs)
        if args and args[0] == "s3":
            original_put = client.put_bucket_lifecycle_configuration

            def mock_put_lifecycle(**kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise _make_operation_aborted_error()
                return original_put(**kw)

            setattr(client, "put_bucket_lifecycle_configuration", mock_put_lifecycle)  # noqa: B010
        return client

    with patch.object(session, "client", side_effect=patched_client):
        backend = S3StorageBackend(session, "test-bucket")

    assert backend is not None
    assert call_count == 2
