"""Handler registry for resource cleanup.

Four registries, all populated via decorators:

1. ``PREPARE_REGISTRY`` — make a resource deletable (e.g. empty a bucket).
2. ``CUSTOM_DELETION_REGISTRY`` — delete via service API instead of CCAPI.
3. ``PRE_DELETE_HOOKS`` — discover extra resources before CCAPI deletion.
4. ``FAILED_RESOURCE_HANDLERS`` — handle resources stuck in DELETE_FAILED.
"""

from __future__ import annotations

from typing import Callable

import boto3

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus, StackResource

HandlerFn = Callable[[Resource, boto3.Session], "HandlerResult"]
PreDeleteHookFn = Callable[[list[StackResource], boto3.Session], list[Resource]]
FailedResourceHandlerFn = Callable[
    [list[StackResource], list[StackResource], boto3.Session, "str | None"], None
]

# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

PREPARE_REGISTRY: dict[str, HandlerFn] = {}
CUSTOM_DELETION_REGISTRY: dict[str, HandlerFn] = {}
PRE_DELETE_HOOKS: dict[str, PreDeleteHookFn] = {}

# Priority-ordered list of (pattern, handler) tuples for failed resource handlers.
# Lower priority values execute first. Handlers use PREFIX matching via startswith(),
# so multiple handlers can match the same resource (intentional fall-through).
FAILED_RESOURCE_HANDLERS: list[tuple[int, str, FailedResourceHandlerFn]] = []


def resource_handler(resource_type: str, *, role: str = "prepare"):
    """Decorator to register a prepare or delete handler for a resource type.

    Usage::

        @resource_handler("AWS::S3::Bucket", role="prepare")
        def _prepare(resource, session): ...


        @resource_handler("AWS::S3::Bucket", role="delete")
        def _delete(resource, session): ...

    Note: The delete handler automatically chains the prepare handler (if registered)
    at call time, so decorator order doesn't matter.
    """

    def decorator(func: HandlerFn) -> HandlerFn:
        if role == "prepare":
            PREPARE_REGISTRY[resource_type] = func
        elif role == "delete":

            def _full_delete(resource: Resource, session: boto3.Session) -> HandlerResult:
                # Look up prepare handler at call time (not decoration time)
                prepare_fn = PREPARE_REGISTRY.get(resource_type)
                if prepare_fn is not None:
                    result = prepare_fn(resource, session)
                    if result.status in (HandlerStatus.FAILED, HandlerStatus.SKIPPED):
                        return result
                return func(resource, session)

            CUSTOM_DELETION_REGISTRY[resource_type] = _full_delete
        else:
            raise ValueError(f"Unknown role {role!r}; expected 'prepare' or 'delete'")
        return func

    return decorator


def pre_delete_hook(resource_type: str):
    """Register a pre-delete discovery hook for a resource type."""

    def decorator(func: PreDeleteHookFn) -> PreDeleteHookFn:
        PRE_DELETE_HOOKS[resource_type] = func
        return func

    return decorator


def failed_resource_handler(type_prefix: str, *, priority: int = 50):
    """Register a handler for resources stuck in DELETE_FAILED.

    Handlers use PREFIX matching (startswith), allowing multiple handlers to match
    the same resource. All matching handlers execute in priority order (lower first).

    Args:
        type_prefix: Resource type prefix to match (e.g., "Custom::", "Custom::AWS")
        priority: Execution order (default 50). Lower values execute first.
                  Suggested ranges: 0-25 (generic), 50 (normal), 75-100 (specific)

    Example:
        @failed_resource_handler("Custom::", priority=10)  # Runs first
        def handle_all_custom(...): ...

        @failed_resource_handler("Custom::AWS", priority=50)  # Runs second
        def handle_aws_custom(...): ...
    """

    def decorator(func: FailedResourceHandlerFn) -> FailedResourceHandlerFn:
        FAILED_RESOURCE_HANDLERS.append((priority, type_prefix, func))
        # Keep sorted by priority for efficient lookup
        FAILED_RESOURCE_HANDLERS.sort(key=lambda x: x[0])
        return func

    return decorator


def has_registered_handlers() -> bool:
    """Return True if any cleanup handlers or hooks are registered."""
    return bool(
        PREPARE_REGISTRY
        or CUSTOM_DELETION_REGISTRY
        or PRE_DELETE_HOOKS
        or FAILED_RESOURCE_HANDLERS  # Now a list, bool still works
    )
