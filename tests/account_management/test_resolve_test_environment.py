"""resolve_test_environment: single OU scan + filtering/gating."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aws_bench.account_management.constants import SCENARIO_ACCOUNT_TAG_KEY
from aws_bench.account_management.exceptions import AccountResolutionError
from aws_bench.account_management.manager import AccountManager
from aws_bench.account_management.models import OrgInfo


def _org() -> OrgInfo:
    return OrgInfo(
        org_id="o-abc",
        root_id="r-1",
        management_account_id="111111111111",
        management_account_email="mgmt@example.com",
    )


@pytest.fixture
def manager():
    with patch("aws_bench.account_management.manager.OrganizationsClient") as cls:
        cls.return_value = MagicMock()
        yield AccountManager()


def _wire(manager, accounts, tags_by_id):
    manager._org.get_org_info.return_value = _org()
    manager._org.find_ou_by_name.return_value = "ou-1"
    manager._org.list_accounts_in_ou.return_value = accounts
    manager._org.get_tags.side_effect = lambda aid: tags_by_id.get(aid, {})


def test_resolve_no_filter_returns_all_tagged_sorted(manager):
    _wire(
        manager,
        [
            {"Id": "222", "Email": "b@x.com", "Status": "ACTIVE"},
            {"Id": "111", "Email": "a@x.com", "Status": "ACTIVE"},
        ],
        {
            "111": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-b/PRIMARY"},
            "222": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-a/PRIMARY"},
        },
    )
    env = manager.resolve_test_environment("my-env")
    assert list(env.accounts) == ["lambda-a", "lambda-b"]
    assert env.ou_id == "ou-1"
    assert env.ou_name == "my-env"


def test_resolve_with_required_filters_to_named_scenarios(manager):
    _wire(
        manager,
        [
            {"Id": "111", "Email": "a@x.com", "Status": "ACTIVE"},
            {"Id": "222", "Email": "b@x.com", "Status": "ACTIVE"},
        ],
        {
            "111": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-a/PRIMARY"},
            "222": {SCENARIO_ACCOUNT_TAG_KEY: "other/PRIMARY"},
        },
    )
    env = manager.resolve_test_environment("my-env", {"lambda-a": {"PRIMARY"}})
    assert list(env.accounts) == ["lambda-a"]


def test_resolve_raises_when_required_scenario_missing(manager):
    _wire(manager, [], {})
    with pytest.raises(AccountResolutionError):
        manager.resolve_test_environment("my-env", {"lambda-a": {"PRIMARY"}})


def test_resolve_raises_when_required_tag_missing(manager):
    _wire(
        manager,
        [{"Id": "111", "Email": "a@x.com", "Status": "ACTIVE"}],
        {"111": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-a/SECONDARY"}},
    )
    with pytest.raises(AccountResolutionError, match="PRIMARY"):
        manager.resolve_test_environment("my-env", {"lambda-a": {"PRIMARY"}})


def test_resolve_lists_accounts_once(manager):
    _wire(
        manager,
        [{"Id": "111", "Email": "a@x.com", "Status": "ACTIVE"}],
        {"111": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-a/PRIMARY"}},
    )
    manager.resolve_test_environment("my-env", {"lambda-a": {"PRIMARY"}})
    assert manager._org.list_accounts_in_ou.call_count == 1


def test_mappings_projection_matches_required(manager):
    _wire(
        manager,
        [{"Id": "111", "Email": "a@x.com", "Status": "ACTIVE"}],
        {"111": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-a/PRIMARY"}},
    )
    env = manager.resolve_test_environment("my-env", {"lambda-a": {"PRIMARY"}})
    assert env.to_scenario_account_mappings() == {"lambda-a": {"PRIMARY": "111"}}


def test_projection_list_returns_all_tagged(manager):
    _wire(
        manager,
        [{"Id": "111", "Email": "a@x.com", "Status": "ACTIVE"}],
        {"111": {SCENARIO_ACCOUNT_TAG_KEY: "lambda-a/PRIMARY"}},
    )
    out = manager.list_scenario_accounts("my-env")
    assert len(out) == 1
    assert out[0].account_id == "111"
