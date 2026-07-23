"""Tests for the ScenarioConfig descriptor in aws_bench.scenario.locator."""

from pathlib import Path

import pytest
from harbor.models.task.id import GitTaskId, LocalTaskId
from pydantic import ValidationError

from aws_bench.dataset.source_ids import GitScenarioId, LocalScenarioId
from aws_bench.scenario.locator import ScenarioConfig


def test_local_to_scenario_id_returns_local():
    c = ScenarioConfig(name="x", path=Path("/tmp/x"))
    tid = c.to_scenario_id()
    assert isinstance(tid, LocalScenarioId)
    assert isinstance(tid, LocalTaskId)


def test_remote_to_scenario_id_returns_git():
    c = ScenarioConfig(
        name="x",
        path=Path("scenarios/x"),
        git_url="https://github.com/example/repo",
        git_commit_id="abc123",
    )
    tid = c.to_scenario_id()
    assert isinstance(tid, GitScenarioId)
    assert isinstance(tid, GitTaskId)
    assert tid.git_url == "https://github.com/example/repo"
    assert tid.git_commit_id == "abc123"


def test_commit_id_without_url_raises():
    with pytest.raises(ValidationError, match="'git_commit_id' requires 'git_url'"):
        ScenarioConfig(name="x", path=Path("/tmp/x"), git_commit_id="abc")
