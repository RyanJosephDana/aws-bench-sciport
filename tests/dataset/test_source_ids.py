"""Tests for the domain-branded scenario / instruction / metric source ids.

The branded ids must (a) remain ``isinstance`` of the task-id primitives the
download client dispatches on, and (b) resolve ``get_local_path()`` under
their own per-domain cache directory.
"""

from pathlib import Path

from harbor.models.task.id import GitTaskId, LocalTaskId

from aws_bench.constants import (
    INSTRUCTION_CACHE_DIR,
    METRIC_CACHE_DIR,
    SCENARIO_CACHE_DIR,
)
from aws_bench.dataset.source_ids import (
    GitInstructionId,
    GitScenarioId,
    GitUVScriptId,
    LocalInstructionId,
    LocalScenarioId,
)

COMMIT = "a" * 40


def test_git_scenario_id_is_git_task_id():
    sid = GitScenarioId(git_url="ssh://x", git_commit_id=COMMIT, path=Path("scenarios/foo"))
    assert isinstance(sid, GitTaskId)


def test_git_metric_id_is_git_task_id():
    sid = GitUVScriptId(git_url="ssh://x", git_commit_id=COMMIT, path=Path("metrics/cost"))
    assert isinstance(sid, GitTaskId)


def test_git_instruction_id_is_git_task_id():
    sid = GitInstructionId(git_url="ssh://x", git_commit_id=COMMIT, path=Path("instr"))
    assert isinstance(sid, GitTaskId)


def test_git_scenario_local_path_under_scenario_cache():
    sid = GitScenarioId(git_url="ssh://x", git_commit_id=COMMIT, path=Path("scenarios/foo"))
    local_path = sid.get_local_path()
    assert SCENARIO_CACHE_DIR in local_path.parents
    assert local_path.name == "foo"


def test_git_metric_local_path_under_metric_cache():
    sid = GitUVScriptId(git_url="ssh://x", git_commit_id=COMMIT, path=Path("metrics/cost"))
    local_path = sid.get_local_path()
    assert METRIC_CACHE_DIR in local_path.parents
    assert local_path.name == "cost"


def test_git_instruction_local_path_under_instruction_cache():
    sid = GitInstructionId(git_url="ssh://x", git_commit_id=COMMIT, path=Path("instr"))
    local_path = sid.get_local_path()
    assert INSTRUCTION_CACHE_DIR in local_path.parents
    assert local_path.name == "instr"


def test_local_subtypes_are_local_task_id():
    assert isinstance(LocalScenarioId(path=Path("/tmp/x")), LocalTaskId)
    assert isinstance(LocalInstructionId(path=Path("/tmp/x")), LocalTaskId)


def test_local_scenario_id_resolves_literal_path():
    sid = LocalScenarioId(path=Path("/tmp/some-scenario"))
    assert sid.get_local_path() == Path("/tmp/some-scenario").expanduser().resolve()
