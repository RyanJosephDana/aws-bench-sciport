"""Tests for the Lambda fast-scan wire protocol."""

from __future__ import annotations

import pytest

from aws_bench.resource_management.fastscan.lambda_protocol import (
    build_event,
    error_envelope,
    ok_envelope,
    parse_event,
    result_from_wire,
    result_to_wire,
)
from aws_bench.resource_management.fastscan.models import FastScanResult


def test_build_and_parse_event_roundtrip():
    event = build_event("111111111111", "us-east-1")
    assert parse_event(event) == ("111111111111", "us-east-1")


def test_parse_event_missing_key_raises():
    with pytest.raises(ValueError):
        parse_event({"account_id": "111111111111"})
    with pytest.raises(ValueError):
        parse_event({"account_id": "", "region": "us-east-1"})


def test_result_wire_roundtrip_preserves_data():
    original = FastScanResult(
        discovered={"ec2:DescribeInstances": ["i-1", "i-2"]},
        failed={"cloudhsm:DescribeClusters": "EndpointConnectionError"},
        empty={"s3:ListBuckets"},
    )
    wire = result_to_wire(original)
    # empty is a JSON list, sorted for determinism.
    assert wire["empty"] == ["s3:ListBuckets"]
    restored = result_from_wire(wire)
    assert restored.discovered == original.discovered
    assert restored.failed == original.failed
    assert restored.empty == original.empty


def test_ok_and_error_envelopes():
    result = FastScanResult(discovered={}, failed={}, empty=set())
    ok = ok_envelope(result)
    assert ok["ok"] is True
    assert result_from_wire(ok["result"]).discovered == {}
    err = error_envelope("boom")
    assert err["ok"] is False
    assert err["error"] == "boom"


@pytest.mark.parametrize("missing", ["discovered", "failed", "empty"])
def test_result_from_wire_rejects_missing_key(missing):
    # A dropped key (esp. "failed") must raise, not silently default to "nothing failed":
    # downstream reads a defaulted failed={} as fully-scanned and could delete live resources.
    wire = {"discovered": {}, "failed": {}, "empty": []}
    del wire[missing]
    with pytest.raises(ValueError, match=missing):
        result_from_wire(wire)


def test_result_from_wire_rejects_null_failed():
    with pytest.raises(ValueError, match="failed"):
        result_from_wire({"discovered": {}, "failed": None, "empty": []})
