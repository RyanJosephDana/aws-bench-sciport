"""Unit tests for the --metric value parser."""

import pytest

from aws_bench.cli.metrics import parse_metric


def test_parse_metric_bare_type():
    m = parse_metric("mean")
    assert m.type.value == "mean"
    assert m.kwargs == {}


def test_parse_metric_uv_script_with_kwargs():
    m = parse_metric("uv-script:script_path=./m.py")
    assert m.type.value == "uv-script"
    assert m.kwargs == {"script_path": "./m.py"}


def test_parse_metric_unknown_type_raises():
    with pytest.raises(ValueError):
        parse_metric("not-a-metric")
