"""Tests for aws_bench.scenario.job_config.

Focuses on ScenarioTrialConfig's descriptor-only shape and JSON round-trip.
"""

from __future__ import annotations

from pathlib import Path

from aws_bench.scenario.job_config import ScenarioTrialConfig
from aws_bench.scenario.locator import ScenarioConfig


def test_trial_config_descriptor_only(tmp_path):
    cfg = ScenarioTrialConfig(
        scenario=ScenarioConfig(name="ec2-small", path=tmp_path / "x"),
        output_dir=tmp_path / "out",
        account_mapping={"PRIMARY": "123456789012"},
        ou_name="test-ou",
    )
    assert cfg.scenario.name == "ec2-small"
    assert cfg.trial_name.startswith("ec2-small")  # generator default


def test_trial_config_json_roundtrip(tmp_path):
    cfg = ScenarioTrialConfig(
        scenario=ScenarioConfig(
            name="ec2-small",
            path=Path("scenarios/ec2-small"),
            git_url="https://github.com/x/y",
            git_commit_id="abc",
        ),
        output_dir=tmp_path / "out",
        account_mapping={"PRIMARY": "123"},
        timeout_multiplier=2.0,
        ou_name="test-ou",
    )
    payload = cfg.model_dump_json()
    restored = ScenarioTrialConfig.model_validate_json(payload)
    assert restored.scenario.name == cfg.scenario.name
    assert restored.scenario.git_url == "https://github.com/x/y"
    assert restored.scenario.git_commit_id == "abc"
    assert restored.timeout_multiplier == 2.0
    assert restored.account_mapping == {"PRIMARY": "123"}


def test_trial_config_no_scenario_dir_field(tmp_path):
    """Old scenario_dir / scenario_config / scenario_name fields are gone."""
    cfg = ScenarioTrialConfig(
        scenario=ScenarioConfig(name="x", path=tmp_path),
        output_dir=tmp_path / "out",
        account_mapping={},
        ou_name="test-ou",
    )
    fields = cfg.__class__.model_fields
    assert "scenario_dir" not in fields
    assert "scenario_config" not in fields
    assert "scenario_name" not in fields
    assert "scenario" in fields
