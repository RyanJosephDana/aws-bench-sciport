"""Tests for fastscan CFN-type ↔ service-endpoint resolution and resource-noun matching."""

from aws_bench.resource_management.fastscan.type_map import (
    cfn_type_resource_nouns,
    cfn_type_service_endpoint,
    collides_with_sibling_type,
    lister_op_noun,
)

SERVICES = {"s3", "ec2", "kafka", "mq", "cognito-idp", "lexv2-models", "amp", "network-firewall"}


def test_endpoint_exact_token():
    assert cfn_type_service_endpoint("AWS::S3::Bucket", SERVICES) == "s3"
    assert cfn_type_service_endpoint("AWS::EC2::VPC", SERVICES) == "ec2"


def test_endpoint_aliases():
    assert cfn_type_service_endpoint("AWS::MSK::Cluster", SERVICES) == "kafka"
    assert cfn_type_service_endpoint("AWS::AmazonMQ::Broker", SERVICES) == "mq"
    assert cfn_type_service_endpoint("AWS::Cognito::UserPool", SERVICES) == "cognito-idp"
    assert cfn_type_service_endpoint("AWS::Lex::Bot", SERVICES) == "lexv2-models"
    assert cfn_type_service_endpoint("AWS::APS::Workspace", SERVICES) == "amp"


def test_endpoint_hyphen_insensitive():
    assert (
        cfn_type_service_endpoint("AWS::NetworkFirewall::Firewall", SERVICES) == "network-firewall"
    )


def test_sibling_colliding_types_are_unscannable_by_noun():
    # These share a lister + resource noun with a sibling type (EC2::Volume / ECR::Repository)
    # and cannot be told apart by identifier, so fast-scan must not attribute them at all —
    # otherwise the sibling's resources are double-typed onto them.
    assert collides_with_sibling_type("AWS::WorkspacesInstances::Volume")
    assert collides_with_sibling_type("AWS::ECR::PublicRepository")
    # A normal type is scannable.
    assert not collides_with_sibling_type("AWS::EC2::Volume")
    assert not collides_with_sibling_type("AWS::ECR::Repository")


def test_endpoint_unmappable():
    assert cfn_type_service_endpoint("Custom::Thing", SERVICES) is None
    assert cfn_type_service_endpoint("AWS::OpsWorks::Stack", SERVICES) is None  # no lister service


def test_resource_tokens_camel_and_plural():
    toks = cfn_type_resource_nouns("AWS::EC2::SecurityGroup")
    assert "securitygroup" in toks and "securitygroups" in toks
    # last-word match enables ScalingPolicy <-> policies
    assert "policies" in cfn_type_resource_nouns("AWS::AutoScaling::ScalingPolicy")


def test_op_noun():
    assert lister_op_noun("DescribeSecurityGroups") == "securitygroups"
    assert lister_op_noun("ListFunctions") == "functions"
