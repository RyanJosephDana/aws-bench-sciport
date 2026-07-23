"""MediaLive cleanup handlers.

CloudControl does not expose a delete for ``AWS::MediaLive::InputSecurityGroup`` (the type is
NON_PROVISIONABLE), so a scanned security group is otherwise undeletable. The fast-scan lister emits
the CCAPI primary identifier ``Id``, which is exactly the arg ``delete_input_security_group`` wants.
"""

from __future__ import annotations

import boto3

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import service_delete
from aws_bench.resource_management.cleanup.models import HandlerResult


@resource_handler("AWS::MediaLive::InputSecurityGroup", role="delete")
def _delete_input_security_group(resource: Resource, session: boto3.Session) -> HandlerResult:
    return service_delete(
        resource,
        session,
        client_name="medialive",
        op_name="delete_input_security_group",
        id_param="InputSecurityGroupId",
        not_found_codes=("NotFoundException",),
        already_gone_message="Input security group already gone",
        log_label="MediaLive input security group",
    )
