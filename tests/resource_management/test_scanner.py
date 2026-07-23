"""Tests for the resource scanner: engine, projection manager, and backend factory."""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from aws_bench.exceptions import OperationCancelled
from aws_bench.resource_management.fastscan import engine as engine_module
from aws_bench.resource_management.fastscan.engine import FastResourceScanner, _CachingSession
from aws_bench.resource_management.fastscan.listers import all_listers
from aws_bench.resource_management.fastscan.listers.model import Lister
from aws_bench.resource_management.fastscan.manager import FastScanManager
from aws_bench.resource_management.fastscan.models import FastScanResult
from aws_bench.resource_management.scanner import (
    SCAN_METHOD_ENV_VAR,
    make_scanner,
    scan_method,
)
from aws_bench.utils import concurrent

# =============================================================================
# Engine: client caching + single-pool scan (one run path).
# =============================================================================


class _RealSession:
    """Stub boto3.Session that counts how many times .client() actually builds a client."""

    def __init__(self):
        self.build_count = 0
        self.region_name = "us-east-1"

    def client(self, service_name, **_kw):
        self.build_count += 1
        return f"client:{service_name}#{self.build_count}"

    def some_other_attr(self):
        return "delegated"


def test_caching_session_builds_one_client_per_service():
    real = _RealSession()
    cs = _CachingSession(real)
    # Repeat calls for the same service return the same memoized client (built once).
    first = cs.client("s3")
    assert cs.client("s3") is first
    assert cs.client("s3") is first
    # A different service builds a second client.
    ec2 = cs.client("ec2")
    assert ec2 != first
    assert real.build_count == 2  # exactly one build per distinct service


def test_caching_session_delegates_other_attrs():
    real = _RealSession()
    cs = _CachingSession(real)
    assert cs.region_name == "us-east-1"
    assert cs.some_other_attr() == "delegated"


def test_caching_session_keys_cache_by_kwargs():
    real = _RealSession()
    cs = _CachingSession(real)
    # Same service + same kwargs → one cached client; different kwargs → its own client, so a
    # caller asking for a different region/config never silently receives the first one.
    c1 = cs.client("kms", region_name="us-west-2")
    assert cs.client("kms", region_name="us-west-2") is c1
    c2 = cs.client("kms", region_name="eu-west-1")
    assert c2 is not c1
    assert real.build_count == 2


def test_client_construction_never_overlaps_across_sessions():
    """boto3 client construction is serialized process-wide, across ALL _CachingSession instances.

    boto3.Session.client() is not thread-safe to call concurrently — overlapping
    construction crashes the interpreter in the OpenSSL C module (SIGSEGV). The
    scan fans out up to MAX_WORKERS_ACCOUNT * MAX_WORKERS_HEAVY region pools, each
    with its OWN _CachingSession, so a per-instance lock does NOT prevent overlap.
    This asserts a shared lock does: no two builds are ever in flight at once.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    in_flight = 0
    max_overlap = 0
    guard = threading.Lock()

    class _SlowBuildSession:
        """A session whose .client() build overlaps if two run concurrently."""

        region_name = "us-east-1"

        def client(self, service_name, **_kw):
            nonlocal in_flight, max_overlap
            with guard:
                in_flight += 1
                max_overlap = max(max_overlap, in_flight)
            # Widen the window where a concurrent build would be detected.
            import time

            time.sleep(0.005)
            with guard:
                in_flight -= 1
            return f"client:{service_name}"

    # Distinct instances (as the real scan has one per region pool) building the same
    # service concurrently — the per-instance lock would let these overlap.
    sessions = [_CachingSession(_SlowBuildSession()) for _ in range(8)]

    def _build(cs):
        return cs.client("dynamodb")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_build, sessions))

    assert max_overlap == 1, f"client construction overlapped ({max_overlap} concurrent builds)"


# -- engine helpers: normalize, error extraction, single-lister runner --


def test_normalize_flattens_strings_and_id_dicts():
    # A lister may return bare strings or dicts; _normalize pulls id/arn/Arn and drops keyless.
    raw = ["str-id", {"id": "d-id"}, {"arn": "d-arn"}, {"Arn": "d-Arn"}, {"other": "x"}, {}]
    assert FastResourceScanner._normalize(raw) == ["str-id", "d-id", "d-arn", "d-Arn"]


def test_error_code_prefers_boto_error_code():
    exc = ClientError({"Error": {"Code": "AccessDenied"}}, "ListStuff")
    assert FastResourceScanner._error_code(exc) == "AccessDenied"


def test_error_code_falls_back_to_message():
    assert FastResourceScanner._error_code(ValueError("boom  \n  bang")) == "boom bang"


def test_run_lister_returns_key_and_normalized_ids():
    scanner = FastResourceScanner(MagicMock())
    lister = Lister("s3", "ListBuckets", lambda _s: [{"Arn": "arn:b"}])
    key, ids, err = scanner._run_lister(lister, MagicMock())
    assert key == "s3:ListBuckets"
    assert ids == ["arn:b"]
    assert err is None


def test_run_lister_captures_error(_instant_lister_retry):
    scanner = FastResourceScanner(MagicMock())

    # A non-retryable code (Throttling is now a transient/retryable code) so the error is
    # captured on the first raise rather than retried.
    def _boom(_s):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListBuckets")

    key, ids, err = scanner._run_lister(Lister("s3", "ListBuckets", _boom), MagicMock())
    assert key == "s3:ListBuckets"
    assert ids == []
    assert err == "AccessDenied"


@pytest.fixture
def _instant_lister_retry(mocker):
    """Neutralize the fresh-account lister backoff so retry tests don't sleep."""
    mocker.patch.object(engine_module.time, "sleep")


def test_run_lister_retries_fresh_account_optin_then_succeeds(_instant_lister_retry):
    """A fresh-account OptInRequired is retried per lister and a later success is captured."""
    scanner = FastResourceScanner(MagicMock())
    calls = {"n": 0}

    def _flaky(_s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ClientError({"Error": {"Code": "OptInRequired"}}, "ListTables")
        return [{"id": "table-1"}]

    key, ids, err = scanner._run_lister(Lister("dynamodb", "ListTables", _flaky), MagicMock())

    assert key == "dynamodb:ListTables"
    assert ids == ["table-1"]
    assert err is None
    assert calls["n"] == 3


def test_run_lister_does_not_retry_non_transient_error(_instant_lister_retry):
    """A non-subscription error is captured on the first raise, with no retry."""
    scanner = FastResourceScanner(MagicMock())
    calls = {"n": 0}

    def _denied(_s):
        calls["n"] += 1
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListTables")

    key, ids, err = scanner._run_lister(Lister("dynamodb", "ListTables", _denied), MagicMock())

    assert key == "dynamodb:ListTables"
    assert ids == []
    assert err == "AccessDenied"
    assert calls["n"] == 1


def test_run_lister_captures_optin_after_budget_exhausted(_instant_lister_retry):
    """When OptInRequired never clears, the lister is finally captured as failed (not raised)."""
    scanner = FastResourceScanner(MagicMock())
    calls = {"n": 0}

    def _always_optin(_s):
        calls["n"] += 1
        raise ClientError({"Error": {"Code": "OptInRequired"}}, "ListTables")

    lister = Lister("dynamodb", "ListTables", _always_optin)
    key, ids, err = scanner._run_lister(lister, MagicMock())

    assert key == "dynamodb:ListTables"
    assert ids == []
    assert err == "OptInRequired"
    # Exhausted the per-lister attempt cap, then the error was swallowed into `failed`.
    assert calls["n"] == engine_module._LISTER_RETRY_ATTEMPTS


def test_run_lister_retries_transient_code_variant(_instant_lister_retry):
    scanner = FastResourceScanner(MagicMock())
    calls = {"n": 0}

    def _flaky(_s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ClientError({"Error": {"Code": "InternalServerErrorException"}}, "GetInsights")
        return [{"id": "insight-1"}]

    key, ids, err = scanner._run_lister(Lister("securityhub", "GetInsights", _flaky), MagicMock())

    assert key == "securityhub:GetInsights"
    assert ids == ["insight-1"]
    assert err is None
    assert calls["n"] == 3


def test_caching_session_builds_region_bound_session():
    scanner = FastResourceScanner(MagicMock())
    creds = {"AccessKeyId": "AKIA", "SecretAccessKey": "secret", "SessionToken": "tok"}
    with patch("aws_bench.resource_management.fastscan.engine.boto3.Session") as mk:
        mk.return_value.region_name = "us-west-2"
        caching = scanner._caching_session(creds, "us-west-2")
    assert isinstance(caching, _CachingSession)
    _, kwargs = mk.call_args
    assert kwargs["region_name"] == "us-west-2"
    assert kwargs["aws_access_key_id"] == "AKIA"


def test_listers_returns_the_unified_set():
    # The instance method delegates to the unified all_listers() (data table + code listers).
    # Compare by identity (service, op, cfn_type): a data lister's ``run`` is a freshly-built
    # closure per call, so the Lister objects are never ``==`` across two assemblies.
    scanner = FastResourceScanner(MagicMock())

    def identities(listers):
        return sorted((x.service, x.op, x.cfn_type) for x in listers)

    assert identities(scanner._listers()) == identities(all_listers())


# -- scan(): cooperative cancellation + timeout-boundary result recovery --


class _FrozenCredentials:
    """Minimal stand-in for botocore frozen credentials."""

    access_key = "AKIA"
    secret_key = "secret"
    token = None


class _StubCredentials:
    def get_frozen_credentials(self):
        return _FrozenCredentials()


def _scanner_with_listers(listers: list[Lister]) -> FastResourceScanner:
    """Build a scanner whose sweep is exactly ``listers`` (no real creds, no client builds)."""
    session = MagicMock()
    session.get_credentials.return_value = _StubCredentials()
    scanner = FastResourceScanner(session)
    # Replace the real caching session (would build boto3 clients) with a plain mock, and pin the
    # lister set to the given listers so the sweep is small and deterministic.
    scanner._caching_session = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    scanner._listers = MagicMock(return_value=listers)  # type: ignore[method-assign]
    return scanner


class _ControllableExecutor:
    """A fake ``interruptible_executor`` whose futures are set (or left running) up front.

    ``submit`` returns a pre-built ``Future`` keyed by the lister's ``service:op``, so the
    test decides exactly which futures are ``done()`` (with a result) versus still running
    when the timeout branch runs — independent of submission order (the sweep shuffles it).
    """

    def __init__(self, futures_by_key: dict[str, Future]) -> None:
        self._futures_by_key = futures_by_key

    def submit(self, fn, lister, *args, **kwargs) -> Future:
        return self._futures_by_key[f"{lister.service}:{lister.op}"]

    def __enter__(self) -> _ControllableExecutor:
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture
def _clear_shutdown():
    """Reset the process-global shutdown flag around a test."""
    concurrent.reset_shutdown()
    yield
    concurrent.reset_shutdown()


def _lister(service: str, op: str) -> Lister:
    return Lister(service=service, op=op, run=lambda _s: [])


def test_scan_records_done_pending_future_as_result_not_timeout():
    """A future finished in the timeout-boundary window is recorded by result, not as failure.

    ``as_completed`` raises ``TimeoutError`` while both listers are still in ``pending``; one
    future is already ``done()`` with a real result and MUST be recorded as a discovery (FIX 2),
    while the genuinely-running one is recorded as a timeout failure.
    """
    scanner = _scanner_with_listers([_lister("s3", "ListBuckets"), _lister("ec2", "DescribeVpcs")])

    done_future: Future = Future()
    done_future.set_result(("s3:ListBuckets", ["bucket-a"], None))
    running_future: Future = Future()  # never resolved → still running at timeout

    fake_executor = _ControllableExecutor(
        {"s3:ListBuckets": done_future, "ec2:DescribeVpcs": running_future}
    )
    with (
        patch(
            "aws_bench.resource_management.fastscan.engine.interruptible_executor",
            return_value=fake_executor,
        ),
        patch(
            "aws_bench.resource_management.fastscan.engine.as_completed",
            side_effect=TimeoutError,
        ),
    ):
        result = scanner.scan("us-east-1", overall_timeout=1.0)

    # The done future's real result is recorded as a discovery, NOT flipped to failed.
    assert result.discovered == {"s3:ListBuckets": ["bucket-a"]}
    assert "s3:ListBuckets" not in result.failed
    # Only the genuinely-running future is recorded as timed out.
    assert "ec2:DescribeVpcs" in result.failed
    assert "timed out" in result.failed["ec2:DescribeVpcs"]
    assert running_future.cancelled()


def test_scan_raises_when_shutdown_flagged(_clear_shutdown):
    """A shutdown flagged before the sweep makes scan() raise the cancellation exception.

    Every lister worker bails at its ``raise_if_shutdown`` entry checkpoint (so its ``run``
    never runs), and the post-loop backstop guarantees the abort even if none were in flight.
    """
    run = MagicMock(return_value=["should-not-run"])
    scanner = _scanner_with_listers([Lister(service="s3", op="ListBuckets", run=run)])

    concurrent.request_shutdown()
    with pytest.raises(OperationCancelled):
        scanner.scan("us-east-1", overall_timeout=5.0)

    # The lister's run never ran: the worker bailed at its shutdown checkpoint.
    run.assert_not_called()


def test_scan_raises_on_duplicate_scan_keys():
    """Two listers on one "service:op" abort the scan loudly (nondeterministic-clobber guard).

    Regression guard for the crash where a data row collided with a code lister on the same
    "service:op": the scanner raises ValueError at build time on a duplicate key.
    """
    scanner = _scanner_with_listers([_lister("s3", "ListBuckets"), _lister("s3", "ListBuckets")])
    with pytest.raises(ValueError, match="Duplicate fast-scan lister keys"):
        scanner.scan("us-east-1", overall_timeout=5.0)


class _RecordingExecutor:
    """Fake executor recording submission order, resolving each submit to a fixed future.

    ``discovered_ids`` is the id list every submitted lister "returns"; pass a non-empty list so
    submitted listers land in ``discovered``, leaving ``empty`` reachable only via the skip branch.
    """

    def __init__(self, discovered_ids: list[str] | None = None) -> None:
        self.submitted: list[Lister] = []
        self._discovered_ids = discovered_ids or []

    def submit(self, fn, lister, _session) -> Future:
        self.submitted.append(lister)
        fut: Future = Future()
        fut.set_result((f"{lister.service}:{lister.op}", list(self._discovered_ids), None))
        return fut

    def __enter__(self) -> _RecordingExecutor:
        return self

    def __exit__(self, *exc) -> None:
        return None


def _submission_order(listers: list[Lister]) -> list[str]:
    """Run scan() with a recording executor and return the order listers were submitted."""
    scanner = _scanner_with_listers(listers)
    rec = _RecordingExecutor()
    with patch(
        "aws_bench.resource_management.fastscan.engine.interruptible_executor",
        return_value=rec,
    ):
        scanner.scan("us-east-1", overall_timeout=5.0)
    return [f"{ln.service}:{ln.op}" for ln in rec.submitted]


def test_scan_shuffles_lister_submission_order():
    """The sweep submits listers in a randomized order, not the service-grouped input order.

    Listers ship grouped by service (e.g. ec2's 80+ listers adjacent). Submitting in that
    order makes a worker wave hammer one service's throttle bucket at once. Shuffling per
    scan spreads services across the wave and decorrelates concurrent (account, region)
    sweeps so they don't hit the same service in lockstep.
    """
    # 60 listers across 3 services, adjacent by service in the input (the shipped shape).
    listers = [_lister(svc, f"Op{i}") for svc in ("ec2", "s3", "iam") for i in range(20)]
    input_order = [f"{ln.service}:{ln.op}" for ln in listers]

    # Every lister is still submitted exactly once (shuffle must not drop or duplicate).
    order = _submission_order(listers)
    assert sorted(order) == sorted(input_order)

    orders = {tuple(_submission_order(listers)) for _ in range(8)}
    assert len(orders) > 1, "submission order never varied — not shuffled"


def _scan_with_skip(
    listers: list[Lister], region: str, skip: dict[tuple[str, str], frozenset[str]]
) -> tuple[FastScanResult, list[str]]:
    """Run scan() with ``skip`` patched in; return the result and the submitted keys."""
    scanner = _scanner_with_listers(listers)
    rec = _RecordingExecutor(discovered_ids=["found"])
    with (
        patch.object(engine_module, "LISTER_REGION_SKIP", skip),
        patch(
            "aws_bench.resource_management.fastscan.engine.interruptible_executor",
            return_value=rec,
        ),
    ):
        result = scanner.scan(region, overall_timeout=5.0)
    return result, [f"{ln.service}:{ln.op}" for ln in rec.submitted]


def test_scan_skips_region_absent_lister_and_records_it_empty():
    """A region-absent lister is recorded ``empty``, not ``failed``.

    A ``failed`` key reads as un-enumerated downstream and hides the type from drift/cleanup.
    """
    skip = {("s3", "ListDirectoryBuckets"): frozenset({"eu-west-3"})}
    result, submitted = _scan_with_skip(
        [_lister("s3", "ListBuckets"), _lister("s3", "ListDirectoryBuckets")], "eu-west-3", skip
    )
    assert "s3:ListDirectoryBuckets" not in submitted
    assert "s3:ListDirectoryBuckets" in result.empty
    assert "s3:ListDirectoryBuckets" not in result.failed


def test_scan_skip_is_operation_level_not_service_level():
    """Skipping ``s3:ListDirectoryBuckets`` MUST NOT skip the live sibling ``s3:ListBuckets``.

    S3 Express is absent where plain S3 is live, so a service-keyed skip would miss every bucket.
    """
    skip = {("s3", "ListDirectoryBuckets"): frozenset({"eu-west-3"})}
    result, submitted = _scan_with_skip(
        [_lister("s3", "ListBuckets"), _lister("s3", "ListDirectoryBuckets")], "eu-west-3", skip
    )
    assert "s3:ListBuckets" in submitted
    assert "s3:ListBuckets" not in result.empty


def test_scan_runs_lister_in_region_not_listed_absent():
    """A lister absent in OTHER regions still runs where the skip-map does not list it."""
    skip = {("aiops", "ListInvestigationGroups"): frozenset({"eu-west-3", "sa-east-1"})}
    result, submitted = _scan_with_skip(
        [_lister("aiops", "ListInvestigationGroups")], "us-east-1", skip
    )
    assert "aiops:ListInvestigationGroups" in submitted
    assert "aiops:ListInvestigationGroups" not in result.empty


def _scan_with_unavailable(
    listers: list[Lister], region: str, unavailable: dict[tuple[str, str], frozenset[str]]
) -> tuple[FastScanResult, list[str]]:
    """Run scan() with UNAVAILABLE_LISTER_REGIONS patched in; return result and submitted keys."""
    scanner = _scanner_with_listers(listers)
    rec = _RecordingExecutor(discovered_ids=["found"])
    with (
        patch.object(engine_module, "UNAVAILABLE_LISTER_REGIONS", unavailable),
        patch(
            "aws_bench.resource_management.fastscan.engine.interruptible_executor",
            return_value=rec,
        ),
    ):
        result = scanner.scan(region, overall_timeout=5.0)
    return result, [f"{ln.service}:{ln.op}" for ln in rec.submitted]


def test_scan_skips_unavailable_lister_and_records_it_empty():
    """A lister listed unavailable in a region is skipped there and recorded ``empty``.

    ``empty`` (not ``failed``) keeps the region scannable — a ``failed`` type would abort it
    via the fail-loud gate — while honestly reporting the lister as unscanned there.
    """
    unavailable = {("greengrass", "list_groups"): frozenset({"us-west-1"})}
    result, submitted = _scan_with_unavailable(
        [_lister("greengrass", "list_groups")], "us-west-1", unavailable
    )
    assert submitted == []
    assert "greengrass:list_groups" in result.empty
    assert not result.failed


def test_scan_runs_unavailable_lister_in_available_region():
    """A lister listed unavailable only in us-west-1 still runs (is submitted) in us-east-1."""
    unavailable = {("greengrass", "list_groups"): frozenset({"us-west-1"})}
    result, submitted = _scan_with_unavailable(
        [_lister("greengrass", "list_groups")], "us-east-1", unavailable
    )
    assert "greengrass:list_groups" in submitted
    assert "greengrass:list_groups" not in result.empty


# =============================================================================
# Manager: projection of native discoveries onto CFN types.
# =============================================================================


def _project(types, *, discovered=None, failed=None, empty=None):
    """Project a raw scan result onto the requested CFN types (the pure entry point)."""
    raw = FastScanResult(discovered=discovered or {}, failed=failed or {}, empty=empty or set())
    return FastScanManager.project(raw, types)


def test_projects_native_ids_onto_cfn_types():
    result = _project(
        ["AWS::S3::Bucket", "AWS::Lambda::Function"],
        discovered={
            "s3:ListBuckets": ["arn:aws:s3:::b1", "arn:aws:s3:::b2"],
            "lambda:ListFunctions": ["arn:aws:lambda:us-east-1:1:function:fn"],
        },
    )
    assert len(result.detected["AWS::S3::Bucket"]) == 2
    assert len(result.detected["AWS::Lambda::Function"]) == 1


def test_unattributed_go_to_service_star_bucket():
    # a discovery whose op-noun matches no requested type lands in AWS::<svc>::*
    result = _project(["AWS::S3::Bucket"], discovered={"s3:ListSomethingWeird": ["arn:aws:s3:::x"]})
    assert "AWS::s3::*" in result.detected
    assert len(result.detected["AWS::s3::*"]) == 1


def test_unattributed_failure_goes_to_service_star_failed_bucket():
    # A lister FAILURE whose op-noun matches no requested type AND whose key is unpinned must
    # surface in ScanResult.failed under AWS::<svc>::* — not evaporate. Without the failed
    # catch-all the failure would appear in neither detected, empty, nor failed, and downstream
    # verify/drift would treat those types as clean when discovery actually failed to enumerate
    # them. Uses an unpinned synthetic key (config:ListSomethingWeird); a pinned key would instead
    # route to its exact CFN type.
    result = _project(
        ["AWS::Config::ConfigRule"],
        failed={"config:ListSomethingWeird": "AccessDenied"},
    )
    assert result.failed == {"AWS::config::*": "AccessDenied"}
    assert result.detected == {}
    assert result.empty == set()


def test_unattributed_failure_suppressed_when_service_discovered_something():
    # If the same service also produced an unattributed discovery, that AWS::<svc>::*
    # detected bucket already signals the service was reachable, so a sibling failed
    # lister does not additionally raise a failed bucket for the service.
    result = _project(
        ["AWS::IAM::Role"],
        discovered={"iam:list_roles": ["arn:aws:iam::1:role/r"]},
        failed={"iam:list_policies": "AccessDenied"},
    )
    assert "AWS::iam::*" in result.detected
    assert result.failed == {}


def test_sibling_colliding_types_are_not_double_attributed():
    # AWS::WorkspacesInstances::Volume and AWS::ECR::PublicRepository share a lister + noun
    # with AWS::EC2::Volume / AWS::ECR::Repository (same ids). They must NOT be attributed the
    # sibling's resources — doing so produced phantom resources and spurious "new resource"
    # verify failures (the EBS volumes / CDK asset repos showed up under the wrong type).
    result = _project(
        [
            "AWS::EC2::Volume",
            "AWS::WorkspacesInstances::Volume",
            "AWS::ECR::Repository",
            "AWS::ECR::PublicRepository",
        ],
        discovered={
            "ec2:DescribeVolumes": ["vol-1", "vol-2"],
            "ecr:DescribeRepositories": ["arn:aws:ecr:us-west-1:1:repository/cdk-assets"],
        },
    )
    # The real types get the resources...
    assert len(result.detected["AWS::EC2::Volume"]) == 2
    assert len(result.detected["AWS::ECR::Repository"]) == 1
    # ...and the colliding sibling types are never populated with them.
    assert "AWS::WorkspacesInstances::Volume" not in result.detected
    assert "AWS::ECR::PublicRepository" not in result.detected


def test_alias_service_projection_msk():
    # MSK cluster discovered under the kafka endpoint maps to AWS::MSK::Cluster
    result = _project(
        ["AWS::MSK::Cluster"],
        discovered={"kafka:ListClusters": ["arn:aws:kafka:us-east-1:1:cluster/c/uuid"]},
    )
    assert len(result.detected.get("AWS::MSK::Cluster", [])) == 1


def test_failed_and_empty_keyed_by_cfn_type():
    # A lister that fails / returns empty must project onto the CFN type it feeds,
    # NOT the raw "service:op" key — every consumer (verify, snapshot) treats
    # ScanResult.failed/.empty keys as CFN type names, exactly like CCAPI does.
    result = _project(
        ["AWS::S3::Bucket", "AWS::Lambda::Function"],
        failed={"s3:ListBuckets": "AccessDenied"},
        empty={"lambda:ListFunctions"},
    )
    assert result.failed == {"AWS::S3::Bucket": "AccessDenied"}
    assert result.empty == {"AWS::Lambda::Function"}


def test_failed_takes_precedence_over_empty_and_detected_wins():
    # A type with discoveries is "detected" even if another of its listers was empty;
    # a type with no ids but a failing lister is "failed", not "empty".
    result = _project(
        ["AWS::EC2::VPC", "AWS::EC2::Subnet"],
        discovered={"ec2:DescribeVpcs": ["vpc-1"]},
        failed={"ec2:DescribeSubnets": "Throttling"},
    )
    assert "AWS::EC2::VPC" in result.detected
    assert result.failed == {"AWS::EC2::Subnet": "Throttling"}
    assert result.empty == set()


def test_cfn_type_override_attributes_directly_not_catch_all():
    # A supplementary lister keyed "ec2:DescribeAddresses" targets AWS::EC2::EIP — a
    # sub-resource type whose noun ("eip") the op noun ("addresses") never matches. With its
    # cfn_type override it must land on AWS::EC2::EIP, NOT the AWS::ec2::* catch-all.
    with patch(
        "aws_bench.resource_management.fastscan.manager.cfn_type_pins",
        return_value={"ec2:DescribeAddresses": "AWS::EC2::EIP"},
    ):
        result = _project(
            ["AWS::EC2::EIP"], discovered={"ec2:DescribeAddresses": ["eipalloc-1", "eipalloc-2"]}
        )
    assert len(result.detected.get("AWS::EC2::EIP", [])) == 2
    assert "AWS::ec2::*" not in result.detected


def test_cfn_type_override_failed_and_empty_reach_the_type():
    with patch(
        "aws_bench.resource_management.fastscan.manager.cfn_type_pins",
        return_value={
            "s3:ListBucketPolicies": "AWS::S3::BucketPolicy",
            "ec2:DescribeAddresses": "AWS::EC2::EIP",
        },
    ):
        result = _project(
            ["AWS::S3::BucketPolicy", "AWS::EC2::EIP"],
            failed={"s3:ListBucketPolicies": "AccessDenied"},
            empty={"ec2:DescribeAddresses"},
        )
    assert result.failed == {"AWS::S3::BucketPolicy": "AccessDenied"}
    assert result.empty == {"AWS::EC2::EIP"}


@patch("aws_bench.resource_management.fastscan.manager.FastResourceScanner")
def test_init_builds_scanner(mock_scanner_cls):
    FastScanManager(MagicMock(region_name="us-east-1"))
    mock_scanner_cls.assert_called_once()


# =============================================================================
# Factory: scan-backend selection + feature flag (fastscan | ccapi).
# =============================================================================


def _session() -> boto3.Session:
    return boto3.Session(region_name="us-east-1")


def test_default_method_is_fastscan_lambda(monkeypatch):
    # Unset → Lambda-with-host-fallback default (host still runs when no account_id is given).
    monkeypatch.delenv(SCAN_METHOD_ENV_VAR, raising=False)
    assert scan_method() == "fastscan-lambda"


def test_unknown_method_falls_back_to_fastscan_lambda(monkeypatch):
    monkeypatch.setenv(SCAN_METHOD_ENV_VAR, "banana")
    assert scan_method() == "fastscan-lambda"


def test_method_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(SCAN_METHOD_ENV_VAR, "CCAPI")
    assert scan_method() == "ccapi"


def test_make_scanner_fastscan_default_is_host(monkeypatch):
    # Default: the in-process host scanner, no in-account Lambda infrastructure.
    monkeypatch.delenv(SCAN_METHOD_ENV_VAR, raising=False)
    sc = make_scanner(_session(), region_name="us-east-1")
    assert type(sc).__name__ == "FastScanManager"


def test_make_scanner_ccapi(monkeypatch):
    monkeypatch.setenv(SCAN_METHOD_ENV_VAR, "ccapi")
    sc = make_scanner(_session(), region_name="us-east-1")
    assert type(sc).__name__ == "_CcapiScanAdapter"


def test_fastscan_lambda_method_is_recognized(monkeypatch):
    # The Lambda tier is a valid, explicit method value (also the unset default).
    monkeypatch.setenv(SCAN_METHOD_ENV_VAR, "fastscan-lambda")
    assert scan_method() == "fastscan-lambda"


def test_make_scanner_returns_lambda_when_account_given(monkeypatch):
    from aws_bench.resource_management.fastscan.lambda_scanner import LambdaScanner

    monkeypatch.delenv(SCAN_METHOD_ENV_VAR, raising=False)
    scanner = make_scanner(_session(), region_name="us-east-1", account_id="111111111111")
    assert isinstance(scanner, LambdaScanner)


def test_make_scanner_host_when_no_account(monkeypatch):
    monkeypatch.delenv(SCAN_METHOD_ENV_VAR, raising=False)
    scanner = make_scanner(_session(), region_name="us-east-1")
    assert isinstance(scanner, FastScanManager)


def test_make_scanner_rollback_forces_host(monkeypatch):
    monkeypatch.setenv(SCAN_METHOD_ENV_VAR, "fastscan")
    scanner = make_scanner(_session(), region_name="us-east-1", account_id="111111111111")
    assert isinstance(scanner, FastScanManager)  # rollback ignores the Lambda path
