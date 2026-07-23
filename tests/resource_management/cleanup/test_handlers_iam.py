"""Tests for the IAM role cleanup (prepare) handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.iam import _prepare
from aws_bench.resource_management.cleanup.models import HandlerStatus


def _session_with_iam() -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    iam = MagicMock()
    # NoSuchEntityException must be a real exception class for except clauses.
    iam.exceptions.NoSuchEntityException = type("NoSuchEntityException", (Exception,), {})
    session.client.return_value = iam
    return session, iam


def _role(name: str) -> Resource:
    return Resource(type="AWS::IAM::Role", identifier=name)


def test_prepare_skips_service_linked_role():
    session, iam = _session_with_iam()
    result = _prepare(_role("AWSServiceRoleForAutoScaling"), session)
    assert result.status == HandlerStatus.SKIPPED
    session.client.assert_not_called()


def test_prepare_skips_protected_role():
    session, iam = _session_with_iam()
    result = _prepare(_role("OrganizationAccountAccessRole"), session)
    assert result.status == HandlerStatus.SKIPPED


def test_prepare_detaches_and_removes_from_instance_profiles():
    session, iam = _session_with_iam()

    def paginator(op: str) -> MagicMock:
        pages = {
            "list_attached_role_policies": [
                {"AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/Foo"}]}
            ],
            "list_role_policies": [{"PolicyNames": ["inline1"]}],
            "list_instance_profiles_for_role": [
                {"InstanceProfiles": [{"InstanceProfileName": "profile-1"}]}
            ],
        }
        p = MagicMock()
        p.paginate.return_value = pages[op]
        return p

    iam.get_paginator.side_effect = paginator
    result = _prepare(_role("EC2ImageBuilderRole"), session)

    iam.detach_role_policy.assert_called_once_with(
        RoleName="EC2ImageBuilderRole", PolicyArn="arn:aws:iam::aws:policy/Foo"
    )
    iam.delete_role_policy.assert_called_once_with(
        RoleName="EC2ImageBuilderRole", PolicyName="inline1"
    )
    iam.remove_role_from_instance_profile.assert_called_once_with(
        RoleName="EC2ImageBuilderRole", InstanceProfileName="profile-1"
    )
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_skips_when_role_not_found():
    session, iam = _session_with_iam()
    iam.get_paginator.side_effect = iam.exceptions.NoSuchEntityException()
    result = _prepare(_role("gone-role"), session)
    assert result.status == HandlerStatus.SKIPPED


def test_prepare_fails_on_other_error():
    session, iam = _session_with_iam()
    iam.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "ListAttachedRolePolicies"
    )
    result = _prepare(_role("EC2SSMRole"), session)
    assert result.status == HandlerStatus.FAILED
