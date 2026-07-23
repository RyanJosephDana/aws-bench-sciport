"""Tests for the EMR Cluster cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.emr import (
    _delete,
    _prepare,
    _wait_for_fleet_reaped,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_CLUSTER_ID = "j-ABC123DEF456"


def _resource() -> Resource:
    return Resource(type="AWS::EMR::Cluster", identifier=_CLUSTER_ID)


def _session_with_state(state: str) -> tuple[MagicMock, MagicMock]:
    """Create a mock session whose EMR client returns the given cluster state."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_cluster.return_value = {"Cluster": {"Status": {"State": state}}}
    return session, client


# -- _prepare --


class TestPrepare:
    def test_terminates_running_cluster(self):
        session, client = _session_with_state("RUNNING")
        result = _prepare(_resource(), session)

        session.client.assert_called_with("emr")
        client.set_termination_protection.assert_called_once_with(
            JobFlowIds=[_CLUSTER_ID], TerminationProtected=False
        )
        client.terminate_job_flows.assert_called_once_with(JobFlowIds=[_CLUSTER_ID])
        assert result.status == HandlerStatus.SUCCESS
        assert "RUNNING" in result.message

    def test_terminates_waiting_cluster(self):
        session, client = _session_with_state("WAITING")
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_called_once_with(JobFlowIds=[_CLUSTER_ID])
        assert result.status == HandlerStatus.SUCCESS

    def test_terminates_starting_cluster(self):
        session, client = _session_with_state("STARTING")
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_called_once_with(JobFlowIds=[_CLUSTER_ID])
        assert result.status == HandlerStatus.SUCCESS

    def test_terminates_bootstrapping_cluster(self):
        session, client = _session_with_state("BOOTSTRAPPING")
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_called_once_with(JobFlowIds=[_CLUSTER_ID])
        assert result.status == HandlerStatus.SUCCESS

    def test_skips_already_terminated_cluster(self):
        session, client = _session_with_state("TERMINATED")
        result = _prepare(_resource(), session)

        client.set_termination_protection.assert_not_called()
        client.terminate_job_flows.assert_not_called()
        assert result.status == HandlerStatus.SKIPPED
        assert "already terminated" in result.message

    def test_skips_terminated_with_errors(self):
        session, client = _session_with_state("TERMINATED_WITH_ERRORS")
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_not_called()
        assert result.status == HandlerStatus.SKIPPED

    def test_skips_when_cluster_not_found(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_cluster.side_effect = ClientError(
            {"Error": {"Code": "ClusterNotFound"}}, "DescribeCluster"
        )
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.SKIPPED
        assert "not found" in result.message

    def test_skips_when_invalid_request(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_cluster.side_effect = ClientError(
            {"Error": {"Code": "InvalidRequestException"}}, "DescribeCluster"
        )
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.SKIPPED

    def test_fails_on_describe_permission_error(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_cluster.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DescribeCluster"
        )
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed to describe cluster" in result.message

    def test_fails_on_describe_botocore_error(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_cluster.side_effect = BotoCoreError()
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Connection error" in result.message

    def test_skips_when_terminate_gets_not_found(self):
        """Cluster exists at describe time but gone by terminate time."""
        session, client = _session_with_state("RUNNING")
        client.terminate_job_flows.side_effect = ClientError(
            {"Error": {"Code": "ClusterNotFound"}}, "TerminateJobFlows"
        )
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.SKIPPED
        assert "not found during termination" in result.message

    def test_fails_on_terminate_permission_error(self):
        session, client = _session_with_state("RUNNING")
        client.terminate_job_flows.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "TerminateJobFlows"
        )
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Failed to terminate" in result.message

    def test_fails_on_terminate_botocore_error(self):
        session, client = _session_with_state("RUNNING")
        client.terminate_job_flows.side_effect = BotoCoreError()
        result = _prepare(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Connection error terminating" in result.message

    def test_disables_protection_before_terminate(self):
        """Protection must be disabled before terminate — it is a no-op on HA clusters."""
        session, client = _session_with_state("WAITING")

        _prepare(_resource(), session)

        names = [call[0] for call in client.mock_calls]
        assert "set_termination_protection" in names
        assert "terminate_job_flows" in names
        assert names.index("set_termination_protection") < names.index("terminate_job_flows")

    def test_skips_when_disable_protection_not_found(self):
        session, client = _session_with_state("RUNNING")
        client.set_termination_protection.side_effect = ClientError(
            {"Error": {"Code": "ClusterNotFound"}}, "SetTerminationProtection"
        )
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_not_called()
        assert result.status == HandlerStatus.SKIPPED
        assert "termination protection" in result.message

    def test_fails_when_disable_protection_permission_error(self):
        session, client = _session_with_state("RUNNING")
        client.set_termination_protection.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "SetTerminationProtection"
        )
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_not_called()
        assert result.status == HandlerStatus.FAILED
        assert "Failed to disable termination protection" in result.message

    def test_fails_when_disable_protection_botocore_error(self):
        session, client = _session_with_state("RUNNING")
        client.set_termination_protection.side_effect = BotoCoreError()
        result = _prepare(_resource(), session)

        client.terminate_job_flows.assert_not_called()
        assert result.status == HandlerStatus.FAILED
        assert "Connection error disabling termination protection" in result.message


# -- _delete --


class TestDelete:
    @patch("aws_bench.resource_management.cleanup.handlers.emr.wait_until")
    def test_success_when_cluster_terminates_and_fleet_reaped(self, mock_wait):
        mock_wait.return_value = True
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client

        result = _delete(_resource(), session)

        assert result.status == HandlerStatus.SUCCESS
        assert "fleet reaped" in result.message
        # one wait for cluster termination, one for the managed EC2 fleet
        assert mock_wait.call_count == 2

    @patch("aws_bench.resource_management.cleanup.handlers.emr.wait_until")
    def test_success_but_fleet_still_reaping(self, mock_wait):
        # cluster reaches a terminal state, but the managed EC2 fleet wait times out
        mock_wait.side_effect = [True, False]
        session = MagicMock()
        session.client.return_value = MagicMock()

        result = _delete(_resource(), session)

        assert result.status == HandlerStatus.SUCCESS
        assert "still reaping" in result.message

    @patch("aws_bench.resource_management.cleanup.handlers.emr.wait_until")
    def test_fails_when_termination_times_out(self, mock_wait):
        mock_wait.return_value = False
        session = MagicMock()
        session.client.return_value = MagicMock()

        result = _delete(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "timed out" in result.message

    @patch("aws_bench.resource_management.cleanup.handlers.emr._TERMINATION_TIMEOUT", 0.05)
    @patch("aws_bench.resource_management.cleanup.handlers.emr._TERMINATION_POLL_INTERVAL", 0.01)
    def test_surfaces_persistent_client_error_on_timeout(self):
        """A describe that keeps failing must report the real error, not a generic timeout.

        wait_until swallows predicate exceptions, so the handler captures the last
        error itself and surfaces it once polling gives up.
        """
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_cluster.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}}, "DescribeCluster"
        )

        result = _delete(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Error polling" in result.message
        assert "InternalServerError" in result.message

    @patch("aws_bench.resource_management.cleanup.handlers.emr._TERMINATION_TIMEOUT", 0.05)
    @patch("aws_bench.resource_management.cleanup.handlers.emr._TERMINATION_POLL_INTERVAL", 0.01)
    def test_surfaces_persistent_botocore_error_on_timeout(self):
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        client.describe_cluster.side_effect = BotoCoreError()

        result = _delete(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "Error polling" in result.message

    @patch("aws_bench.resource_management.cleanup.handlers.emr._TERMINATION_TIMEOUT", 0.05)
    @patch("aws_bench.resource_management.cleanup.handlers.emr._TERMINATION_POLL_INTERVAL", 0.01)
    def test_generic_timeout_when_cluster_never_terminates_without_error(self):
        """No error, just never terminal -> generic timeout message (not an error one)."""
        session, _ = _session_with_state("TERMINATING")

        result = _delete(_resource(), session)

        assert result.status == HandlerStatus.FAILED
        assert "timed out" in result.message
        assert "Error polling" not in result.message

    def test_delete_waits_use_expected_timeouts(self):
        """Cluster-termination wait then fleet-reap wait, each with its own budget."""
        session, _ = _session_with_state("TERMINATED")

        with patch("aws_bench.resource_management.cleanup.handlers.emr.wait_until") as mock_wait:
            mock_wait.return_value = True
            _delete(_resource(), session)

            assert mock_wait.call_count == 2
            cluster_call, fleet_call = mock_wait.call_args_list
            assert cluster_call[1]["timeout"] == 900
            assert cluster_call[1]["interval"] == 15
            assert fleet_call[1]["timeout"] == 300
            assert fleet_call[1]["interval"] == 15


# -- _wait_for_fleet_reaped --


class TestFleetReap:
    def test_returns_true_when_no_live_instances(self):
        session = MagicMock()
        ec2 = MagicMock()
        session.client.return_value = ec2
        ec2.describe_instances.return_value = {"Reservations": []}

        assert _wait_for_fleet_reaped(session, _CLUSTER_ID) is True

        _, kwargs = ec2.describe_instances.call_args
        filters = {f["Name"]: f["Values"] for f in kwargs["Filters"]}
        assert filters["tag:aws:elasticmapreduce:job-flow-id"] == [_CLUSTER_ID]
        assert "running" in filters["instance-state-name"]
        # terminated instances linger in DescribeInstances but are already gone
        assert "terminated" not in filters["instance-state-name"]

    @patch("aws_bench.resource_management.cleanup.handlers.emr._FLEET_REAP_TIMEOUT", 0.05)
    @patch("aws_bench.resource_management.cleanup.handlers.emr._FLEET_REAP_POLL_INTERVAL", 0.01)
    def test_returns_false_while_instances_present(self):
        session = MagicMock()
        ec2 = MagicMock()
        session.client.return_value = ec2
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"InstanceId": "i-abc123"}]}]
        }

        assert _wait_for_fleet_reaped(session, _CLUSTER_ID) is False
