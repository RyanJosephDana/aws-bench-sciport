"""Tests for AwsBenchRegistry.get_dataset_spec partial-version resolution."""

import pytest

from aws_bench.dataset.exceptions import RegistryValidationError
from aws_bench.dataset.registry import AwsBenchDatasetSpec, AwsBenchRegistry


def _registry_with_versions(name: str, versions: list[str]) -> AwsBenchRegistry:
    return AwsBenchRegistry(
        datasets=[
            AwsBenchDatasetSpec(
                name=name,
                version=v,
                description="",
                tasks=[],
                scenarios=[],
            )
            for v in versions
        ],
    )


def test_bare_name_returns_highest():
    reg = _registry_with_versions("ds", ["1.0.0", "1.0.3", "1.1.0"])
    spec = reg.get_dataset_spec("ds")
    assert spec.version == "1.1.0"


def test_exact_version_returns_match():
    reg = _registry_with_versions("ds", ["1.0.0", "1.0.3", "1.1.0"])
    spec = reg.get_dataset_spec("ds", "1.0.3")
    assert spec.version == "1.0.3"


def test_partial_version_returns_highest_patch_in_band():
    reg = _registry_with_versions("ds", ["1.0.0", "1.0.3", "1.1.0"])
    spec = reg.get_dataset_spec("ds", "1.0")
    assert spec.version == "1.0.3"


def test_partial_version_no_match_in_band_raises():
    reg = _registry_with_versions("ds", ["1.1.0", "2.0.0"])
    with pytest.raises(RegistryValidationError, match="no version matching"):
        reg.get_dataset_spec("ds", "1.0")


def test_partial_version_excludes_higher_minor():
    """1.0 must not match 1.10.0 (prefix matching is '1.0.', not '1.0')."""
    reg = _registry_with_versions("ds", ["1.0.0", "1.10.0"])
    spec = reg.get_dataset_spec("ds", "1.0")
    assert spec.version == "1.0.0"  # not 1.10.0


def test_unknown_dataset_raises():
    reg = _registry_with_versions("ds", ["1.0.0"])
    with pytest.raises(RegistryValidationError, match="not found in registry"):
        reg.get_dataset_spec("other")


def test_exact_version_miss_raises():
    reg = _registry_with_versions("ds", ["1.0.0"])
    with pytest.raises(RegistryValidationError, match="not found"):
        reg.get_dataset_spec("ds", "2.0.0")
