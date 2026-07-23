"""Service-specific verifier implementations.

Each function checks if a specific AWS resource type still exists
by calling the appropriate service API.

All verifiers should return True if the resource exists, False if it doesn't exist,
and let exceptions propagate if verification fails (handled by the caller).
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from aws_bench.resource_management.cleanup.verification.registry import verifies
from aws_bench.utils.concurrent import build_client


@verifies("AWS::S3::Bucket")
def _check_s3_bucket(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "s3")
    try:
        client.list_objects_v2(Bucket=physical_id, MaxKeys=1)
        return True
    except client.exceptions.NoSuchBucket:
        return False


@verifies("AWS::S3Express::DirectoryBucket")
def _check_s3express_directory_bucket(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "s3")
    try:
        client.list_objects_v2(Bucket=physical_id, MaxKeys=1)
        return True
    except client.exceptions.NoSuchBucket:
        return False
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in ("NoSuchBucket", "404"):
            return False
        raise


@verifies("AWS::EC2::EIP")
def _check_eip(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "ec2")
    # Scanner emits composite ``PublicIp|AllocationId``; describe_addresses wants the bare
    # AllocationId. A bare id has no '|', so rsplit is a no-op.
    allocation_id = physical_id.rsplit("|", 1)[-1]
    try:
        resp = client.describe_addresses(AllocationIds=[allocation_id])
        return bool(resp.get("Addresses"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "InvalidAllocationID.NotFound":
            return False
        raise


@verifies("AWS::ECS::Service")
def _check_ecs_service(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "ecs")
    try:
        cluster = physical_id.rsplit("/", 1)[0].replace(":service/", ":cluster/")
        resp = client.describe_services(cluster=cluster, services=[physical_id])
        return any(svc["status"] != "INACTIVE" for svc in resp.get("services", []))
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("ServiceNotFoundException", "ClusterNotFoundException"):
            return False
        raise


@verifies("AWS::DocDB::DBCluster")
def _check_docdb_cluster(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "docdb")
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=physical_id)
        return bool(resp.get("DBClusters"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "DBClusterNotFoundFault":
            return False
        raise


@verifies("AWS::DocDB::DBInstance")
def _check_docdb_instance(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "docdb")
    try:
        resp = client.describe_db_instances(DBInstanceIdentifier=physical_id)
        return bool(resp.get("DBInstances"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "DBInstanceNotFoundFault":
            return False
        raise


@verifies("AWS::DocDB::DBSubnetGroup")
def _check_docdb_subnet_group(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "docdb")
    try:
        resp = client.describe_db_subnet_groups(DBSubnetGroupName=physical_id)
        return bool(resp.get("DBSubnetGroups"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "DBSubnetGroupNotFoundFault":
            return False
        raise


@verifies("AWS::EMR::Cluster")
def _check_emr_cluster(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "emr")
    try:
        resp = client.describe_cluster(ClusterId=physical_id)
        state = resp["Cluster"]["Status"]["State"]
        return state not in ("TERMINATED", "TERMINATED_WITH_ERRORS")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("InvalidRequestException", "ClusterNotFound"):
            return False
        raise


@verifies("AWS::SageMaker::NotebookInstance")
def _check_sagemaker_notebook(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "sagemaker")
    try:
        client.describe_notebook_instance(NotebookInstanceName=physical_id)
        return True
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("ValidationException", "ResourceNotFound"):
            return False
        raise


@verifies("AWS::SageMaker::Model")
def _check_sagemaker_model(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "sagemaker")
    try:
        client.describe_model(ModelName=physical_id)
        return True
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("ValidationException", "ResourceNotFound"):
            return False
        raise


@verifies("AWS::IAM::Policy")
def _check_iam_policy(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "iam")
    try:
        client.get_policy(PolicyArn=physical_id)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return False
        raise


@verifies("AWS::CodeBuild::Project")
def _check_codebuild_project(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "codebuild")
    try:
        resp = client.batch_get_projects(names=[physical_id])
        return bool(resp.get("projects"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


@verifies("AWS::Pinpoint::App")
def _check_pinpoint_app(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "pinpoint")
    try:
        client.get_app(ApplicationId=physical_id)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "NotFoundException":
            return False
        raise


@verifies("AWS::AppStream::Fleet")
def _check_appstream_fleet(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "appstream")
    try:
        resp = client.describe_fleets(Names=[physical_id])
        return bool(resp.get("Fleets"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


@verifies("AWS::Glue::Workflow")
def _check_glue_workflow(session: boto3.Session, physical_id: str) -> bool:
    client = build_client(session, "glue")
    try:
        client.get_workflow(Name=physical_id)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "EntityNotFoundException":
            return False
        raise
