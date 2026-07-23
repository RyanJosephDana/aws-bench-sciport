"""Tests for the local LambdaScanner adapter (invoke + raise-on-failure, no fallback)."""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import MagicMock

import pytest
import tenacity

from aws_bench.logging.logger import TRACE
from aws_bench.resource_management.fastscan import constants
from aws_bench.resource_management.fastscan import lambda_scanner as ls
from aws_bench.resource_management.fastscan.lambda_protocol import ok_envelope
from aws_bench.resource_management.fastscan.models import FastScanResult


@pytest.fixture(autouse=True)
def _stub_projection(mocker):
    """Projection is exercised elsewhere; here assert the RAW result reaching it."""
    proj = mocker.patch.object(ls.FastScanManager, "project", staticmethod(lambda raw, types: raw))
    mocker.patch.object(ls.FastScanManager, "get_scannable_types", lambda self: ["AWS::EC2::VPC"])
    return proj


def _lambda_client_returning(envelope: dict) -> MagicMock:
    client = MagicMock()
    client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps(envelope).encode()),
    }
    return client


def _mgmt_lambda(mocker, client: MagicMock) -> None:
    mgmt = MagicMock()
    mgmt.client.return_value = client
    mocker.patch.object(
        ls.CredentialProvider,
        "get",
        return_value=MagicMock(get_management_session=lambda: mgmt),
    )


@pytest.fixture(autouse=True)
def _instant_backoff(mocker):
    """Neutralize _invoke_with_retry's tenacity backoff so retry tests don't sleep."""
    mocker.patch.object(
        ls.LambdaScanner._invoke_with_retry.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


def test_scan_resources_uses_lambda_result(mocker):
    raw = FastScanResult(discovered={"ec2:DescribeVpcs": ["vpc-1"]}, failed={}, empty=set())
    _mgmt_lambda(mocker, _lambda_client_returning(ok_envelope(raw)))

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    result = scanner.scan_resources(region="us-east-1")

    # project() is stubbed to return the raw it received → assert Lambda's raw came through.
    # (Typed as ScanResult; the stub makes it a FastScanResult at runtime, hence the ignore.)
    assert result.discovered == {"ec2:DescribeVpcs": ["vpc-1"]}  # type: ignore[attr-defined]


def test_scan_resources_raises_on_error_envelope_after_retries(mocker):
    # An error envelope is a retryable fault; a persistent one raises (no host fallback).
    client = _lambda_client_returning({"ok": False, "error": "boom"})
    _mgmt_lambda(mocker, client)

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with pytest.raises(ls.LambdaScanTransient):
        scanner.scan_resources(region="us-east-1")

    assert client.invoke.call_count == ls._INVOKE_RETRY_ATTEMPTS


def test_scan_resources_raises_when_invoke_keeps_failing(mocker):
    client = MagicMock()
    client.invoke.side_effect = RuntimeError("no such function")
    _mgmt_lambda(mocker, client)

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with pytest.raises(ls.LambdaScanTransient):
        scanner.scan_resources(region="us-east-1")

    assert client.invoke.call_count == ls._INVOKE_RETRY_ATTEMPTS


def test_scan_resources_retries_then_succeeds(mocker):
    # A transient blip on the first invoke followed by success must not fail the scan.
    raw = FastScanResult(discovered={"ec2:DescribeVpcs": ["vpc-1"]}, failed={}, empty=set())
    client = MagicMock()
    good = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(ok_envelope(raw)).encode())}
    client.invoke.side_effect = [RuntimeError("throttled"), good]
    _mgmt_lambda(mocker, client)

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    result = scanner.scan_resources(region="us-east-1")

    assert result.discovered == {"ec2:DescribeVpcs": ["vpc-1"]}  # type: ignore[attr-defined]
    assert client.invoke.call_count == 2


def test_scan_resources_raises_on_ok_envelope_missing_result(mocker):
    # ok:true but no result → a fault, not a silent empty scan.
    _mgmt_lambda(mocker, _lambda_client_returning({"ok": True}))

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with pytest.raises(ls.LambdaScanTransient):
        scanner.scan_resources(region="us-east-1")


def test_scan_resources_does_not_retry_lambda_function_error(mocker):
    # A FunctionError (200 + FunctionError header, {"errorMessage": ...} body) means the runtime
    # killed the process (timeout / OOM / oversized response), deterministic against this fixed
    # (account, region) — so the scan raises LambdaScanFatal on the first invoke, no retries.
    def _function_error(**_kwargs):
        return {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": io.BytesIO(
                json.dumps({"errorMessage": "Task timed out after 480.00 seconds"}).encode()
            ),
        }

    client = MagicMock()
    client.invoke.side_effect = _function_error
    _mgmt_lambda(mocker, client)

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with pytest.raises(ls.LambdaScanFatal, match="timed out"):
        scanner.scan_resources(region="us-east-1")

    # A deterministic runtime kill is not retried: LambdaScanFatal is a sibling of
    # LambdaScanTransient, so the retry predicate skips it.
    assert client.invoke.call_count == 1
    assert not issubclass(ls.LambdaScanFatal, ls.LambdaScanTransient)


def test_scan_resources_raises_on_malformed_result_missing_failed(mocker):
    # A result missing "failed" must not deserialize to failed={} ("nothing failed") — that
    # would let downstream treat un-enumerated types as clean and delete live resources.
    _mgmt_lambda(
        mocker,
        _lambda_client_returning({"ok": True, "result": {"discovered": {}, "empty": []}}),
    )

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with pytest.raises(ls.LambdaScanTransient):
        scanner.scan_resources(region="us-east-1")


def test_invoke_client_read_timeout_exceeds_lambda_timeout():
    # The invoke read_timeout must outlast the Lambda's own run, or a healthy long scan is
    # abandoned mid-flight. This is the nested-timeout ordering's outermost layer.
    assert constants.HOST_INVOKE_READ_TIMEOUT_S > constants.LAMBDA_FUNCTION_TIMEOUT_S
    # botocore.Config sets these from **kwargs, so pyright can't see the attributes.
    assert ls._INVOKE_CONFIG.read_timeout == constants.HOST_INVOKE_READ_TIMEOUT_S  # type: ignore[attr-defined]
    # Botocore's own retries are off so tenacity is the single retry authority.
    assert ls._INVOKE_CONFIG.retries == {"max_attempts": 0}  # type: ignore[attr-defined]


def test_scan_logs_lambda_path(mocker, caplog):
    raw = FastScanResult(discovered={}, failed={}, empty=set())
    _mgmt_lambda(mocker, _lambda_client_returning(ok_envelope(raw)))

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with caplog.at_level(logging.DEBUG):
        scanner.scan_resources(region="us-east-1")
    assert any("via lambda" in r.message.lower() for r in caplog.records)


def test_scan_logs_each_failed_lister_host_side(mocker, caplog):
    # The Lambda's failed-lister dict rides the wire in raw.failed; the host re-logs each at
    # TRACE (below DEBUG) — mostly listers not available in a region, high-volume, kept in the
    # ledger run.log but off the DEBUG job/trial sinks. Capture at TRACE to see them.
    raw = FastScanResult(
        discovered={},
        failed={"ec2:DescribeVpcs": "AccessDenied", "s3:ListBuckets": "Throttling"},
        empty=set(),
    )
    _mgmt_lambda(mocker, _lambda_client_returning(ok_envelope(raw)))

    scanner = ls.LambdaScanner(MagicMock(), account_id="111111111111", region_name="us-east-1")
    with caplog.at_level(TRACE):
        scanner.scan_resources(region="us-east-1")

    messages = [r.getMessage() for r in caplog.records]
    assert any("ec2:DescribeVpcs" in m and "AccessDenied" in m for m in messages)
    assert any("s3:ListBuckets" in m and "Throttling" in m for m in messages)
