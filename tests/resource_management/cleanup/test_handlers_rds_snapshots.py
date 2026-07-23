"""Tests for the RDS snapshot / tenant-database cleanup handlers (service-API delete)."""

from __future__ import annotations

from unittest.mock import MagicMock

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.databases import (
    _delete_rds_db_cluster_snapshot,
    _delete_rds_db_snapshot,
    _delete_rds_tenant_database,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

# The lister emits the ARN; delete needs the identifier (the ARN tail after the marker). Automated
# snapshots carry the ``rds:`` owner prefix and are normally filtered upstream — the handler SKIPs
# any that still reach it.
_MANUAL_SNAP_ARN = "arn:aws:rds:us-east-1:111122223333:snapshot:my-manual-snap"
_MANUAL_SNAP_ID = "my-manual-snap"
_AUTO_SNAP = "arn:aws:rds:us-east-1:111122223333:snapshot:rds:db-2026-07-04-06-07"
_AUTO_CLUSTER_SNAP = "arn:aws:rds:us-east-1:111122223333:cluster-snapshot:rds:cl-2026-07-04-05-40"
_MANUAL_CLUSTER_SNAP_ARN = "arn:aws:rds:us-east-1:111122223333:cluster-snapshot:my-manual-cl-snap"
_MANUAL_CLUSTER_SNAP_ID = "my-manual-cl-snap"


def test_delete_db_snapshot_skips_automated():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    r = Resource(type="AWS::RDS::DBSnapshot", identifier=_AUTO_SNAP)
    result = _delete_rds_db_snapshot(r, session)
    assert result.status == HandlerStatus.SKIPPED
    client.delete_db_snapshot.assert_not_called()


def test_delete_db_snapshot_deletes_manual_by_identifier_not_arn():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    r = Resource(type="AWS::RDS::DBSnapshot", identifier=_MANUAL_SNAP_ARN)
    result = _delete_rds_db_snapshot(r, session)
    assert result.status == HandlerStatus.SUCCESS
    # The ARN would return DBSnapshotNotFound; delete must use the extracted identifier.
    client.delete_db_snapshot.assert_called_once_with(DBSnapshotIdentifier=_MANUAL_SNAP_ID)


def test_delete_db_cluster_snapshot_skips_automated():
    session = MagicMock()
    session.client.return_value = MagicMock()
    r = Resource(type="AWS::RDS::DBClusterSnapshot", identifier=_AUTO_CLUSTER_SNAP)
    result = _delete_rds_db_cluster_snapshot(r, session)
    assert result.status == HandlerStatus.SKIPPED
    session.client.return_value.delete_db_cluster_snapshot.assert_not_called()


def test_delete_db_cluster_snapshot_deletes_manual_by_identifier_not_arn():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    r = Resource(type="AWS::RDS::DBClusterSnapshot", identifier=_MANUAL_CLUSTER_SNAP_ARN)
    result = _delete_rds_db_cluster_snapshot(r, session)
    assert result.status == HandlerStatus.SUCCESS
    client.delete_db_cluster_snapshot.assert_called_once_with(
        DBClusterSnapshotIdentifier=_MANUAL_CLUSTER_SNAP_ID
    )


def test_delete_tenant_database_looks_up_and_deletes():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    arn = "arn:aws:rds:us-east-1:111122223333:tenant-database:abc"
    client.describe_tenant_databases.return_value = {
        "TenantDatabases": [{"DBInstanceIdentifier": "inst-1", "TenantDBName": "tenant1"}]
    }
    r = Resource(type="AWS::RDS::TenantDatabase", identifier=arn)
    result = _delete_rds_tenant_database(r, session)
    assert result.status == HandlerStatus.SUCCESS
    client.delete_tenant_database.assert_called_once_with(
        DBInstanceIdentifier="inst-1", TenantDBName="tenant1", SkipFinalSnapshot=True
    )


def test_delete_tenant_database_skips_when_absent():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.describe_tenant_databases.return_value = {"TenantDatabases": []}
    client.get_paginator.return_value.paginate.return_value = [{"TenantDatabases": []}]
    r = Resource(
        type="AWS::RDS::TenantDatabase", identifier="arn:aws:rds:us-east-1:1:tenant-database:x"
    )
    result = _delete_rds_tenant_database(r, session)
    assert result.status == HandlerStatus.SKIPPED
    client.delete_tenant_database.assert_not_called()
