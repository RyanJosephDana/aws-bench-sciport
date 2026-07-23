"""Tests for ``aws_bench.dataset.registry`` — Pydantic models + schema validation."""

import logging
from pathlib import Path

import pytest
from harbor.models.task.id import GitTaskId, LocalTaskId

from aws_bench.dataset.exceptions import RegistryValidationError
from aws_bench.dataset.registry import (
    AwsBenchDatasetSpec,
    AwsBenchRegistry,
    RegistryScenarioId,
    RegistryValidator,
)
from aws_bench.dataset.source_ids import (
    GitInstructionId,
    GitScenarioId,
    LocalInstructionId,
    LocalScenarioId,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── Parsing ──


def test_parse_valid_registry():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_valid.json")

    assert reg.path == FIXTURES / "registry_valid.json"
    assert reg.url is None
    assert len(reg.datasets) == 1

    ds = reg.datasets[0]
    assert ds.name == "aws-bench"
    assert ds.version == "1.0.0"
    assert ds.description == "aws-bench evaluates agent performance on AWS tasks."

    assert len(ds.tasks) == 1
    assert ds.tasks[0].name == "lambda-not-reading-appconfig-value"
    assert ds.tasks[0].git_commit_id == "a05de74eaaa55030bcc5ffeb5923fac8a77fd1fd"

    assert len(ds.scenarios) == 1
    assert ds.scenarios[0].name == "lambda-with-broken-environment-variables"
    assert ds.scenarios[0].git_commit_id == "a05de74eaaa55030bcc5ffeb5923fac8a77fd1fd"


def test_parse_legacy_tasks_only_registry():
    # A registry that omits ``scenarios`` entirely still parses; the field
    # defaults to an empty list.
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_legacy_tasks_only.json")

    assert len(reg.datasets) == 1
    ds = reg.datasets[0]
    assert ds.name == "legacy-tasks-only"
    assert ds.tasks
    assert ds.scenarios == []


def test_default_registry_at_repo_root_parses():
    repo_root = Path(__file__).parents[2]
    registry_path = repo_root / "registry.json"
    if not registry_path.exists():
        pytest.skip("registry.json not present at repo root")

    reg = AwsBenchRegistry.from_path(registry_path)
    assert len(reg.datasets) >= 1


# ── Schema validation ──


def test_schema_validation_missing_scenario_name():
    # Scenarios require ``name`` and ``path``; dropping ``name`` is rejected.
    with pytest.raises(RegistryValidationError) as exc_info:
        AwsBenchRegistry.from_path(FIXTURES / "registry_missing_scenario_name.json")

    msg = str(exc_info.value)
    assert "Invalid registry schema" in msg
    assert "name" in msg.lower()


def test_schema_validation_missing_task_path(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '[{"name": "x", "version": "1.0", "tasks": '
        '[{"name": "t", "git_url": "u", "git_commit_id": "c"}], "scenarios": []}]'
    )
    with pytest.raises(RegistryValidationError) as exc_info:
        AwsBenchRegistry.from_path(bad)
    assert "Invalid registry schema" in str(exc_info.value)


def test_invalid_json_wraps_error(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json")
    with pytest.raises(RegistryValidationError) as exc_info:
        AwsBenchRegistry.from_path(bad)
    assert "Invalid JSON" in str(exc_info.value)


def test_missing_required_top_level_field(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text('[{"name": "x", "tasks": [], "scenarios": []}]')
    with pytest.raises(RegistryValidationError):
        AwsBenchRegistry.from_path(bad)


# ── RegistryScenarioId ──


def test_registry_scenario_id_to_source_id_git():
    s = RegistryScenarioId(
        name="foo",
        git_url="https://example.com/repo.git",
        git_commit_id="abc",
        path=Path("scenarios/foo"),
    )
    src = s.to_source_id()
    assert isinstance(src, GitScenarioId)
    assert isinstance(src, GitTaskId)
    assert src.git_url == "https://example.com/repo.git"
    assert src.git_commit_id == "abc"
    assert src.path == Path("scenarios/foo")


def test_registry_scenario_id_to_source_id_local():
    s = RegistryScenarioId(name="foo", path=Path("scenarios/foo"))
    src = s.to_source_id()
    assert isinstance(src, LocalScenarioId)
    assert isinstance(src, LocalTaskId)
    assert src.path == Path("scenarios/foo")


def test_registry_scenario_id_get_name():
    s = RegistryScenarioId(name="my-scenario", path=Path("."))
    assert s.get_name() == "my-scenario"


def test_dataset_spec_scenarios_default_to_empty_list():
    ds = AwsBenchDatasetSpec(name="x", version="1.0", description="d", tasks=[])
    assert ds.scenarios == []
    assert ds.extra_instruction_paths == []


# ── Uniqueness validation ──


def test_duplicate_scenario_names_fails():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_duplicate_scenarios.json")
    with pytest.raises(RegistryValidationError) as exc_info:
        RegistryValidator(reg).validate()

    msg = str(exc_info.value)
    assert "bad-dupes@1.0" in msg
    assert "duplicate scenario name(s)" in msg
    assert "lambda-with-broken-env" in msg


def test_duplicate_task_names_fails():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_duplicate_tasks.json")
    with pytest.raises(RegistryValidationError) as exc_info:
        RegistryValidator(reg).validate()

    msg = str(exc_info.value)
    assert "bad-dupes@1.0" in msg
    assert "duplicate task name(s)" in msg
    assert "my-task" in msg


def test_uniqueness_valid_registry_does_not_raise():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_valid.json")
    RegistryValidator(reg).validate()  # no exception


# ── Cross-dataset consistency (warns, does not raise) ──


def test_cross_dataset_divergent_scenario_warns(caplog):
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_divergent_cross_dataset.json")

    with caplog.at_level(logging.WARNING, logger="aws_bench.dataset.registry"):
        RegistryValidator(reg).validate()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1
    msg = warnings[0].getMessage()
    assert "shared-scenario" in msg
    assert "aws-bench@1.0" in msg
    assert "aws-bench-troubleshooting@1.0" in msg
    assert "divergent" in msg.lower()


def test_cross_dataset_consistent_scenario_no_warn(caplog):
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_consistent_cross_dataset.json")

    with caplog.at_level(logging.WARNING, logger="aws_bench.dataset.registry"):
        RegistryValidator(reg).validate()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "divergent" in r.getMessage().lower()
    ]
    assert warnings == []


def test_single_occurrence_scenario_no_warn(caplog):
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_valid.json")

    with caplog.at_level(logging.WARNING, logger="aws_bench.dataset.registry"):
        RegistryValidator(reg).validate()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "divergent" in r.getMessage().lower()
    ]
    assert warnings == []


# ── get_dataset_spec ──


def test_explicit_version_returns_right_spec():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_multi_version.json")
    spec = reg.get_dataset_spec("aws-bench", "1.1.0")
    assert spec.name == "aws-bench"
    assert spec.version == "1.1.0"


def test_version_none_resolves_to_highest_semver():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_multi_version.json")
    spec = reg.get_dataset_spec("aws-bench")
    # Of 1.0.0 / 1.1.0 / 2.0.0, the highest is picked.
    assert spec.version == "2.0.0"


def test_missing_dataset_name_lists_available():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_multi_version.json")
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.get_dataset_spec("does-not-exist")

    msg = str(exc_info.value)
    assert "does-not-exist" in msg
    assert "not found" in msg
    # Available names are listed so the error is actionable.
    assert "aws-bench" in msg
    assert "aws-bench-troubleshooting" in msg


def test_missing_version_lists_available_versions():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_multi_version.json")
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.get_dataset_spec("aws-bench", "9.9.0")

    msg = str(exc_info.value)
    assert "aws-bench" in msg
    assert "9.9.0" in msg
    assert "Available versions" in msg
    for v in ["1.0.0", "1.1.0", "2.0.0"]:
        assert v in msg


def test_lookup_returns_correct_dataset_not_just_first_match():
    reg = AwsBenchRegistry.from_path(FIXTURES / "registry_multi_version.json")
    spec_ab = reg.get_dataset_spec("aws-bench-troubleshooting", "1.0.0")
    assert spec_ab.name == "aws-bench-troubleshooting"
    assert spec_ab.version == "1.0.0"


# ── SemVer shape validation ──


def _registry_with_versions(versions: list[str]) -> AwsBenchRegistry:
    return AwsBenchRegistry(
        datasets=[
            AwsBenchDatasetSpec(name=f"ds-{i}", version=v, description="d", tasks=[])
            for i, v in enumerate(versions)
        ]
    )


def test_valid_semver_versions_pass():
    reg = _registry_with_versions(
        [
            "0.1.0",
            "1.0.0",
            "10.20.30",
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-0.3.7",
            "1.0.0-rc.1+build.1",
            "1.0.0+20130313144700",
        ]
    )
    RegistryValidator(reg).validate()  # no exception


@pytest.mark.parametrize(
    "bad",
    [
        "1.0",  # too few parts
        "1",
        "v1.0.0",  # leading 'v' is not valid SemVer
        "01.0.0",  # leading zero in major
        "1.0.0-",  # empty pre-release
        "1.0.0+",  # empty build metadata
        "head",
        "",
        "  ",
        "1.2.3.4",  # too many parts
        "garbage",
    ],
)
def test_invalid_semver_versions_fail(bad):
    reg = _registry_with_versions([bad])
    with pytest.raises(RegistryValidationError) as exc_info:
        RegistryValidator(reg).validate()

    msg = str(exc_info.value)
    assert "not valid SemVer" in msg
    assert repr(bad) in msg
    assert "semver.org" in msg


def test_one_invalid_version_among_many_fails_and_lists_all_invalid():
    reg = _registry_with_versions(["0.1.0", "1.0", "2.0.0", "v3.0.0"])
    with pytest.raises(RegistryValidationError) as exc_info:
        RegistryValidator(reg).validate()

    msg = str(exc_info.value)
    assert "ds-1@'1.0'" in msg
    assert "ds-3@'v3.0.0'" in msg
    assert "ds-0" not in msg
    assert "ds-2" not in msg


def test_shipped_registry_has_valid_semver_versions():
    repo_root = Path(__file__).parents[2]
    registry_path = repo_root / "registry.json"
    if not registry_path.exists():
        pytest.skip("registry.json not present at repo root")

    reg = AwsBenchRegistry.from_path(registry_path)
    RegistryValidator(reg).validate()


def test_head_literal_is_rejected():
    reg = _registry_with_versions(["head"])
    with pytest.raises(RegistryValidationError) as exc_info:
        RegistryValidator(reg).validate()
    assert "not valid SemVer" in str(exc_info.value)


# ── Metric / instruction descriptors ──


def test_metric_and_instruction_fetch_errors_are_dataset_errors():
    from aws_bench.dataset.exceptions import (
        DatasetError,
        ExtraInstructionFetchError,
        MetricFetchError,
    )

    assert issubclass(MetricFetchError, DatasetError)
    assert issubclass(ExtraInstructionFetchError, DatasetError)


def test_extra_instruction_path_id_local_and_git_forms():
    from harbor.models.task.id import GitTaskId, LocalTaskId

    from aws_bench.dataset.registry import RegistryExtraInstructionPathId

    # A registry instruction may be a local path (no git): to_source_id()
    # returns a LocalInstructionId keyed on the parent dir.
    local = RegistryExtraInstructionPathId(path=Path("instructions/x.md"))
    assert isinstance(local.to_source_id(), LocalInstructionId)
    assert isinstance(local.to_source_id(), LocalTaskId)
    assert local.to_source_id().path == Path("instructions")

    # Git form maps to a GitInstructionId keyed on the parent dir.
    git = RegistryExtraInstructionPathId(
        path=Path("instructions/x.md"), git_url="ssh://x", git_commit_id="a" * 40
    )
    sid = git.to_source_id()
    assert isinstance(sid, GitInstructionId)
    assert isinstance(sid, GitTaskId)
    assert sid.path == Path("instructions")
    assert sid.git_commit_id == "a" * 40


def test_dataset_spec_metrics_and_instructions_default_empty():
    from aws_bench.dataset.registry import AwsBenchDatasetSpec

    spec = AwsBenchDatasetSpec(name="d", version="1.0.0", description="d", tasks=[])
    assert spec.metrics == []
    assert spec.extra_instruction_paths == []


def test_dataset_spec_parses_declarative_and_git_uvscript_metrics():
    from aws_bench.dataset.registry import AwsBenchDatasetSpec

    spec = AwsBenchDatasetSpec.model_validate(
        {
            "name": "d",
            "version": "1.0.0",
            "description": "d",
            "tasks": [],
            "metrics": [
                {"type": "mean"},
                {
                    "type": "uv-script",
                    "kwargs": {
                        "git_url": "ssh://x",
                        "git_commit_id": "a" * 40,
                        "script_path": "metrics/cost/cost.py",
                    },
                },
            ],
            "extra_instruction_paths": [
                {"git_url": "ssh://x", "git_commit_id": "a" * 40, "path": "i/x.md"}
            ],
        }
    )
    assert spec.metrics[0].type.value == "mean"
    # git keys are preserved verbatim in the uv-script's kwargs.
    assert spec.metrics[1].type.value == "uv-script"
    assert spec.metrics[1].kwargs["git_url"] == "ssh://x"
    assert spec.metrics[1].kwargs["script_path"] == "metrics/cost/cost.py"
    assert spec.extra_instruction_paths[0].path.name == "x.md"
