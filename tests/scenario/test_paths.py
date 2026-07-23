"""Tests for aws_bench.scenario.paths."""

from __future__ import annotations

from pathlib import Path

from aws_bench.scenario.events import ScenarioPhase
from aws_bench.scenario.paths import ScenarioPaths


def _make_phase_layout(
    root: Path,
    *,
    phase_scripts: tuple[ScenarioPhase, ...] = (ScenarioPhase.DEPLOY,),
) -> Path:
    """Build a scenario directory; materialize a ``<phase>/<phase>.sh`` per entry."""
    root.mkdir(parents=True, exist_ok=True)
    for phase in phase_scripts:
        (root / phase).mkdir(exist_ok=True)
        (root / phase / f"{phase}.sh").write_text("#!/bin/sh\n")
    return root


def test_paths_resolve_to_expected_locations(tmp_path):
    p = ScenarioPaths(tmp_path)
    assert p.config_path == tmp_path / "scenario.toml"
    assert p.dockerfile_path == tmp_path / "scenario" / "Dockerfile"
    assert p.phase_dir(ScenarioPhase.DEPLOY) == tmp_path / "deploy"
    assert p.phase_script_path(ScenarioPhase.DEPLOY) == tmp_path / "deploy" / "deploy.sh"
    assert p.phase_script_path(ScenarioPhase.VERIFY) == tmp_path / "verify" / "verify.sh"
    assert p.phase_script_path(ScenarioPhase.CLEANUP) == tmp_path / "cleanup" / "cleanup.sh"
    assert p.phase_script_path(ScenarioPhase.RESET) == tmp_path / "reset" / "reset.sh"


def test_has_phase_script_true_when_present(tmp_path):
    sd = _make_phase_layout(tmp_path, phase_scripts=(ScenarioPhase.DEPLOY, ScenarioPhase.VERIFY))
    paths = ScenarioPaths(sd)
    assert paths.has_phase_script(ScenarioPhase.DEPLOY) is True
    assert paths.has_phase_script(ScenarioPhase.VERIFY) is True


def test_has_phase_script_false_when_absent(tmp_path):
    sd = _make_phase_layout(tmp_path, phase_scripts=(ScenarioPhase.DEPLOY,))
    paths = ScenarioPaths(sd)
    assert paths.has_phase_script(ScenarioPhase.VERIFY) is False
    assert paths.has_phase_script(ScenarioPhase.CLEANUP) is False
    assert paths.has_phase_script(ScenarioPhase.RESET) is False


def test_has_phase_script_false_when_only_dir_present(tmp_path):
    """A phase dir with no entry script reads as absent (benign — phase skipped)."""
    sd = _make_phase_layout(tmp_path)
    (sd / "verify").mkdir()  # dir but no verify.sh
    assert ScenarioPaths(sd).has_phase_script(ScenarioPhase.VERIFY) is False
