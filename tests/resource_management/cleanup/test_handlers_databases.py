"""Tests for database cluster cleanup handlers (DocDB, RDS, Neptune)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.databases import (
    _cluster_gone,
    _delete_cluster,
    _delete_docdb,
    _delete_docdb_cluster_parameter_group,
    _delete_neptune,
    _delete_rds_cluster,
    _delete_rds_instance,
    _instances_gone,
    _prepare_cluster,
    _prepare_docdb,
    _prepare_neptune,
    _prepare_rds_cluster,
    _prepare_rds_instance,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus


@pytest.fixture(autouse=True)
def _fast_wait_until():
    """Patch the real poll so cluster-delete tests never sleep; default success.

    Tests that need a timeout set ``mock.return_value = False``; the
    ``_prepare_cluster`` tests that patch ``wait_until`` themselves override this
    within their own ``with patch`` block.
    """
    with patch(
        "aws_bench.resource_management.cleanup.handlers.databases.wait_until",
        return_value=True,
    ) as mock:
        yield mock


# -- _prepare_cluster --


def test_prepare_cluster_not_found():
    client = MagicMock()
    client.describe_db_clusters.side_effect = Exception("not found")
    result = _prepare_cluster(client, "c1", "AWS::DocDB::DBCluster")
    assert result.status == HandlerStatus.SKIPPED


def test_prepare_cluster_disables_protection_and_deletes_instances():
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DeletionProtection": True,
                "DBClusterMembers": [{"DBInstanceIdentifier": "i1"}],
            }
        ]
    }
    with patch(
        "aws_bench.resource_management.cleanup.handlers.databases.wait_until",
        return_value=True,
    ):
        result = _prepare_cluster(client, "c1", "AWS::DocDB::DBCluster")
    client.modify_db_cluster.assert_called_once_with(
        DBClusterIdentifier="c1", DeletionProtection=False
    )
    client.delete_db_instance.assert_called_once_with(DBInstanceIdentifier="i1")
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_cluster_no_protection_no_members():
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [{"DeletionProtection": False, "DBClusterMembers": []}]
    }
    result = _prepare_cluster(client, "c1", "AWS::DocDB::DBCluster")
    client.modify_db_cluster.assert_not_called()
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_cluster_instance_delete_fails():
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DeletionProtection": False,
                "DBClusterMembers": [{"DBInstanceIdentifier": "i1"}],
            }
        ]
    }
    client.delete_db_instance.side_effect = Exception("fail")
    with patch(
        "aws_bench.resource_management.cleanup.handlers.databases.wait_until",
        return_value=True,
    ):
        result = _prepare_cluster(client, "c1", "AWS::DocDB::DBCluster")
    assert result.status == HandlerStatus.FAILED
    assert "i1" in result.message


def test_prepare_cluster_timeout_waiting_for_instances():
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DeletionProtection": False,
                "DBClusterMembers": [{"DBInstanceIdentifier": "i1"}],
            }
        ]
    }
    with patch(
        "aws_bench.resource_management.cleanup.handlers.databases.wait_until",
        return_value=False,
    ):
        result = _prepare_cluster(client, "c1", "AWS::DocDB::DBCluster")
    assert result.status == HandlerStatus.FAILED
    assert "Timed out" in result.message


# -- _instances_gone --


def test_instances_gone_true():
    client = MagicMock()
    client.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterMembers": []}]}
    assert _instances_gone(client, "c1") is True


def test_instances_gone_false():
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterMembers": [{"DBInstanceIdentifier": "i1"}]}]
    }
    assert _instances_gone(client, "c1") is False


def test_instances_gone_returns_false_on_generic_error():
    client = MagicMock()
    client.describe_db_clusters.side_effect = Exception("timeout")
    assert _instances_gone(client, "c1") is False


# -- _delete_cluster --


def test_delete_cluster_success(_fast_wait_until):
    client = MagicMock()
    result = _delete_cluster(client, "c1", "AWS::DocDB::DBCluster")
    client.delete_db_cluster.assert_called_once_with(
        DBClusterIdentifier="c1", SkipFinalSnapshot=True
    )
    _fast_wait_until.assert_called_once()
    assert result.status == HandlerStatus.SUCCESS


def test_delete_cluster_timeout_waiting_for_deletion(_fast_wait_until):
    _fast_wait_until.return_value = False
    client = MagicMock()
    result = _delete_cluster(client, "c1", "AWS::RDS::DBCluster")
    assert result.status == HandlerStatus.FAILED
    assert "Timed out waiting for cluster to delete" in result.message


# -- RDS Instance --


def test_prepare_rds_instance_not_found():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_db_instances.side_effect = ClientError(
        {"Error": {"Code": "DBInstanceNotFoundFault"}}, "DescribeDBInstances"
    )
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _prepare_rds_instance(r, session)
    assert result.status == HandlerStatus.SKIPPED


def test_prepare_rds_instance_disables_protection():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_db_instances.return_value = {"DBInstances": [{"DeletionProtection": True}]}
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _prepare_rds_instance(r, session)
    client.modify_db_instance.assert_called_once()
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_rds_instance_no_protection():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_db_instances.return_value = {"DBInstances": [{"DeletionProtection": False}]}
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _prepare_rds_instance(r, session)
    client.modify_db_instance.assert_not_called()
    assert result.status == HandlerStatus.SUCCESS


def test_delete_rds_instance():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _delete_rds_instance(r, session)
    client.delete_db_instance.assert_called_once()
    assert result.status == HandlerStatus.SUCCESS


# -- Wrappers (DocDB, RDS Cluster, Neptune) --


def test_prepare_docdb_delegates():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_db_clusters.side_effect = Exception("not found")
    result = _prepare_docdb(Resource(type="AWS::DocDB::DBCluster", identifier="c1"), session)
    session.client.assert_called_with("docdb")
    assert result.status == HandlerStatus.SKIPPED


def test_delete_docdb_delegates():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete_docdb(Resource(type="AWS::DocDB::DBCluster", identifier="c1"), session)
    session.client.assert_called_with("docdb")
    assert result.status == HandlerStatus.SUCCESS


# -- DocDB cluster parameter group (service-API delete; CCAPI can't) --


def test_delete_docdb_cluster_parameter_group_deletes_by_name():
    """A custom group is deleted via the DocDB API, passing the NAME (what the lister now emits)."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    resource = Resource(type="AWS::DocDB::DBClusterParameterGroup", identifier="my-docdb-pg")
    result = _delete_docdb_cluster_parameter_group(resource, session)
    session.client.assert_called_with("docdb")
    client.delete_db_cluster_parameter_group.assert_called_once_with(
        DBClusterParameterGroupName="my-docdb-pg"
    )
    assert result.status == HandlerStatus.SUCCESS


def test_delete_docdb_cluster_parameter_group_skips_aws_default():
    """An AWS-reserved ``default.`` group is undeletable — skipped, never calls the delete API."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    resource = Resource(type="AWS::DocDB::DBClusterParameterGroup", identifier="default.docdb5.0")
    result = _delete_docdb_cluster_parameter_group(resource, session)
    client.delete_db_cluster_parameter_group.assert_not_called()
    assert result.status == HandlerStatus.SKIPPED


def test_delete_docdb_cluster_parameter_group_reports_failure():
    """A service error surfaces as FAILED, not a raised exception."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_db_cluster_parameter_group.side_effect = ClientError(
        {"Error": {"Code": "InvalidDBParameterGroupState"}}, "DeleteDBClusterParameterGroup"
    )
    resource = Resource(type="AWS::DocDB::DBClusterParameterGroup", identifier="in-use-pg")
    result = _delete_docdb_cluster_parameter_group(resource, session)
    assert result.status == HandlerStatus.FAILED


def test_prepare_rds_cluster_delegates():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_db_clusters.side_effect = Exception("not found")
    result = _prepare_rds_cluster(Resource(type="AWS::RDS::DBCluster", identifier="c1"), session)
    session.client.assert_called_with("rds")
    assert result.status == HandlerStatus.SKIPPED


def test_delete_rds_cluster_delegates():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete_rds_cluster(Resource(type="AWS::RDS::DBCluster", identifier="c1"), session)
    session.client.assert_called_with("rds")
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_neptune_delegates():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_db_clusters.side_effect = Exception("not found")
    result = _prepare_neptune(Resource(type="AWS::Neptune::DBCluster", identifier="c1"), session)
    session.client.assert_called_with("neptune")
    assert result.status == HandlerStatus.SKIPPED


def test_delete_neptune_delegates():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete_neptune(Resource(type="AWS::Neptune::DBCluster", identifier="c1"), session)
    session.client.assert_called_with("neptune")
    assert result.status == HandlerStatus.SUCCESS


def test_delete_cluster_handles_exception():
    """Test _delete_cluster handles deletion errors."""
    client = MagicMock()
    client.delete_db_cluster.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "DeleteDBCluster"
    )

    result = _delete_cluster(client, "my-cluster", "AWS::RDS::DBCluster")

    assert result.status == HandlerStatus.FAILED
    assert "Failed to delete cluster" in result.message


def test_prepare_cluster_handles_instance_deletion_error():
    """Test _prepare_cluster handles error when deleting instances."""
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DeletionProtection": False,
                "DBClusterMembers": [{"DBInstanceIdentifier": "i1"}],
            }
        ]
    }
    # Instance deletion fails
    client.delete_db_instance.side_effect = ClientError(
        {"Error": {"Code": "InvalidDBInstanceState"}}, "DeleteDBInstance"
    )

    result = _prepare_cluster(client, "c1", "AWS::DocDB::DBCluster")

    assert result.status == HandlerStatus.FAILED
    assert "Failed to delete instances" in result.message


def test_instances_gone_client_error_not_found():
    """Test _instances_gone handles ClientError with NotFound in code."""
    client = MagicMock()
    error = ClientError({"Error": {"Code": "DBClusterNotFoundFault"}}, "DescribeDBClusters")
    client.describe_db_clusters.side_effect = error
    assert _instances_gone(client, "c1") is True


def test_instances_gone_client_error_other():
    """Test _instances_gone handles other ClientError gracefully."""
    client = MagicMock()
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "DescribeDBClusters")
    client.describe_db_clusters.side_effect = error
    assert _instances_gone(client, "c1") is False


def test_prepare_rds_instance_client_error_other():
    """Test _prepare_rds_instance handles non-NotFound ClientError."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    error = ClientError({"Error": {"Code": "InternalServiceError"}}, "DescribeDBInstances")
    client.describe_db_instances.side_effect = error
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _prepare_rds_instance(r, session)
    assert result.status == HandlerStatus.FAILED


def test_delete_rds_instance_client_error():
    """Test _delete_rds_instance handles ClientError."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    error = ClientError({"Error": {"Code": "InvalidDBInstanceState"}}, "DeleteDBInstance")
    client.delete_db_instance.side_effect = error
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _delete_rds_instance(r, session)
    assert result.status == HandlerStatus.FAILED


def test_delete_cluster_botocore_error():
    """Test _delete_cluster handles BotoCoreError (e.g., connection errors)."""
    from botocore.exceptions import EndpointConnectionError

    client = MagicMock()
    error = EndpointConnectionError(endpoint_url="https://rds.us-east-1.amazonaws.com")
    client.delete_db_cluster.side_effect = error
    result = _delete_cluster(client, "cluster1", "AWS::RDS::DBCluster")
    assert result.status == HandlerStatus.FAILED
    assert "Failed to delete cluster" in result.message


def test_prepare_rds_instance_botocore_error():
    """Test _prepare_rds_instance handles BotoCoreError."""
    from botocore.exceptions import ReadTimeoutError

    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    error = ReadTimeoutError(endpoint_url="https://rds.us-east-1.amazonaws.com")
    client.describe_db_instances.side_effect = error
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _prepare_rds_instance(r, session)
    assert result.status == HandlerStatus.FAILED
    assert "Connection error" in result.message


def test_delete_rds_instance_botocore_error():
    """Test _delete_rds_instance handles BotoCoreError."""
    from botocore.exceptions import ConnectionClosedError

    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    error = ConnectionClosedError(endpoint_url="https://rds.us-east-1.amazonaws.com")
    client.delete_db_instance.side_effect = error
    r = Resource(type="AWS::RDS::DBInstance", identifier="db1")
    result = _delete_rds_instance(r, session)
    assert result.status == HandlerStatus.FAILED
    assert "Failed to delete instance" in result.message


def test_cluster_gone_true_when_absent():
    client = MagicMock()
    client.describe_db_clusters.return_value = {"DBClusters": []}
    assert _cluster_gone(client, "c1") is True


def test_cluster_gone_true_on_not_found():
    client = MagicMock()
    client.describe_db_clusters.side_effect = ClientError(
        {"Error": {"Code": "DBClusterNotFoundFault"}}, "DescribeDBClusters"
    )
    assert _cluster_gone(client, "c1") is True


def test_cluster_gone_false_when_present():
    client = MagicMock()
    client.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterIdentifier": "c1"}]}
    assert _cluster_gone(client, "c1") is False
