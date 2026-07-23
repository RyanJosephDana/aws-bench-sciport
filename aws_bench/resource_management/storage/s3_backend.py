"""S3-backed storage with retry logic and optimistic locking."""

import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.storage.exceptions import (
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
)
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

# Buckets this process has provisioned. One management account → one shared
# state bucket, so provisioning runs once per process; later backends skip it,
# avoiding the OperationAborted race on concurrent config PUTs. The lock guards
# concurrent first-timers (backends are built on to_thread/executor workers).
_provisioned_buckets: set[str] = set()
_provisioned_buckets_lock = threading.Lock()

# S3 configuration constants
DEFAULT_REGION = "us-east-1"
DEFAULT_PREFIX = "snapshots/"
MAX_RETRY_ATTEMPTS = 3
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 60
S3_BATCH_DELETE_MAX_KEYS = 1000

# S3 error codes
ERROR_CODE_404 = "404"
ERROR_CODE_403 = "403"
ERROR_CODE_PRECONDITION_FAILED = "PreconditionFailed"
ERROR_CODE_NO_SUCH_KEY = "NoSuchKey"
ERROR_CODE_BUCKET_ALREADY_OWNED = "BucketAlreadyOwnedByYou"
ERROR_CODE_BUCKET_ALREADY_EXISTS = "BucketAlreadyExists"
ERROR_CODE_INVALID_BUCKET_NAME = "InvalidBucketName"

# Transient error codes
TRANSIENT_ERROR_CODES = ["ServiceUnavailable", "SlowDown", "RequestTimeout"]

# Bucket-level concurrency error (concurrent PUTs to bucket config are rejected)
ERROR_CODE_OPERATION_ABORTED = "OperationAborted"

# S3 bucket configuration
ENCRYPTION_ALGORITHM = "AES256"
VERSIONING_STATUS_ENABLED = "Enabled"


class S3StorageBackend:
    """S3-backed storage with strong consistency and retry logic.

    All operations use S3's native ETags for optimistic locking.
    Transient errors (throttling, 503) are retried automatically via boto3.
    """

    def __init__(
        self,
        session: boto3.Session,
        bucket_name: str,
        prefix: str = DEFAULT_PREFIX,
        region: str = DEFAULT_REGION,
    ):
        """Initialize S3 backend with auto-create.

        Args:
            session: Boto3 session with management account credentials
            bucket_name: S3 bucket name (globally unique)
            prefix: Key prefix for all operations
            region: Region to create bucket (default: us-east-1)

        Raises:
            StorageError: If bucket check or creation fails
        """
        # Configure S3 client with retry
        config = Config(
            retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "standard"},
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
        )
        self._s3 = build_client(session, "s3", region_name=region, config=config)
        self._bucket = bucket_name
        self._prefix = prefix
        self._region = region

        logger.debug(f"Initializing S3 backend: bucket={bucket_name}, region={region}")
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self) -> None:
        """Provision the bucket once per process: check existence, create, configure.

        The first pass completes a partially-configured bucket; later backends
        for the same bucket skip the idempotent PUTs (see ``_provisioned_buckets``).

        Raises:
            StorageError: If bucket check or creation fails
        """
        if self._bucket in _provisioned_buckets:
            return

        # Re-check under the lock so concurrent first-timers provision exactly once.
        with _provisioned_buckets_lock:
            if self._bucket in _provisioned_buckets:
                return

            try:
                self._s3.head_bucket(Bucket=self._bucket)
                logger.debug(f"S3 bucket exists: {self._bucket}")

            except ClientError as e:
                error_code = e.response["Error"]["Code"]

                if error_code == ERROR_CODE_404:
                    self._create_bucket()

                elif error_code == ERROR_CODE_403:
                    raise StorageError(
                        f"S3 bucket '{self._bucket}' exists but you don't have access. "
                        f"Check IAM permissions for the management account."
                    )
                else:
                    raise StorageError(f"Failed to check S3 bucket '{self._bucket}': {e}")

            self._configure_bucket()
            _provisioned_buckets.add(self._bucket)

    def _create_bucket(self) -> None:
        """Create S3 bucket (no configuration applied here).

        Configuration is applied separately in _configure_bucket() to ensure
        partially-configured buckets are completed on next instantiation.
        """
        try:
            logger.info(f"Creating S3 bucket: {self._bucket} in {self._region}")

            # Create bucket (region-specific API)
            if self._region == DEFAULT_REGION:
                # us-east-1 doesn't accept LocationConstraint
                self._s3.create_bucket(Bucket=self._bucket)
            else:
                self._s3.create_bucket(
                    Bucket=self._bucket,
                    CreateBucketConfiguration={"LocationConstraint": self._region},
                )

            logger.info(f"Created S3 bucket '{self._bucket}'")

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code == ERROR_CODE_BUCKET_ALREADY_OWNED:
                # Race condition: another process created it
                logger.info(f"Bucket '{self._bucket}' already exists (created by another process)")
                return
            elif error_code == ERROR_CODE_BUCKET_ALREADY_EXISTS:
                raise StorageError(f"S3 bucket name '{self._bucket}' is already taken globally.")
            elif error_code == ERROR_CODE_INVALID_BUCKET_NAME:
                raise StorageError(
                    f"Invalid S3 bucket name '{self._bucket}'. "
                    f"Must be 3-63 chars, lowercase, no underscores."
                )
            else:
                raise StorageError(f"Failed to create S3 bucket '{self._bucket}': {e}")

    @retry(
        retry=retry_if_exception(
            lambda e: (
                isinstance(e, ClientError)
                and e.response["Error"]["Code"] == ERROR_CODE_OPERATION_ABORTED
            )
        ),
        wait=wait_exponential_jitter(initial=0.1, max=2),
        stop=stop_after_attempt(5),
        before_sleep=lambda rs: logger.debug(
            f"Bucket-config PUT got OperationAborted, retrying (attempt {rs.attempt_number})"
        ),
        reraise=True,
    )
    def _put_bucket_config_with_retry(self, operation: str, **kwargs) -> None:
        """Call a ``put_bucket_*`` config op, retrying S3's OperationAborted.

        OperationAborted is S3 rejecting a cross-process race on the shared
        bucket's config (the in-process race is already excluded by provisioning
        once; see ``_provisioned_buckets``).
        """
        getattr(self._s3, operation)(Bucket=self._bucket, **kwargs)

    def _configure_bucket(self) -> None:
        """Apply bucket configuration (idempotent).

        Configures:
        - Versioning (enabled)
        - Server-side encryption (AES256)
        - Public access block (all blocked)
        - Lifecycle policy (expire old versions after 30 days)

        This is called unconditionally after bucket creation or verification
        to ensure partially-configured buckets are completed.

        Raises:
            StorageError: If configuration fails
        """
        try:
            logger.debug(f"Applying configuration to S3 bucket: {self._bucket}")

            self._put_bucket_config_with_retry(
                "put_bucket_versioning",
                VersioningConfiguration={"Status": VERSIONING_STATUS_ENABLED},
            )

            self._put_bucket_config_with_retry(
                "put_bucket_encryption",
                ServerSideEncryptionConfiguration={
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": ENCRYPTION_ALGORITHM
                            }
                        }
                    ]
                },
            )

            self._put_bucket_config_with_retry(
                "put_public_access_block",
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )

            self._put_bucket_config_with_retry(
                "put_bucket_lifecycle_configuration",
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "ExpireOldVersions",
                            "Status": "Enabled",
                            "NoncurrentVersionExpiration": {
                                "NoncurrentDays": 30,
                            },
                            "Filter": {},
                        }
                    ]
                },
            )

            logger.debug(
                f"Applied configuration to '{self._bucket}': versioning, encryption, "
                f"public access block, and lifecycle policy"
            )

        except ClientError as e:
            raise StorageError(f"Failed to configure S3 bucket '{self._bucket}': {e}")

    def _make_full_key(self, relative_key: str) -> str:
        """Combine prefix with relative key."""
        return f"{self._prefix}{relative_key}"

    def save(self, key: str, data: bytes, expected_etag: str | None) -> str:
        """Save data to S3 with conditional write using ETag-based optimistic locking.

        Args:
            key: Relative key within prefix
            data: Data to save
            expected_etag: Expected ETag for conditional write (None for initial write)

        Returns:
            New ETag after successful write

        Raises:
            StorageConflictError: If ETag mismatch (concurrent modification)
            StorageNotFoundError: If expected_etag provided but key doesn't exist
            StorageError: For transient errors (retryable=True)
        """
        full_key = self._make_full_key(key)
        logger.debug(f"Saving to {full_key} (expected_etag={expected_etag})")

        try:
            # Prepare put_object kwargs
            put_kwargs = {
                "Bucket": self._bucket,
                "Key": full_key,
                "Body": data,
            }

            # Add conditional write if expected_etag provided
            if expected_etag is not None:
                put_kwargs["IfMatch"] = expected_etag

            # Perform the PUT
            response = self._s3.put_object(**put_kwargs)

            # Extract and return the new ETag
            new_etag = response["ETag"].strip('"')
            logger.debug(f"Saved {full_key} with ETag {new_etag}")
            return new_etag

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code == ERROR_CODE_PRECONDITION_FAILED:
                try:
                    head_response = self._s3.head_object(Bucket=self._bucket, Key=full_key)
                    actual_etag = head_response["ETag"].strip('"')
                    raise StorageConflictError(
                        key=full_key, expected=expected_etag, actual=actual_etag
                    )
                except ClientError as head_error:
                    head_error_code = head_error.response["Error"]["Code"]
                    if head_error_code in (ERROR_CODE_NO_SUCH_KEY, ERROR_CODE_404):
                        raise StorageNotFoundError(key=full_key)
                    raise StorageError(f"Failed to verify key state for {full_key}: {head_error}")

            elif error_code in (ERROR_CODE_NO_SUCH_KEY, ERROR_CODE_404):
                if expected_etag is not None:
                    raise StorageNotFoundError(key=full_key)
                raise StorageError(f"Unexpected NoSuchKey for {full_key}: {e}")

            elif error_code in TRANSIENT_ERROR_CODES:
                raise StorageError(f"Transient error saving {full_key}: {e}")

            else:
                raise StorageError(f"Failed to save {full_key}: {e}")

    def load(self, key: str) -> tuple[bytes, str]:
        """Load data from S3.

        Args:
            key: Relative key within prefix

        Returns:
            Tuple of (data, etag)

        Raises:
            StorageNotFoundError: If key doesn't exist
            StorageError: For transient errors (retryable=True)
        """
        full_key = self._make_full_key(key)
        logger.debug(f"Loading from {full_key}")

        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=full_key)

            # Read data and extract ETag
            data = response["Body"].read()
            etag = response["ETag"].strip('"')

            logger.debug(f"Loaded {full_key} ({len(data)} bytes, ETag {etag})")
            return (data, etag)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code in (ERROR_CODE_NO_SUCH_KEY, ERROR_CODE_404):
                raise StorageNotFoundError(key=full_key)

            elif error_code in TRANSIENT_ERROR_CODES:
                # Transient errors - mark as retryable
                raise StorageError(f"Transient error loading {full_key}: {e}")

            else:
                raise StorageError(f"Failed to load {full_key}: {e}")

    def exists(self, key: str) -> bool:
        """Check if key exists in S3 using HEAD operation (fast, no download).

        Args:
            key: Relative key within prefix

        Returns:
            True if key exists, False otherwise

        Raises:
            StorageError: For transient errors (retryable=True)
        """
        full_key = self._make_full_key(key)
        logger.debug(f"Checking existence of {full_key}")

        try:
            self._s3.head_object(Bucket=self._bucket, Key=full_key)
            logger.debug(f"Key exists: {full_key}")
            return True

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code in (ERROR_CODE_NO_SUCH_KEY, ERROR_CODE_404):
                logger.debug(f"Key does not exist: {full_key}")
                return False

            elif error_code in TRANSIENT_ERROR_CODES:
                # Transient errors - mark as retryable
                raise StorageError(f"Transient error checking {full_key}: {e}")

            else:
                raise StorageError(f"Failed to check {full_key}: {e}")

    def delete(self, key: str) -> None:
        """Delete key from S3 (idempotent).

        Args:
            key: Relative key within prefix

        Raises:
            StorageError: For transient errors (retryable=True)
        """
        full_key = self._make_full_key(key)
        logger.debug(f"Deleting {full_key}")

        try:
            self._s3.delete_object(Bucket=self._bucket, Key=full_key)
            logger.debug(f"Deleted {full_key}")

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code in TRANSIENT_ERROR_CODES:
                raise StorageError(f"Transient error deleting {full_key}: {e}")
            else:
                raise StorageError(f"Failed to delete {full_key}: {e}")

    def list_keys(self, prefix: str) -> list[str]:
        """List all keys with given prefix using S3 pagination.

        Args:
            prefix: Prefix to filter keys (relative to bucket prefix)

        Returns:
            List of relative keys (without bucket prefix)

        Raises:
            StorageError: For transient errors (retryable=True)
        """
        full_prefix = self._make_full_key(prefix)
        logger.debug(f"Listing keys with prefix: {full_prefix}")

        try:
            keys = []
            paginator = self._s3.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(Bucket=self._bucket, Prefix=full_prefix)

            for page in page_iterator:
                # S3 returns no 'Contents' key if no objects match
                if "Contents" in page:
                    for obj in page["Contents"]:
                        # Strip bucket prefix to get relative key
                        full_key = obj["Key"]
                        relative_key = full_key[len(self._prefix) :]
                        keys.append(relative_key)

            logger.debug(f"Found {len(keys)} keys with prefix {full_prefix}")
            return keys

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code in TRANSIENT_ERROR_CODES:
                # Transient errors - mark as retryable
                raise StorageError(f"Transient error listing keys with prefix {full_prefix}: {e}")

            else:
                raise StorageError(f"Failed to list keys with prefix {full_prefix}: {e}")

    def bulk_delete(self, keys: list[str]) -> dict[str, Exception | None]:
        """Delete multiple keys using S3 batch delete API (up to 1000 keys).

        Args:
            keys: List of relative keys within prefix (max 1000)

        Returns:
            Dict mapping each key to None (success) or Exception (failure)

        Raises:
            StorageError: For transient errors (retryable=True) or if >1000 keys
        """
        if len(keys) == 0:
            logger.debug("Bulk delete called with empty list")
            return {}

        if len(keys) > S3_BATCH_DELETE_MAX_KEYS:
            raise StorageError(
                f"S3 batch delete supports max {S3_BATCH_DELETE_MAX_KEYS} keys, got {len(keys)}"
            )

        logger.debug(f"Bulk deleting {len(keys)} keys")

        try:
            # Build delete request
            full_keys = [self._make_full_key(key) for key in keys]
            delete_objects = [{"Key": full_key} for full_key in full_keys]

            # Perform batch delete
            response = self._s3.delete_objects(
                Bucket=self._bucket, Delete={"Objects": delete_objects}
            )

            # Build result dict (default all to success)
            result: dict[str, Exception | None] = dict.fromkeys(keys)

            # Check for errors
            if "Errors" in response:
                for error in response["Errors"]:
                    # Find the relative key for this error
                    error_full_key = error["Key"]
                    error_relative_key = error_full_key[len(self._prefix) :]

                    # Create exception for this key
                    error_msg = f"{error['Code']}: {error['Message']}"
                    result[error_relative_key] = StorageError(error_msg)

            logger.debug(f"Bulk deleted {len(keys)} keys, {len(response.get('Errors', []))} errors")
            return result

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code in TRANSIENT_ERROR_CODES:
                # Transient errors - mark as retryable
                raise StorageError(f"Transient error during bulk delete: {e}")

            else:
                raise StorageError(f"Failed to bulk delete: {e}")
