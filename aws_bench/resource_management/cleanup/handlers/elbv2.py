"""Elastic Load Balancing v2 cleanup handler.

A task-created ``AWS::ElasticLoadBalancingV2::LoadBalancer`` is surfaced by the
post-run reset as a new resource but raw CCAPI cannot clear it: the load balancer
owns service-managed ENIs that block its own deletion until it is torn down via
the elbv2 API.

Only the load balancer needs a custom (elbv2-API) delete, which also removes its
listeners and releases its ENIs. The paired ``TargetGroup`` has its own elbv2-API
delete handler below (a target group still referenced by a listener/rule fails to
delete via CCAPI). ELB deletion is asynchronous, so the load-balancer handler does
not return until it has WAITED for the load balancer to reach terminal deletion
(``load_balancers_deleted`` waiter) — by then the listeners that referenced the
target group are gone, so the target-group delete succeeds on that (or a later)
pass. The ordering holds because the handler blocks on terminal deletion, not
because of phase timing.
"""

from __future__ import annotations

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError, WaiterError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import service_delete
from aws_bench.resource_management.cleanup.models import HandlerResult

_NOT_FOUND_CODES = ("LoadBalancerNotFound",)
_TARGET_GROUP_NOT_FOUND_CODES = ("TargetGroupNotFound",)

# Bounded waiter config so terminal-deletion polling cannot hang forever:
# 15 attempts x 10s = up to 150s, ample for ENI/listener teardown.
_WAITER_DELAY_SEC = 10
_WAITER_MAX_ATTEMPTS = 15


def _wait_for_terminal_deletion(client: BaseClient, load_balancer_arn: str) -> None:
    """Block until the load balancer reaches terminal deletion.

    ELB deletion (and its listener + service-managed ENI release) is asynchronous;
    the CCAPI fallback deletes the paired target group immediately after this
    handler, so we must not return until the listeners are gone. If the waiter
    times out but the load balancer is actually gone, treat it as done; otherwise
    let the ``WaiterError`` propagate (a still-in-flight load balancer is a real
    problem the operator should see, mapped to FAILED by ``service_delete``).
    """
    waiter = client.get_waiter("load_balancers_deleted")
    try:
        waiter.wait(
            LoadBalancerArns=[load_balancer_arn],
            WaiterConfig={"Delay": _WAITER_DELAY_SEC, "MaxAttempts": _WAITER_MAX_ATTEMPTS},
        )
    except WaiterError:
        try:
            client.describe_load_balancers(LoadBalancerArns=[load_balancer_arn])
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES:
                return  # Actually gone despite the waiter timeout.
            raise
        raise  # Still present after the bounded wait — surface it.


@resource_handler("AWS::ElasticLoadBalancingV2::LoadBalancer", role="delete")
def _delete_load_balancer(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the load balancer via the elbv2 API, then wait for terminal deletion."""
    return service_delete(
        resource,
        session,
        client_name="elbv2",
        op_name="delete_load_balancer",
        id_param="LoadBalancerArn",
        not_found_codes=_NOT_FOUND_CODES,
        already_gone_message="Load balancer already gone",
        log_label="load balancer",
        post_delete=lambda client: _wait_for_terminal_deletion(client, resource.identifier),
    )


@resource_handler("AWS::ElasticLoadBalancingV2::TargetGroup", role="delete")
def _delete_target_group(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete an ELBv2 target group via the elbv2 API.

    A target group whose load balancer/listeners are gone can be deleted directly.
    Historically this was left to the CCAPI deleter, but a target group still
    referenced by a listener or rule fails to delete (observed: the
    ``opensearch`` stack ending DELETE_FAILED on ``TargetGroup…`` because the
    listener referencing it had not yet been torn down). Giving it a first-class
    elbv2-API delete handler with the shared error mapping means: an already-gone
    target group -> SUCCESS; one still ``ResourceInUse`` -> FAILED, so it is
    surfaced and retried on a later pass (by then the load-balancer handler has
    waited for terminal deletion, removing the referencing listener).
    """
    return service_delete(
        resource,
        session,
        client_name="elbv2",
        op_name="delete_target_group",
        id_param="TargetGroupArn",
        not_found_codes=_TARGET_GROUP_NOT_FOUND_CODES,
        already_gone_message="Target group already gone",
        log_label="target group",
    )
