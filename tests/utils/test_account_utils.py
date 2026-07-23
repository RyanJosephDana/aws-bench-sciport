"""Tests for aws_bench.utils.account_utils."""

from aws_bench.account_management.models import ScenarioAccount
from aws_bench.utils.account_utils import filter_by_scenario_name, group_accounts_by_scenario


def test_group_accounts_by_scenario():
    """Groups accounts by scenario_name."""
    accounts = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="scenario-a",
            account_tag="SECONDARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="333333333333",
            email="test3@example.com",
            scenario_name="scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = group_accounts_by_scenario(accounts)

    assert len(result) == 2
    assert "scenario-a" in result
    assert "scenario-b" in result
    assert len(result["scenario-a"]) == 2
    assert len(result["scenario-b"]) == 1
    assert result["scenario-a"][0].account_id == "111111111111"
    assert result["scenario-a"][1].account_id == "222222222222"
    assert result["scenario-b"][0].account_id == "333333333333"


def test_group_accounts_by_scenario_empty():
    """Returns empty dict for empty input."""
    assert group_accounts_by_scenario([]) == {}


def test_filter_by_scenario_name_no_filters():
    """Returns all items when no filters specified."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name)

    assert len(result) == 2
    assert result == items


def test_filter_by_scenario_name_include_exact():
    """Include filter with exact match."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, include=["scenario-a"])

    assert len(result) == 1
    assert result[0].scenario_name == "scenario-a"


def test_filter_by_scenario_name_include_glob():
    """Include filter with fnmatch glob pattern."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="333333333333",
            email="test3@example.com",
            scenario_name="other-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, include=["scenario-*"])

    assert len(result) == 2
    assert result[0].scenario_name == "scenario-a"
    assert result[1].scenario_name == "scenario-b"


def test_filter_by_scenario_name_exclude_exact():
    """Exclude filter with exact match."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, exclude=["scenario-a"])

    assert len(result) == 1
    assert result[0].scenario_name == "scenario-b"


def test_filter_by_scenario_name_exclude_glob():
    """Exclude filter with fnmatch glob pattern."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="test-scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="prod-scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="333333333333",
            email="test3@example.com",
            scenario_name="test-scenario-c",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, exclude=["test-*"])

    assert len(result) == 1
    assert result[0].scenario_name == "prod-scenario-b"


def test_filter_by_scenario_name_include_then_exclude():
    """Include and exclude both specified - include gates first, exclude removes."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="scenario-b",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="333333333333",
            email="test3@example.com",
            scenario_name="other-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(
        items, lambda x: x.scenario_name, include=["scenario-*"], exclude=["scenario-b"]
    )

    assert len(result) == 1
    assert result[0].scenario_name == "scenario-a"


def test_filter_by_scenario_name_empty_list():
    """Returns empty list for empty input."""
    items: list[ScenarioAccount] = []
    result = filter_by_scenario_name(items, lambda x: x.scenario_name, include=["scenario-*"])

    assert result == []


def test_filter_by_scenario_name_no_match():
    """Returns empty list when no items match include filter."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="scenario-a",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, include=["other-*"])

    assert result == []


def test_filter_by_scenario_name_multiple_include_patterns():
    """Multiple include patterns - item kept if any matches."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="prod-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="test-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="333333333333",
            email="test3@example.com",
            scenario_name="dev-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, include=["prod-*", "test-*"])

    assert len(result) == 2
    assert result[0].scenario_name == "prod-scenario"
    assert result[1].scenario_name == "test-scenario"


def test_filter_by_scenario_name_multiple_exclude_patterns():
    """Multiple exclude patterns - item dropped if any matches."""
    items = [
        ScenarioAccount(
            account_id="111111111111",
            email="test1@example.com",
            scenario_name="prod-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="222222222222",
            email="test2@example.com",
            scenario_name="test-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
        ScenarioAccount(
            account_id="333333333333",
            email="test3@example.com",
            scenario_name="dev-scenario",
            account_tag="PRIMARY",
            status="ACTIVE",
        ),
    ]

    result = filter_by_scenario_name(items, lambda x: x.scenario_name, exclude=["prod-*", "test-*"])

    assert len(result) == 1
    assert result[0].scenario_name == "dev-scenario"
