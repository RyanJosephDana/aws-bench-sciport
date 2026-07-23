"""CCAPI resource deletion — batched, ordered deletion with retry."""

from __future__ import annotations

import functools
import time
from collections import defaultdict
from collections.abc import Callable, Iterable

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.exceptions import (
    CloudControlResourceDeletionException,
    ResourceExistenceUnsupportedError,
    is_not_found_error,
)
from aws_bench.resource_management.ccapi.models import (
    CUSTOM_RESOURCE_PREFIX,
    MAX_WORKERS_LIGHT,
    PROTECTED_IAM_ROLE_NAMES,
    SERVICE_ROLE_PREFIX,
    DeletionFailureEvent,
    PollResult,
    Resource,
    SubmitResult,
)
from aws_bench.utils.concurrent import interruptible_executor, raise_if_shutdown

logger = get_logger(__name__)

_TIMEOUT = 900  # 15 minutes - VPC resources can take 10-15 min for async deletion
_POLLING_INTERVAL = 3
_PASSES_PER_BATCH = 2
_PROGRESS_LOG_INTERVAL_SEC = 30  # Log deletion progress every 30 seconds

# Deletion is ordered by level, highest first (see _order_batches). Each level's
# batch runs to a terminal state before the next starts, so a dependency that
# must outlive its dependents is given a LOWER level. Types not listed here use
# _MAX_RESOURCE_LEVEL and are deleted first — this covers the "owners" (EC2
# instances, load balancers, DB instances) whose teardown releases the dependent
# infra below, so the default-first behavior is intentional.
#
# The tail levels below fix the post-run reset's "dependent object" / "currently
# in use" / "instances still members" residuals:
# the owner deletes first (default level), then its freed plumbing drains in
# order — ENIs/volumes/param groups, then security groups, then subnets/route
# tables, then the VPC itself, then the IPAM pool it allocated from, then IAM
# roles last.
_LEVEL_TO_RESOURCE_TYPES: dict[int, list[str]] = {
    # Target groups: after their load balancer/listener (referenced by listeners).
    50: ["AWS::ElasticLoadBalancingV2::TargetGroup"],
    # Freed once the owning instance/LB/DMS instance (default level) is gone.
    40: [
        "AWS::EC2::NetworkInterface",
        "AWS::EC2::Volume",
        # Parameter/option groups: only deletable once member DB instances/clusters
        # (default level) are deleted.
        "AWS::RDS::DBParameterGroup",
        "AWS::RDS::DBClusterParameterGroup",
        "AWS::RDS::OptionGroup",
        "AWS::RDS::DBSubnetGroup",
        "AWS::Neptune::DBParameterGroup",
        "AWS::Neptune::DBClusterParameterGroup",
        "AWS::Neptune::DBSubnetGroup",
        "AWS::DocDB::DBSubnetGroup",
    ],
    # Security groups: after the ENIs that reference them are released.
    30: ["AWS::EC2::SecurityGroup"],
    # VPC plumbing: after ENIs and security groups are gone.
    20: [
        "AWS::EC2::Subnet",
        "AWS::EC2::RouteTable",
        "AWS::EC2::NetworkAcl",
    ],
    # The VPC itself: after all its plumbing is gone.
    10: ["AWS::EC2::VPC"],
    # IPAM child pool: after the VPC frees its allocation, so the CIDR can deprovision.
    5: ["AWS::EC2::IPAMPool"],
    # IAM roles are deleted last since other resources may depend on them.
    0: ["AWS::IAM::Role"],
}
_MAX_RESOURCE_LEVEL = 1000
_RESOURCE_TYPE_TO_LEVELS = {rt: lvl for lvl, rts in _LEVEL_TO_RESOURCE_TYPES.items() for rt in rts}

ResourceExistsFn = Callable[[Resource], bool]


class Deleter:
    """Handles batched, ordered resource deletion via CCAPI."""

    def __init__(self, client, *, resource_exists_fn: ResourceExistsFn) -> None:
        """Initialize with a CCAPI client and resource existence checker."""
        self._client = client
        self._resource_exists_fn = resource_exists_fn

    def delete_resources(
        self,
        resources: list[Resource],
        *,
        n_passes: int = _PASSES_PER_BATCH,
    ) -> dict[Resource, DeletionFailureEvent]:
        """Delete resources via CCAPI with ordered batching and retry.

        Returns dict of Resource → failure for resources that couldn't be deleted.
        """
        filtered = self._filter_deletable(resources)
        if not filtered:
            return {}
        batches = self._order_batches(filtered)
        return self._execute_batches(batches, n_passes)

    def _filter_deletable(self, resources: list[Resource]) -> list[Resource]:
        with interruptible_executor(max_workers=MAX_WORKERS_LIGHT) as executor:
            check_fn = functools.partial(
                self._check_deletable, resource_exists_fn=self._resource_exists_fn
            )
            filtered = [
                resource for resource in executor.map(check_fn, resources) if resource is not None
            ]
        logger.debug("After filtering: %d of %d resources to delete", len(filtered), len(resources))
        return filtered

    def _execute_batches(
        self,
        batches: list[list[Resource]],
        n_passes: int,
    ) -> dict[Resource, DeletionFailureEvent]:
        all_failures: dict[Resource, DeletionFailureEvent] = {}
        for batch in batches:
            token_to_resource: dict[str, Resource] = {}
            remaining = set(batch)
            pending_tokens: set[str] = set()
            failed: dict[Resource, DeletionFailureEvent] = {}

            for _ in range(n_passes):
                if not remaining:
                    break
                self._process_batch_pass(remaining, pending_tokens, token_to_resource, failed)

            for token in pending_tokens:
                all_failures[token_to_resource[token]] = DeletionFailureEvent("Still pending")
            all_failures.update(failed)
        return all_failures

    def _process_batch_pass(
        self,
        remaining: set[Resource],
        pending_tokens: set[str],
        token_to_resource: dict[str, Resource],
        failed: dict[Resource, DeletionFailureEvent],
    ) -> None:
        """Run one submit-and-poll pass, updating all mutable state in place."""
        submit_result = self._submit_deletions(remaining)
        pending_tokens.update(submit_result.tokens)
        token_to_resource.update(submit_result.tokens)

        poll_result = self._poll_deletions(pending_tokens)
        for token in poll_result.succeeded | poll_result.pending:
            failed.pop(token_to_resource[token], None)
        failed.update(submit_result.failures)
        failed.update(
            {token_to_resource[token]: poll_result.failed[token] for token in poll_result.failed}
        )
        # Drop concurrent-op resources: no token, so retrying just wastes a call.
        remaining -= submit_result.already_handled
        remaining -= {
            token_to_resource[token] for token in poll_result.succeeded | poll_result.pending
        }
        pending_tokens -= poll_result.succeeded | set(poll_result.failed)

    def _check_deletable(
        self, resource: Resource, resource_exists_fn: ResourceExistsFn
    ) -> Resource | None:
        """Return the resource if it should be deleted, None otherwise."""
        if resource.type.count("::") != 2 or resource.type.startswith(CUSTOM_RESOURCE_PREFIX):
            logger.debug(
                "Skip %s %s: not a CCAPI-compatible type", resource.type, resource.identifier
            )
            return None
        try:
            if not resource_exists_fn(resource):
                logger.debug("Skip %s %s: no longer exists", resource.type, resource.identifier)
                return None
        except ResourceExistenceUnsupportedError:
            # CCAPI fundamentally cannot operate on this type, so delete_resource would fail the
            # same way — skipping is correct here (there is nothing to attempt).
            logger.debug("Skip %s %s: not supported by CCAPI", resource.type, resource.identifier)
            return None
        except Exception as e:
            # Existence is UNVERIFIED (throttle, a Cloud Control handler InternalFailure, or
            # another transient error). The caller already identified this resource as an orphan
            # to remove, so attempt the delete rather than silently leaking it.
            logger.warning(
                "Existence unverified for %s %s (%s); attempting deletion anyway to avoid "
                "leaking a possible orphan",
                resource.type,
                resource.identifier,
                e,
            )
        if resource.type == "AWS::IAM::Role":
            if resource.identifier.startswith(SERVICE_ROLE_PREFIX):
                logger.info(f"Skip {resource.type} {resource.identifier}: service role")
                return None
            if resource.identifier in PROTECTED_IAM_ROLE_NAMES:
                logger.warning(
                    f"Skip {resource.type} {resource.identifier}: protected role — "
                    f"deleting it would sever cross-account access"
                )
                return None
        return resource

    def _delete_resource_request(self, resource: Resource) -> str:
        """Create a deletion request.

        Returns the RequestToken, or "" when a concurrent op is already deleting
        the resource (caller treats "" as "already handled" and stops retrying).
        """
        try:
            resp = self._client.delete_resource(
                TypeName=resource.type, Identifier=resource.identifier
            )
            return resp["ProgressEvent"]["RequestToken"]
        except Exception as e:
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if error_code == "ConcurrentOperationException":
                # Already being deleted by another op (e.g. stack deletion); skip.
                logger.info(
                    "Skip %s %s: concurrent deletion already in progress",
                    resource.type,
                    resource.identifier,
                )
                return ""  # Empty token signals "already handled"
            if error_code == "ResourceNotFoundException" or is_not_found_error(e):
                # Already gone — e.g. deleted out-of-band, or between an unverified existence
                # check (see _check_deletable) and this call. A missing resource is the desired
                # end state, not a failure, so treat it as handled rather than raising.
                logger.info("Skip %s %s: already gone", resource.type, resource.identifier)
                return ""  # Empty token signals "already handled"
            logger.error("Error deleting %s %s: %s", resource.type, resource.identifier, e)
            raise CloudControlResourceDeletionException(resource, e) from e

    def _order_batches(self, resources: list[Resource]) -> list[list[Resource]]:
        by_level: dict[int, list[Resource]] = defaultdict(list)
        for resource in resources:
            by_level[_RESOURCE_TYPE_TO_LEVELS.get(resource.type, _MAX_RESOURCE_LEVEL)].append(
                resource
            )
        return [by_level[lvl] for lvl in sorted(by_level, reverse=True)]

    def _submit_deletions(self, resources: Iterable[Resource]) -> SubmitResult:
        tokens: dict[str, Resource] = {}
        failures: dict[Resource, DeletionFailureEvent] = {}
        already_handled: set[Resource] = set()
        for resource in resources:
            try:
                token = self._delete_resource_request(resource)
                if token:
                    tokens[token] = resource
                else:
                    # Empty token: concurrent op already deleting it — don't retry.
                    already_handled.add(resource)
            except CloudControlResourceDeletionException as e:
                failures[resource] = DeletionFailureEvent(str(e))
        return SubmitResult(tokens=tokens, failures=failures, already_handled=already_handled)

    def _poll_deletions(self, tokens: Iterable[str]) -> PollResult:
        pending = set(tokens)
        succeeded: set[str] = set()
        failed: dict[str, DeletionFailureEvent] = {}
        start = time.monotonic()
        last_log_time = start
        total_tokens = len(pending)

        while pending and time.monotonic() - start < _TIMEOUT:
            raise_if_shutdown()
            for token in list(pending):
                self._check_token_status(token, pending, succeeded, failed)
            if pending:
                # Log progress periodically
                if time.monotonic() - last_log_time >= _PROGRESS_LOG_INTERVAL_SEC:
                    elapsed = time.monotonic() - start
                    logger.debug(
                        "CCAPI deletion progress: %d/%d resources completed (%ds elapsed)",
                        len(succeeded) + len(failed),
                        total_tokens,
                        int(elapsed),
                    )
                    last_log_time = time.monotonic()
                raise_if_shutdown()
                time.sleep(_POLLING_INTERVAL)

        if pending:
            logger.warning(
                "Polling timed out after %ds with %d token(s) still pending",
                _TIMEOUT,
                len(pending),
            )

        return PollResult(succeeded=succeeded, pending=pending, failed=failed)

    def _check_token_status(
        self,
        token: str,
        pending: set[str],
        success: set[str],
        failed: dict[str, DeletionFailureEvent],
    ) -> None:
        """Check a single deletion token and update the tracking sets."""
        try:
            resp = self._client.get_resource_request_status(RequestToken=token)
            status = resp["ProgressEvent"]["OperationStatus"]
        except Exception as exc:
            pending.discard(token)
            failed[token] = DeletionFailureEvent(str(exc))
            return

        if status == "SUCCESS":
            pending.discard(token)
            success.add(token)
        elif status == "FAILED":
            pending.discard(token)
            failed[token] = DeletionFailureEvent.from_ccapi_event(resp["ProgressEvent"])
