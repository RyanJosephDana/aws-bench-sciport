"""Unit tests for registry metric/instruction resolution on AwsBenchDatasetConfig."""

from pathlib import Path
from unittest.mock import patch

import pytest

from aws_bench.dataset.config import AwsBenchDatasetConfig
from aws_bench.dataset.registry import (
    AwsBenchDatasetSpec,
    AwsBenchRegistry,
)


def _registry_with(spec: AwsBenchDatasetSpec) -> AwsBenchRegistry:
    return AwsBenchRegistry(path=Path("reg.json"), datasets=[spec])


@pytest.mark.asyncio
async def test_local_mode_resolves_to_empty(tmp_path):
    # Local mode has no registry spec: the resolve_* methods short-circuit to
    # [] (the job flow calls them unconditionally).
    cfg = AwsBenchDatasetConfig(scenarios_path=tmp_path)
    assert await cfg.resolve_metric_configs() == []
    assert await cfg.resolve_instruction_paths() == []


def test_registry_spec_returns_dataset_spec():
    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "version": "1.0.0",
            "description": "d",
            "tasks": [],
            "metrics": [{"type": "mean"}],
        }
    )
    with patch.object(cfg, "_load_registry", return_value=_registry_with(spec)):
        resolved = cfg._registry_spec()
    assert resolved.metrics[0].type.value == "mean"


@pytest.mark.asyncio
async def test_resolve_instruction_paths_fetches_remote_and_passes_local(tmp_path):
    from aws_bench.dataset.registry import RegistryExtraInstructionPathId

    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec(
        name="d",
        description="d",
        version="1.0.0",
        tasks=[],
        extra_instruction_paths=[
            RegistryExtraInstructionPathId(
                path=Path("instr/remote.md"), git_url="ssh://x", git_commit_id="a" * 40
            ),
            RegistryExtraInstructionPathId(path=tmp_path / "local.md"),
        ],
    )
    fetched_dir = tmp_path / "fetched"
    fetched_dir.mkdir()
    (fetched_dir / "remote.md").write_text("guardrails")
    (tmp_path / "local.md").write_text("local instruction")

    async def fake_fetch_one(self, source_id, output_dir):
        return fetched_dir

    with patch.object(cfg, "_registry_spec", return_value=spec):
        with patch.object(AwsBenchDatasetConfig, "_fetch_one", fake_fetch_one):
            paths = await cfg.resolve_instruction_paths()

    assert paths[0] == fetched_dir / "remote.md"
    assert paths[1] == tmp_path / "local.md"


@pytest.mark.asyncio
async def test_resolve_instruction_paths_empty_file_warns(tmp_path, caplog):
    import logging

    from aws_bench.dataset.registry import RegistryExtraInstructionPathId

    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    fetched_dir = tmp_path / "f"
    fetched_dir.mkdir()
    (fetched_dir / "empty.md").write_text("   \n")
    spec = AwsBenchDatasetSpec(
        name="d",
        description="d",
        version="1.0.0",
        tasks=[],
        extra_instruction_paths=[
            RegistryExtraInstructionPathId(
                path=Path("instr/empty.md"), git_url="ssh://x", git_commit_id="a" * 40
            )
        ],
    )

    async def fake_fetch_one(self, source_id, output_dir):
        return fetched_dir

    with patch.object(cfg, "_registry_spec", return_value=spec):
        with patch.object(AwsBenchDatasetConfig, "_fetch_one", fake_fetch_one):
            with caplog.at_level(logging.WARNING):
                await cfg.resolve_instruction_paths()

    assert any("empty" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_resolve_metric_configs_declarative_passthrough():
    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "version": "1.0.0",
            "description": "d",
            "tasks": [],
            "metrics": [{"type": "mean"}, {"type": "max"}],
        }
    )
    with patch.object(cfg, "_registry_spec", return_value=spec):
        configs = await cfg.resolve_metric_configs()
    assert [c.type.value for c in configs] == ["mean", "max"]


@pytest.mark.asyncio
async def test_resolve_metric_configs_uv_script_resolves_path(tmp_path):
    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "description": "d",
            "version": "1.0.0",
            "tasks": [],
            "metrics": [
                {
                    "type": "uv-script",
                    "kwargs": {
                        "git_url": "ssh://x",
                        "git_commit_id": "a" * 40,
                        "script_path": "metrics/cost/metric.py",
                    },
                }
            ],
        }
    )
    fetched_dir = tmp_path / "cost"
    fetched_dir.mkdir()
    (fetched_dir / "metric.py").write_text("# script")

    async def fake_fetch_one(self, source_id, output_dir):
        return fetched_dir

    with patch.object(cfg, "_registry_spec", return_value=spec):
        with patch.object(AwsBenchDatasetConfig, "_fetch_one", fake_fetch_one):
            configs = await cfg.resolve_metric_configs()

    assert configs[0].type.value == "uv-script"
    # only the resolved local script_path survives into kwargs (no git keys)
    assert configs[0].kwargs == {"script_path": str(fetched_dir / "metric.py")}


@pytest.mark.asyncio
async def test_resolve_metric_configs_local_uv_script_passthrough(tmp_path):
    # A LOCAL uv-script (script_path only, no git_url) is passed through
    # verbatim — the literal path is read directly. No fetch happens.
    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "description": "d",
            "version": "1.0.0",
            "tasks": [],
            "metrics": [
                {
                    "type": "uv-script",
                    "kwargs": {"script_path": str(tmp_path / "local_metric.py")},
                }
            ],
        }
    )

    async def fail_fetch_one(self, source_id, output_dir):
        raise AssertionError("local uv-script must not trigger a fetch")

    with patch.object(cfg, "_registry_spec", return_value=spec):
        with patch.object(AwsBenchDatasetConfig, "_fetch_one", fail_fetch_one):
            configs = await cfg.resolve_metric_configs()

    assert configs[0].type.value == "uv-script"
    # script_path survives untouched; no git keys are introduced.
    assert configs[0].kwargs == {"script_path": str(tmp_path / "local_metric.py")}


@pytest.mark.asyncio
async def test_resolve_metric_configs_uv_script_missing_file_raises(tmp_path):
    from aws_bench.dataset.exceptions import MetricFetchError

    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "description": "d",
            "version": "1.0.0",
            "tasks": [],
            "metrics": [
                {
                    "type": "uv-script",
                    "kwargs": {
                        "git_url": "ssh://x",
                        "git_commit_id": "a" * 40,
                        "script_path": "metrics/cost/metric.py",
                    },
                }
            ],
        }
    )
    empty_dir = tmp_path / "cost"
    empty_dir.mkdir()  # metric.py absent

    async def fake_fetch_one(self, source_id, output_dir):
        return empty_dir

    with patch.object(cfg, "_registry_spec", return_value=spec):
        with patch.object(AwsBenchDatasetConfig, "_fetch_one", fake_fetch_one):
            with pytest.raises(MetricFetchError):
                await cfg.resolve_metric_configs()


@pytest.mark.asyncio
async def test_resolve_metric_configs_git_uv_script_missing_commit_raises(tmp_path):
    # A git uv-script (has git_url) that omits git_commit_id is malformed and
    # must fail with an actionable message before any fetch is attempted.
    from aws_bench.dataset.exceptions import MetricFetchError

    cfg = AwsBenchDatasetConfig(name="d", version="1.0.0")
    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "description": "d",
            "version": "1.0.0",
            "tasks": [],
            "metrics": [
                {
                    "type": "uv-script",
                    "kwargs": {
                        "git_url": "ssh://x",
                        "script_path": "metrics/cost/metric.py",
                    },
                }
            ],
        }
    )

    async def fail_if_fetched(self, source_id, output_dir):
        raise AssertionError("malformed metric must not trigger a fetch")

    with patch.object(cfg, "_registry_spec", return_value=spec):
        with patch.object(AwsBenchDatasetConfig, "_fetch_one", fail_if_fetched):
            with pytest.raises(MetricFetchError, match="git_commit_id"):
                await cfg.resolve_metric_configs()
