"""Tests for aws_bench.task.script_runner.ScriptRunner."""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from harbor.models.task.config import TaskOS

from aws_bench.dataset.models import ScriptType
from aws_bench.task.exceptions import (
    ScriptExecutionError,
    ScriptOutputDownloadError,
    ScriptResultFileEmptyError,
    ScriptResultFileNotFoundError,
    ScriptResultParseError,
    ScriptUploadError,
)
from aws_bench.task.script_runner import ScriptRunner

DUMMY_CREDS = {
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_SESSION_TOKEN": "FwoGZXIvYXdzEBYaDHqa0AP",
}


@dataclass
class FakeExecResult:
    """Mock exec result matching the environment's ExecResult interface."""

    return_code: int
    stdout: str = ""
    stderr: str = ""


def _make_task_dir(tmp_path: Path, script_type: ScriptType, body: str) -> Path:
    """Create a task directory with a script of the given type."""
    task_dir = tmp_path / "task"
    script_dir = task_dir / script_type.value
    script_dir.mkdir(parents=True)
    script_file = script_dir / f"{script_type.value}.sh"
    script_file.write_text(f"#!/bin/bash\n{body}\n")
    script_file.chmod(0o755)
    return task_dir


def _make_environment() -> MagicMock:
    """Create a mock environment matching the BaseEnvironment interface.

    ``env.os`` is a real ``TaskOS`` so ``EnvironmentPaths.for_os(env.os)`` in
    :class:`ScriptRunner._container_output_dir` resolves to the Linux paths
    (``/logs/...``) the test bodies expect.
    """
    env = AsyncMock()
    env.os = TaskOS.LINUX
    env.exec = AsyncMock(return_value=FakeExecResult(return_code=0))
    env.upload_dir = AsyncMock()
    env.download_dir = AsyncMock()
    env.download_file = AsyncMock()
    return env


def _make_trial_paths(tmp_path: Path) -> MagicMock:
    """Create a mock TrialPaths."""
    trial_paths = MagicMock()
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir(parents=True, exist_ok=True)
    trial_paths.trial_dir = trial_dir
    return trial_paths


# ── Basic execution ──


@pytest.mark.asyncio
async def test_script_runner_uploads_and_executes(tmp_path):
    """ScriptRunner uploads script dir and executes entry script."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo hello")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )
    result = await runner.run()

    # Verify upload was called with correct paths
    env.upload_dir.assert_called_once()
    upload_call = env.upload_dir.call_args
    assert upload_call.kwargs["source_dir"] == task_dir / "pre_invoke"
    assert upload_call.kwargs["target_dir"] == "/pre_invoke"

    # Verify exec was called (mkdir, chmod, script, rm -rf cleanup)
    assert env.exec.call_count == 4
    assert result == {}


@pytest.mark.asyncio
async def test_script_runner_post_invoke_no_cleanup(tmp_path):
    """POST_INVOKE does not clean up container paths."""
    task_dir = _make_task_dir(tmp_path, ScriptType.POST_INVOKE, "echo done")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.POST_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )
    await runner.run()

    # POST_INVOKE: mkdir + chmod + exec + download = no rm -rf call
    exec_commands = [
        str(call.kwargs.get("command", call.args[0] if call.args else ""))
        for call in env.exec.call_args_list
    ]
    assert not any("rm -rf" in cmd for cmd in exec_commands)


@pytest.mark.asyncio
async def test_script_runner_pre_invoke_cleans_up(tmp_path):
    """PRE_INVOKE cleans up /pre_invoke/ and /logs/pre_invoke/ from container."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo hello")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )
    await runner.run()

    # Last exec call should be the rm -rf cleanup
    last_exec = env.exec.call_args_list[-1]
    cmd = last_exec.kwargs.get("command", last_exec.args[0] if last_exec.args else "")
    assert "rm -rf" in cmd
    assert "/pre_invoke" in cmd


# ── Output file handling ──


@pytest.mark.asyncio
async def test_script_runner_downloads_output_file(tmp_path):
    """Output file is read from /logs/{type}/ after bulk download."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    # Simulate download_dir writing the result file alongside stdout.log
    async def fake_download_dir(source_dir, target_dir):
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        (Path(target_dir) / "placeholder.json").write_text('{"KEY": "VALUE"}')

    env.download_dir = AsyncMock(side_effect=fake_download_dir)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )
    result = await runner.run(output_file_name="placeholder.json")

    assert result == {"KEY": "VALUE"}
    env.download_file.assert_not_called()


@pytest.mark.asyncio
async def test_script_runner_missing_output_file_raises(tmp_path):
    """Raises when /logs/{type}/{output_file_name} is absent.

    Pre-invoke must always write the file — empty `{}` is valid for
    "no placeholders", but a missing file means the script crashed.
    """
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    # download_dir succeeds but writes no placeholder.json
    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )

    with pytest.raises(ScriptResultFileNotFoundError, match="placeholder.json"):
        await runner.run(output_file_name="placeholder.json")


@pytest.mark.asyncio
async def test_script_runner_empty_result_file_raises(tmp_path):
    """Raises ScriptResultFileEmptyError when result file is zero bytes."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    async def fake_download_dir(source_dir, target_dir):
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        (Path(target_dir) / "placeholder.json").write_text("")

    env.download_dir = AsyncMock(side_effect=fake_download_dir)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )

    with pytest.raises(ScriptResultFileEmptyError):
        await runner.run(output_file_name="placeholder.json")


@pytest.mark.asyncio
async def test_script_runner_unparseable_result_file_raises(tmp_path):
    """Raises ScriptResultParseError when result file is not valid JSON."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    async def fake_download_dir(source_dir, target_dir):
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        (Path(target_dir) / "placeholder.json").write_text("not json {{{")

    env.download_dir = AsyncMock(side_effect=fake_download_dir)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )

    with pytest.raises(ScriptResultParseError):
        await runner.run(output_file_name="placeholder.json")


@pytest.mark.asyncio
async def test_script_runner_empty_dict_result_is_valid(tmp_path):
    """Script writing `{}` is the valid 'no placeholders' signal."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    async def fake_download_dir(source_dir, target_dir):
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        (Path(target_dir) / "placeholder.json").write_text("{}")

    env.download_dir = AsyncMock(side_effect=fake_download_dir)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env=DUMMY_CREDS,
    )
    result = await runner.run(output_file_name="placeholder.json")

    assert result == {}


# ── Error handling ──


@pytest.mark.asyncio
async def test_script_runner_script_dir_not_found(tmp_path):
    """FileNotFoundError when task dir has no script directory."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
    )

    with pytest.raises(FileNotFoundError, match="Script directory not found"):
        await runner.run()


@pytest.mark.asyncio
async def test_script_runner_entry_script_not_found(tmp_path):
    """FileNotFoundError when directory exists but entry script is missing."""
    task_dir = tmp_path / "task"
    (task_dir / "pre_invoke").mkdir(parents=True)
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
    )

    with pytest.raises(FileNotFoundError, match="No linux pre_invoke entry script found"):
        await runner.run()


@pytest.mark.asyncio
async def test_script_runner_upload_failure(tmp_path):
    """ScriptUploadError when upload_dir fails."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    env.upload_dir = AsyncMock(side_effect=Exception("connection refused"))
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
    )

    with pytest.raises(ScriptUploadError):
        await runner.run()


@pytest.mark.asyncio
async def test_script_runner_nonzero_exit_code(tmp_path):
    """ScriptExecutionError when script exits non-zero."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "exit 1")
    env = _make_environment()
    env.exec = AsyncMock(return_value=FakeExecResult(return_code=0))
    trial_paths = _make_trial_paths(tmp_path)

    # First two execs (mkdir, chmod) succeed, third (script) fails
    env.exec = AsyncMock(
        side_effect=[
            FakeExecResult(return_code=0),  # mkdir
            FakeExecResult(return_code=0),  # chmod
            FakeExecResult(return_code=1, stderr="error"),  # script
        ]
    )

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
    )

    with pytest.raises(ScriptExecutionError, match="failed with exit code 1"):
        await runner.run()


@pytest.mark.asyncio
async def test_script_runner_download_failure(tmp_path):
    """ScriptOutputDownloadError when download_dir fails."""
    task_dir = _make_task_dir(tmp_path, ScriptType.POST_INVOKE, "echo ok")
    env = _make_environment()
    env.download_dir = AsyncMock(side_effect=Exception("timeout"))
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.POST_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
    )

    with pytest.raises(ScriptOutputDownloadError):
        await runner.run()


# ── Override env ──


@pytest.mark.asyncio
async def test_script_runner_passes_override_env(tmp_path):
    """Override env vars are passed to the exec call."""
    task_dir = _make_task_dir(tmp_path, ScriptType.PRE_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.PRE_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
        override_env={"MY_VAR": "hello", **DUMMY_CREDS},
    )
    await runner.run()

    # The script exec call (3rd one: after mkdir and chmod) should have env
    script_exec_call = env.exec.call_args_list[2]
    passed_env = script_exec_call.kwargs.get("env")
    assert passed_env is not None
    assert passed_env["MY_VAR"] == "hello"
    assert passed_env["AWS_ACCESS_KEY_ID"] == DUMMY_CREDS["AWS_ACCESS_KEY_ID"]


@pytest.mark.asyncio
async def test_script_runner_no_env_passes_none(tmp_path):
    """When no override_env, exec is called with env=None."""
    task_dir = _make_task_dir(tmp_path, ScriptType.POST_INVOKE, "echo ok")
    env = _make_environment()
    trial_paths = _make_trial_paths(tmp_path)

    runner = ScriptRunner(
        script_type=ScriptType.POST_INVOKE,
        task_dir=task_dir,
        trial_paths=trial_paths,
        environment=env,
    )
    await runner.run()

    # Script exec call should have env=None
    script_exec_call = env.exec.call_args_list[2]
    assert script_exec_call.kwargs.get("env") is None
