"""Shared construction-time validation on AwsBenchDatasetConfig."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aws_bench.dataset.config import AwsBenchDatasetConfig


def test_rejects_package_name_with_slash():
    with pytest.raises(ValidationError, match="[Pp]ackage"):
        AwsBenchDatasetConfig(name="org/dataset")


def test_rejects_ref():
    with pytest.raises(ValidationError, match="ref"):
        AwsBenchDatasetConfig(name="my-dataset", ref="latest")


def test_rejects_registry_url_with_local_path():
    with pytest.raises(ValidationError, match="registry"):
        AwsBenchDatasetConfig(path=Path("tasks"), registry_url="https://x")


def test_rejects_registry_path_with_scenarios_path():
    with pytest.raises(ValidationError, match="registry"):
        AwsBenchDatasetConfig(scenarios_path=Path("scenarios"), registry_path=Path("reg.json"))


def test_bare_registry_name_with_version_is_valid():
    cfg = AwsBenchDatasetConfig(name="my-dataset", version="1.0.0")
    assert cfg.name == "my-dataset"
    assert cfg.version == "1.0.0"


def test_local_with_both_paths_is_valid():
    cfg = AwsBenchDatasetConfig(path=Path("tasks"), scenarios_path=Path("scenarios"))
    assert cfg.is_local()


def test_validate_run_rejects_tasks_only():
    cfg = AwsBenchDatasetConfig(path=Path("tasks"))
    with pytest.raises(ValueError, match="both"):
        cfg.validate_run()


def test_validate_run_rejects_scenarios_only():
    cfg = AwsBenchDatasetConfig(scenarios_path=Path("scenarios"))
    with pytest.raises(ValueError, match="both"):
        cfg.validate_run()


def test_validate_run_accepts_both_local_paths():
    cfg = AwsBenchDatasetConfig(path=Path("tasks"), scenarios_path=Path("scenarios"))
    cfg.validate_run()  # no raise


def test_validate_run_accepts_registry():
    cfg = AwsBenchDatasetConfig(name="my-dataset")
    cfg.validate_run()  # no raise (not local; rule does not apply)


def test_validate_env_rejects_tasks_path():
    cfg = AwsBenchDatasetConfig(path=Path("tasks"), scenarios_path=Path("scenarios"))
    with pytest.raises(ValueError, match="scenarios only"):
        cfg.validate_env()


def test_validate_env_accepts_scenarios_only():
    cfg = AwsBenchDatasetConfig(scenarios_path=Path("scenarios"))
    cfg.validate_env()  # no raise


def test_validate_env_accepts_registry():
    cfg = AwsBenchDatasetConfig(name="my-dataset")
    cfg.validate_env()  # no raise


def test_from_cli_args_run_registry_splits_name_and_version():
    cfg = AwsBenchDatasetConfig.from_cli_args(dataset="my-dataset@1.0.0")
    assert cfg.name == "my-dataset"
    assert cfg.version == "1.0.0"


def test_from_cli_args_run_local_both_paths():
    cfg = AwsBenchDatasetConfig.from_cli_args(path=Path("tasks"), scenario_path=Path("scenarios"))
    assert cfg.path == Path("tasks")
    assert cfg.scenarios_path == Path("scenarios")
    assert cfg.name is None


def test_from_cli_args_run_passes_task_filters():
    cfg = AwsBenchDatasetConfig.from_cli_args(
        path=Path("tasks"),
        scenario_path=Path("scenarios"),
        include_task_names=["ec2*"],
        exclude_task_names=["*-slow"],
        n_tasks=3,
    )
    assert cfg.task_names == ["ec2*"]
    assert cfg.exclude_task_names == ["*-slow"]
    assert cfg.n_tasks == 3


def test_from_cli_args_env_positional_still_works():
    # env calls from_cli_args(scenario_path, dataset, ...) positionally.
    cfg = AwsBenchDatasetConfig.from_cli_args(Path("scenarios"), None)
    assert cfg.scenarios_path == Path("scenarios")
    assert cfg.path is None


def test_from_cli_args_no_source_raises_value_error():
    # No source: the model validator raises on construction. ValidationError
    # is a ValueError subclass, so env's `except ValueError` catches it.
    with pytest.raises(ValueError, match="No dataset source"):
        AwsBenchDatasetConfig.from_cli_args()
