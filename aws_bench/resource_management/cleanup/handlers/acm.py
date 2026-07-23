"""ACM certificate cleanup handler.

Cloud Control (CCAPI) does not support ``AWS::CertificateManager::Certificate``
(``UnsupportedActionException``), so a certificate left behind once its consumers
(an ALB listener, CloudFront distribution, API Gateway domain, ...) are gone is
never swept and surfaces as an orphan. This custom delete handler removes it via
the ACM API. A certificate already gone maps to SUCCESS; one still referenced by a
live resource raises ``ResourceInUseException`` -> FAILED (correctly reported so it
is retried/surfaced rather than silently dropped).
"""

from __future__ import annotations

import boto3

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import service_delete
from aws_bench.resource_management.cleanup.models import HandlerResult

# delete_certificate faults meaning the certificate is already gone (treat as done).
_ACM_NOT_FOUND_CODES = ("ResourceNotFoundException",)


@resource_handler("AWS::CertificateManager::Certificate", role="delete")
def _delete_acm_certificate(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete an ACM certificate via the ACM API (CCAPI does not support ACM)."""
    return service_delete(
        resource,
        session,
        client_name="acm",
        op_name="delete_certificate",
        id_param="CertificateArn",
        not_found_codes=_ACM_NOT_FOUND_CODES,
        already_gone_message="ACM certificate already gone",
        log_label="ACM certificate",
    )
