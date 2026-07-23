"""Tests for the AWS Batch compute-environment cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.batch import (
    _delete,
    _prepare,
    _region_from_arn,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_CE_ARN = "arn:aws:batch:us-east-1:123456789012:compute-environment/environment-x"
_JQ_ARN = "arn:aws:batch:us-east-1:123456789012:job-queue/queue-x"
_WAIT_TARGET = "aws_bench.resource_management.cleanup.handlers.batch.wait_until"


def _patch_wait(return_value: bool):
    """Patch the handler's wait_until to a fixed result (no real polling)."""
    return patch(_WAIT_TARGET, return_value=return_value)


def _resource() -> Resource:
    return Resource(type="AWS::Batch::ComputeEnvironment", identifier=_CE_ARN)


def _session_with_ce(state: str, status: str, *, queues: list[dict] | None = None):
    """Mock session whose batch client returns a CE in the given state/status."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_compute_environments.return_value = {
        "computeEnvironments": [{"state": state, "status": status, "statusReason": "ok"}]
    }
    paginator = MagicMock()
    paginator.paginate.return_value = [{"jobQueues": queues or []}]
    client.get_paginator.return_value = paginator
    return session, client


# -- _region_from_arn --


class TestRegionFromArn:
    def test_extracts_region_from_arn(self):
        assert _region_from_arn(_CE_ARN) == "us-east-1"

    def test_returns_none_for_non_arn(self):
        assert _region_from_arn("environment-x") is None

    def test_returns_none_for_malformed_arn(self):
        assert _region_from_arn("arn:aws:batch") is None


# -- _prepare --


class TestPrepare:
    def test_builds_client_in_ce_region(self):
        session, _ = _session_with_ce("ENABLED", "VALID")
        _prepare(_resource(), session)
        session.client.assert_called_with("batch", region_name="us-east-1")

    def test_disables_and_deletes_enabled_ce(self):
        session, client = _session_with_ce("ENABLED", "VALID")
        with _patch_wait(True):
            result = _prepare(_resource(), session)

        client.update_compute_environment.assert_called_once_with(
            computeEnvironment=_CE_ARN, state="DISABLED"
        )
        client.delete_compute_environment.assert_called_once_with(computeEnvironment=_CE_ARN)
        assert result.status == HandlerStatus.SUCCESS

    def test_skips_disable_when_already_disabled(self):
        session, client = _session_with_ce("DISABLED", "VALID")
        with _patch_wait(True):
            result = _prepare(_resource(), session)

        client.update_compute_environment.assert_not_called()
        client.delete_compute_environment.assert_called_once()
        assert result.status == HandlerStatus.SUCCESS

    def test_disassociates_and_deletes_referencing_job_queue(self):
        queue = {
            "jobQueueArn": _JQ_ARN,
            "state": "ENABLED",
            "status": "VALID",
            "computeEnvironmentOrder": [{"computeEnvironment": _CE_ARN, "order": 1}],
        }
        session, client = _session_with_ce("ENABLED", "VALID", queues=[queue])
        with _patch_wait(True):
            _prepare(_resource(), session)

        client.update_job_queue.assert_called_once_with(jobQueue=_JQ_ARN, state="DISABLED")
        client.delete_job_queue.assert_called_once_with(jobQueue=_JQ_ARN)

    def test_ignores_job_queue_not_referencing_this_ce(self):
        other_ce = "arn:aws:batch:us-east-1:1:compute-environment/other"
        queue = {
            "jobQueueArn": _JQ_ARN,
            "state": "ENABLED",
            "status": "VALID",
            "computeEnvironmentOrder": [{"computeEnvironment": other_ce}],
        }
        session, client = _session_with_ce("ENABLED", "VALID", queues=[queue])
        with _patch_wait(True):
            _prepare(_resource(), session)

        client.delete_job_queue.assert_not_called()

    def test_skips_when_ce_already_gone(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_compute_environments.return_value = {"computeEnvironments": []}
        result = _prepare(_resource(), session)

        client.delete_compute_environment.assert_not_called()
        assert result.status == HandlerStatus.SKIPPED
        assert "already gone" in result.message

    def test_skips_when_ce_already_deleting(self):
        session, client = _session_with_ce("DISABLED", "DELETING")
        result = _prepare(_resource(), session)

        client.delete_compute_environment.assert_not_called()
        assert result.status == HandlerStatus.SKIPPED
        assert "already deleting" in result.message

    def test_fails_on_describe_error(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_compute_environments.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DescribeComputeEnvironments"
        )
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed to describe" in result.message

    def test_fails_on_delete_error(self):
        session, client = _session_with_ce("DISABLED", "VALID")
        client.delete_compute_environment.side_effect = BotoCoreError()
        with _patch_wait(True):
            result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed to disable/delete" in result.message


# -- _delete --


class TestDelete:
    def test_success_when_ce_gone(self):
        session, client = _session_with_ce("DISABLED", "DELETING")
        with _patch_wait(True):
            result = _delete(_resource(), session)

        assert result.status == HandlerStatus.SUCCESS
        assert "deleted" in result.message

    def test_fails_with_status_reason_when_not_gone(self):
        """A stuck INVALID CE (e.g. deleted service role) surfaces its statusReason."""
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_compute_environments.return_value = {
            "computeEnvironments": [
                {
                    "state": "DISABLED",
                    "status": "INVALID",
                    "statusReason": "CLIENT_ERROR - not authorized to assume service role",
                }
            ]
        }
        with _patch_wait(False):
            result = _delete(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "INVALID" in result.message
        assert "service role" in result.message

    def test_delete_uses_ce_delete_timeout(self):
        session, _ = _session_with_ce("DISABLED", "DELETING")
        with patch(_WAIT_TARGET) as mock_wait:
            mock_wait.return_value = True
            _delete(_resource(), session)

            _, kwargs = mock_wait.call_args
            assert kwargs["timeout"] == 900
            assert kwargs["interval"] == 15
