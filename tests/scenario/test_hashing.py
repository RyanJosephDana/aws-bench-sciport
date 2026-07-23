"""Tests for scenario hashing utility."""

import tempfile
from pathlib import Path

import pytest

from aws_bench.scenario.hashing import compute_scenario_hash


def test_compute_scenario_hash_basic():
    """Computes hash of scenario directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        scenario_dir = Path(tmpdir)
        build_context = scenario_dir / "scenario"
        build_context.mkdir()

        # Create test files
        (build_context / "Dockerfile").write_text("FROM python:3.12")
        (build_context / "script.sh").write_text("echo hello")

        result = compute_scenario_hash(scenario_dir)

        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex digest


def test_compute_scenario_hash_deterministic():
    """Hash is deterministic - same input produces same hash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        scenario_dir = Path(tmpdir)
        build_context = scenario_dir / "scenario"
        build_context.mkdir()

        (build_context / "Dockerfile").write_text("FROM python:3.12")
        (build_context / "script.sh").write_text("echo hello")

        hash1 = compute_scenario_hash(scenario_dir)
        hash2 = compute_scenario_hash(scenario_dir)

        assert hash1 == hash2


def test_compute_scenario_hash_different_content():
    """Different content produces different hash."""
    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        # Scenario 1
        scenario_dir1 = Path(tmpdir1)
        build_context1 = scenario_dir1 / "scenario"
        build_context1.mkdir()
        (build_context1 / "Dockerfile").write_text("FROM python:3.12")

        # Scenario 2 (different content)
        scenario_dir2 = Path(tmpdir2)
        build_context2 = scenario_dir2 / "scenario"
        build_context2.mkdir()
        (build_context2 / "Dockerfile").write_text("FROM python:3.13")

        hash1 = compute_scenario_hash(scenario_dir1)
        hash2 = compute_scenario_hash(scenario_dir2)

        assert hash1 != hash2


def test_compute_scenario_hash_missing_directory():
    """Raises ValueError if the scenario directory does not exist."""
    missing_dir = Path(tempfile.gettempdir()) / "aws-bench-nonexistent-scenario-dir"

    with pytest.raises(ValueError, match="Scenario directory not found"):
        compute_scenario_hash(missing_dir)
