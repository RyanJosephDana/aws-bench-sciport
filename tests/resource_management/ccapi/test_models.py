"""Tests for aws_bench.resource_management.ccapi.models."""

from __future__ import annotations

import pytest

from aws_bench.resource_management.ccapi.models import (
    DeletionFailureEvent,
    Resource,
)

# -- Resource --


def test_resource_creation():
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    assert resource.type == "AWS::S3::Bucket"
    assert resource.identifier == "my-bucket"


def test_resource_frozen():
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    with pytest.raises(AttributeError):
        setattr(resource, "type", "AWS::EC2::Instance")


def test_resource_equality():
    resource_a = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    resource_b = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    assert resource_a == resource_b


def test_resource_hashable():
    resource = Resource(type="AWS::S3::Bucket", identifier="my-bucket")
    assert {resource: "value"}[resource] == "value"


# -- DeletionFailureEvent --


def test_deletion_failure_event_creation():
    event = DeletionFailureEvent(status_message="Access denied")
    assert event.status_message == "Access denied"


def test_deletion_failure_event_from_ccapi_event():
    event = DeletionFailureEvent.from_ccapi_event({"StatusMessage": "Resource not found"})
    assert event.status_message == "Resource not found"


def test_deletion_failure_event_from_ccapi_event_missing_message():
    event = DeletionFailureEvent.from_ccapi_event({})
    assert event.status_message == "Unknown error"
