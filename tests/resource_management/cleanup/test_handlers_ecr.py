"""Tests for ECR repository cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import (
    CUSTOM_DELETION_REGISTRY,
    PREPARE_REGISTRY,
)
from aws_bench.resource_management.cleanup.handlers.ecr import (
    _delete as _delete_ecr,
)
from aws_bench.resource_management.cleanup.handlers.ecr import (
    _prepare as _prepare_ecr,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus


def test_prepare_ecr_private():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [
        {"imageIds": [{"imageDigest": "sha:1"}]}
    ]
    r = Resource(type="AWS::ECR::Repository", identifier="repo1")
    _prepare_ecr(r, session, service="ecr")
    client.batch_delete_image.assert_called_once()


def test_prepare_ecr_public():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [
        {"imageDetails": [{"imageDigest": "sha:1"}]}
    ]
    r = Resource(type="AWS::ECR::PublicRepository", identifier="repo1")
    _prepare_ecr(r, session, service="ecr-public", region="us-east-1")
    client.batch_delete_image.assert_called_once()


def test_prepare_ecr_noop_empty():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [{"imageIds": []}]
    r = Resource(type="AWS::ECR::Repository", identifier="repo1")
    _prepare_ecr(r, session, service="ecr")
    client.batch_delete_image.assert_not_called()


def _repo_not_found_client_error():
    """A botocore ClientError with the ECR repository-not-found code."""
    return ClientError(
        {"Error": {"Code": "RepositoryNotFoundException", "Message": "gone"}},
        "ListImages",
    )


def test_prepare_ecr_repo_already_gone_is_success():
    """A repo that no longer exists is a SUCCESS (already-gone), not a failure.

    Otherwise a since-deleted bootstrap/task repo surfaces as a false DELETE_FAILED
    (RC10b): the scan lags the actual state and the prepare pass raises on a repo
    that is genuinely already cleaned up.
    """
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.side_effect = _repo_not_found_client_error()
    r = Resource(type="AWS::ECR::Repository", identifier="gone-repo")
    result = _prepare_ecr(r, session, service="ecr")
    assert result.status is HandlerStatus.SUCCESS


def test_delete_ecr_repo_already_gone_is_success():
    """delete_repository on a since-deleted repo is a SUCCESS (already-gone)."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_repository.side_effect = ClientError(
        {"Error": {"Code": "RepositoryNotFoundException", "Message": "gone"}},
        "DeleteRepository",
    )
    result = _delete_ecr(
        Resource(type="AWS::ECR::Repository", identifier="gone-repo"), session, service="ecr"
    )
    assert result.status is HandlerStatus.SUCCESS


def test_delete_ecr_private():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    _delete_ecr(Resource(type="AWS::ECR::Repository", identifier="r"), session, service="ecr")
    client.delete_repository.assert_called_once()


def test_delete_ecr_public_with_region():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    _delete_ecr(
        Resource(type="AWS::ECR::PublicRepository", identifier="r"),
        session,
        service="ecr-public",
        region="us-east-1",
    )
    session.client.assert_called_with("ecr-public", region_name="us-east-1")


def test_prepare_private_via_registry():
    """Test registered prepare handler for private ECR."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [{"imageIds": []}]

    handler = PREPARE_REGISTRY.get("AWS::ECR::Repository")
    assert handler is not None
    handler(Resource(type="AWS::ECR::Repository", identifier="repo1"), session)


def test_delete_private_via_registry():
    """Test registered delete handler for private ECR."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    handler = CUSTOM_DELETION_REGISTRY.get("AWS::ECR::Repository")
    assert handler is not None
    handler(Resource(type="AWS::ECR::Repository", identifier="repo1"), session)


def test_prepare_public_via_registry():
    """Test registered prepare handler for public ECR."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value.paginate.return_value = [{"imageDetails": []}]

    handler = PREPARE_REGISTRY.get("AWS::ECR::PublicRepository")
    assert handler is not None
    handler(Resource(type="AWS::ECR::PublicRepository", identifier="repo1"), session)


def test_delete_public_via_registry():
    """Test registered delete handler for public ECR."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    handler = CUSTOM_DELETION_REGISTRY.get("AWS::ECR::PublicRepository")
    assert handler is not None
    handler(Resource(type="AWS::ECR::PublicRepository", identifier="repo1"), session)


def test_delete_pull_through_cache_rule_via_registry():
    """The pull-through cache rule handler deletes by the emitted EcrRepositoryPrefix."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    handler = CUSTOM_DELETION_REGISTRY.get("AWS::ECR::PullThroughCacheRule")
    assert handler is not None
    result = handler(
        Resource(type="AWS::ECR::PullThroughCacheRule", identifier="ecr-public"), session
    )
    assert result.status is HandlerStatus.SUCCESS
    client.delete_pull_through_cache_rule.assert_called_once_with(ecrRepositoryPrefix="ecr-public")


def test_delete_registry_policy_via_registry():
    """The registry-policy handler deletes the account singleton (delete takes no argument)."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client

    handler = CUSTOM_DELETION_REGISTRY.get("AWS::ECR::RegistryPolicy")
    assert handler is not None
    result = handler(Resource(type="AWS::ECR::RegistryPolicy", identifier="123456789012"), session)
    assert result.status is HandlerStatus.SUCCESS
    client.delete_registry_policy.assert_called_once_with()
