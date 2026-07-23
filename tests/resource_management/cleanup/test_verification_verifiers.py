"""Tests for verification service-specific verifiers and edge cases."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from aws_bench.resource_management.cleanup.models import ExistenceStatus, StackResource
from aws_bench.resource_management.cleanup.verification.manager import ResourceVerifier
from aws_bench.resource_management.cleanup.verification.verifiers import (
    _check_appstream_fleet,
    _check_codebuild_project,
    _check_docdb_cluster,
    _check_docdb_instance,
    _check_docdb_subnet_group,
    _check_ecs_service,
    _check_eip,
    _check_emr_cluster,
    _check_glue_workflow,
    _check_iam_policy,
    _check_pinpoint_app,
    _check_s3_bucket,
    _check_sagemaker_model,
    _check_sagemaker_notebook,
)

# -- Service verifiers --


def test_check_ecs_service_active():
    session = MagicMock()
    session.client.return_value.describe_services.return_value = {
        "services": [{"status": "ACTIVE"}]
    }
    assert _check_ecs_service(session, "arn:aws:ecs:us-east-1:123:service/cluster/svc") is True


def test_check_ecs_service_inactive():
    session = MagicMock()
    session.client.return_value.describe_services.return_value = {
        "services": [{"status": "INACTIVE"}]
    }
    assert _check_ecs_service(session, "arn:aws:ecs:us-east-1:123:service/cluster/svc") is False


def test_check_docdb_cluster_exists():
    session = MagicMock()
    session.client.return_value.describe_db_clusters.return_value = {"DBClusters": [{}]}
    assert _check_docdb_cluster(session, "c1") is True


def test_check_docdb_cluster_empty():
    session = MagicMock()
    session.client.return_value.describe_db_clusters.return_value = {"DBClusters": []}
    assert _check_docdb_cluster(session, "c1") is False


def test_check_docdb_instance_exists():
    session = MagicMock()
    session.client.return_value.describe_db_instances.return_value = {"DBInstances": [{}]}
    assert _check_docdb_instance(session, "i1") is True


def test_check_docdb_subnet_group_exists():
    session = MagicMock()
    session.client.return_value.describe_db_subnet_groups.return_value = {"DBSubnetGroups": [{}]}
    assert _check_docdb_subnet_group(session, "sg1") is True


def test_check_emr_cluster_running():
    session = MagicMock()
    session.client.return_value.describe_cluster.return_value = {
        "Cluster": {"Status": {"State": "RUNNING"}}
    }
    assert _check_emr_cluster(session, "j-123") is True


def test_check_emr_cluster_terminated():
    session = MagicMock()
    session.client.return_value.describe_cluster.return_value = {
        "Cluster": {"Status": {"State": "TERMINATED"}}
    }
    assert _check_emr_cluster(session, "j-123") is False


def test_check_sagemaker_notebook():
    session = MagicMock()
    assert _check_sagemaker_notebook(session, "nb1") is True
    session.client.return_value.describe_notebook_instance.assert_called_once()


def test_check_sagemaker_model():
    session = MagicMock()
    assert _check_sagemaker_model(session, "m1") is True
    session.client.return_value.describe_model.assert_called_once()


def test_check_iam_policy():
    session = MagicMock()
    assert _check_iam_policy(session, "arn:aws:iam::123:policy/p") is True
    session.client.return_value.get_policy.assert_called_once()


def test_check_codebuild_project_exists():
    session = MagicMock()
    session.client.return_value.batch_get_projects.return_value = {"projects": [{}]}
    assert _check_codebuild_project(session, "proj1") is True


def test_check_codebuild_project_empty():
    session = MagicMock()
    session.client.return_value.batch_get_projects.return_value = {"projects": []}
    assert _check_codebuild_project(session, "proj1") is False


def test_check_pinpoint_app():
    session = MagicMock()
    assert _check_pinpoint_app(session, "app1") is True
    session.client.return_value.get_app.assert_called_once()


def test_check_appstream_fleet_exists():
    session = MagicMock()
    session.client.return_value.describe_fleets.return_value = {"Fleets": [{}]}
    assert _check_appstream_fleet(session, "fleet1") is True


def test_check_appstream_fleet_empty():
    session = MagicMock()
    session.client.return_value.describe_fleets.return_value = {"Fleets": []}
    assert _check_appstream_fleet(session, "fleet1") is False


def test_check_glue_workflow():
    session = MagicMock()
    assert _check_glue_workflow(session, "wf1") is True
    session.client.return_value.get_workflow.assert_called_once()


# -- Verifier error paths --


def test_verify_single_verifier_exception_returns_unknown():
    session = MagicMock()
    verifier = ResourceVerifier(session)
    resource = StackResource("L1", "b1", "AWS::S3::Bucket", "CREATE_COMPLETE")
    with patch(
        "aws_bench.resource_management.cleanup.verification.registry._VERIFIER_REGISTRY",
        {"AWS::S3::Bucket": MagicMock(side_effect=Exception("boom"))},
    ):
        result = asyncio.run(verifier.verify_resources([resource]))
    assert result[0].existence_status == ExistenceStatus.UNKNOWN


def test_verify_single_ccapi_unsupported_returns_skipped():
    from botocore.exceptions import ClientError

    session = MagicMock()
    verifier = ResourceVerifier(session)
    resource = StackResource("L1", "x1", "AWS::Custom::Thing", "CREATE_COMPLETE")
    error = ClientError(
        {"Error": {"Code": "UnsupportedActionException", "Message": "nope"}}, "GetResource"
    )
    with (
        patch("aws_bench.resource_management.cleanup.verification.registry._VERIFIER_REGISTRY", {}),
        patch.object(verifier._ccm, "resource_exists", side_effect=error),
    ):
        result = asyncio.run(verifier.verify_resources([resource]))
    assert result[0].existence_status == ExistenceStatus.SKIPPED


# -- Exception path tests --


def test_check_s3_bucket_exists():
    session = MagicMock()
    session.client.return_value.list_objects_v2.return_value = {}
    assert _check_s3_bucket(session, "my-bucket") is True


def test_check_s3_bucket_not_found():
    session = MagicMock()
    client = session.client.return_value
    client.exceptions.NoSuchBucket = type("NoSuchBucket", (Exception,), {})
    client.list_objects_v2.side_effect = client.exceptions.NoSuchBucket()
    assert _check_s3_bucket(session, "my-bucket") is False


def test_check_eip_exists_with_composite_id():
    """Verifier must query describe_addresses with the AllocationId part of the composite id."""
    session = MagicMock()
    client = session.client.return_value
    client.describe_addresses.return_value = {"Addresses": [{"AllocationId": "eipalloc-123"}]}
    assert _check_eip(session, "1.2.3.4|eipalloc-123") is True
    client.describe_addresses.assert_called_once_with(AllocationIds=["eipalloc-123"])


def test_check_eip_exists_bare_id_still_supported():
    """A bare AllocationId (older snapshots / non-composite callers) still works."""
    session = MagicMock()
    session.client.return_value.describe_addresses.return_value = {
        "Addresses": [{"AllocationId": "eipalloc-123"}]
    }
    assert _check_eip(session, "eipalloc-123") is True


def test_check_eip_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InvalidAllocationID.NotFound"}}, "DescribeAddresses")
    session.client.return_value.describe_addresses.side_effect = error
    assert _check_eip(session, "1.2.3.4|eipalloc-123") is False


def test_check_eip_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeAddresses")
    session.client.return_value.describe_addresses.side_effect = error
    with pytest.raises(ClientError):
        _check_eip(session, "eipalloc-123")


def test_check_ecs_service_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ServiceNotFoundException"}}, "DescribeServices")
    session.client.return_value.describe_services.side_effect = error
    assert _check_ecs_service(session, "arn:aws:ecs:us-east-1:123:service/cluster/svc") is False


def test_check_ecs_cluster_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ClusterNotFoundException"}}, "DescribeServices")
    session.client.return_value.describe_services.side_effect = error
    assert _check_ecs_service(session, "arn:aws:ecs:us-east-1:123:service/cluster/svc") is False


def test_check_ecs_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InvalidParameterException"}}, "DescribeServices")
    session.client.return_value.describe_services.side_effect = error
    with pytest.raises(ClientError):
        _check_ecs_service(session, "arn:aws:ecs:us-east-1:123:service/cluster/svc")


def test_check_docdb_cluster_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "DBClusterNotFoundFault"}}, "DescribeDBClusters")
    session.client.return_value.describe_db_clusters.side_effect = error
    assert _check_docdb_cluster(session, "cluster1") is False


def test_check_docdb_cluster_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalServiceError"}}, "DescribeDBClusters")
    session.client.return_value.describe_db_clusters.side_effect = error
    with pytest.raises(ClientError):
        _check_docdb_cluster(session, "cluster1")


def test_check_docdb_instance_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "DBInstanceNotFoundFault"}}, "DescribeDBInstances")
    session.client.return_value.describe_db_instances.side_effect = error
    assert _check_docdb_instance(session, "instance1") is False


def test_check_docdb_instance_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalServiceError"}}, "DescribeDBInstances")
    session.client.return_value.describe_db_instances.side_effect = error
    with pytest.raises(ClientError):
        _check_docdb_instance(session, "instance1")


def test_check_docdb_subnet_group_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "DBSubnetGroupNotFoundFault"}}, "DescribeDBSubnetGroups")
    session.client.return_value.describe_db_subnet_groups.side_effect = error
    assert _check_docdb_subnet_group(session, "sg1") is False


def test_check_docdb_subnet_group_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalServiceError"}}, "DescribeDBSubnetGroups")
    session.client.return_value.describe_db_subnet_groups.side_effect = error
    with pytest.raises(ClientError):
        _check_docdb_subnet_group(session, "sg1")


def test_check_emr_cluster_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InvalidRequestException"}}, "DescribeCluster")
    session.client.return_value.describe_cluster.side_effect = error
    assert _check_emr_cluster(session, "j-123") is False


def test_check_emr_cluster_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalServerException"}}, "DescribeCluster")
    session.client.return_value.describe_cluster.side_effect = error
    with pytest.raises(ClientError):
        _check_emr_cluster(session, "j-123")


def test_check_sagemaker_notebook_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ValidationException"}}, "DescribeNotebookInstance")
    session.client.return_value.describe_notebook_instance.side_effect = error
    assert _check_sagemaker_notebook(session, "nb1") is False


def test_check_sagemaker_notebook_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalError"}}, "DescribeNotebookInstance")
    session.client.return_value.describe_notebook_instance.side_effect = error
    with pytest.raises(ClientError):
        _check_sagemaker_notebook(session, "nb1")


def test_check_sagemaker_model_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ResourceNotFound"}}, "DescribeModel")
    session.client.return_value.describe_model.side_effect = error
    assert _check_sagemaker_model(session, "model1") is False


def test_check_sagemaker_model_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalError"}}, "DescribeModel")
    session.client.return_value.describe_model.side_effect = error
    with pytest.raises(ClientError):
        _check_sagemaker_model(session, "model1")


def test_check_iam_policy_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "NoSuchEntity"}}, "GetPolicy")
    session.client.return_value.get_policy.side_effect = error
    assert _check_iam_policy(session, "arn:aws:iam::123:policy/p") is False


def test_check_iam_policy_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ServiceFailure"}}, "GetPolicy")
    session.client.return_value.get_policy.side_effect = error
    with pytest.raises(ClientError):
        _check_iam_policy(session, "arn:aws:iam::123:policy/p")


def test_check_codebuild_project_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "BatchGetProjects")
    session.client.return_value.batch_get_projects.side_effect = error
    assert _check_codebuild_project(session, "proj1") is False


def test_check_codebuild_project_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InvalidInputException"}}, "BatchGetProjects")
    session.client.return_value.batch_get_projects.side_effect = error
    with pytest.raises(ClientError):
        _check_codebuild_project(session, "proj1")


def test_check_pinpoint_app_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "NotFoundException"}}, "GetApp")
    session.client.return_value.get_app.side_effect = error
    assert _check_pinpoint_app(session, "app1") is False


def test_check_pinpoint_app_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalServerErrorException"}}, "GetApp")
    session.client.return_value.get_app.side_effect = error
    with pytest.raises(ClientError):
        _check_pinpoint_app(session, "app1")


def test_check_appstream_fleet_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeFleets")
    session.client.return_value.describe_fleets.side_effect = error
    assert _check_appstream_fleet(session, "fleet1") is False


def test_check_appstream_fleet_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InvalidParameterException"}}, "DescribeFleets")
    session.client.return_value.describe_fleets.side_effect = error
    with pytest.raises(ClientError):
        _check_appstream_fleet(session, "fleet1")


def test_check_glue_workflow_not_found():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "EntityNotFoundException"}}, "GetWorkflow")
    session.client.return_value.get_workflow.side_effect = error
    assert _check_glue_workflow(session, "wf1") is False


def test_check_glue_workflow_other_error_raises():
    session = MagicMock()
    error = ClientError({"Error": {"Code": "InternalServiceException"}}, "GetWorkflow")
    session.client.return_value.get_workflow.side_effect = error
    with pytest.raises(ClientError):
        _check_glue_workflow(session, "wf1")
