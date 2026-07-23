"""Tests for aws_bench.resource_management.utils.file_io."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aws_bench.resource_management.utils.file_io import write_json


def test_creates_parent_dirs_and_writes_valid_json(tmp_path):
    path = tmp_path / "sub" / "nested" / "manifest.json"
    data = {"stacks": ["a", "b"], "count": 2}
    write_json(data, path)
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == data


def test_does_not_raise_on_unwritable_path():
    path = Path("/dev/null/impossible/manifest.json")
    with pytest.raises(OSError):
        write_json({"key": "value"}, path)


def test_overwrites_existing_file(tmp_path):
    path = tmp_path / "manifest.json"
    write_json({"version": 1}, path)
    write_json({"version": 2}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 2}
