"""Shared pytest fixtures and helpers for the aws-bench test suite."""

from pathlib import Path

import pytest
import tenacity

from aws_bench.utils.retry import retrying_git_fetch


@pytest.fixture(autouse=True)
def _no_git_fetch_backoff():
    """Neutralize the shared git-fetch retry backoff so tests don't sleep; restore after.

    ``@tenacity.retry`` attaches the controller as ``.retry`` on the decorated
    function; pyright can't see it, hence the ignore.
    """
    controller = retrying_git_fetch.retry  # type: ignore[attr-defined]
    original = controller.wait
    controller.wait = tenacity.wait_none()
    try:
        yield
    finally:
        controller.wait = original


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Run each test in a fresh tmp cwd so relative output dirs don't leak.

    ``ScenarioJobConfig`` (→ ``scenario-jobs/``) and Harbor's ``JobConfig``
    (→ ``jobs/``) default ``jobs_dir`` to a *relative* path resolved against the
    cwd; a test that hits the mkdir/persist path without overriding it would
    otherwise litter those dirs into the repo root.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    """Redirect the command ledger to a tmp dir so tests never touch real HOME.

    ``LOGS_DIR`` is an *absolute* path (``~/.aws-bench/logs``), so the cwd
    isolation above does not cover it. The Typer root callback opens a ledger
    entry for every command, so any test that drives the CLI through
    ``CliRunner`` would otherwise write entries into the developer's real
    ``~/.aws-bench/logs/``. Point it at the per-test tmp dir.
    """
    monkeypatch.setattr("aws_bench.logging.ledger.LOGS_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _isolate_provisioned_buckets(monkeypatch):
    """Reset the module-global provisioned-bucket cache so tests don't leak it.

    Without this, a test reusing a bucket name (e.g. moto 'test-bucket') would
    skip provisioning against its own fresh moto S3 and fail.
    """
    monkeypatch.setattr(
        "aws_bench.resource_management.storage.s3_backend._provisioned_buckets", set()
    )


def _scenario_toml(name: str) -> str:
    return f'''
schema_version = "1.0"
[scenario]
name = "{name}"
description = "test"
account_tags = ["PRIMARY"]
regions = ["us-east-1"]
[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 1024
[deploy]
timeout_sec = 60.0
[verify]
timeout_sec = 60.0
[cleanup]
timeout_sec = 60.0
'''


def make_scenario_layout(scenario_dir: Path, name: str) -> Path:
    """Build a minimal valid scenario directory at ``scenario_dir``.

    Writes scenario.toml, scenario/Dockerfile, deploy/deploy.sh under
    the directory. Used by integration tests to materialize the on-disk
    side of a registry-fetched scenario without going through Docker
    or the registry.
    """
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "scenario.toml").write_text(_scenario_toml(name))
    (scenario_dir / "scenario").mkdir(exist_ok=True)
    (scenario_dir / "scenario" / "Dockerfile").write_text("FROM scratch\n")
    (scenario_dir / "deploy").mkdir(exist_ok=True)
    (scenario_dir / "deploy" / "deploy.sh").write_text("#!/bin/sh\n")
    return scenario_dir


@pytest.fixture
def scenario_layout_factory():
    """Return a callable that creates a minimal valid scenario directory."""
    return make_scenario_layout
