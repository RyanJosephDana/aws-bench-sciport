"""Fast-scan engine: the concurrent native-API lister sweep.

Depends only on the fast-scan package, ``utils.concurrent``, and the standard
library + boto3 — deliberately free of the CloudControl (``ccapi``) projection
layer and the credential-provider chain, so it is the whole import closure the
discovery Lambda ships. Projection onto CloudFormation type names lives in
``fastscan.manager.FastScanManager`` (host-only).
"""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import Future, as_completed
from typing import Any

import boto3
from botocore.exceptions import ClientError

from aws_bench.account_management.constants import FRESH_ACCOUNT_TRANSIENT_CODES
from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.fastscan.constants import SCAN_SWEEP_TIMEOUT_S
from aws_bench.resource_management.fastscan.listers import all_listers
from aws_bench.resource_management.fastscan.listers.model import Lister, SessionLike
from aws_bench.resource_management.fastscan.listers.region_policy import UNAVAILABLE_LISTER_REGIONS
from aws_bench.resource_management.fastscan.listers.region_skip import LISTER_REGION_SKIP
from aws_bench.resource_management.fastscan.models import MAX_LISTER_WORKERS, FastScanResult
from aws_bench.utils.concurrent import (
    build_client,
    build_session,
    interruptible_executor,
    raise_if_shutdown,
)

logger = get_logger(__name__)

# Transient server-side error codes (5xx + throttling) that warrant a retry.
# These are NOT retried by botocore's standard/adaptive mode in all cases
# (adaptive handles throttling but not all 5xx variants), so we retry them
# at the lister level as well. If they persist beyond all retries, they
# trigger a snapshot abort to prevent partial/unreliable state.
_TRANSIENT_SERVER_CODES = frozenset(
    {
        "InternalError",
        "InternalServerError",
        "InternalServiceError",
        "InternalServiceException",
        "InternalException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
    }
)

_RETRYABLE_CODES = FRESH_ACCOUNT_TRANSIENT_CODES | _TRANSIENT_SERVER_CODES

_LISTER_RETRY_ATTEMPTS = 8
_LISTER_RETRY_MAX_WAIT = 60.0


def _is_retryable(code: str | None) -> bool:
    return bool(code) and any(pattern in code for pattern in _RETRYABLE_CODES)


class _CachingSession:
    """A boto3.Session proxy that memoizes ``.client(service)`` per service.

    Builds via the shared construction lock (see ``build_client``); built clients
    are safe to call concurrently, only construction races. The per-instance cache
    pre-check stays lock-free so repeat lookups never contend.
    """

    def __init__(self, session: SessionLike) -> None:
        self._session = session
        self._clients: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}
        self._cache_lock = threading.Lock()

    def client(self, service_name: str, **kwargs: Any) -> Any:
        """Return a cached client for ``service_name`` + kwargs (built once, reused thereafter)."""
        cache_key = (service_name, tuple(sorted((k, repr(v)) for k, v in kwargs.items())))
        cached = self._clients.get(cache_key)
        if cached is not None:
            return cached
        with self._cache_lock:
            cached = self._clients.get(cache_key)
            if cached is None:
                cached = build_client(self._session, service_name, **kwargs)
                self._clients[cache_key] = cached
            return cached

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class FastResourceScanner:
    """Discovers resources via per-service native list/describe APIs."""

    def __init__(self, session: boto3.Session) -> None:
        """Initialize with a boto3 session whose frozen creds drive the cached region sweep."""
        self._session = session

    def _listers(self) -> list[Lister]:
        """Every lister to run: the unified :func:`~.listers.all_listers` set (one run path)."""
        return all_listers()

    def _caching_session(self, creds: dict[str, str], region: str) -> _CachingSession:
        """Build one region-bound session from frozen creds, wrapped to cache clients."""
        sess = build_session(
            lambda: boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds.get("SessionToken"),
                region_name=region,
            )
        )
        return _CachingSession(sess)

    @staticmethod
    def _normalize(raw: list[Any]) -> list[str]:
        """Coerce a lister's return (list[str] | list[dict]) to a flat id list."""
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                rid = item.get("id") or item.get("arn") or item.get("Arn")
                if rid:
                    out.append(str(rid))
        return out

    @staticmethod
    def _error_code(exc: Exception) -> str:
        """Extract a compact error code/message from a boto/other exception."""
        resp = getattr(exc, "response", None)
        if isinstance(resp, dict):
            code = resp.get("Error", {}).get("Code")
            if code:
                return str(code)
        return re.sub(r"\s+", " ", str(exc))[:200]

    @staticmethod
    def _run_lister_call(lister: Lister, session: _CachingSession) -> list[Any]:
        """Invoke one lister's AWS call, retrying transient errors.

        Retries fresh-account-transient codes, 5xx server errors, and throttling.
        Short wait (exponential 2s→60s + jitter, 8 attempts): hundreds of listers
        share MAX_LISTER_WORKERS under overall_timeout.
        """
        for attempt in range(1, _LISTER_RETRY_ATTEMPTS + 1):
            try:
                return lister.run(session) or []
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                logger.debug(
                    "Lister %s:%s failed with %s: %s",
                    lister.service,
                    lister.op,
                    code,
                    exc,
                )
                if attempt == _LISTER_RETRY_ATTEMPTS or not _is_retryable(code):
                    raise
                wait = min(2.0 * 2 ** (attempt - 1), _LISTER_RETRY_MAX_WAIT) + random.uniform(0, 2)
                time.sleep(wait)
        return []  # unreachable: the loop returns or raises on the final attempt

    def _run_lister(
        self, lister: Lister, session: _CachingSession
    ) -> tuple[str, list[str], str | None]:
        """Run one lister against the shared caching session, returning (key, ids, error)."""
        # A queued lister bails on a flagged shutdown before running its AWS call.
        # OperationCancelled travels past the trial's Exception handlers.
        raise_if_shutdown()
        key = f"{lister.service}:{lister.op}"
        try:
            return key, self._normalize(self._run_lister_call(lister, session)), None
        except Exception as exc:  # noqa: BLE001
            error = self._error_code(exc)
            logger.debug("Lister %s failed: %s | full: %s", key, error, exc)
            return key, [], error

    def scan(self, region: str, *, overall_timeout: float = SCAN_SWEEP_TIMEOUT_S) -> FastScanResult:
        """Run every lister concurrently against the account in ``region`` (one pool)."""
        frozen = self._session.get_credentials().get_frozen_credentials()
        creds = {
            "AccessKeyId": frozen.access_key,
            "SecretAccessKey": frozen.secret_key,
            "SessionToken": frozen.token,
        }
        session = self._caching_session(creds, region)
        result = FastScanResult()

        def _record(key: str, ids: list[str], error: str | None) -> None:
            if error is not None:
                result.failed[key] = error
            elif ids:
                # Extend, don't overwrite: if two listers share a "service:op" key their
                # ids merge instead of the last-completing future clobbering the rest.
                result.discovered.setdefault(key, []).extend(ids)
            else:
                result.empty.add(key)

        listers = self._listers()
        # Enforce key uniqueness: a duplicate "service:op" means two listers feed the same
        # result key, whose merge order is nondeterministic. Fail at build time — a superseded
        # simple lister must be listed in the listers package's SUPERSEDED_BY_CUSTOM_LISTER.
        keys = [f"{lister.service}:{lister.op}" for lister in listers]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(
                f"Duplicate fast-scan lister keys (add the superseded simple lister to "
                f"SUPERSEDED_BY_CUSTOM_LISTER): {', '.join(duplicates)}"
            )

        # Skip a lister in this region if its (service, op) has no endpoint here (probe-measured
        # LISTER_REGION_SKIP) or its endpoint is reachable but the scan can't obtain a result here
        # (UNAVAILABLE_LISTER_REGIONS — no successful response across retries, which would abort
        # the region via the fail-loud gate). Both maps are (service, op)-keyed, so a healthy
        # sibling op is never dropped. Record it ``empty``, never ``failed``: downstream treats a
        # failed type as un-enumerated and skips it, hiding real resources.
        to_run: list[Lister] = []
        for lister in listers:
            key = (lister.service, lister.op)
            no_endpoint = region in LISTER_REGION_SKIP.get(key, frozenset())
            unavailable = region in UNAVAILABLE_LISTER_REGIONS.get(key, frozenset())
            if no_endpoint or unavailable:
                result.empty.add(f"{lister.service}:{lister.op}")
            else:
                to_run.append(lister)
        skipped = len(listers) - len(to_run)
        listers = to_run

        logger.info(
            f"Fast-scanning {region} with {len(listers)} listers on {MAX_LISTER_WORKERS} workers "
            f"({skipped} skipped: no endpoint or no result available in region)"
        )

        # Listers ship grouped by service; submitting in order makes a worker wave hammer one
        # service's throttle bucket. Shuffle to spread services across the wave and decorrelate
        # concurrent (account, region) sweeps. Order does not affect the result.
        random.shuffle(listers)

        # Each future is mapped to its "<service>:<op>" key so a lister still pending at the
        # timeout is recorded as failed, not dropped — a dropped key makes its CFN type look
        # empty/clean when it was never enumerated. interruptible_executor cancels queued
        # futures and re-raises on Ctrl+C, so with the workers' raise_if_shutdown checkpoints a
        # SIGTERM mid-sweep unwinds promptly rather than blocking up to overall_timeout.
        with interruptible_executor(max_workers=MAX_LISTER_WORKERS) as pool:
            futures: dict[Future[tuple[str, list[str], str | None]], str] = {}
            for lister in listers:
                futures[pool.submit(self._run_lister, lister, session)] = (
                    f"{lister.service}:{lister.op}"
                )
            pending = set(futures)
            try:
                for fut in as_completed(futures, timeout=overall_timeout):
                    pending.discard(fut)
                    _record(*fut.result())
            except TimeoutError:
                # A future can finish between as_completed's timeout and this handler yet still
                # be in ``pending``. Record its real result: falsely failing a scanned type
                # would make find_new_resources treat it as un-enumerated and skip it. Only
                # futures still running are recorded as timed out.
                for fut in pending:
                    if fut.done():
                        _record(*fut.result())
                        continue
                    fut.cancel()
                    _record(futures[fut], [], f"timed out after {overall_timeout}s")
                logger.warning(
                    f"Fast-scan in {region} timed out with {len(pending)} listers pending"
                )
        # A shutdown flagged with no in-flight worker left to raise would otherwise return a
        # result here; abort with the cancellation exception instead.
        raise_if_shutdown()
        return result
