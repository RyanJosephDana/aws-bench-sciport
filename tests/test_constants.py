"""Tests for ``aws_bench.constants``."""

from pathlib import Path

from aws_bench.constants import (
    CACHE_DIR,
    DEFAULT_REGISTRY_URL,
    INSTRUCTION_CACHE_DIR,
    METRIC_CACHE_DIR,
    OUTPUT_DIR,
    SCENARIO_CACHE_DIR,
)

# ── Cache paths ──


def test_cache_dir_is_under_output_dir():
    assert CACHE_DIR == OUTPUT_DIR / "cache"


def test_scenario_cache_dir_is_under_cache_dir():
    assert SCENARIO_CACHE_DIR == CACHE_DIR / "scenarios"


def test_scenario_cache_dir_is_under_aws_bench_home():
    # End-to-end: ~/.aws-bench/cache/scenarios/
    assert SCENARIO_CACHE_DIR == Path.home() / ".aws-bench" / "cache" / "scenarios"


def test_metric_and_instruction_cache_dirs_are_under_cache_dir():
    assert METRIC_CACHE_DIR == CACHE_DIR / "metrics"
    assert INSTRUCTION_CACHE_DIR == CACHE_DIR / "extra_instructions"


# ── Default registry URL ──


def test_default_registry_url_is_a_string():
    # registry.json is hosted at a URL, not bundled with the wheel.
    assert isinstance(DEFAULT_REGISTRY_URL, str)
    assert DEFAULT_REGISTRY_URL  # non-empty


def test_default_registry_url_is_https():
    assert DEFAULT_REGISTRY_URL.startswith("https://")


def test_default_registry_url_points_to_registry_json():
    assert "registry.json" in DEFAULT_REGISTRY_URL
