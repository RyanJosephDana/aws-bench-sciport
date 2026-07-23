"""Tests for the simplified exception hierarchy."""

from aws_bench.exceptions import AWSBenchError
from aws_bench.resource_management.exceptions import (
    ConfigurationError,
    DeploymentError,
)

# -- DeploymentError inheritance --


def test_deployment_error_is_subclass_of_aws_bench_error():
    assert issubclass(DeploymentError, AWSBenchError)


def test_deployment_error_is_subclass_of_exception():
    assert issubclass(DeploymentError, Exception)


def test_deployment_error_instance_caught_by_aws_bench_error():
    with __import__("pytest").raises(AWSBenchError):
        raise DeploymentError("deploy failed")


def test_deployment_error_message_preserved():
    exc = DeploymentError("something broke")
    assert str(exc) == "something broke"


# -- ConfigurationError inheritance --


def test_configuration_error_is_subclass_of_deployment_error():
    assert issubclass(ConfigurationError, DeploymentError)


def test_configuration_error_is_subclass_of_aws_bench_error():
    assert issubclass(ConfigurationError, AWSBenchError)


def test_configuration_error_instance_caught_by_deployment_error():
    with __import__("pytest").raises(DeploymentError):
        raise ConfigurationError("bad config")


def test_configuration_error_instance_caught_by_aws_bench_error():
    with __import__("pytest").raises(AWSBenchError):
        raise ConfigurationError("bad config")


def test_configuration_error_message_preserved():
    exc = ConfigurationError("missing cdk dir")
    assert str(exc) == "missing cdk dir"


# -- Module exposes exactly two exceptions --


def test_module_has_deployment_error():
    import aws_bench.resource_management.exceptions as mod

    assert hasattr(mod, "DeploymentError")


def test_module_has_configuration_error():
    import aws_bench.resource_management.exceptions as mod

    assert hasattr(mod, "ConfigurationError")


def test_cleanup_error_hierarchy():
    from aws_bench.exceptions import AWSBenchError
    from aws_bench.resource_management.exceptions import CleanupError

    assert issubclass(CleanupError, AWSBenchError)
