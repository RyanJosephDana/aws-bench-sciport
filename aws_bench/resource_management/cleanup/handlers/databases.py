"""Database cluster cleanup handlers (DocDB, RDS, Neptune)."""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.resource_management.utils.polling import wait_until
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_INSTANCE_WAIT_TIMEOUT = 600
_INSTANCE_WAIT_INTERVAL = 15
_CLUSTER_WAIT_TIMEOUT = 900


# ── Helpers ──────────────────────────────────────────────────────────


def _prepare_cluster(client, cluster_id: str, resource_type: str) -> HandlerResult:
    """Disable deletion protection and delete all member instances."""
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
    except Exception as e:
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource_type,
            action="prepare",
            status=HandlerStatus.SKIPPED,
            message=f"Cluster not found: {e}",
        )
    if cluster.get("DeletionProtection"):
        client.modify_db_cluster(DBClusterIdentifier=cluster_id, DeletionProtection=False)
    failed_instances = []
    for member in cluster.get("DBClusterMembers", []):
        instance_id = member["DBInstanceIdentifier"]
        try:
            client.delete_db_instance(DBInstanceIdentifier=instance_id)
        except Exception as e:
            logger.warning("Could not delete instance '%s': %s", instance_id, e)
            failed_instances.append(instance_id)
    if cluster.get("DBClusterMembers") and not failed_instances:
        if not wait_until(
            lambda: _instances_gone(client, cluster_id),
            timeout=_INSTANCE_WAIT_TIMEOUT,
            interval=_INSTANCE_WAIT_INTERVAL,
        ):
            return HandlerResult(
                resource_id=cluster_id,
                resource_type=resource_type,
                action="prepare",
                status=HandlerStatus.FAILED,
                message="Timed out waiting for member instances to terminate",
            )
    status = HandlerStatus.FAILED if failed_instances else HandlerStatus.SUCCESS
    message = f"Failed to delete instances: {failed_instances}" if failed_instances else ""
    return HandlerResult(
        resource_id=cluster_id,
        resource_type=resource_type,
        action="prepare",
        status=status,
        message=message,
    )


def _instances_gone(client, cluster_id: str) -> bool:
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        return not resp["DBClusters"][0].get("DBClusterMembers")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if "NotFound" in error_code:
            return True
        logger.debug("Error checking instances for '%s': %s", cluster_id, error_code)
        return False
    except Exception as e:
        # Unexpected error
        logger.warning("Unexpected error checking instances for '%s': %s", cluster_id, e)
        return False


def _cluster_gone(client, cluster_id: str) -> bool:
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        return not resp.get("DBClusters")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if "NotFound" in error_code:
            return True
        logger.debug("Error checking cluster '%s': %s", cluster_id, error_code)
        return False
    except Exception as e:
        logger.warning("Unexpected error checking cluster '%s': %s", cluster_id, e)
        return False


def _delete_cluster(client, cluster_id: str, resource_type: str) -> HandlerResult:
    try:
        client.delete_db_cluster(DBClusterIdentifier=cluster_id, SkipFinalSnapshot=True)
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource_type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete cluster: {e}",
        )
    if not wait_until(
        lambda: _cluster_gone(client, cluster_id),
        timeout=_CLUSTER_WAIT_TIMEOUT,
        interval=_INSTANCE_WAIT_INTERVAL,
    ):
        return HandlerResult(
            resource_id=cluster_id,
            resource_type=resource_type,
            action="delete",
            status=HandlerStatus.FAILED,
            message="Timed out waiting for cluster to delete",
        )
    return HandlerResult(
        resource_id=cluster_id,
        resource_type=resource_type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


# ── DocDB ────────────────────────────────────────────────────────────


@resource_handler("AWS::DocDB::DBCluster", role="prepare")
def _prepare_docdb(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _prepare_cluster(build_client(session, "docdb"), resource.identifier, resource.type)


@resource_handler("AWS::DocDB::DBCluster", role="delete")
def _delete_docdb(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _delete_cluster(build_client(session, "docdb"), resource.identifier, resource.type)


# CloudControl can't delete AWS::DocDB::DBClusterParameterGroup (UnsupportedActionException), so
# route it to the DocDB service API, which deletes by name. RDS and Neptune cluster PGs support
# CCAPI delete, so they need no handler here.
@resource_handler("AWS::DocDB::DBClusterParameterGroup", role="delete")
def _delete_docdb_cluster_parameter_group(
    resource: Resource, session: boto3.Session
) -> HandlerResult:
    # AWS-reserved ``default.<eng>`` groups are undeletable; they are filtered upstream as
    # AWS-managed drift noise (see verify/comparators.py), so this is a backstop for any that reach
    # the handler.
    if resource.identifier.startswith("default."):
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.SKIPPED,
            message="AWS-reserved default cluster parameter group — undeletable",
        )
    try:
        build_client(session, "docdb").delete_db_cluster_parameter_group(
            DBClusterParameterGroupName=resource.identifier
        )
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete cluster parameter group: {e}",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


# ── RDS Instance ─────────────────────────────────────────────────────


@resource_handler("AWS::RDS::DBInstance", role="prepare")
def _prepare_rds_instance(resource: Resource, session: boto3.Session) -> HandlerResult:
    client = build_client(session, "rds")
    try:
        resp = client.describe_db_instances(DBInstanceIdentifier=resource.identifier)
        instance = resp["DBInstances"][0]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("DBInstanceNotFoundFault", "DBInstanceNotFound"):
            logger.debug("Instance '%s' not found", resource.identifier)
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="prepare",
                status=HandlerStatus.SKIPPED,
                message="Instance not found",
            )
        logger.warning("Error describing instance '%s': %s", resource.identifier, e)
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Error describing instance: {e}",
        )
    except BotoCoreError as e:
        logger.warning("Connection error describing instance '%s': %s", resource.identifier, e)
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="prepare",
            status=HandlerStatus.FAILED,
            message=f"Connection error: {e}",
        )
    if instance.get("DeletionProtection"):
        client.modify_db_instance(
            DBInstanceIdentifier=resource.identifier, DeletionProtection=False
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message="Deletion protection disabled",
    )


@resource_handler("AWS::RDS::DBInstance", role="delete")
def _delete_rds_instance(resource: Resource, session: boto3.Session) -> HandlerResult:
    try:
        build_client(session, "rds").delete_db_instance(
            DBInstanceIdentifier=resource.identifier,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True,
        )
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete instance: {e}",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


# ── RDS Cluster ──────────────────────────────────────────────────────


@resource_handler("AWS::RDS::DBCluster", role="prepare")
def _prepare_rds_cluster(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _prepare_cluster(build_client(session, "rds"), resource.identifier, resource.type)


@resource_handler("AWS::RDS::DBCluster", role="delete")
def _delete_rds_cluster(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _delete_cluster(build_client(session, "rds"), resource.identifier, resource.type)


# ── Neptune ──────────────────────────────────────────────────────────


@resource_handler("AWS::Neptune::DBCluster", role="prepare")
def _prepare_neptune(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _prepare_cluster(build_client(session, "neptune"), resource.identifier, resource.type)


@resource_handler("AWS::Neptune::DBCluster", role="delete")
def _delete_neptune(resource: Resource, session: boto3.Session) -> HandlerResult:
    return _delete_cluster(build_client(session, "neptune"), resource.identifier, resource.type)


# ── RDS snapshots (no CFN type; deleted via the RDS service API) ──────
#
# CloudControl has no AWS::RDS::DBSnapshot / DBClusterSnapshot type, so fast-scan detects these but
# they need a service-API delete. Two facts verified live against RDS:
#   1. Only MANUAL snapshots can be deleted. AUTOMATED (system) snapshots reject a manual delete
#      ("automated snapshots cannot be deleted") and AWS removes them with their parent DB. They are
#      filtered upstream as AWS-managed drift noise (see verify/comparators.py AWS_MANAGED_FILTERS),
#      so they should not reach this handler; the guard here is a backstop that SKIPs any that do.
#   2. delete_db_snapshot wants the snapshot IDENTIFIER, not the ARN — passing the ARN returns
#      DBSnapshotNotFound. The lister emits the ARN, so extract the identifier (the ARN tail after
#      ``:snapshot:`` / ``:cluster-snapshot:``).


def _is_automated_snapshot(identifier: str) -> bool:
    """True if an RDS snapshot ARN/id is an automated (system) snapshot, not a manual one.

    An automated snapshot's *resource name* is owned by the ``rds`` system, so its id carries an
    ``rds:`` owner prefix: ARNs look like ``…:snapshot:rds:<db>-<date>`` /
    ``…:cluster-snapshot:rds:<cl>-<date>``, and the bare id form is ``rds:<db>-<date>``. (Note the
    ``arn:aws:rds:`` *service* segment is present on every RDS ARN and must NOT be treated as the
    automated marker — match only the resource-name ``rds:`` prefix.)
    """
    return "snapshot:rds:" in identifier or identifier.startswith("rds:")


def _snapshot_identifier(arn_or_id: str, marker: str) -> str:
    """The RDS snapshot identifier delete_* wants, from the lister's ARN (or a bare id).

    ``marker`` is ``":snapshot:"`` (instance) or ``":cluster-snapshot:"`` (cluster). The ARN tail
    after the marker is the DBSnapshotIdentifier / DBClusterSnapshotIdentifier the delete API needs
    (the ARN itself returns DBSnapshotNotFound — verified live).
    """
    return arn_or_id.split(marker, 1)[-1] if marker in arn_or_id else arn_or_id


@resource_handler("AWS::RDS::DBSnapshot", role="delete")
def _delete_rds_db_snapshot(resource: Resource, session: boto3.Session) -> HandlerResult:
    if _is_automated_snapshot(resource.identifier):
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.SKIPPED,
            message="Automated snapshot — deleted by AWS retention when its DB instance is removed",
        )
    try:
        build_client(session, "rds").delete_db_snapshot(
            DBSnapshotIdentifier=_snapshot_identifier(resource.identifier, ":snapshot:")
        )
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete DB snapshot: {e}",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


@resource_handler("AWS::RDS::DBClusterSnapshot", role="delete")
def _delete_rds_db_cluster_snapshot(resource: Resource, session: boto3.Session) -> HandlerResult:
    if _is_automated_snapshot(resource.identifier):
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.SKIPPED,
            message="Automated cluster snapshot — removed by AWS retention with its DB cluster",
        )
    try:
        build_client(session, "rds").delete_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=_snapshot_identifier(
                resource.identifier, ":cluster-snapshot:"
            )
        )
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete DB cluster snapshot: {e}",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )


# ── RDS tenant databases (Oracle CDB; no CFN type) ───────────────────
#
# DeleteTenantDatabase needs both the parent DBInstanceIdentifier and the TenantDBName. The lister
# emits the tenant-database ARN (arn:aws:rds:region:acct:tenant-database:<id>), from which neither
# is directly recoverable, so the handler looks the tenant DB up by ARN to get both.


@resource_handler("AWS::RDS::TenantDatabase", role="delete")
def _delete_rds_tenant_database(resource: Resource, session: boto3.Session) -> HandlerResult:
    client = build_client(session, "rds")
    try:
        resp = client.describe_tenant_databases(
            Filters=[{"Name": "tenant-database-resource-id", "Values": [resource.identifier]}]
        )
        tenants = resp.get("TenantDatabases", [])
        if not tenants:
            # Fall back to matching the ARN directly across all tenant databases.
            tenants = [
                t
                for page in client.get_paginator("describe_tenant_databases").paginate()
                for t in page.get("TenantDatabases", [])
                if resource.identifier
                in (t.get("TenantDatabaseARN"), t.get("TenantDatabaseResourceId"))
            ]
        if not tenants:
            return HandlerResult(
                resource_id=resource.identifier,
                resource_type=resource.type,
                action="delete",
                status=HandlerStatus.SKIPPED,
                message="Tenant database not found (already deleted with its DB instance)",
            )
        tenant = tenants[0]
        client.delete_tenant_database(
            DBInstanceIdentifier=tenant["DBInstanceIdentifier"],
            TenantDBName=tenant["TenantDBName"],
            SkipFinalSnapshot=True,
        )
    except (ClientError, BotoCoreError) as e:
        return HandlerResult(
            resource_id=resource.identifier,
            resource_type=resource.type,
            action="delete",
            status=HandlerStatus.FAILED,
            message=f"Failed to delete tenant database: {e}",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="delete",
        status=HandlerStatus.SUCCESS,
    )
