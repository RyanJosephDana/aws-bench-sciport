"""Local scan adapter that runs the fast scan in a management-account Lambda.

Drop-in behind ``make_scanner``: for one (account, region) it invokes the
discovery Lambda synchronously and projects the returned raw FastScanResult
onto CFN types. A transient invoke failure is retried with backoff; if the
Lambda still cannot produce a result, the scan raises rather than falling back
to the in-process host scan — the host path is exactly what broke at scale and
motivated moving the scan to Lambda, so silently degrading to it would defeat
the feature and surface as a mysterious failure downstream.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import boto3
import tenacity
from botocore.config import Config

from aws_bench.exceptions import AWSBenchError
from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import ScanResult
from aws_bench.resource_management.fastscan.constants import (
    HOST_INVOKE_READ_TIMEOUT_S,
    LAMBDA_FUNCTION_NAME,
)
from aws_bench.resource_management.fastscan.lambda_protocol import build_event, result_from_wire
from aws_bench.resource_management.fastscan.manager import FastScanManager
from aws_bench.resource_management.fastscan.models import FastScanResult
from aws_bench.utils.concurrent import build_client
from aws_bench.utils.credentials_provider import CredentialProvider

logger = get_logger(__name__)

# A transient invoke fault (throttle, cold-start timeout, brief control-plane blip) is retried;
# an exhausted retry propagates. Tenacity is safe here because this adapter is host-only, not in
# the Lambda closure.
_INVOKE_RETRY_ATTEMPTS = 4

# The synchronous invoke must outlast the Lambda's own run, or the client abandons a still-healthy
# execution at botocore's 60s default read_timeout — a spurious fault plus a server-side zombie.
# Botocore's own retries are disabled so tenacity is the single retry authority; otherwise a slow
# scan would be re-invoked under each tenacity attempt.
_INVOKE_CONFIG = Config(
    read_timeout=HOST_INVOKE_READ_TIMEOUT_S,
    connect_timeout=10,
    retries={"max_attempts": 0},
)

# Cap concurrent in-flight invokes across all (account, region) scanners in the process. Each
# invoke holds a synchronous connection for the scan's length; the accounts × regions fan-out
# could otherwise exhaust the account's Lambda concurrency, and a throttled invoke raises (no
# fallback). The semaphore queues the excess instead of failing it.
_MAX_CONCURRENT_INVOKES = 50
_invoke_semaphore = threading.Semaphore(_MAX_CONCURRENT_INVOKES)


class LambdaScanTransient(AWSBenchError):
    """A transient fault invoking the discovery Lambda; retried, then propagated if it persists."""


class LambdaScanFatal(AWSBenchError):
    """A deterministic Lambda-level fault: the runtime killed the process.

    The kill is a task timeout, OOM, or an oversized (>6MB) response payload. Not retried:
    re-invoking runs the identical scan against the identical (account, region) into the same
    wall, so a retry only burns another full function-timeout of wall-clock and cost before
    failing the same way. It propagates on the first occurrence. A sibling of (not a subclass
    of) :class:`LambdaScanTransient` precisely so the retry predicate skips it.
    """


class LambdaScanner:
    """Scanner backend that offloads one region's scan to the discovery Lambda."""

    def __init__(
        self, session: boto3.Session, account_id: str, region_name: str | None = None
    ) -> None:
        """Bind the target-account session and the account id the Lambda scans.

        ``session`` is the target-account regional session, used only for projection
        (the type universe); ``account_id`` names the account the Lambda scans.
        """
        self._session = session
        self._account_id = account_id
        self._region_name = region_name or session.region_name
        self._manager = FastScanManager(session, region_name=region_name)
        self._lambda_client: Any = None

    def scan_resources(
        self, resource_types: list[str] | None = None, region: str | None = None
    ) -> ScanResult:
        """Return a projected ScanResult from the discovery Lambda; raise if it cannot.

        Raises :class:`LambdaScanTransient` when a transient fault persists across retries,
        or :class:`LambdaScanFatal` immediately on a deterministic runtime-kill fault (task
        timeout / OOM / oversized response). There is deliberately no host-scan fallback for
        either — see module docstring.
        """
        region = region or self._region_name or "us-east-1"
        types = (
            resource_types if resource_types is not None else self._manager.get_scannable_types()
        )
        raw = self._invoke_with_retry(self._account_id, region)
        logger.debug("Fast-scan for %s via lambda", region)
        # Re-log the wire's failed listers host-side (also in CloudWatch). Mostly
        # listers not available in a region — expected, high-volume, so TRACE:
        # kept in the ledger run.log, filtered off the DEBUG job/trial sinks.
        for lister_key, error in raw.failed.items():
            logger.trace("Lister %s failed in %s: %s", lister_key, region, error)
        return FastScanManager.project(raw, types)

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(LambdaScanTransient),
        wait=tenacity.wait_exponential(multiplier=2, min=2, max=15) + tenacity.wait_random(0, 2),
        stop=tenacity.stop_after_attempt(_INVOKE_RETRY_ATTEMPTS),
        reraise=True,
    )
    def _invoke_with_retry(self, account_id: str, region: str) -> FastScanResult:
        """Invoke the Lambda, retrying transient faults with backoff; reraise if exhausted."""
        return self._invoke(account_id, region)

    def _client(self) -> Any:
        """The management-account Lambda client, built once and reused.

        Built lazily and cached: construction serializes on the process-wide build lock, so
        rebuilding it per invoke (and per retry) would contend needlessly; built clients are
        safe to call concurrently.
        """
        if self._lambda_client is None:
            mgmt_session = CredentialProvider.get().get_management_session()
            self._lambda_client = build_client(mgmt_session, "lambda", config=_INVOKE_CONFIG)
        return self._lambda_client

    def _invoke(self, account_id: str, region: str) -> FastScanResult:
        """Synchronously invoke the discovery Lambda and return its raw result.

        Raises :class:`LambdaScanFatal` (not retried) on a runtime-kill fault, and
        :class:`LambdaScanTransient` (retried) on any transient fault.
        """
        try:
            with _invoke_semaphore:
                response = self._client().invoke(
                    FunctionName=LAMBDA_FUNCTION_NAME,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(build_event(account_id, region)).encode(),
                )
            # FunctionError on a 200 means the runtime killed the process — task timeout, OOM, or
            # an oversized (>6MB) response — with an {"errorMessage": ...} body, not our envelope.
            # (An error inside the handler is caught there and returned as ok:false, handled
            # below.) All three are deterministic against this fixed (account, region), so raise
            # the non-retryable LambdaScanFatal.
            if response.get("FunctionError"):
                payload = json.loads(response["Payload"].read())
                detail = payload.get("errorMessage", response["FunctionError"])
                raise LambdaScanFatal(f"lambda function error: {detail}")
            payload = json.loads(response["Payload"].read())
            if not payload.get("ok"):
                raise LambdaScanTransient(payload.get("error", "unknown lambda error"))
            result = payload.get("result")
            if result is None:
                raise LambdaScanTransient("ok envelope missing 'result'")
            return result_from_wire(result)
        except (LambdaScanTransient, LambdaScanFatal):
            raise  # already classified; don't re-wrap the deterministic one as retryable
        except Exception as exc:  # noqa: BLE001 — any invoke/deserialize fault is a retryable fault
            raise LambdaScanTransient(str(exc)) from exc
