"""Tests for StackInspector."""

import json

import boto3
from moto import mock_aws

from aws_bench.resource_management.snapshot.models import StackMetadata
from aws_bench.resource_management.verify.stack_inspector import StackInspector

# ===========================================================================
# StackInspector initialization
# ===========================================================================


@mock_aws
def test_stack_inspector_initialization():
    """Test StackInspector initialization."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    assert inspector._cfn_client is not None


# ===========================================================================
# check_stack_status - scenarios
# ===========================================================================


@mock_aws
def test_check_stack_status_all_stacks_match():
    """Test stack status check when all stacks match baseline."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create stack
    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    # Define baseline
    stack_metadata = {
        "test-stack": StackMetadata(
            status="CREATE_COMPLETE",
            template_hash="sha256:abc123",
        )
    }

    result = inspector.check_stack_status(stack_metadata)

    assert result.success is True
    assert result.error_reason == ""


@mock_aws
def test_check_stack_status_stack_missing():
    """Test stack status check when stack is missing."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    # Define baseline for non-existent stack
    stack_metadata = {
        "missing-stack": StackMetadata(
            status="CREATE_COMPLETE",
            template_hash="sha256:abc123",
        )
    }

    result = inspector.check_stack_status(stack_metadata)

    assert result.success is False
    assert "status mismatch" in result.error_reason
    assert result.error_details is not None
    assert "missing-stack" in result.error_details
    assert result.error_details["missing-stack"]["expected"] == "CREATE_COMPLETE"
    assert result.error_details["missing-stack"]["actual"] == "MISSING"


@mock_aws
def test_check_stack_status_status_changed():
    """Test stack status check when status differs from baseline."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create stack
    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    # Define baseline with different status
    stack_metadata = {
        "test-stack": StackMetadata(
            status="UPDATE_COMPLETE",  # Different from CREATE_COMPLETE
            template_hash="sha256:abc123",
        )
    }

    result = inspector.check_stack_status(stack_metadata)

    assert result.success is False
    assert "status mismatch" in result.error_reason
    assert result.error_details is not None
    assert "test-stack" in result.error_details
    assert result.error_details["test-stack"]["expected"] == "UPDATE_COMPLETE"
    assert result.error_details["test-stack"]["actual"] == "CREATE_COMPLETE"


# ===========================================================================
# check_template_hash - scenarios
# ===========================================================================


@mock_aws
def test_check_template_hash_all_match():
    """Test template hash check when all templates match."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create stack
    template = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}
    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps(template),
    )

    # Compute actual hash
    template_hash = inspector._compute_template_hash(template)

    # Define baseline with matching hash
    stack_metadata = {
        "test-stack": StackMetadata(
            status="CREATE_COMPLETE",
            template_hash=template_hash,
        )
    }

    result = inspector.check_template_hash(stack_metadata)

    assert result.success is True


@mock_aws
def test_check_template_hash_mismatch():
    """Test template hash check when hash differs."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create stack
    template = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}
    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps(template),
    )

    # Define baseline with different hash
    stack_metadata = {
        "test-stack": StackMetadata(
            status="CREATE_COMPLETE",
            template_hash="sha256:different",
        )
    }

    result = inspector.check_template_hash(stack_metadata)

    assert result.success is False
    assert "template changed" in result.error_reason
    # error_details now carries the mismatched stack NAMES so reset can delete+redeploy
    # them, rather than a static advisory string.
    assert result.error_details == {"template_mismatch_stacks": ["test-stack"]}


@mock_aws
def test_check_template_hash_collects_all_mismatched_stacks():
    """Every template-mismatched stack is reported, not just the first.

    Reset deletes each named stack for re-setup, so the check must not short-circuit
    on the first mismatch or the others would be left drifted.
    """
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    template = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}
    for name in ("stack-a", "stack-b"):
        cfn.create_stack(StackName=name, TemplateBody=json.dumps(template))

    stack_metadata = {
        "stack-a": StackMetadata(status="CREATE_COMPLETE", template_hash="sha256:wrong-a"),
        "stack-b": StackMetadata(status="CREATE_COMPLETE", template_hash="sha256:wrong-b"),
    }

    result = inspector.check_template_hash(stack_metadata)

    assert result.success is False
    assert isinstance(result.error_details, dict)
    assert sorted(result.error_details["template_mismatch_stacks"]) == ["stack-a", "stack-b"]
    assert "+1 more" in result.error_reason


@mock_aws
def test_check_template_hash_skips_cdk_toolkit():
    """Test that CDKToolkit is skipped during template verification."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create CDKToolkit stack
    template = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}
    cfn.create_stack(
        StackName="CDKToolkit",
        TemplateBody=json.dumps(template),
    )

    # Define baseline with different hash (should be skipped)
    stack_metadata = {
        "CDKToolkit": StackMetadata(
            status="CREATE_COMPLETE",
            template_hash="sha256:different",
        )
    }

    result = inspector.check_template_hash(stack_metadata)

    # Should succeed because CDKToolkit is skipped
    assert result.success is True


@mock_aws
def test_check_template_hash_get_template_fails():
    """Test template hash check when getting template fails."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    # Define baseline for non-existent stack
    stack_metadata = {
        "missing-stack": StackMetadata(
            status="CREATE_COMPLETE",
            template_hash="sha256:abc123",
        )
    }

    result = inspector.check_template_hash(stack_metadata)

    assert result.success is False
    assert "Failed to verify template" in result.error_reason


# ===========================================================================
# _compute_template_hash - various formats
# ===========================================================================


@mock_aws
def test_compute_template_hash_dict():
    """Test computing hash from dict template."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}},
    }

    hash_result = inspector._compute_template_hash(template)

    assert hash_result.startswith("sha256:")
    assert len(hash_result) == 71  # "sha256:" (7) + 64 hex chars


@mock_aws
def test_compute_template_hash_json_string():
    """Test computing hash from JSON string template."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    template = '{"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}'

    hash_result = inspector._compute_template_hash(template)

    assert hash_result.startswith("sha256:")


@mock_aws
def test_compute_template_hash_yaml_string():
    """Test computing hash from YAML string template."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    template = "AWSTemplateFormatVersion: 2010-09-09\nResources: {}"

    hash_result = inspector._compute_template_hash(template)

    assert hash_result.startswith("sha256:")


@mock_aws
def test_compute_template_hash_consistent():
    """Test that hash computation is consistent."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    template = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}

    hash1 = inspector._compute_template_hash(template)
    hash2 = inspector._compute_template_hash(template)

    assert hash1 == hash2


@mock_aws
def test_compute_template_hash_key_order_independent():
    """Test that hash is independent of key order."""
    session = boto3.Session(region_name="us-east-1")
    inspector = StackInspector(session)

    template1 = {"Resources": {}, "AWSTemplateFormatVersion": "2010-09-09"}
    template2 = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}

    hash1 = inspector._compute_template_hash(template1)
    hash2 = inspector._compute_template_hash(template2)

    # Should be same because we sort keys
    assert hash1 == hash2


# ===========================================================================
# _list_cloudformation_stacks - filtering
# ===========================================================================


@mock_aws
def test_list_cloudformation_stacks_filters_deleted():
    """Test that DELETE_COMPLETE stacks are filtered out."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create and delete a stack
    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )
    cfn.delete_stack(StackName="test-stack")

    stacks = inspector._list_cloudformation_stacks()

    # Should not include deleted stacks
    assert not any(s["StackName"] == "test-stack" for s in stacks)


@mock_aws
def test_list_cloudformation_stacks_includes_active():
    """Test that active stacks are included."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create stack
    cfn.create_stack(
        StackName="test-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    stacks = inspector._list_cloudformation_stacks()

    # Should include active stack
    assert any(s["StackName"] == "test-stack" for s in stacks)


@mock_aws
def test_list_cloudformation_stacks_filters_nested():
    """Test that nested stacks are filtered out."""
    session = boto3.Session(region_name="us-east-1")
    cfn = session.client("cloudformation")
    inspector = StackInspector(session)

    # Create parent stack
    cfn.create_stack(
        StackName="parent-stack",
        TemplateBody=json.dumps({"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}),
    )

    stacks = inspector._list_cloudformation_stacks()

    # Should only include root stacks (no ParentId)
    assert all("ParentId" not in s for s in stacks)
