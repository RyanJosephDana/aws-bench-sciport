"""Tests for the DMS replication cleanup handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.dms import (
    _TASK_GONE,
    _get_task_status,
    _is_task_stopped,
    _prepare_endpoint,
    _prepare_replication_task,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_TASK_ARN = "arn:aws:dms:us-east-1:123456789012:task:ABCDEFGHIJKLMNOP"
_ENDPOINT_ARN = "arn:aws:dms:us-east-1:123456789012:endpoint:ABCDEFGHIJKLMNOP"


def _task_resource() -> Resource:
    return Resource(type="AWS::DMS::ReplicationTask", identifier=_TASK_ARN)


def _endpoint_resource() -> Resource:
    return Resource(type="AWS::DMS::Endpoint", identifier=_ENDPOINT_ARN)


def _mock_session(task_status: str | None = None) -> tuple[MagicMock, MagicMock]:
    """Create a mock session with a DMS client that returns the given task status."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    if task_status is None:
        # Task not found — empty response.
        client.describe_replication_tasks.return_value = {"ReplicationTasks": []}
    else:
        client.describe_replication_tasks.return_value = {
            "ReplicationTasks": [{"Status": task_status, "ReplicationTaskArn": _TASK_ARN}]
        }
    return session, client


# ── _get_task_status ─────────────────────────────────────────────────────────


class TestGetTaskStatus:
    """Tests for the _get_task_status helper."""

    def test_returns_status_lowercase(self):
        _, client = _mock_session("Running")
        assert _get_task_status(client, _TASK_ARN) == "running"

    def test_returns_gone_when_no_tasks(self):
        _, client = _mock_session(None)
        assert _get_task_status(client, _TASK_ARN) == _TASK_GONE

    def test_returns_gone_on_not_found_fault(self):
        _, client = _mock_session()
        client.describe_replication_tasks.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundFault"}}, "DescribeReplicationTasks"
        )
        assert _get_task_status(client, _TASK_ARN) == _TASK_GONE

    def test_raises_on_transient_client_error(self):
        """A non-not-found error (e.g. throttling) must propagate, not read as gone."""
        _, client = _mock_session()
        client.describe_replication_tasks.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"
        )
        with pytest.raises(ClientError):
            _get_task_status(client, _TASK_ARN)

    def test_raises_on_botocore_error(self):
        _, client = _mock_session()
        client.describe_replication_tasks.side_effect = BotoCoreError()
        with pytest.raises(BotoCoreError):
            _get_task_status(client, _TASK_ARN)

    def test_calls_with_correct_filter(self):
        _, client = _mock_session("stopped")
        _get_task_status(client, _TASK_ARN)
        client.describe_replication_tasks.assert_called_once_with(
            Filters=[{"Name": "replication-task-arn", "Values": [_TASK_ARN]}]
        )


# ── _is_task_stopped ─────────────────────────────────────────────────────────


class TestIsTaskStopped:
    """Tests for the _is_task_stopped helper."""

    def test_returns_true_for_stopped(self):
        _, client = _mock_session("stopped")
        assert _is_task_stopped(client, _TASK_ARN) is True

    def test_returns_true_for_failed(self):
        _, client = _mock_session("failed")
        assert _is_task_stopped(client, _TASK_ARN) is True

    def test_returns_true_for_ready(self):
        _, client = _mock_session("ready")
        assert _is_task_stopped(client, _TASK_ARN) is True

    def test_returns_true_for_deleting(self):
        _, client = _mock_session("deleting")
        assert _is_task_stopped(client, _TASK_ARN) is True

    def test_returns_true_when_gone(self):
        _, client = _mock_session(None)
        assert _is_task_stopped(client, _TASK_ARN) is True

    def test_propagates_transient_error(self):
        """A throttled describe must raise so throttling isn't read as stopped."""
        _, client = _mock_session()
        client.describe_replication_tasks.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"
        )
        with pytest.raises(ClientError):
            _is_task_stopped(client, _TASK_ARN)

    def test_returns_false_for_running(self):
        _, client = _mock_session("running")
        assert _is_task_stopped(client, _TASK_ARN) is False

    def test_returns_false_for_starting(self):
        _, client = _mock_session("starting")
        assert _is_task_stopped(client, _TASK_ARN) is False

    def test_returns_false_for_stopping(self):
        _, client = _mock_session("stopping")
        assert _is_task_stopped(client, _TASK_ARN) is False


# ── _prepare_replication_task ────────────────────────────────────────────────


class TestPrepareReplicationTask:
    """Tests for the replication task prepare handler."""

    def test_skips_when_task_not_found(self):
        session, _ = _mock_session(None)
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SKIPPED
        assert "not found" in result.message

    def test_fails_on_transient_describe_error_at_entry(self):
        """A throttled initial describe must FAIL, not silently SKIP as 'not found'.

        Skipping on a transient error would leave a still-running task unstopped.
        """
        session, client = _mock_session()
        client.describe_replication_tasks.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"
        )
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.FAILED
        assert "describe" in result.message.lower()
        client.stop_replication_task.assert_not_called()

    def test_succeeds_when_already_stopped(self):
        session, client = _mock_session("stopped")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SUCCESS
        assert "already in state" in result.message
        client.stop_replication_task.assert_not_called()

    def test_succeeds_when_already_failed(self):
        session, client = _mock_session("failed")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SUCCESS
        client.stop_replication_task.assert_not_called()

    def test_succeeds_when_already_ready(self):
        session, client = _mock_session("ready")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SUCCESS
        client.stop_replication_task.assert_not_called()

    def test_fails_on_unexpected_state(self):
        """An unexpected state (e.g. modifying) must FAIL, not report a false stop.

        prepare-SUCCESS chains into the delete step; deleting a task mid-operation
        causes the DELETE_FAILED this handler exists to prevent.
        """
        for state in ("creating", "modifying", "testing", "moving-tables"):
            session, client = _mock_session(state)
            result = _prepare_replication_task(_task_resource(), session)
            assert result.status == HandlerStatus.FAILED, state
            assert "unexpected" in result.message.lower()
            assert state in result.message
            client.stop_replication_task.assert_not_called()

    @patch("aws_bench.resource_management.cleanup.handlers.dms.wait_until", return_value=True)
    def test_stops_running_task_and_waits(self, mock_wait):
        session, client = _mock_session("running")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SUCCESS
        client.stop_replication_task.assert_called_once_with(ReplicationTaskArn=_TASK_ARN)
        mock_wait.assert_called_once()

    @patch("aws_bench.resource_management.cleanup.handlers.dms.wait_until", return_value=True)
    def test_stops_starting_task(self, mock_wait):
        session, client = _mock_session("starting")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SUCCESS
        client.stop_replication_task.assert_called_once_with(ReplicationTaskArn=_TASK_ARN)

    @patch("aws_bench.resource_management.cleanup.handlers.dms.wait_until", return_value=True)
    def test_waits_without_stop_if_already_stopping(self, mock_wait):
        session, client = _mock_session("stopping")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SUCCESS
        # Should NOT call stop since it's already stopping.
        client.stop_replication_task.assert_not_called()
        mock_wait.assert_called_once()

    @patch("aws_bench.resource_management.cleanup.handlers.dms.wait_until", return_value=False)
    def test_fails_when_stop_times_out(self, mock_wait):
        session, client = _mock_session("running")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.FAILED
        assert "did not stop" in result.message

    @patch("aws_bench.resource_management.cleanup.handlers.dms._STOP_WAIT_TIMEOUT", 0.05)
    @patch("aws_bench.resource_management.cleanup.handlers.dms._STOP_WAIT_INTERVAL", 0.01)
    def test_surfaces_polling_error_on_timeout(self):
        """A persistent throttle during the stop-wait surfaces the real error."""
        session, client = _mock_session("running")

        # Entry describe succeeds ("running"); the post-stop poll describes throttle.
        client.describe_replication_tasks.side_effect = [
            {"ReplicationTasks": [{"Status": "running", "ReplicationTaskArn": _TASK_ARN}]},
            ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"),
            ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"),
            ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"),
            ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"),
            ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeReplicationTasks"),
        ]
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.FAILED
        assert "Error polling" in result.message

    def test_skips_on_not_found_during_stop(self):
        session, client = _mock_session("running")
        client.stop_replication_task.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundFault"}}, "StopReplicationTask"
        )
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SKIPPED
        assert "not found" in result.message.lower()

    def test_skips_on_invalid_state_during_stop(self):
        session, client = _mock_session("running")
        client.stop_replication_task.side_effect = ClientError(
            {"Error": {"Code": "InvalidResourceStateFault"}}, "StopReplicationTask"
        )
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.SKIPPED

    def test_fails_on_access_denied_during_stop(self):
        session, client = _mock_session("running")
        client.stop_replication_task.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "StopReplicationTask"
        )
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.FAILED
        assert "Failed to stop" in result.message

    def test_fails_on_botocore_error_during_stop(self):
        session, client = _mock_session("running")
        client.stop_replication_task.side_effect = BotoCoreError()
        result = _prepare_replication_task(_task_resource(), session)
        assert result.status == HandlerStatus.FAILED
        assert "Connection error" in result.message

    def test_uses_dms_client(self):
        session, _ = _mock_session("stopped")
        _prepare_replication_task(_task_resource(), session)
        session.client.assert_called_with("dms")

    def test_result_contains_correct_resource_info(self):
        session, _ = _mock_session("stopped")
        result = _prepare_replication_task(_task_resource(), session)
        assert result.resource_id == _TASK_ARN
        assert result.resource_type == "AWS::DMS::ReplicationTask"
        assert result.action == "prepare"


# ── _prepare_endpoint ────────────────────────────────────────────────────────


class TestPrepareEndpoint:
    """Tests for the DMS endpoint prepare handler (no-op)."""

    def test_always_returns_success(self):
        session = MagicMock()
        result = _prepare_endpoint(_endpoint_resource(), session)
        assert result.status == HandlerStatus.SUCCESS

    def test_does_not_make_api_calls(self):
        session = MagicMock()
        _prepare_endpoint(_endpoint_resource(), session)
        session.client.assert_not_called()

    def test_result_contains_correct_resource_info(self):
        session = MagicMock()
        result = _prepare_endpoint(_endpoint_resource(), session)
        assert result.resource_id == _ENDPOINT_ARN
        assert result.resource_type == "AWS::DMS::Endpoint"
        assert result.action == "prepare"
        assert "No preparation needed" in result.message
