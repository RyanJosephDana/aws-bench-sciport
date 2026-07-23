"""Tests for aws_bench.resource_management.cleanup.resource_sweeper."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aws_bench.resource_management.cleanup.resource_sweeper import ResourceSweeper


@pytest.mark.asyncio
async def test_sweep_builds_stack_resources_and_calls_cleaner():
    session = MagicMock()
    resources_by_type = {"AWS::S3::Bucket": [{"Identifier": "b1"}, {"Identifier": "b2"}]}
    with patch("aws_bench.resource_management.cleanup.resource_sweeper.ResourceCleaner") as RC:
        RC.return_value.cleanup = AsyncMock(return_value={})
        failures = await ResourceSweeper(session).delete(resources_by_type)
    assert failures == {}
    passed = RC.return_value.cleanup.call_args.args[0]
    assert {r.physical_id for r in passed} == {"b1", "b2"}
    assert all(r.resource_type == "AWS::S3::Bucket" for r in passed)


@pytest.mark.asyncio
async def test_sweep_noop_on_empty():
    with patch("aws_bench.resource_management.cleanup.resource_sweeper.ResourceCleaner") as RC:
        failures = await ResourceSweeper(MagicMock()).delete({})
    assert failures == {}
    RC.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_skips_catch_all_types(caplog):
    """A synthetic ``AWS::<svc>::*`` bucket type is skipped and warned, not deleted.

    Fast-scan emits catch-all bucket types that CCAPI cannot delete; passing them
    to the cleaner is a guaranteed no-op, so skip them but warn (naming them) so a
    real orphan bucketed under a catch-all still surfaces.
    """
    resources_by_type = {
        "AWS::EC2::*": [{"Identifier": "some-uncleanable"}],
        "AWS::S3::Bucket": [{"Identifier": "b1"}],
    }
    with (
        patch("aws_bench.resource_management.cleanup.resource_sweeper.ResourceCleaner") as RC,
        caplog.at_level(logging.WARNING),
    ):
        RC.return_value.cleanup = AsyncMock(return_value={})
        await ResourceSweeper(MagicMock()).delete(resources_by_type)
    passed = RC.return_value.cleanup.call_args.args[0]
    # Only the real deletable type reaches the cleaner.
    assert {r.resource_type for r in passed} == {"AWS::S3::Bucket"}
    assert "AWS::EC2::*" in caplog.text


@pytest.mark.asyncio
async def test_sweep_noop_when_only_catch_all_types():
    """If every entry is a catch-all, nothing reaches the cleaner."""
    with patch("aws_bench.resource_management.cleanup.resource_sweeper.ResourceCleaner") as RC:
        failures = await ResourceSweeper(MagicMock()).delete({"AWS::EC2::*": [{"Identifier": "x"}]})
    assert failures == {}
    RC.assert_not_called()
