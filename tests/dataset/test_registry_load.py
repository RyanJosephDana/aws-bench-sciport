"""Tests for AwsBenchRegistry.from_url TTL caching."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from aws_bench.dataset.exceptions import RegistryValidationError
from aws_bench.dataset.registry import AwsBenchRegistry

VALID_REGISTRY = [
    {
        "name": "test-dataset",
        "version": "1.0.0",
        "description": "test",
        "tasks": [],
        "scenarios": [],
    }
]


def test_from_url_writes_cache_and_returns_registry(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("aws_bench.dataset.registry.REGISTRY_CACHE_DIR", cache_dir)
    fake_response = MagicMock()
    fake_response.text = json.dumps(VALID_REGISTRY)
    fake_response.raise_for_status = lambda: None

    with patch("requests.Session.get", return_value=fake_response) as mock_get:
        reg = AwsBenchRegistry.from_url("https://example.com/registry.json")
    assert mock_get.call_count == 1
    assert reg.url == "https://example.com/registry.json"
    assert len(reg.datasets) == 1
    assert any(cache_dir.glob("registry-*.json"))


def test_from_url_uses_cache_within_ttl(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr("aws_bench.dataset.registry.REGISTRY_CACHE_DIR", cache_dir)
    url = "https://example.com/registry.json"
    h = hashlib.sha256(url.encode()).hexdigest()[:12]
    cache_file = cache_dir / f"registry-{h}.json"
    cache_file.write_text(json.dumps(VALID_REGISTRY))

    with patch("requests.Session.get") as mock_get:
        reg = AwsBenchRegistry.from_url(url)
    mock_get.assert_not_called()
    assert reg.url == url


def test_from_url_raises_on_request_failure(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("aws_bench.dataset.registry.REGISTRY_CACHE_DIR", cache_dir)
    with patch(
        "requests.Session.get",
        side_effect=requests.RequestException("network down"),
    ):
        with pytest.raises(RegistryValidationError, match="Failed to fetch"):
            AwsBenchRegistry.from_url("https://example.com/registry.json")


def test_from_url_does_not_cache_invalid_response(tmp_path, monkeypatch):
    """A bad response must not poison the cache for the next hour."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("aws_bench.dataset.registry.REGISTRY_CACHE_DIR", cache_dir)
    bad_response = MagicMock()
    bad_response.text = "not json"
    bad_response.raise_for_status = lambda: None

    with patch("requests.Session.get", return_value=bad_response):
        with pytest.raises(RegistryValidationError):
            AwsBenchRegistry.from_url("https://example.com/registry.json")
    assert not list(cache_dir.glob("registry-*.json"))
