"""cache_scenarios batched fail-fast fetch tests."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aws_bench.dataset.config import AwsBenchDatasetConfig
from aws_bench.dataset.exceptions import ScenarioFetchError
from aws_bench.scenario.locator import ScenarioConfig


@pytest.mark.asyncio
async def test_all_local_no_taskclient_constructed(tmp_path):
    """Local-only inputs touch no TaskClient and stay absent from the map.

    Local scenarios are resolved by callers via ``get_local_path()``; only git
    scenarios appear in the cache map (mirrors ``cache_tasks``).
    """
    cfg = AwsBenchDatasetConfig(scenarios_path=tmp_path)
    local_configs = [
        ScenarioConfig(name="x", path=tmp_path / "x"),
        ScenarioConfig(name="y", path=tmp_path / "y"),
    ]
    with patch.object(cfg, "get_scenario_configs", MagicMock(return_value=local_configs)):
        with patch("aws_bench.dataset.config.TaskClient") as MockClient:
            result = await cfg.cache_scenarios()
    MockClient.assert_not_called()
    assert result == {}


@pytest.mark.asyncio
async def test_remote_batched_single_download_call(tmp_path, monkeypatch):
    """All remote scenarios go through ONE batched download_tasks call."""
    cfg = AwsBenchDatasetConfig(scenarios_path=tmp_path)
    remote_configs = [
        ScenarioConfig(name="a", path=Path("s/a"), git_url="https://x", git_commit_id="c"),
        ScenarioConfig(name="b", path=Path("s/b"), git_url="https://x", git_commit_id="c"),
    ]
    calls = []

    async def fake_download(self, task_ids, **kwargs):
        calls.append(task_ids)
        result = MagicMock()
        result.results = [MagicMock(path=tmp_path / tid.path.name) for tid in task_ids]
        return result

    monkeypatch.setattr("aws_bench.dataset.config.TaskClient.download_tasks", fake_download)
    with patch.object(cfg, "get_scenario_configs", MagicMock(return_value=remote_configs)):
        result = await cfg.cache_scenarios()

    # One batched call carrying both ids (not one call per scenario).
    assert len(calls) == 1
    assert len(calls[0]) == 2
    # Map is keyed by scenario id; both git scenarios resolved to on-disk paths.
    # pyright reports unhashable for a set-comp of GitScenarioId | LocalScenarioId;
    # both are frozen pydantic models and hashable at runtime.
    assert {c.to_scenario_id() for c in remote_configs} == set(result.keys())  # type: ignore[reportUnhashable]
    assert {p.name for p in result.values()} == {"a", "b"}


@pytest.mark.asyncio
async def test_git_failure_raises_fail_fast(tmp_path, monkeypatch):
    """A git clone failure raises ScenarioFetchError (fail-fast, no partial result)."""
    cfg = AwsBenchDatasetConfig(scenarios_path=tmp_path)
    remote_configs = [
        ScenarioConfig(name="ok", path=Path("s/ok"), git_url="https://x", git_commit_id="a"),
        ScenarioConfig(name="bad", path=Path("s/bad"), git_url="https://x", git_commit_id="b"),
    ]

    async def fake_download(self, task_ids, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=["git"], stderr=b"clone failed")

    monkeypatch.setattr("aws_bench.dataset.config.TaskClient.download_tasks", fake_download)
    with patch.object(cfg, "get_scenario_configs", MagicMock(return_value=remote_configs)):
        with pytest.raises(ScenarioFetchError, match="clone failed"):
            await cfg.cache_scenarios()


@pytest.mark.asyncio
async def test_filenotfounderror_raises_fail_fast(tmp_path, monkeypatch):
    """FileNotFoundError from the download path is wrapped as ScenarioFetchError."""
    cfg = AwsBenchDatasetConfig(scenarios_path=tmp_path)
    remote_configs = [
        ScenarioConfig(name="missing", path=Path("/no"), git_url="https://x", git_commit_id="a"),
    ]

    async def fake_download(self, **kwargs):
        raise FileNotFoundError("Local task /no not found")

    monkeypatch.setattr("aws_bench.dataset.config.TaskClient.download_tasks", fake_download)
    with patch.object(cfg, "get_scenario_configs", MagicMock(return_value=remote_configs)):
        with pytest.raises(ScenarioFetchError, match="FileNotFoundError"):
            await cfg.cache_scenarios()
