"""Wire contract between the local LambdaScanner and the in-Lambda handler.

Pure data-shape functions — no boto3, so both sides import them cheaply and
the serialization stays the single source of truth for the invoke event and
the returned raw FastScanResult.
"""

from __future__ import annotations

from typing import Any

from aws_bench.resource_management.fastscan.models import FastScanResult

EVENT_ACCOUNT_KEY = "account_id"
EVENT_REGION_KEY = "region"


def build_event(account_id: str, region: str) -> dict[str, str]:
    """Build the Lambda invoke payload for one (account, region) scan."""
    return {EVENT_ACCOUNT_KEY: account_id, EVENT_REGION_KEY: region}


def parse_event(event: dict[str, Any]) -> tuple[str, str]:
    """Extract (account_id, region) from an invoke event; raise if either is absent."""
    account_id = event.get(EVENT_ACCOUNT_KEY)
    region = event.get(EVENT_REGION_KEY)
    if not account_id or not region:
        raise ValueError(f"event missing {EVENT_ACCOUNT_KEY!r}/{EVENT_REGION_KEY!r}: {event!r}")
    return account_id, region


def result_to_wire(result: FastScanResult) -> dict[str, Any]:
    """Serialize a FastScanResult to a JSON-safe dict (set → sorted list)."""
    return {
        "discovered": result.discovered,
        "failed": result.failed,
        "empty": sorted(result.empty),
    }


_REQUIRED_RESULT_KEYS = ("discovered", "failed", "empty")


def result_from_wire(payload: dict[str, Any]) -> FastScanResult:
    """Rebuild a FastScanResult from its wire dict, rejecting a malformed result.

    All three keys must be present. A defaulted-away ``failed`` is the dangerous case:
    types that could not be enumerated would deserialize as ``failed={}`` ("nothing
    failed"), which downstream reads as fully-scanned — so cleanup could delete resources
    a wire-format drift merely failed to list. A missing key is a fault, not an empty scan.
    """
    missing = [key for key in _REQUIRED_RESULT_KEYS if payload.get(key) is None]
    if missing:
        raise ValueError(f"malformed scan result, missing key(s): {', '.join(missing)}")
    return FastScanResult(
        discovered=payload["discovered"],
        failed=payload["failed"],
        empty=set(payload["empty"]),
    )


def ok_envelope(result: FastScanResult) -> dict[str, Any]:
    """Success envelope carrying the serialized scan result."""
    return {"ok": True, "result": result_to_wire(result)}


def error_envelope(message: str) -> dict[str, Any]:
    """Failure envelope; the caller raises LambdaScanTransient on it (no host fallback)."""
    return {"ok": False, "error": message}
