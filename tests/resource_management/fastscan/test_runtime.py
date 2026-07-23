"""Tests for the fastscan collect() runtime helper (mocked boto clients)."""

from aws_bench.resource_management.fastscan.runtime import MAX_PAGES_PER_LISTER, collect


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _Client:
    def __init__(self, *, paginated=None, direct=None):
        self._paginated = paginated or {}
        self._direct = direct or {}

    def can_paginate(self, op):
        return op in self._paginated

    def get_paginator(self, op):
        return _Paginator(self._paginated[op])

    def __getattr__(self, name):
        if name in self._direct:
            return lambda **_k: self._direct[name]
        raise AttributeError(name)


def test_collect_single_key_with_id_field():
    c = _Client(paginated={"list_buckets": [{"Buckets": [{"BucketArn": "a"}, {"BucketArn": "b"}]}]})
    assert collect(c, "list_buckets", "Buckets", "BucketArn") == ["a", "b"]


def test_collect_nested_path_flattens():
    pages = [{"Reservations": [{"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}]}]
    c = _Client(paginated={"describe_instances": pages})
    got = collect(c, "describe_instances", "Reservations.Instances", "InstanceId")
    assert got == ["i-1", "i-2"]


def test_collect_id_field_none_item_is_id():
    c = _Client(paginated={"list_queues": [{"QueueUrls": ["u1", "u2"]}]})
    assert collect(c, "list_queues", "QueueUrls", None) == ["u1", "u2"]


def test_collect_status_filter():
    pages = [{"jobs": [{"a": "x", "s": "ON"}, {"a": "y", "s": "OFF"}]}]
    c = _Client(paginated={"list_jobs": pages})
    assert collect(c, "list_jobs", "jobs", "a", "s", ["ON"]) == ["x"]


def test_collect_nested_status_filter():
    """A dotted status_field (e.g. EC2 State.Name) is resolved through the nested dict."""
    pages = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": "i-live", "State": {"Name": "running"}},
                        {"InstanceId": "i-dead", "State": {"Name": "terminated"}},
                        {"InstanceId": "i-nostate"},
                    ]
                }
            ]
        }
    ]
    c = _Client(paginated={"describe_instances": pages})
    got = collect(
        c,
        "describe_instances",
        "Reservations.Instances",
        "InstanceId",
        "State.Name",
        ["pending", "running", "stopping", "stopped"],
    )
    # terminated is excluded; a missing nested state resolves to None and is also excluded.
    assert got == ["i-live"]


def test_collect_status_exclude_drops_only_listed_states():
    """status_exclude is a blocklist: only the listed states are dropped, the rest are kept."""
    pages = [
        {
            "DBClusters": [
                {"DBClusterArn": "a", "Status": "available"},
                {"DBClusterArn": "b", "Status": "deleting"},
                {"DBClusterArn": "c", "Status": "failed"},
            ]
        }
    ]
    c = _Client(paginated={"describe_db_clusters": pages})
    got = collect(
        c,
        "describe_db_clusters",
        "DBClusters",
        "DBClusterArn",
        status_field="Status",
        status_exclude=["deleting"],
    )
    # only the actively-deleting cluster is dropped; available AND failed still surface.
    assert got == ["a", "c"]


def test_collect_status_exclude_keeps_missing_status():
    """A missing/None status is KEPT under a blocklist (unlike an allowlist, which drops it).

    This is the safe default for orphan detection: an unknown-status resource must still surface.
    """
    pages = [{"X": [{"Id": "known", "S": "deleting"}, {"Id": "nostate"}]}]
    c = _Client(paginated={"list_x": pages})
    got = collect(c, "list_x", "X", "Id", status_field="S", status_exclude=["deleting"])
    assert got == ["nostate"]


def test_collect_status_filter_and_exclude_combined():
    """When both are set an item must be in the allowlist AND not in the blocklist."""
    pages = [
        {
            "X": [
                {"Id": "keep", "S": "active"},
                {"Id": "excluded", "S": "deleting"},
                {"Id": "not-allowed", "S": "other"},
            ]
        }
    ]
    c = _Client(paginated={"list_x": pages})
    got = collect(
        c,
        "list_x",
        "X",
        "Id",
        status_field="S",
        status_filter=["active", "deleting"],
        status_exclude=["deleting"],
    )
    assert got == ["keep"]


def test_collect_non_paginatable_single_call():
    c = _Client(direct={"describe_images": {"Images": [{"Arn": "x"}]}})
    assert collect(c, "describe_images", "Images", "Arn") == ["x"]


def test_collect_coerces_non_str():
    c = _Client(paginated={"list_x": [{"X": [{"Id": 123}]}]})
    assert collect(c, "list_x", "X", "Id") == ["123"]


def test_collect_caps_pages_at_max():
    """A lister pulling more than MAX_PAGES_PER_LISTER pages is truncated, not run unbounded.

    Bounds a single in-flight lister's drain at the sweep deadline so it can't push the scan
    past its wall-clock budget (in-Lambda, past the function timeout).
    """
    over = MAX_PAGES_PER_LISTER + 5
    pages = [{"X": [{"Id": f"id-{i}"}]} for i in range(over)]
    c = _Client(paginated={"list_x": pages})

    got = collect(c, "list_x", "X", "Id")

    # Exactly one id per page, capped at the page limit.
    assert got == [f"id-{i}" for i in range(MAX_PAGES_PER_LISTER)]
