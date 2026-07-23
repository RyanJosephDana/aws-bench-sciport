"""Tests for the per-run deferred-deletion registry."""

from __future__ import annotations

import asyncio

from aws_bench.resource_management.deferred import (
    deferred_scope,
    deferred_snapshot,
    exclude_deferred,
    is_deferred,
    mark_deferred,
)
from aws_bench.utils.concurrent import interruptible_executor

_T = "AWS::Lambda::Function"


# -- inert outside a scope --


def test_noops_outside_scope():
    # mark is a no-op and the queries report nothing / pass through unchanged.
    mark_deferred(_T, "fn")
    assert not is_deferred(_T, "fn")
    assert deferred_snapshot() == frozenset()
    resources = {_T: [{"Identifier": "fn"}]}
    assert exclude_deferred(resources) == resources


# -- mark / query / exclude --


def test_mark_and_query_within_scope():
    with deferred_scope():
        mark_deferred(_T, "cf-edge")
        assert is_deferred(_T, "cf-edge")
        assert not is_deferred(_T, "other")
        assert deferred_snapshot() == frozenset({(_T, "cf-edge")})


def test_exclude_deferred_drops_only_marked_entries():
    with deferred_scope():
        mark_deferred(_T, "cf-edge")
        out = exclude_deferred(
            {
                _T: [{"Identifier": "cf-edge"}, {"Identifier": "keep"}],
                "AWS::S3::Bucket": [{"Identifier": "b"}],
            }
        )
    assert out == {
        _T: [{"Identifier": "keep"}],
        "AWS::S3::Bucket": [{"Identifier": "b"}],
    }


def test_exclude_deferred_drops_emptied_type():
    with deferred_scope():
        mark_deferred(_T, "only")
        out = exclude_deferred({_T: [{"Identifier": "only"}]})
    assert out == {}


def test_exclude_deferred_is_type_scoped():
    """A deferred (type, id) does not exclude a same-id resource of another type."""
    with deferred_scope():
        mark_deferred(_T, "shared-name")
        out = exclude_deferred({"AWS::S3::Bucket": [{"Identifier": "shared-name"}]})
    assert out == {"AWS::S3::Bucket": [{"Identifier": "shared-name"}]}


# -- scoping --


def test_scopes_are_isolated_and_reset_on_exit():
    with deferred_scope():
        mark_deferred(_T, "a")
        assert is_deferred(_T, "a")
    assert not is_deferred(_T, "a")
    with deferred_scope():
        assert not is_deferred(_T, "a")


# -- context propagation into worker threads --


def test_propagates_through_interruptible_executor():
    """A handler running in a context-copying worker mutates the same set."""
    with deferred_scope():
        with interruptible_executor(max_workers=2) as executor:
            list(executor.map(lambda name: mark_deferred(_T, name), ["fn-a", "fn-b"]))
        assert is_deferred(_T, "fn-a")
        assert is_deferred(_T, "fn-b")


def test_propagates_through_asyncio_to_thread():
    async def run() -> bool:
        with deferred_scope():
            await asyncio.to_thread(mark_deferred, _T, "fn")
            return is_deferred(_T, "fn")

    assert asyncio.run(run()) is True


def test_concurrent_tasks_get_isolated_sets():
    async def one(name: str) -> frozenset[tuple[str, str]]:
        with deferred_scope():
            await asyncio.sleep(0)  # interleave with the sibling task
            mark_deferred(_T, name)
            await asyncio.sleep(0)
            return deferred_snapshot()

    async def run():
        return await asyncio.gather(one("a"), one("b"))

    snap_a, snap_b = asyncio.run(run())
    assert snap_a == frozenset({(_T, "a")})
    assert snap_b == frozenset({(_T, "b")})
