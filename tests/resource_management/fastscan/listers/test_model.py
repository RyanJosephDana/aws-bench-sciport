"""Tests for the one Lister model and its data-defined ``run`` (paginate + walk + pull)."""

from unittest.mock import MagicMock

from aws_bench.resource_management.fastscan.listers.model import Lister


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


def _session(client):
    session = MagicMock()
    session.client.side_effect = lambda *_a, **_k: client
    return session


def test_lister_identity_and_optional_pin():
    lister = Lister("s3", "ListBuckets", lambda _s: [], cfn_type="AWS::S3::Bucket")
    assert (lister.service, lister.op, lister.cfn_type) == ("s3", "ListBuckets", "AWS::S3::Bucket")
    assert lister.cfn_type is None or isinstance(lister.cfn_type, str)
    assert Lister("s3", "ListBuckets", lambda _s: []).cfn_type is None


def test_from_row_paginates_and_pulls_id_field():
    lister = Lister.from_row(
        service="mq",
        op="ListBrokers",
        method="list_brokers",
        result_path="BrokerSummaries",
        id_field="BrokerArn",
        cfn_type="AWS::AmazonMQ::Broker",
    )
    client = _Client(paginated={"list_brokers": [{"BrokerSummaries": [{"BrokerArn": "a"}]}]})
    assert lister.run(_session(client)) == ["a"]
    assert lister.cfn_type == "AWS::AmazonMQ::Broker"


def test_from_row_bare_item_list_when_id_field_none():
    # A ``None`` id_field means the result items are bare id strings (the folded auto ``*``).
    lister = Lister.from_row(
        service="svc", op="ListThings", method="list_things", result_path="things", id_field=None
    )
    client = _Client(paginated={"list_things": [{"things": ["t1", "t2"]}]})
    assert lister.run(_session(client)) == ["t1", "t2"]


def test_from_row_coerces_non_string_ids():
    lister = Lister.from_row(
        service="svc", op="ListX", method="list_x", result_path="X", id_field="Id"
    )
    client = _Client(paginated={"list_x": [{"X": [{"Id": 7}]}]})
    assert lister.run(_session(client)) == ["7"]


def test_from_row_non_paginatable_single_call():
    lister = Lister.from_row(
        service="svc",
        op="DescribeThings",
        method="describe_things",
        result_path="things",
        id_field="Arn",
    )
    client = _Client(direct={"describe_things": {"things": [{"Arn": "z"}]}})
    assert lister.run(_session(client)) == ["z"]


def test_from_row_applies_status_filter():
    lister = Lister.from_row(
        service="backup",
        op="ListBackupJobs",
        method="list_backup_jobs",
        result_path="BackupJobs",
        id_field="BackupJobId",
        status_field="State",
        status_filter=("RUNNING",),
    )
    client = _Client(
        paginated={
            "list_backup_jobs": [
                {
                    "BackupJobs": [
                        {"BackupJobId": "live", "State": "RUNNING"},
                        {"BackupJobId": "done", "State": "COMPLETED"},
                    ]
                }
            ]
        }
    )
    assert lister.run(_session(client)) == ["live"]


def test_from_row_applies_status_exclude():
    """A row's status_exclude threads through to collect() as a blocklist (keeps missing status)."""
    lister = Lister.from_row(
        service="rds",
        op="DescribeDBClusters",
        method="describe_db_clusters",
        result_path="DBClusters",
        id_field="DBClusterArn",
        status_field="Status",
        status_exclude=("deleting",),
    )
    client = _Client(
        paginated={
            "describe_db_clusters": [
                {
                    "DBClusters": [
                        {"DBClusterArn": "live", "Status": "available"},
                        {"DBClusterArn": "gone", "Status": "deleting"},
                        {"DBClusterArn": "stuck", "Status": "failed"},
                        {"DBClusterArn": "unknown"},
                    ]
                }
            ]
        }
    )
    # deleting is dropped; available/failed/unknown-status all surface as orphan candidates.
    assert lister.run(_session(client)) == ["live", "stuck", "unknown"]
