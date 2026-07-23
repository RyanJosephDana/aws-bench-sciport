"""Git-mode tests for AwsBenchDatasetConfig task caching + mapping.

These exercise the download_dir path: git tasks are materialized under
``download_dir`` (or TASK_CACHE_DIR), and ``get_task_map`` must read each task
from its ACTUAL downloaded path rather than re-deriving it via
``GitTaskId.get_local_path()`` (which hardcodes TASK_CACHE_DIR and ignores
download_dir).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from harbor.models.task.id import GitTaskId
from harbor.models.trial.config import TaskConfig
from harbor.tasks.client import BatchDownloadResult, TaskDownloadResult

from aws_bench.dataset.config import AwsBenchDatasetConfig
from aws_bench.dataset.task_config import AwsBenchTask
from tests.dataset.test_config_local import _make_task_layout


def _git_task_config(download_dir: Path, path: Path = Path("tasks/git-task")) -> TaskConfig:
    """A git TaskConfig with download_dir set (mirrors _get_registry_task_configs)."""
    return TaskConfig(
        path=path,
        git_url="https://example.com/repo.git",
        git_commit_id="a" * 40,
        download_dir=download_dir,
        source="aws-bench-all",
    )


def _batch_for(task_id: GitTaskId, real_path: Path) -> BatchDownloadResult:
    """A download result whose single entry points at a real on-disk task dir."""
    return BatchDownloadResult(
        results=[TaskDownloadResult(path=real_path, download_time_sec=0.0, cached=False)],
        total_time_sec=0.0,
    )


@pytest.mark.asyncio
async def test_get_tasks_git_task_read_from_downloaded_path(tmp_path, monkeypatch):
    """get_tasks reads a git task from its actual downloaded path.

    A dataset with download_dir set materializes git tasks there, not under
    TASK_CACHE_DIR. AwsBenchTask must be built from the download result's
    ``.path`` because GitTaskId.get_local_path() hardcodes TASK_CACHE_DIR.
    """
    download_dir = tmp_path / "download"
    # Build a REAL valid task dir under download_dir (NOT under TASK_CACHE_DIR).
    real_dir = _make_task_layout(download_dir / "materialized" / "git-task")

    cfg = AwsBenchDatasetConfig(name="aws-bench-all")
    tc = _git_task_config(download_dir)
    task_id = tc.get_task_id()
    assert isinstance(task_id, GitTaskId)

    async def fake_download(self, task_ids, **kwargs):
        # The fix must read from this path, NOT from task_id.get_local_path().
        return _batch_for(task_ids[0], real_dir)

    monkeypatch.setattr("aws_bench.dataset.config.TaskClient.download_tasks", fake_download)
    with patch.object(cfg, "get_task_configs", _async_return([tc])):
        pairs = await cfg.get_tasks()

    assert len(pairs) == 1
    paired_config, task = pairs[0]
    assert paired_config is tc
    assert isinstance(task, AwsBenchTask)
    # The task was built from the mocked download path, not TASK_CACHE_DIR.
    assert task.paths.task_dir == real_dir.resolve()


@pytest.mark.asyncio
async def test_cache_tasks_returns_resolved_git_paths(tmp_path, monkeypatch):
    """cache_tasks returns {git task_id: downloaded result.path}; local tasks absent."""
    download_dir = tmp_path / "download"
    real_dir = tmp_path / "materialized" / "git-task"
    real_dir.mkdir(parents=True)

    cfg = AwsBenchDatasetConfig(name="aws-bench-all")
    git_tc = _git_task_config(download_dir)
    local_tc = TaskConfig(path=tmp_path / "local-task", source="aws-bench-all")
    git_id = git_tc.get_task_id()

    async def fake_download(self, task_ids, **kwargs):
        return _batch_for(task_ids[0], real_dir)

    monkeypatch.setattr("aws_bench.dataset.config.TaskClient.download_tasks", fake_download)
    resolved = await cfg.cache_tasks([git_tc, local_tc])

    # Only the git task is in the map, pointing at its real downloaded path.
    assert resolved == {git_id: real_dir}  # type: ignore[reportUnhashable]


@pytest.mark.asyncio
async def test_cache_tasks_no_git_returns_empty(tmp_path):
    """cache_tasks with only local tasks returns an empty map (TaskClient untouched)."""
    cfg = AwsBenchDatasetConfig(name="aws-bench-all")
    local_tc = TaskConfig(path=tmp_path / "local-task", source="aws-bench-all")

    with patch("aws_bench.dataset.config.TaskClient") as MockClient:
        resolved = await cfg.cache_tasks([local_tc])

    MockClient.assert_not_called()
    assert resolved == {}


def _async_return(value):
    """Build an AsyncMock-like callable returning ``value`` (for patch.object on coros)."""

    async def _coro(*args, **kwargs):
        return value

    return MagicMock(side_effect=_coro)
