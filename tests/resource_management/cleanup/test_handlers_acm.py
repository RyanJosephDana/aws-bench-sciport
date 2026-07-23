"""Tests for the ACM certificate cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.acm import _delete_acm_certificate
from aws_bench.resource_management.cleanup.models import HandlerStatus


def _resource() -> Resource:
    return Resource(
        type="AWS::CertificateManager::Certificate",
        identifier="arn:aws:acm:us-east-1:111122223333:certificate/abc-123",
    )


def _session_with(acm: MagicMock) -> MagicMock:
    session = MagicMock()
    session.client.side_effect = lambda service, **_kw: acm
    return session


def test_delete_acm_certificate_deletes_via_acm_api():
    """CCAPI cannot delete ACM certs, so the handler calls acm.delete_certificate."""
    acm = MagicMock()
    result = _delete_acm_certificate(_resource(), _session_with(acm))
    acm.delete_certificate.assert_called_once_with(
        CertificateArn="arn:aws:acm:us-east-1:111122223333:certificate/abc-123"
    )
    assert result.status == HandlerStatus.SUCCESS


def test_delete_acm_certificate_already_gone_is_success():
    """A ResourceNotFoundException means the cert is already gone -> SUCCESS."""
    acm = MagicMock()
    acm.delete_certificate.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "DeleteCertificate"
    )
    result = _delete_acm_certificate(_resource(), _session_with(acm))
    assert result.status == HandlerStatus.SUCCESS


def test_delete_acm_certificate_in_use_is_failure():
    """A cert still referenced by a live resource -> FAILED (correctly reported)."""
    acm = MagicMock()
    acm.delete_certificate.side_effect = ClientError(
        {"Error": {"Code": "ResourceInUseException", "Message": "in use"}}, "DeleteCertificate"
    )
    result = _delete_acm_certificate(_resource(), _session_with(acm))
    assert result.status == HandlerStatus.FAILED
