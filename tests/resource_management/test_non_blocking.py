"""Tests for non-blocking quota provisioning.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
"""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.models import (
    QuotaConfiguration,
    QuotaIncreaseRequest,
    QuotaIncreaseResult,
    QuotaStatus,
)
from aws_bench.resource_management.quota_manager import QuotaManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, message: str = "mocked") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "request_service_quota_increase",
    )


def _build_manager_and_client(
    scenarios: list[str],
) -> tuple[QuotaManager, MagicMock]:
    """Return (QuotaManager, mock_client) wired together."""
    client = MagicMock()
    side_effects: list = []
    for s in scenarios:
        if s == "success":
            side_effects.append({"RequestedQuota": {"Id": "mock-id"}})
        elif s == "already_exists":
            side_effects.append(_make_client_error("ResourceAlreadyExistsException"))
        elif s == "illegal_arg":
            side_effects.append(_make_client_error("IllegalArgumentException"))
        else:
            side_effects.append(_make_client_error(s, f"Unexpected: {s}"))

    def _side_effect(**kwargs):
        val = side_effects.pop(0)
        if isinstance(val, Exception):
            raise val
        return val

    client.request_service_quota_increase.side_effect = _side_effect

    mock_session = MagicMock()
    mock_session.client.return_value = client
    mock_cred = MagicMock()
    mock_cred.get_session_for_account.return_value = mock_session

    return QuotaManager(mock_cred), client


def _simple_config(n: int = 1) -> QuotaConfiguration:
    return QuotaConfiguration(
        increases=[QuotaIncreaseRequest(f"svc-{i}", f"Q-{i}", 10.0) for i in range(n)],
    )


# ===========================================================================
# request_quotas — submits without polling
# ===========================================================================


def test_request_quotas_submits_requests_without_polling():
    """Non-blocking submits requests and returns results immediately."""
    mgr, client = _build_manager_and_client(["success", "success"])
    config = _simple_config(2)

    results = mgr.request_quotas(config, "111111111111", "Role")

    assert len(results) == 2
    assert all(isinstance(r, QuotaIncreaseResult) for r in results)


def test_request_quotas_returns_correct_statuses():
    """Non-blocking maps API outcomes to correct statuses."""
    mgr, _ = _build_manager_and_client(["success", "already_exists", "illegal_arg"])
    config = _simple_config(3)

    results = mgr.request_quotas(config, "111111111111", "Role")

    assert results[0].status == QuotaStatus.REQUESTED
    assert results[1].status == QuotaStatus.ALREADY_PENDING
    assert results[2].status == QuotaStatus.ALREADY_MET


def test_request_quotas_catches_deployment_error_and_logs_warning(caplog):
    """Individual DeploymentError is caught and logged, not raised."""
    mgr, _ = _build_manager_and_client(["AccessDenied"])
    config = _simple_config(1)

    with caplog.at_level("WARNING"):
        results = mgr.request_quotas(config, "111111111111", "Role")

    assert len(results) == 1
    assert results[0].status == QuotaStatus.FAILED
    assert results[0].error_message  # non-empty error message
    assert "Failed to request quota increase" in caplog.text


def test_request_quotas_continues_after_individual_failure():
    """After one request fails, remaining requests are still submitted."""
    mgr, client = _build_manager_and_client(["AccessDenied", "success"])
    config = _simple_config(2)

    results = mgr.request_quotas(config, "111111111111", "Role")

    assert len(results) == 2
    assert results[0].error_message != ""
    assert results[1].status == QuotaStatus.REQUESTED
    assert client.request_service_quota_increase.call_count == 2
