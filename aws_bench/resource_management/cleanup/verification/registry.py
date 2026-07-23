"""Registry for custom resource verifier functions.

To add a new service-API verifier, use the ``@verifies`` decorator::

    @verifies("AWS::MyService::Thing")
    def _check_my_thing(session: boto3.Session, physical_id: str) -> bool:
        return bool(session.client("myservice").describe_thing(Id=physical_id))

To mark a sub-resource type as unchecked (can't be verified independently),
add it to ``UNCHECKED_SUBRESOURCE_TYPES``.
"""

from __future__ import annotations

from typing import Callable

import boto3

VerifierFn = Callable[[boto3.Session, str], bool]

_VERIFIER_REGISTRY: dict[str, VerifierFn] = {}

# Account-level singletons that always "exist" — skip verification entirely.
SKIP_TYPES = {"AWS::ApiGateway::Account"}

# Sub-resource types that can't be checked without parent context.
# Marked as UNCHECKED_SUBRESOURCE during verification since they may be orphans
# if parent deletion failed.
UNCHECKED_SUBRESOURCE_TYPES = frozenset(
    {
        "AWS::Lambda::Permission",
        "AWS::SQS::QueuePolicy",
        "AWS::SNS::TopicPolicy",
        "AWS::IAM::AccessKey",
        "AWS::Glue::Table",
        "AWS::Route53::RecordSet",
        "AWS::AppConfig::HostedConfigurationVersion",
        "AWS::AppConfig::ConfigurationProfile",
        "AWS::AppConfig::Deployment",
        "AWS::AppConfig::Environment",
        "AWS::ApiGateway::Resource",
        "AWS::ApiGateway::Deployment",
        "AWS::ApiGateway::Stage",
        "AWS::EC2::NetworkAclEntry",
        "AWS::Logs::LogStream",
        "AWS::CloudFormation::CustomResource",
    }
)


def verifies(resource_type: str):
    """Register a service-API existence check for a resource type."""

    def decorator(func: VerifierFn) -> VerifierFn:
        _VERIFIER_REGISTRY[resource_type] = func
        return func

    return decorator


def get_verifier(resource_type: str) -> VerifierFn | None:
    """Get the registered verifier function for a resource type."""
    return _VERIFIER_REGISTRY.get(resource_type)
