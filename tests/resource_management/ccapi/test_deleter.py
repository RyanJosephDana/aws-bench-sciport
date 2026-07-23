"""Tests for aws_bench.resource_management.ccapi.deleter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.deleter import (
    _MAX_RESOURCE_LEVEL,
    _RESOURCE_TYPE_TO_LEVELS,
    Deleter,
)
from aws_bench.resource_management.ccapi.exceptions import (
    CloudControlResourceDeletionException,
    ResourceExistenceCheckError,
    ResourceExistenceThrottledError,
    ResourceExistenceUnsupportedError,
)
from aws_bench.resource_management.ccapi.models import (
    Resource,
)

# -- _check_deletable --


def test_check_deletable_skips_non_ccapi_type():
    resource = Resource(type="Custom::MyThing", identifier="x")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, lambda r: True
        )
        is None
    )


def test_check_deletable_skips_single_colon_type():
    resource = Resource(type="AWS::S3", identifier="x")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, lambda r: True
        )
        is None
    )


def test_check_deletable_skips_nonexistent_resource():
    resource = Resource(type="AWS::S3::Bucket", identifier="gone")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, lambda r: False
        )
        is None
    )


def test_check_deletable_skips_service_linked_role():
    resource = Resource(type="AWS::IAM::Role", identifier="AWSServiceRoleForECS")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, lambda r: True
        )
        is None
    )


def test_check_deletable_skips_organization_account_access_role():
    """Skip OrganizationAccountAccessRole — deleting it severs cross-account access."""
    resource = Resource(type="AWS::IAM::Role", identifier="OrganizationAccountAccessRole")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, lambda r: True
        )
        is None
    )


def test_check_deletable_returns_resource_when_deletable():
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, lambda r: True
        )
        is resource
    )


def test_check_deletable_skips_unsupported_type():
    """CCAPI-unsupported types are skipped — delete_resource cannot handle them either."""
    resource = Resource(type="AWS::S3::Bucket", identifier="b")
    exc = ResourceExistenceUnsupportedError("CCAPI does not support AWS::S3::Bucket")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, MagicMock(side_effect=exc)
        )
        is None
    )


def test_check_deletable_attempts_delete_when_existence_check_errors():
    """A failed (non-unsupported) existence check must NOT skip a scanner-confirmed orphan.

    Regression test: a CCAPI GetResource HandlerInternalFailure on a still-live
    Kinesis stream was treated as "skip", leaking the stream. An unverified existence check
    means "unknown", not "gone" — the resource must still be attempted for deletion.
    """
    resource = Resource(type="AWS::Kinesis::Stream", identifier="bench-stream-193512")
    exc = ResourceExistenceCheckError(
        "Failed to check existence of AWS::Kinesis::Stream 'bench-stream-193512': "
        "HandlerInternalFailureException ... InternalFailure"
    )
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, MagicMock(side_effect=exc)
        )
        is resource
    )


def test_check_deletable_attempts_delete_when_existence_check_throttled():
    """A throttled existence check is unverified (not gone) — attempt the delete."""
    resource = Resource(type="AWS::S3::Bucket", identifier="b")
    exc = ResourceExistenceThrottledError("throttled")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, MagicMock(side_effect=exc)
        )
        is resource
    )


def test_check_deletable_attempts_delete_on_unexpected_error():
    """Any unverified existence outcome — even an unexpected error — attempts deletion."""
    resource = Resource(type="AWS::S3::Bucket", identifier="b")
    assert (
        Deleter(MagicMock(), resource_exists_fn=lambda r: True)._check_deletable(
            resource, MagicMock(side_effect=RuntimeError("fail"))
        )
        is resource
    )


# -- _order_batches --


def test_order_batches_iam_roles_in_separate_batch():
    role = Resource(type="AWS::IAM::Role", identifier="my-role")
    bucket = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    batches = Deleter(MagicMock(), resource_exists_fn=lambda r: True)._order_batches([role, bucket])
    assert len(batches) == 2
    assert bucket in batches[0]
    assert role in batches[1]


def test_order_batches_single_type_single_batch():
    resources = [Resource(type="AWS::S3::Bucket", identifier=f"b{idx}") for idx in range(3)]
    batches = Deleter(MagicMock(), resource_exists_fn=lambda r: True)._order_batches(resources)
    assert len(batches) == 1
    assert len(batches[0]) == 3


def test_order_batches_dependency_chain_owner_first_then_plumbing():
    """Owner (default level) deletes first; freed infra drains in dependency order.

    The post-run reset's 'dependent object' / 'instances still members' residuals
    are fixed by ordering: instance/LB/DB-instance first, then ENIs/volumes/param
    groups, then security groups, then VPC plumbing, then IAM roles last.
    """
    instance = Resource(type="AWS::EC2::Instance", identifier="i-1")
    eni = Resource(type="AWS::EC2::NetworkInterface", identifier="eni-1")
    param_group = Resource(type="AWS::RDS::DBParameterGroup", identifier="pg-1")
    subnet_group = Resource(type="AWS::RDS::DBSubnetGroup", identifier="sng-1")
    sg = Resource(type="AWS::EC2::SecurityGroup", identifier="sg-1")
    subnet = Resource(type="AWS::EC2::Subnet", identifier="subnet-1")
    role = Resource(type="AWS::IAM::Role", identifier="role-1")

    batches = Deleter(MagicMock(), resource_exists_fn=lambda r: True)._order_batches(
        [subnet, role, sg, eni, param_group, subnet_group, instance]
    )

    # One batch per distinct level, highest level (default owner) first.
    flat_order = [r for batch in batches for r in batch]
    assert flat_order.index(instance) < flat_order.index(eni)
    assert flat_order.index(instance) < flat_order.index(param_group)
    # DB subnet group outlives its cluster/instance but drains before the raw subnet.
    assert flat_order.index(instance) < flat_order.index(subnet_group)
    assert flat_order.index(subnet_group) < flat_order.index(subnet)
    assert flat_order.index(eni) < flat_order.index(sg)
    assert flat_order.index(sg) < flat_order.index(subnet)
    assert flat_order.index(subnet) < flat_order.index(role)
    # IAM role is always the final batch.
    assert role in batches[-1]


def test_db_subnet_groups_held_below_default_level():
    """DB subnet groups must not sit at the default level, or they race the cluster.

    Guards the fix directly at the table so a future edit that drops them (back to
    the default level) fails here, independent of any ordering-inference test.
    """
    for rt in (
        "AWS::RDS::DBSubnetGroup",
        "AWS::Neptune::DBSubnetGroup",
        "AWS::DocDB::DBSubnetGroup",
    ):
        assert _RESOURCE_TYPE_TO_LEVELS.get(rt, _MAX_RESOURCE_LEVEL) < _MAX_RESOURCE_LEVEL


def test_order_batches_target_group_before_plumbing_after_owner():
    """ELBv2 target group deletes after its load balancer but before SGs/subnets."""
    lb = Resource(type="AWS::ElasticLoadBalancingV2::LoadBalancer", identifier="lb-1")
    tg = Resource(type="AWS::ElasticLoadBalancingV2::TargetGroup", identifier="tg-1")
    sg = Resource(type="AWS::EC2::SecurityGroup", identifier="sg-1")
    batches = Deleter(MagicMock(), resource_exists_fn=lambda r: True)._order_batches([sg, tg, lb])
    flat_order = [r for batch in batches for r in batch]
    assert flat_order.index(lb) < flat_order.index(tg)
    assert flat_order.index(tg) < flat_order.index(sg)


def test_order_batches_vpc_deleted_after_plumbing_before_iam():
    """VPC deletes after its subnets/SGs/ENIs, else CCAPI fails 'has dependencies'.

    A VPC left at the default level would be attempted first (before its own
    plumbing) and leak; it must sit below subnets/route tables and above IAM roles.
    """
    eni = Resource(type="AWS::EC2::NetworkInterface", identifier="eni-1")
    sg = Resource(type="AWS::EC2::SecurityGroup", identifier="sg-1")
    subnet = Resource(type="AWS::EC2::Subnet", identifier="subnet-1")
    vpc = Resource(type="AWS::EC2::VPC", identifier="vpc-1")
    role = Resource(type="AWS::IAM::Role", identifier="role-1")

    batches = Deleter(MagicMock(), resource_exists_fn=lambda r: True)._order_batches(
        [vpc, role, subnet, sg, eni]
    )
    flat_order = [r for batch in batches for r in batch]
    assert flat_order.index(eni) < flat_order.index(sg)
    assert flat_order.index(sg) < flat_order.index(subnet)
    assert flat_order.index(subnet) < flat_order.index(vpc)
    assert flat_order.index(vpc) < flat_order.index(role)
    assert role in batches[-1]


def test_order_batches_ipam_pool_deleted_after_vpc():
    """An IPAM child pool deletes AFTER the VPC that allocates from it.

    The VPC frees its IPAM allocation only on delete, so the pool's CIDR can
    deprovision (and the pool delete) only once the VPC is gone. A pool left at
    the default level would be attempted first and its CIDR would never free.
    """
    vpc = Resource(type="AWS::EC2::VPC", identifier="vpc-1")
    pool = Resource(type="AWS::EC2::IPAMPool", identifier="ipam-pool-1")

    batches = Deleter(MagicMock(), resource_exists_fn=lambda r: True)._order_batches([pool, vpc])
    flat_order = [r for batch in batches for r in batch]
    assert flat_order.index(vpc) < flat_order.index(pool)


# -- _delete_resource_request --


def test_delete_resource_returns_request_token():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok-123"}}
    resource = Resource(type="AWS::S3::Bucket", identifier="b")
    assert (
        Deleter(client, resource_exists_fn=lambda r: True)._delete_resource_request(resource)
        == "tok-123"
    )


def test_delete_resource_raises_on_failure():
    client = MagicMock()
    client.delete_resource.side_effect = Exception("denied")
    resource = Resource(type="AWS::S3::Bucket", identifier="b")
    with pytest.raises(CloudControlResourceDeletionException):
        Deleter(client, resource_exists_fn=lambda r: True)._delete_resource_request(resource)


def test_delete_resource_treats_not_found_as_already_handled():
    """Deleting an already-gone resource is the desired end state — handled, not a failure.

    Pairs with _check_deletable now attempting deletion on an unverified existence check:
    if the resource turns out to be gone, that must not surface as a spurious failure.
    """
    client = MagicMock()
    client.delete_resource.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "DeleteResource"
    )
    resource = Resource(type="AWS::Kinesis::Stream", identifier="gone")
    assert (
        Deleter(client, resource_exists_fn=lambda r: True)._delete_resource_request(resource) == ""
    )


def test_delete_resource_treats_general_service_not_found_as_already_handled():
    """CCAPI wraps some 'not found' as GeneralServiceException — also treat as handled."""
    client = MagicMock()
    client.delete_resource.side_effect = ClientError(
        {"Error": {"Code": "GeneralServiceException", "Message": "Resource does not exist"}},
        "DeleteResource",
    )
    resource = Resource(type="AWS::EC2::Subnet", identifier="subnet-gone")
    assert (
        Deleter(client, resource_exists_fn=lambda r: True)._delete_resource_request(resource) == ""
    )


# -- _submit_deletions --


def test_submit_deletions_collects_tokens_and_failures():
    client = MagicMock()
    good = Resource(type="AWS::S3::Bucket", identifier="good")
    bad = Resource(type="AWS::S3::Bucket", identifier="bad")

    def side_effect(TypeName, Identifier):
        if Identifier == "bad":
            raise Exception("denied")
        return {"ProgressEvent": {"RequestToken": "tok-good"}}

    client.delete_resource.side_effect = side_effect
    result = Deleter(client, resource_exists_fn=lambda r: True)._submit_deletions([good, bad])
    assert "tok-good" in result.tokens
    assert bad in result.failures


def test_submit_deletions_classifies_concurrent_operation_as_already_handled():
    """A ConcurrentOperationException is not a failure and gets no token.

    It must land in already_handled (so the caller stops retrying it), NOT in
    failures — a concurrent stack delete is the expected path, not an error.
    """
    client = MagicMock()
    concurrent = Resource(type="AWS::S3::Bucket", identifier="concurrent")
    client.delete_resource.side_effect = ClientError(
        {"Error": {"Code": "ConcurrentOperationException"}}, "DeleteResource"
    )

    result = Deleter(client, resource_exists_fn=lambda r: True)._submit_deletions([concurrent])

    assert concurrent in result.already_handled
    assert concurrent not in result.failures
    assert not result.tokens


def test_submit_deletions_classifies_not_found_as_already_handled():
    """A not-found on delete lands in already_handled, not failures — it is already gone."""
    client = MagicMock()
    gone = Resource(type="AWS::Kinesis::Stream", identifier="gone")
    client.delete_resource.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DeleteResource"
    )

    result = Deleter(client, resource_exists_fn=lambda r: True)._submit_deletions([gone])

    assert gone in result.already_handled
    assert gone not in result.failures
    assert not result.tokens


def test_process_batch_pass_drops_concurrent_without_retry_or_failure():
    """An already-handled resource is removed from remaining and never marked failed."""
    client = MagicMock()
    concurrent = Resource(type="AWS::S3::Bucket", identifier="concurrent")
    client.delete_resource.side_effect = ClientError(
        {"Error": {"Code": "ConcurrentOperationException"}}, "DeleteResource"
    )

    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    remaining = {concurrent}
    pending_tokens: set[str] = set()
    token_to_resource: dict[str, Resource] = {}
    failed: dict = {}
    deleter._process_batch_pass(remaining, pending_tokens, token_to_resource, failed)

    assert not remaining  # dropped -> not resubmitted on the next pass
    assert concurrent not in failed  # not surfaced as a cleanup failure


# -- _check_token_status --


def test_check_token_status_success():
    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }
    pending, success, failed = {" tok"}, set(), {}
    Deleter(client, resource_exists_fn=lambda r: True)._check_token_status(
        " tok", pending, success, failed
    )
    assert " tok" in success
    assert " tok" not in pending


def test_check_token_status_failed():
    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "FAILED", "StatusMessage": "denied"}
    }
    pending, success, failed = {"tok"}, set(), {}
    Deleter(client, resource_exists_fn=lambda r: True)._check_token_status(
        "tok", pending, success, failed
    )
    assert "tok" in failed
    assert "tok" not in pending


def test_check_token_status_in_progress_no_change():
    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "IN_PROGRESS"}
    }
    pending, success, failed = {"tok"}, set(), {}
    Deleter(client, resource_exists_fn=lambda r: True)._check_token_status(
        "tok", pending, success, failed
    )
    assert "tok" in pending
    assert "tok" not in success


def test_check_token_status_exception_marks_failed():
    client = MagicMock()
    client.get_resource_request_status.side_effect = RuntimeError("timeout")
    pending, success, failed = {"tok"}, set(), {}
    Deleter(client, resource_exists_fn=lambda r: True)._check_token_status(
        "tok", pending, success, failed
    )
    assert "tok" in failed
    assert "tok" not in pending


# -- Deleter.delete_resources --


def test_delete_resources_returns_empty_when_nothing_deletable():
    client = MagicMock()
    resources = [Resource(type="Custom::MyThing", identifier="x")]
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    result = deleter.delete_resources(resources)
    assert result == {}


def test_delete_resources_full_flow():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }
    resources = [Resource(type="AWS::S3::Bucket", identifier="b1")]
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    result = deleter.delete_resources(resources, n_passes=1)
    assert result == {}


def test_delete_resources_submits_orphan_when_existence_check_errors():
    """End-to-end regression test.

    An orphan whose CCAPI existence check errors (HandlerInternalFailure) must be submitted
    for deletion, not filtered out. Before the fix, _filter_deletable dropped it and
    delete_resource was never called, leaking the resource.
    """
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }

    def existence_errors(_resource):
        raise ResourceExistenceCheckError("HandlerInternalFailureException ... InternalFailure")

    resource = Resource(type="AWS::Kinesis::Stream", identifier="bench-stream-193512")
    deleter = Deleter(client, resource_exists_fn=existence_errors)
    result = deleter.delete_resources([resource], n_passes=1)

    assert result == {}  # deleted, not skipped
    client.delete_resource.assert_called_once_with(
        TypeName="AWS::Kinesis::Stream", Identifier="bench-stream-193512"
    )


def test_delete_resources_skips_unsupported_type_end_to_end():
    """A CCAPI-unsupported type is still filtered out end-to-end (delete never attempted)."""
    client = MagicMock()

    def unsupported(_resource):
        raise ResourceExistenceUnsupportedError("CCAPI does not support AWS::Foo::Bar")

    resource = Resource(type="AWS::Foo::Bar", identifier="x")
    deleter = Deleter(client, resource_exists_fn=unsupported)
    result = deleter.delete_resources([resource], n_passes=1)

    assert result == {}
    client.delete_resource.assert_not_called()


# -- Deleter._filter_deletable --


def test_filter_deletable_filters():
    resources = [
        Resource(type="AWS::S3::Bucket", identifier="good"),
        Resource(type="Custom::MyThing", identifier="bad"),
    ]
    deleter = Deleter(MagicMock(), resource_exists_fn=lambda r: True)
    result = deleter._filter_deletable(resources)
    assert len(result) == 1
    assert result[0].identifier == "good"


# -- Deleter._process_batch_pass --


def test_process_batch_pass_success():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    remaining = {r}
    pending_tokens: set[str] = set()
    token_to_resource: dict[str, Resource] = {}
    failed = {}
    deleter._process_batch_pass(remaining, pending_tokens, token_to_resource, failed)
    assert not remaining
    assert not failed
    assert "tok" not in pending_tokens


def test_process_batch_pass_failure():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "FAILED", "StatusMessage": "denied"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    remaining = {r}
    pending_tokens: set[str] = set()
    token_to_resource: dict[str, Resource] = {}
    failed = {}
    deleter._process_batch_pass(remaining, pending_tokens, token_to_resource, failed)
    assert r in remaining
    assert r in failed


def test_process_batch_pass_submit_failure():
    client = MagicMock()
    client.delete_resource.side_effect = Exception("denied")
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    remaining = {r}
    pending_tokens: set[str] = set()
    token_to_resource: dict[str, Resource] = {}
    failed = {}
    deleter._process_batch_pass(remaining, pending_tokens, token_to_resource, failed)
    assert r in remaining
    assert r in failed


def test_process_batch_pass_pending():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "IN_PROGRESS"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    remaining = {r}
    pending_tokens: set[str] = set()
    token_to_resource: dict[str, Resource] = {}
    failed = {}
    with (
        patch("aws_bench.resource_management.ccapi.deleter._TIMEOUT", 0.01),
        patch("aws_bench.resource_management.ccapi.deleter._POLLING_INTERVAL", 0.001),
    ):
        deleter._process_batch_pass(remaining, pending_tokens, token_to_resource, failed)
    assert not remaining
    assert "tok" in pending_tokens
    assert not failed


# -- Deleter._execute_batches --


def test_execute_batches_success():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    result = deleter._execute_batches([[r]], n_passes=1)
    assert result == {}


def test_execute_batches_failure():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "FAILED", "StatusMessage": "denied"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    result = deleter._execute_batches([[r]], n_passes=1)
    assert r in result


def test_execute_batches_still_pending():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "IN_PROGRESS"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    with (
        patch("aws_bench.resource_management.ccapi.deleter._TIMEOUT", 0.01),
        patch("aws_bench.resource_management.ccapi.deleter._POLLING_INTERVAL", 0.001),
    ):
        result = deleter._execute_batches([[r]], n_passes=1)
    assert r in result


def test_execute_batches_empty_remaining_breaks():
    client = MagicMock()
    client.delete_resource.return_value = {"ProgressEvent": {"RequestToken": "tok"}}
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }
    r = Resource(type="AWS::S3::Bucket", identifier="b1")
    deleter = Deleter(client, resource_exists_fn=lambda r: True)
    result = deleter._execute_batches([[r]], n_passes=3)
    assert result == {}


# -- _poll_deletions --


def test_poll_deletions_success():
    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "SUCCESS"}
    }
    result = Deleter(client, resource_exists_fn=lambda r: True)._poll_deletions(["tok"])
    assert "tok" in result.succeeded
    assert not result.pending
    assert not result.failed


def test_poll_deletions_timeout():
    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "IN_PROGRESS"}
    }
    with (
        patch("aws_bench.resource_management.ccapi.deleter._TIMEOUT", 0.01),
        patch("aws_bench.resource_management.ccapi.deleter._POLLING_INTERVAL", 0.001),
    ):
        result = Deleter(client, resource_exists_fn=lambda r: True)._poll_deletions(["tok"])
    assert "tok" in result.pending


def test_poll_deletions_failed():
    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "FAILED", "StatusMessage": "denied"}
    }
    result = Deleter(client, resource_exists_fn=lambda r: True)._poll_deletions(["tok"])
    assert "tok" in result.failed


def test_poll_deletions_raises_on_shutdown():
    """A shutdown mid-poll unwinds the wait instead of running on."""
    from aws_bench.exceptions import OperationCancelled
    from aws_bench.utils import concurrent

    client = MagicMock()
    client.get_resource_request_status.return_value = {
        "ProgressEvent": {"OperationStatus": "IN_PROGRESS"}
    }
    concurrent.reset_shutdown()
    concurrent.request_shutdown()
    try:
        with pytest.raises(OperationCancelled):
            Deleter(client, resource_exists_fn=lambda r: True)._poll_deletions(["tok"])
    finally:
        concurrent.reset_shutdown()
