"""Tests for AwsBenchDatasetConfig._get_registry_task_configs override."""

from pathlib import Path
from unittest.mock import patch

import pytest
from harbor.models.registry import RegistryTaskId

from aws_bench.dataset.config import AwsBenchDatasetConfig
from aws_bench.dataset.registry import AwsBenchDatasetSpec, AwsBenchRegistry


@pytest.fixture
def fake_registry():
    return AwsBenchRegistry(
        datasets=[
            AwsBenchDatasetSpec(
                name="aws-bench-test",
                version="1.0.0",
                description="",
                tasks=[
                    RegistryTaskId(
                        name="task-a",
                        git_url="https://github.com/x/y",
                        git_commit_id="abc",
                        path=Path("tasks/task-a"),
                    ),
                    RegistryTaskId(
                        name="task-b",
                        git_url="https://github.com/x/y",
                        git_commit_id="abc",
                        path=Path("tasks/task-b"),
                    ),
                ],
                scenarios=[],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_registry_task_configs_returns_taskconfigs(fake_registry):
    cfg = AwsBenchDatasetConfig(name="aws-bench-test", version="1.0.0")
    with patch.object(cfg, "_load_registry", return_value=fake_registry):
        with patch("aws_bench.dataset.config.RegistryValidator"):
            configs = await cfg._get_registry_task_configs()
    assert all(c.path is not None for c in configs)
    names = {Path(c.path).name for c in configs if c.path is not None}
    assert names == {"task-a", "task-b"}
    assert all(c.git_url == "https://github.com/x/y" for c in configs)
    assert all(c.git_commit_id == "abc" for c in configs)
    assert all(c.source == "aws-bench-test" for c in configs)


@pytest.mark.asyncio
async def test_registry_task_configs_filter_applies(fake_registry):
    cfg = AwsBenchDatasetConfig(
        name="aws-bench-test",
        version="1.0.0",
        task_names=["task-a"],
    )
    with patch.object(cfg, "_load_registry", return_value=fake_registry):
        with patch("aws_bench.dataset.config.RegistryValidator"):
            configs = await cfg._get_registry_task_configs()
    assert len(configs) == 1
    assert configs[0].path is not None
    assert Path(configs[0].path).name == "task-a"
