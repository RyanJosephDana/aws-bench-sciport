"""Tests for aws_bench.resource_management.cleanup.models."""

from __future__ import annotations

import pytest

from aws_bench.resource_management.cleanup.models import (
    GLOBAL_RESOURCE_TYPES,
    SWEEPABLE_INFRA_TYPES,
    CleanupSummary,
    DeletionSummary,
    RegionResult,
    StackDeletionResult,
    StackDeletionStatus,
    StackResource,
    exclude_infra_resources,
    is_infra_identifier,
    partition_by_scope,
)


def _make_result(name, status):
    return StackDeletionResult(stack_name=name, status=status)


# -- CleanupSummary.all_stacks_succeeded --


def test_cleanup_summary_succeeds_when_clean_and_scan_complete():
    summary = CleanupSummary(regions=[RegionResult(region="us-east-1")])
    assert summary.all_stacks_succeeded is True


def test_cleanup_summary_incomplete_scan_is_not_a_pass():
    """An unfinished orphan scan must not read as success, even with no failures."""
    summary = CleanupSummary(
        regions=[RegionResult(region="us-east-1")],
        scan_incomplete=True,
    )
    assert summary.all_stacks_succeeded is False


def test_cleanup_summary_region_error_is_not_a_pass():
    summary = CleanupSummary(regions=[RegionResult(region="us-east-1", error="boom")])
    assert summary.all_stacks_succeeded is False


def test_failure_reason_reports_region_error_when_not_clean():
    """A region error with no failed stacks / orphans still yields a non-empty reason.

    Otherwise the empty reason produces a malformed verdict downstream ("account_id: ").
    """
    summary = CleanupSummary(
        regions=[
            RegionResult(region="us-east-1", error="boom"),
            RegionResult(region="us-west-2"),
        ]
    )
    assert summary.is_clean is False
    reason = summary.failure_reason
    assert reason  # non-empty
    assert "1 region" in reason and "error" in reason.lower()


# -- CleanupSummary.failure_reason aggregation --


def test_failure_reason_empty_when_clean():
    """A clean summary has no cause to report."""
    summary = CleanupSummary(regions=[RegionResult(region="us-east-1")])
    assert summary.is_clean is True
    assert summary.failure_reason == ""


def test_failure_reason_single_cause_reads_naturally():
    """A lone cause is reported verbatim, with no separator."""
    summary = CleanupSummary(regions=[RegionResult(region="us-east-1", stacks_failed=["stack-a"])])
    assert summary.failure_reason == "1 stack(s) failed to delete"
    assert "; " not in summary.failure_reason


def test_failure_reason_joins_multiple_causes_with_semicolon():
    """Two causes are both surfaced, joined by '; ' in severity order."""
    summary = CleanupSummary(
        regions=[RegionResult(region="us-east-1", stacks_failed=["stack-a"])],
        orphaned_resources={"AWS::S3::Bucket": ["bucket-1"]},
    )
    assert summary.failure_reason == "1 stack(s) failed to delete; 1 orphaned resource(s) remain"


def test_failure_reason_aggregates_all_causes_in_severity_order():
    """Every non-empty cause appears, ordered most- to least-severe.

    The old first-match logic masked all but the most-severe cause; the verdict
    must list them all so an operator sees the full picture.
    """
    summary = CleanupSummary(
        regions=[RegionResult(region="us-east-1", stacks_failed=["stack-a"], error="boom")],
        orphaned_resources={"AWS::S3::Bucket": ["bucket-1"]},
        scan_incomplete=True,
    )
    reason = summary.failure_reason

    scan_part = "scan incomplete"
    failed_part = "1 stack(s) failed to delete"
    orphan_part = "1 orphaned resource(s) remain"
    region_part = "1 region(s) errored during cleanup: us-east-1"

    for part in (scan_part, failed_part, orphan_part, region_part):
        assert part in reason
    # severity order: scan_incomplete > failed stacks > orphaned > region errors
    assert (
        reason.index(scan_part)
        < reason.index(failed_part)
        < reason.index(orphan_part)
        < reason.index(region_part)
    )
    assert reason.count("; ") == 3


# -- DeletionSummary --


def test_succeeded_filters_correctly():
    summary = DeletionSummary(
        results=[
            _make_result("a", StackDeletionStatus.SUCCESS),
            _make_result("b", StackDeletionStatus.FAILED),
            _make_result("c", StackDeletionStatus.SUCCESS),
        ]
    )
    assert len(summary.succeeded) == 2
    assert {result.stack_name for result in summary.succeeded} == {"a", "c"}


def test_failed_filters_correctly():
    summary = DeletionSummary(
        results=[
            _make_result("a", StackDeletionStatus.SUCCESS),
            _make_result("b", StackDeletionStatus.FAILED),
        ]
    )
    assert len(summary.failed) == 1
    assert summary.failed[0].stack_name == "b"


def test_all_succeeded_true_when_no_failures():
    """NOT_FOUND is not a failure — all_succeeded should be True."""
    summary = DeletionSummary(
        results=[
            _make_result("a", StackDeletionStatus.SUCCESS),
            _make_result("b", StackDeletionStatus.NOT_FOUND),
        ]
    )
    assert summary.all_succeeded is True


def test_all_succeeded_false_when_any_failed():
    summary = DeletionSummary(
        results=[
            _make_result("a", StackDeletionStatus.SUCCESS),
            _make_result("b", StackDeletionStatus.FAILED),
        ]
    )
    assert summary.all_succeeded is False


def test_deferred_result_excluded_from_failed():
    """A deferred stack keeps FAILED status but must not count as a failure."""
    deferred = StackDeletionResult(
        stack_name="opensearch",
        status=StackDeletionStatus.FAILED,
        deferred=True,
    )
    summary = DeletionSummary(results=[_make_result("a", StackDeletionStatus.SUCCESS), deferred])
    assert summary.failed == []
    assert [r.stack_name for r in summary.deferred] == ["opensearch"]
    # Not counted as deleted either — it is a distinct third bucket.
    assert {r.stack_name for r in summary.succeeded} == {"a"}


def test_all_succeeded_true_when_only_deferred_and_success():
    summary = DeletionSummary(
        results=[
            _make_result("a", StackDeletionStatus.SUCCESS),
            StackDeletionResult(stack_name="b", status=StackDeletionStatus.FAILED, deferred=True),
        ]
    )
    assert summary.all_succeeded is True


def test_all_succeeded_false_when_real_failure_alongside_deferred():
    summary = DeletionSummary(
        results=[
            StackDeletionResult(stack_name="a", status=StackDeletionStatus.FAILED, deferred=True),
            _make_result("b", StackDeletionStatus.FAILED),
        ]
    )
    assert summary.all_succeeded is False
    assert [r.stack_name for r in summary.failed] == ["b"]


def test_empty_summary_is_vacuously_successful():
    summary = DeletionSummary()
    assert summary.succeeded == []
    assert summary.failed == []
    assert summary.all_succeeded is True


# -- StackResource --


def test_stack_resource_fields():
    resource = StackResource(
        logical_id="MyBucket",
        physical_id="my-bucket-abc123",
        resource_type="AWS::S3::Bucket",
        status="CREATE_COMPLETE",
    )
    assert resource.logical_id == "MyBucket"
    assert resource.physical_id == "my-bucket-abc123"
    assert resource.resource_type == "AWS::S3::Bucket"
    assert resource.status == "CREATE_COMPLETE"


# -- exclude_infra_resources --


def test_removes_cdktoolkit_resources():
    resources = {
        "AWS::S3::Bucket": [
            {"Identifier": "CDKToolkit-bucket-123"},
            {"Identifier": "my-app-bucket"},
        ]
    }
    result = exclude_infra_resources(resources)
    assert len(result["AWS::S3::Bucket"]) == 1
    assert result["AWS::S3::Bucket"][0]["Identifier"] == "my-app-bucket"


def test_removes_cdk_hnb_resources():
    resources = {"AWS::S3::Bucket": [{"Identifier": "cdk-hnb659fds-assets-123-us-east-1"}]}
    result = exclude_infra_resources(resources)
    assert "AWS::S3::Bucket" not in result


def test_keeps_non_infra_resources():
    resources = {"AWS::Lambda::Function": [{"Identifier": "my-function"}]}
    result = exclude_infra_resources(resources)
    assert len(result["AWS::Lambda::Function"]) == 1


def test_empty_input():
    assert exclude_infra_resources({}) == {}


def test_removes_type_when_all_filtered():
    resources = {"AWS::S3::Bucket": [{"Identifier": "CDKToolkit-bucket"}]}
    result = exclude_infra_resources(resources)
    assert "AWS::S3::Bucket" not in result


def test_removes_cdk_bootstrap_ssm_param_by_arn_substring():
    """The bootstrap param's identifier is the ARN, so a prefix check misses it."""
    arn = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/cdk-bootstrap/hnb659fds/version"
    resources = {"AWS::SSM::Parameter": [{"Identifier": arn}]}
    result = exclude_infra_resources(resources)
    assert "AWS::SSM::Parameter" not in result


def test_keeps_scenario_owned_ssm_param():
    arn = "arn:aws:ssm:us-east-1:123456789012:parameter/app/db"
    resources = {"AWS::SSM::Parameter": [{"Identifier": arn}]}
    result = exclude_infra_resources(resources)
    assert len(result["AWS::SSM::Parameter"]) == 1


@pytest.mark.parametrize(
    ("identifier", "is_infra"),
    [
        pytest.param("CDKToolkit", True, id="cdktoolkit-stack-prefix"),
        pytest.param("cdk-hnb659fds-assets-123-us-east-1", True, id="cdk-asset-prefix"),
        pytest.param(
            "arn:aws:ssm:ap-northeast-1:123456789012:parameter/cdk-bootstrap/hnb659fds/version",
            True,
            id="bootstrap-param-arn-substring",
        ),
        pytest.param(
            "arn:aws:ssm:us-east-1:123456789012:parameter/app/db", False, id="scenario-ssm-param"
        ),
        pytest.param("my-app-bucket", False, id="plain-scenario-name"),
    ],
)
def test_is_infra_identifier(identifier: str, is_infra: bool):
    assert is_infra_identifier(identifier) is is_infra


# -- exclude_infra_resources: keep_types (Fix 1) --


def test_keep_types_reincludes_regional_cdk_asset_bucket():
    """include_infra reclaims the CDKToolkit assets bucket via keep_types."""
    resources = {
        "AWS::S3::Bucket": [
            {"Identifier": "cdk-hnb659fds-assets-123-us-east-1"},
            {"Identifier": "my-app-bucket"},
        ]
    }
    result = exclude_infra_resources(resources, keep_types=SWEEPABLE_INFRA_TYPES)
    ids = {i["Identifier"] for i in result["AWS::S3::Bucket"]}
    assert ids == {"cdk-hnb659fds-assets-123-us-east-1", "my-app-bucket"}


def test_keep_types_never_reincludes_bootstrap_iam_role():
    """The core Fix-1 guarantee: bootstrap IAM roles stay excluded even with keep_types.

    IAM::Role is not in SWEEPABLE_INFRA_TYPES, so the cfn-exec-role is filtered out
    regardless of the infra opt-in — it must never become a sweep candidate while a
    stack could still reference it as its RoleARN.
    """
    resources = {
        "AWS::IAM::Role": [
            {"Identifier": "cdk-hnb659fds-cfn-exec-role-123-us-east-1"},
            {"Identifier": "cdk-hnb659fds-deploy-role-123-us-east-1"},
        ]
    }
    result = exclude_infra_resources(resources, keep_types=SWEEPABLE_INFRA_TYPES)
    assert "AWS::IAM::Role" not in result


def test_keep_types_defaults_to_full_infra_exclusion():
    """With no keep_types (default), infra of every type — including buckets — is dropped."""
    resources = {"AWS::S3::Bucket": [{"Identifier": "cdk-hnb659fds-assets-123-us-east-1"}]}
    assert exclude_infra_resources(resources) == {}


# -- partition_by_scope (Fix 2) --


def test_partition_by_scope_splits_global_from_regional():
    resources = {
        "AWS::IAM::Role": [{"Identifier": "some-role"}],
        "AWS::S3::Bucket": [{"Identifier": "some-bucket"}],
        "AWS::EC2::Subnet": [{"Identifier": "subnet-1"}],
    }
    regional, global_ = partition_by_scope(resources)
    assert set(global_) == {"AWS::IAM::Role"}
    assert set(regional) == {"AWS::S3::Bucket", "AWS::EC2::Subnet"}


def test_partition_by_scope_empty():
    assert partition_by_scope({}) == ({}, {})


def test_iam_role_is_a_global_type():
    """Guards the constant that drives the barrier: IAM roles must be treated global."""
    assert "AWS::IAM::Role" in GLOBAL_RESOURCE_TYPES
