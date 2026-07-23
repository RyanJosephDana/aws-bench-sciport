"""Registry-mode tests for AwsBenchDatasetConfig.get_scenario_configs."""

from pathlib import Path
from unittest.mock import patch

import pytest

from aws_bench.dataset.config import AwsBenchDatasetConfig
from aws_bench.dataset.registry import (
    AwsBenchDatasetSpec,
    AwsBenchRegistry,
    RegistryScenarioId,
)


@pytest.fixture
def fake_registry():
    return AwsBenchRegistry(
        datasets=[
            AwsBenchDatasetSpec(
                name="aws-bench-test",
                version="1.0.0",
                description="",
                tasks=[],
                scenarios=[
                    RegistryScenarioId(
                        name="ec2-small",
                        git_url="https://github.com/x/y",
                        git_commit_id="abc",
                        path=Path("scenarios/ec2-small"),
                    ),
                    RegistryScenarioId(
                        name="rds-mysql",
                        git_url="https://github.com/x/y",
                        git_commit_id="abc",
                        path=Path("scenarios/rds-mysql"),
                    ),
                ],
            ),
        ],
    )


def test_registry_mode_returns_descriptors(fake_registry):
    cfg = AwsBenchDatasetConfig(name="aws-bench-test", version="1.0.0")
    with patch.object(cfg, "_load_registry", return_value=fake_registry):
        configs = cfg.get_scenario_configs()
    assert {c.name for c in configs} == {"ec2-small", "rds-mysql"}
    assert all(c.git_url is not None for c in configs)
    assert configs[0].source == "aws-bench-test"


def test_registry_mode_filters_apply(fake_registry):
    cfg = AwsBenchDatasetConfig(
        name="aws-bench-test",
        version="1.0.0",
        include_scenario_names=["ec2*"],
    )
    with patch.object(cfg, "_load_registry", return_value=fake_registry):
        configs = cfg.get_scenario_configs()
    assert {c.name for c in configs} == {"ec2-small"}
