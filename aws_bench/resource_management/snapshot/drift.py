"""CloudFormation drift detection for snapshot capture.

``detect_stacks_drift`` submits ``DetectStackDrift`` for every stack up front,
then polls the whole set of detection ids. Detections run concurrently
server-side, so wall-clock is ``max(per-stack)`` rather than the sum.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.snapshot.models import DriftBaseline, ResourceDrift
from aws_bench.resource_management.utils.cloudformation import (
    DRIFT_DETECTION_COMPLETE,
    DRIFT_DETECTION_FAILED,
    get_stack_resource_drifts,
)
from aws_bench.utils.concurrent import build_client, raise_if_shutdown

logger = get_logger(__name__)

# States where drift detection is not supported
DRIFT_UNSUPPORTED_STATES = {
    "ROLLBACK_COMPLETE",
    "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_FAILED",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
    "CREATE_IN_PROGRESS",
    "CREATE_FAILED",
    "UPDATE_FAILED",
    "UPDATE_IN_PROGRESS",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    "IMPORT_IN_PROGRESS",
    "IMPORT_ROLLBACK_IN_PROGRESS",
    "REVIEW_IN_PROGRESS",
}

# Seconds between sweeps over the pending detection-id set.
DRIFT_POLL_INTERVAL_SEC = 5

# Whole-set budget for all detections to reach a terminal status. Bounds
# max(per-stack), not the sum; a stack still pending at the deadline is failed.
DRIFT_DETECTION_TIMEOUT_SEC = 600

# Adaptive retries make botocore self-throttle on Throttling responses, keeping
# the up-front submit burst safe.
DRIFT_CLIENT_CONFIG = Config(retries={"max_attempts": 8, "mode": "adaptive"})

# Max drift detections in flight per account/region. CloudFormation drift APIs
# throttle ("Rate exceeded") and the engine's internals fail with "Internal
# Failure" when too many detections run at once; submitting in bounded waves
# keeps concurrent detections under that ceiling.
DRIFT_MAX_IN_FLIGHT = 12

# Total detection budget as a multiple of the stack count — caps retries of
# failed stacks so a persistently-failing one can't loop forever.
DRIFT_BUDGET_MULTIPLIER = 2

# Backoff after a wave with failures, letting transient load clear before retry.
DRIFT_RETRY_BACKOFF_SEC = 5

# Per-resource FailureReason phrases that mean a tracked resource is gone (deleted
# out-of-band, so Describe<Resource> permanently fails) — the only delete-warranting
# cause. Everything else DETECTION_FAILED reports (Internal Failure, throttling,
# timeout) is transient and must not trigger a delete. Kept specific: a bare
# "not found" is too generic (IAM propagation / eventual consistency say it too).
_RESOURCE_GONE_REASON_MARKERS = (
    "unable to describe",
    "does not exist",
    "nosuchbucket",
    "nosuchentity",
    "resourcenotfoundexception",
    "resource not found",
)


@dataclass(frozen=True)
class TerminalDrift:
    """A stack's terminal drift-detection outcome from one poll pass."""

    status: str  # DRIFT_DETECTION_COMPLETE or DRIFT_DETECTION_FAILED
    reason: str = ""  # DetectionStatusReason, only meaningful when FAILED


def is_resource_unreadable_reason(reason: str) -> bool:
    """Whether a DETECTION_FAILED reason means a tracked resource is permanently gone.

    ``reason`` is CloudFormation's ``DetectionStatusReason`` — JSON of the form
    ``{"Summary": ..., "Failures": [{"Resource", "FailureReason"}, ...]}``. Because
    this gates a destructive delete, the markers are matched ONLY against each
    per-resource ``FailureReason`` — never the ``Summary``, whose generic "Failed to
    detect drift on resources" wording appears even when the underlying per-resource
    failure is transient (e.g. throttling). Unparseable reasons fall back to scanning
    the whole string; unrecognised reasons return False so an unfamiliar failure is
    left in place, never deleted.
    """
    failure_reasons = _extract_failure_reasons(reason)
    if failure_reasons is None:  # not the expected JSON shape — scan whole string
        failure_reasons = [reason]
    return any(
        marker in fr.lower() for fr in failure_reasons for marker in _RESOURCE_GONE_REASON_MARKERS
    )


def _extract_failure_reasons(reason: str) -> list[str] | None:
    """Return the per-resource ``FailureReason`` strings, or None if not parseable.

    None means ``reason`` isn't the expected ``{"Failures": [...]}`` JSON, so the
    caller should fall back to matching the raw string.
    """
    try:
        parsed = json.loads(reason)
    except (json.JSONDecodeError, TypeError):
        return None
    failures = parsed.get("Failures") if isinstance(parsed, dict) else None
    if not isinstance(failures, list):
        return None
    return [f["FailureReason"] for f in failures if isinstance(f, dict) and "FailureReason" in f]


def _parse_resource_drifts(raw_drifts: list[dict]) -> list[ResourceDrift]:
    """Convert raw CloudFormation drift dicts to ``ResourceDrift`` objects."""
    return [
        ResourceDrift(
            logical_resource_id=rd["LogicalResourceId"],
            stack_resource_drift_status=rd["StackResourceDriftStatus"],
            property_differences=rd.get("PropertyDifferences", []),
        )
        for rd in raw_drifts
    ]


def _submit_drift_detections(
    cfn_client: BaseClient,
    stacks: list[tuple[str, str]],
    results: dict[str, DriftBaseline],
) -> dict[str, str]:
    """Kick off drift detection for every supported stack.

    Writes SKIPPED/DETECTION_FAILED baselines into ``results``; returns
    ``{stack_name: detection_id}`` for stacks that must be polled.
    """
    detection_ids: dict[str, str] = {}
    for stack_name, stack_status in stacks:
        raise_if_shutdown()
        if stack_status in DRIFT_UNSUPPORTED_STATES:
            logger.debug(f"Skipping drift detection for stack {stack_name} in state {stack_status}")
            results[stack_name] = DriftBaseline(
                detection_status=f"SKIPPED_{stack_status}", resource_drifts=[]
            )
            continue
        logger.debug(f"Submitting drift detection for stack {stack_name} in state {stack_status}")
        try:
            resp = cfn_client.detect_stack_drift(StackName=stack_name)
            detection_id = resp["StackDriftDetectionId"]
            detection_ids[stack_name] = detection_id
        except (BotoCoreError, ClientError) as e:
            logger.debug(f"Failed to start drift detection for stack {stack_name}: {e}")
            # Record the error as the reason (transient, won't match resource-gone).
            results[stack_name] = DriftBaseline(
                detection_status=DRIFT_DETECTION_FAILED,
                resource_drifts=[],
                detection_status_reason=str(e),
            )

    logger.debug(
        f"Submitted {len(detection_ids)} drift detection(s); "
        f"{len(stacks) - len(detection_ids)} skipped or failed to submit"
    )
    return detection_ids


def _poll_drift_detections(
    cfn_client: BaseClient,
    detection_ids: dict[str, str],
) -> dict[str, TerminalDrift]:
    """Poll the detection-id set to terminal status or the set-wide timeout.

    Returns ``{stack_name: TerminalDrift}``; stacks still pending at the
    deadline are omitted (the caller treats their absence as a timeout).
    """
    terminal: dict[str, TerminalDrift] = {}
    pending = dict(detection_ids)
    deadline = time.monotonic() + DRIFT_DETECTION_TIMEOUT_SEC

    if pending:
        logger.debug(f"Polling {len(pending)} drift detection(s) to completion")

    while pending and time.monotonic() < deadline:
        raise_if_shutdown()
        for stack_name, detection_id in list(pending.items()):
            try:
                resp = cfn_client.describe_stack_drift_detection_status(
                    StackDriftDetectionId=detection_id
                )
                status = resp["DetectionStatus"]
            except (BotoCoreError, ClientError) as e:
                # A failed status read doesn't mean detection failed — it's still
                # running server-side. Keep the stack pending and re-poll; the
                # set-wide timeout is the backstop if the error persists.
                logger.debug(
                    f"Error polling drift detection for stack {stack_name}, will retry: {e}"
                )
                continue
            if status in (DRIFT_DETECTION_COMPLETE, DRIFT_DETECTION_FAILED):
                failed = status == DRIFT_DETECTION_FAILED
                reason = resp.get("DetectionStatusReason", "") if failed else ""
                terminal[stack_name] = TerminalDrift(status=status, reason=reason)
                del pending[stack_name]
                if failed:
                    logger.debug(f"Drift detection failed for stack {stack_name}: {reason or 'NA'}")
        if pending:
            logger.debug(
                f"{len(pending)} drift detection(s) still in progress, "
                f"re-poll in {DRIFT_POLL_INTERVAL_SEC}s: {sorted(pending)}"
            )
            raise_if_shutdown()
            time.sleep(DRIFT_POLL_INTERVAL_SEC)

    return terminal


def _collect_drift_results(
    cfn_client: BaseClient,
    terminal: dict[str, TerminalDrift],
    results: dict[str, DriftBaseline],
) -> None:
    """Fetch resource drifts for completed stacks, recording baselines in ``results``."""
    for stack_name, outcome in terminal.items():
        if outcome.status == DRIFT_DETECTION_FAILED:
            results[stack_name] = DriftBaseline(
                detection_status=DRIFT_DETECTION_FAILED,
                resource_drifts=[],
                detection_status_reason=outcome.reason,
            )
            continue
        try:
            raw_drifts = get_stack_resource_drifts(cfn_client, stack_name)
            results[stack_name] = DriftBaseline(
                detection_status=DRIFT_DETECTION_COMPLETE,
                resource_drifts=_parse_resource_drifts(raw_drifts),
            )
            logger.debug(f"Collected {len(raw_drifts)} resource drift(s) for stack {stack_name}")
        except (BotoCoreError, ClientError) as e:
            # Re-queued and retried in a later wave; only post-retry exhaustion is terminal.
            logger.debug(f"Failed to get drifts for stack {stack_name}: {e}")
            results[stack_name] = DriftBaseline(
                detection_status=DRIFT_DETECTION_FAILED,
                resource_drifts=[],
                detection_status_reason=str(e),
            )


def _detect_wave(
    cfn_client: BaseClient,
    stacks: list[tuple[str, str]],
    results: dict[str, DriftBaseline],
) -> None:
    """Submit → poll → collect one wave of stacks, recording baselines in ``results``."""
    detection_ids = _submit_drift_detections(cfn_client, stacks, results)
    terminal = _poll_drift_detections(cfn_client, detection_ids)

    # A submitted stack missing from `terminal` timed out — fail it (transient).
    for stack_name in detection_ids:
        if stack_name not in terminal:
            # Re-queued for a later wave; a per-attempt timeout is not terminal.
            logger.debug(
                f"Drift detection for stack {stack_name} did not complete within "
                f"{DRIFT_DETECTION_TIMEOUT_SEC}s"
            )
            results[stack_name] = DriftBaseline(
                detection_status=DRIFT_DETECTION_FAILED,
                resource_drifts=[],
                detection_status_reason=f"Timed out after {DRIFT_DETECTION_TIMEOUT_SEC}s",
            )

    _collect_drift_results(cfn_client, terminal, results)


def detect_stacks_drift(
    cfn_client: BaseClient,
    stacks: list[tuple[str, str]],
    *,
    max_in_flight: int = DRIFT_MAX_IN_FLIGHT,
    budget_multiplier: int = DRIFT_BUDGET_MULTIPLIER,
) -> dict[str, DriftBaseline]:
    """Detect drift for many stacks in bounded waves, retrying transient failures.

    Waves of at most ``max_in_flight`` keep concurrent detections under
    CloudFormation's throttle/failure ceiling; a TRANSIENT ``DETECTION_FAILED``
    stack is re-queued and retried in a later wave, capped at ``len(stacks) *
    budget_multiplier`` total detections. A resource-unreadable failure is
    permanent and is NOT retried (retrying can't help).

    Args:
        cfn_client: CloudFormation client (ideally built with DRIFT_CLIENT_CONFIG).
        stacks: ``(stack_name, stack_status)`` pairs to check.
        max_in_flight: Max detections in flight at once (per account/region).
        budget_multiplier: Total-detection cap as a multiple of the stack count.

    Returns:
        ``{stack_name: DriftBaseline}`` for every input stack: ``SKIPPED_<state>``
        for drift-unsupported states (no API call), ``DETECTION_FAILED`` on error
        or timeout. Failures are isolated per stack.
    """
    logger.debug(f"Starting drift detection for {len(stacks)} stack(s) in waves of {max_in_flight}")
    results: dict[str, DriftBaseline] = {}

    queue = list(stacks)
    budget = len(stacks) * budget_multiplier
    submitted = 0

    while queue and submitted < budget:
        wave = queue[:max_in_flight]
        queue = queue[max_in_flight:]
        submitted += len(wave)

        _detect_wave(cfn_client, wave, results)

        # Retry only TRANSIENT failures. A resource-unreadable failure is permanent
        # (the resource is gone) — re-queueing it just burns the budget with backoff
        # sleeps on every reset, so classify it now and move on.
        failed = [
            (n, s)
            for n, s in wave
            if results[n].detection_status == DRIFT_DETECTION_FAILED
            and not is_resource_unreadable_reason(results[n].detection_status_reason)
        ]
        if failed:
            queue.extend(failed)
            # Back off before retrying — but only when work remains (no cost on
            # the healthy path).
            if queue and submitted < budget:
                logger.debug(f"Re-queued {len(failed)} failed stack(s) for retry")
                raise_if_shutdown()
                time.sleep(DRIFT_RETRY_BACKOFF_SEC)

    completed = sum(1 for b in results.values() if b.detection_status == DRIFT_DETECTION_COMPLETE)
    skipped = sum(1 for b in results.values() if b.detection_status.startswith("SKIPPED_"))
    failed_final = [n for n, b in results.items() if b.detection_status == DRIFT_DETECTION_FAILED]
    logger.debug(
        f"Drift detection finished: {completed} completed, {skipped} skipped, "
        f"{len(failed_final)} failed ({submitted} detection(s) submitted)"
    )
    return results


def get_drift_client(session: boto3.Session, region_name: str | None = None) -> BaseClient:
    """Return a CloudFormation client for drift detection (adaptive retries)."""
    return build_client(
        session, "cloudformation", region_name=region_name, config=DRIFT_CLIENT_CONFIG
    )
