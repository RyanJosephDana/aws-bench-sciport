"""Tests for the Bedrock Knowledge Base cleanup handler."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.bedrock import (
    _DELETE_REISSUE_MAX,
    _delete,
    _prepare,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_KB = "AIH9VG6DII"
# An AWS-created KB execution role (what the guard in _ensure_role_can_teardown
# permits granting to); a name NOT matching this prefix must be left untouched.
_KB_EXEC_ROLE = "AmazonBedrockExecutionRoleForKnowledgeBase_test"


def _resource() -> Resource:
    return Resource(type="AWS::Bedrock::KnowledgeBase", identifier=_KB)


def _session_with_data_sources(ds_ids: list[str]) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Session whose ``client(name)`` routes to distinct bedrock-agent / iam mocks."""
    session = MagicMock()
    ba = MagicMock()
    iam = MagicMock()

    def _client(name, *args, **kwargs):
        return iam if name == "iam" else ba

    session.client.side_effect = _client
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"dataSourceSummaries": [{"dataSourceId": ds} for ds in ds_ids]}
    ]
    ba.get_paginator.return_value = paginator
    ba.get_knowledge_base.return_value = {
        "knowledgeBase": {"roleArn": f"arn:aws:iam::x:role/{_KB_EXEC_ROLE}"}
    }
    return session, ba, iam


# -- _prepare --


def test_prepare_deletes_all_data_sources():
    session, ba, iam = _session_with_data_sources(["ds1", "ds2"])
    result = _prepare(_resource(), session)
    assert ba.delete_data_source.call_count == 2
    ba.delete_data_source.assert_any_call(knowledgeBaseId=_KB, dataSourceId="ds1")
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_grants_teardown_policy_to_execution_role():
    """Prepare attaches a teardown policy to the KB's execution role.

    An agent-created role may lack the actions Bedrock needs to tear down the
    KB's backing store (e.g. ``sqlworkbench:*`` for a Redshift KB), which wedges
    the async delete in ``DELETE_UNSUCCESSFUL``; prepare grants them first.
    """
    session, ba, iam = _session_with_data_sources([])
    _prepare(_resource(), session)
    iam.put_role_policy.assert_called_once()
    kwargs = iam.put_role_policy.call_args.kwargs
    assert kwargs["RoleName"] == _KB_EXEC_ROLE
    assert "sqlworkbench:*" in kwargs["PolicyDocument"]


def test_prepare_does_not_grant_to_non_kb_exec_role():
    """The teardown grant is NEVER attached to a baseline/shared/non-KB role.

    Guards against contaminating a role a KB points at that reset will not delete
    (e.g. a CDK-authored ``BedrockDistillationRole``): attaching a broad policy to
    it would outlive the reset and leak into later tasks.
    """
    session, ba, iam = _session_with_data_sources([])
    ba.get_knowledge_base.return_value = {
        "knowledgeBase": {"roleArn": "arn:aws:iam::x:role/BedrockDistillationRole"}
    }
    result = _prepare(_resource(), session)
    iam.put_role_policy.assert_not_called()
    # Prepare still succeeds (data-source deletion is what matters here).
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_tolerates_missing_execution_role():
    """A missing execution role does not fail prepare (best-effort grant)."""
    session, ba, iam = _session_with_data_sources([])
    iam.put_role_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity"}}, "PutRolePolicy"
    )
    result = _prepare(_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_no_data_sources_succeeds():
    session, ba, iam = _session_with_data_sources([])
    result = _prepare(_resource(), session)
    ba.delete_data_source.assert_not_called()
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_skips_when_kb_not_found():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "ListDataSources"
    )
    result = _prepare(_resource(), session)
    assert result.status == HandlerStatus.SKIPPED


def test_prepare_fails_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListDataSources"
    )
    result = _prepare(_resource(), session)
    assert result.status == HandlerStatus.FAILED


# -- _delete --


def _not_found(op: str = "GetKnowledgeBase") -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException"}}, op)


def test_delete_success_waits_for_terminal_deletion(monkeypatch):
    monkeypatch.setattr(
        "aws_bench.resource_management.cleanup.handlers.bedrock._WAITER_INTERVAL_SEC", 0
    )
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    # KB is still present on the first poll, gone on the second: the handler must
    # WAIT for terminal deletion (get_knowledge_base -> not-found) before success,
    # so a still-DELETING KB never races the post-run reset verification.
    client.get_knowledge_base.side_effect = [{"knowledgeBase": {}}, _not_found()]
    result = _delete(_resource(), session)
    client.delete_knowledge_base.assert_called_once_with(knowledgeBaseId=_KB)
    assert client.get_knowledge_base.call_count >= 2
    assert result.status == HandlerStatus.SUCCESS


def test_delete_already_gone_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_knowledge_base.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DeleteKnowledgeBase"
    )
    result = _delete(_resource(), session)
    # Already gone at the delete call: no terminal-deletion poll needed.
    client.get_knowledge_base.assert_not_called()
    assert result.status == HandlerStatus.SUCCESS


def test_delete_waiter_gone_immediately_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_knowledge_base.side_effect = _not_found()
    result = _delete(_resource(), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_failure_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_knowledge_base.side_effect = ClientError(
        {"Error": {"Code": "ConflictException"}}, "DeleteKnowledgeBase"
    )
    result = _delete(_resource(), session)
    # A failed delete short-circuits before the terminal-deletion poll.
    client.get_knowledge_base.assert_not_called()
    assert result.status == HandlerStatus.FAILED


def test_delete_fails_when_kb_never_reaches_terminal_deletion(monkeypatch):
    """A KB stuck in DELETING past the bounded wait maps to FAILED, not a hang."""
    # Collapse the bounded poll to a single immediate check so the test is fast.
    monkeypatch.setattr(
        "aws_bench.resource_management.cleanup.handlers.bedrock._WAITER_TIMEOUT_SEC", 0
    )
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    # get_knowledge_base always succeeds -> KB never disappears -> timeout path.
    client.get_knowledge_base.return_value = {"knowledgeBase": {"status": "DELETING"}}
    result = _delete(_resource(), session)
    assert result.status == HandlerStatus.FAILED


def test_delete_reissues_on_delete_unsuccessful_then_succeeds(monkeypatch):
    """A DELETE_UNSUCCESSFUL KB is re-issued, then succeeds once IAM propagates.

    Regression for the live IAM race: the teardown policy granted in prepare had
    not propagated when Bedrock's first delete assumed the role, so the KB went
    terminally DELETE_UNSUCCESSFUL. Bedrock never retries on its own, so the
    handler must re-issue delete_knowledge_base; the second attempt (policy now
    effective) finalizes, and the handler reports SUCCESS.
    """
    monkeypatch.setattr(
        "aws_bench.resource_management.cleanup.handlers.bedrock._WAITER_INTERVAL_SEC", 0
    )
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    # First poll: terminal DELETE_UNSUCCESSFUL (triggers a re-issue); second poll:
    # gone. The re-issued delete_knowledge_base is the second call to that op.
    client.get_knowledge_base.side_effect = [
        {"knowledgeBase": {"status": "DELETE_UNSUCCESSFUL"}},
        _not_found(),
    ]
    result = _delete(_resource(), session)
    # Initial delete + one re-issue after the DELETE_UNSUCCESSFUL observation.
    assert client.delete_knowledge_base.call_count == 2
    assert result.status == HandlerStatus.SUCCESS


def test_delete_fails_when_reissues_exhausted(monkeypatch):
    """A KB stuck in DELETE_UNSUCCESSFUL re-issues up to the cap, then maps to FAILED.

    Drives the re-issue budget for real: interval 0 (no sleeps) with a small
    positive timeout so ``wait_until`` actually calls the predicate, and the KB
    stays DELETE_UNSUCCESSFUL forever. Asserts the delete is issued exactly
    ``_DELETE_REISSUE_MAX`` extra times beyond the initial one and the cap holds
    (a regression that unbounded or removed the cap would fail this).
    """
    monkeypatch.setattr(
        "aws_bench.resource_management.cleanup.handlers.bedrock._WAITER_INTERVAL_SEC", 0
    )
    monkeypatch.setattr(
        "aws_bench.resource_management.cleanup.handlers.bedrock._WAITER_TIMEOUT_SEC", 1
    )
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_knowledge_base.return_value = {"knowledgeBase": {"status": "DELETE_UNSUCCESSFUL"}}
    result = _delete(_resource(), session)
    # 1 initial delete (via service_delete) + _DELETE_REISSUE_MAX re-issues; the cap
    # holds (the poll keeps observing DELETE_UNSUCCESSFUL but stops re-issuing).
    assert client.delete_knowledge_base.call_count == _DELETE_REISSUE_MAX + 1
    assert result.status == HandlerStatus.FAILED
