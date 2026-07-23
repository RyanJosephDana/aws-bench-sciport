"""Tests for the simple listers: the big data table turned into Listers by the assembler."""

from typing import Any

import boto3

from aws_bench.resource_management.fastscan.listers.lister_registry import _simple_listers
from aws_bench.resource_management.fastscan.listers.simple_listers import SIMPLE_LISTERS
from aws_bench.resource_management.fastscan.runtime import collect


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _FakeClient:
    """Minimal paginating client stub for driving a lister's config through ``collect``."""

    def __init__(self, paginated):
        self._paginated = paginated

    def can_paginate(self, op):
        return op in self._paginated

    def get_paginator(self, op):
        return _Paginator(self._paginated[op])


def test_simple_listers_cover_every_row_and_are_uniquely_keyed():
    listers = _simple_listers()
    assert len(listers) == len(SIMPLE_LISTERS)
    assert len(listers) > 1000  # the bulk of the discovery set lives in the simple listers
    keys = [(lister.service, lister.op) for lister in listers]
    assert len(keys) == len(set(keys)), "duplicate (service, op) simple listers"
    assert all(callable(lister.run) for lister in listers)


# docdb reuses the RDS-shaped describe APIs to emit BOTH an AWS::RDS::* and an AWS::DocDB::* row
# from one method — a legitimate same-method pair (two distinct CFN types), unlike the accidental
# PascalCase/snake_case duplicates that scanned the same API twice for the SAME type. Allowlisted
# per exact (service, method); docdb:describe_event_subscriptions is deliberately NOT here — both
# its rows pinned AWS::DocDB::EventSubscription, so it was a real dup and was deduped.
_ALLOWED_SAME_METHOD_KEYS = {
    ("docdb", "describe_db_cluster_parameter_groups"),
    ("docdb", "describe_db_clusters"),
    ("docdb", "describe_db_instances"),
    ("docdb", "describe_db_subnet_groups"),
}


def test_no_accidental_same_method_duplicates():
    """Two listers on one (service, method) scan the same API twice per region.

    The (service, op) guard above misses these when the ops differ only by casing
    (``ListFlows`` vs ``list_flows``). Guard on (service, method) too, allowing only the
    known dual-type docdb rows (each pinning a distinct CFN type).
    """
    seen: dict[tuple[str, str], int] = {}
    for e in SIMPLE_LISTERS:
        seen[(e.service, e.method)] = seen.get((e.service, e.method), 0) + 1
    offenders = sorted(k for k, n in seen.items() if n > 1 and k not in _ALLOWED_SAME_METHOD_KEYS)
    assert not offenders, f"same boto3 method scanned by 2+ listers (dedup them): {offenders}"


def test_result_path_top_segment_exists_in_the_botocore_output_shape():
    """Each lister's result_path must start at a real member of the op's output shape.

    ``walk_path`` resolves result_path case-sensitively off the response dict, so a wrong-case
    top segment (e.g. ``addonInstances`` when boto3 returns ``AddonInstances``) silently yields
    zero resources — a false-clean that the empty/failed channels can't catch. Validating only
    the TOP segment against botocore avoids false positives on legitimate nested paths
    (``Reservations.Instances``) whose deeper segments this check doesn't touch.
    """
    clients: dict[str, Any] = {}

    def client(service: str) -> Any:
        if service not in clients:
            clients[service] = boto3.client(service, region_name="us-east-1")
        return clients[service]

    offenders = []
    for entry in SIMPLE_LISTERS:
        c = client(entry.service)
        api = c.meta.method_to_api_mapping.get(entry.method)
        if not api:
            # Forward-looking lister for an API not yet in the installed botocore; skip.
            continue
        output = c.meta.service_model.operation_model(api).output_shape
        if output is None:
            continue
        top = entry.result_path.split(".")[0]
        if top not in output.members:
            offenders.append(
                f"{entry.service}:{entry.op} result_path top {top!r} "
                f"not in {sorted(output.members)}"
            )
    assert not offenders, "result_path top segment not in botocore output shape:\n" + "\n".join(
        offenders
    )


def test_id_field_exists_on_the_botocore_element_shape():
    """A flat-path lister's id_field must be a real field of the response element.

    ``collect`` pulls ``item.get(id_field)``; a field that does not exist yields ``None`` and the
    resource is dropped — silent under-enumeration. Checked only for flat (non-dotted) result
    paths, where the element is ``output.members[top].member``; nested paths reach the element
    through intermediate keys this check does not resolve. (id_field=None means bare-string items.)
    """
    clients: dict[str, Any] = {}

    def client(service: str) -> Any:
        if service not in clients:
            clients[service] = boto3.client(service, region_name="us-east-1")
        return clients[service]

    offenders = []
    for entry in SIMPLE_LISTERS:
        if entry.id_field is None or "." in entry.result_path:
            continue
        c = client(entry.service)
        api = c.meta.method_to_api_mapping.get(entry.method)
        if not api:
            # Forward-looking lister for an API not yet in the installed botocore; skip.
            continue
        output = c.meta.service_model.operation_model(api).output_shape
        if output is None:
            continue
        top = entry.result_path.split(".")[0]
        if top not in output.members:
            continue  # already reported by the result_path test
        shape = output.members[top]
        element = shape.member if shape.type_name == "list" else shape
        if not hasattr(element, "members"):
            continue  # a list of scalars; id_field on it is meaningless but harmless
        if entry.id_field not in element.members:
            offenders.append(
                f"{entry.service}:{entry.op} id_field {entry.id_field!r} "
                f"not in {sorted(element.members)}"
            )
    assert not offenders, "id_field not a field of the botocore element shape:\n" + "\n".join(
        offenders
    )


def test_simple_listers_carry_their_cfn_type_pin():
    # The boto3-introspected entries folded in from the former manifest pin an exact CFN type when
    # unambiguous; the hand-curated entries leave it unpinned (guessed by noun downstream).
    by_key = {(lister.service, lister.op): lister for lister in _simple_listers()}
    assert by_key[("acm", "list_certificates")].cfn_type == "AWS::CertificateManager::Certificate"
    # Phase 2 live-proved this method maps to a single CFN type (list_policies returns only managed
    # policies; primaryIdentifier PolicyArn == the emitted Arn), so it is now pinned.
    assert by_key[("iam", "list_policies")].cfn_type == "AWS::IAM::ManagedPolicy"
    assert by_key[("accessanalyzer", "ListAnalyzers")].cfn_type is None


def test_ambiguous_methods_stay_unpinned():
    # A method that genuinely feeds several distinct CFN sub-types must collapse to ONE unpinned
    # lister (one that falls to the service catch-all), never several pinned entries fighting over
    # the same key. (iam:list_policies and lakeformation:list_permissions were once assumed
    # ambiguous but were live-proven in Phase 2 to map to a single CFN type — see
    # test_simple_listers_carry_their_cfn_type_pin — so they are now pinned, not listed here.)
    by_key = {(lister.service, lister.op): lister for lister in _simple_listers()}
    for key in (
        (
            "servicediscovery",
            "list_namespaces",
        ),  # Http / PrivateDns / PublicDns namespace sub-types
        ("networkmanager", "list_attachments"),  # several attachment sub-types share one call
    ):
        assert key in by_key, f"{key} should be a single folded lister"
        assert by_key[key].cfn_type is None, f"{key} feeds multiple CFN types; must be unpinned"


def test_bare_item_entries_have_a_none_id_field():
    # A ``None`` id_field means the result items are bare id strings (the former manifest ``*``).
    none_id_entries = [entry for entry in SIMPLE_LISTERS if entry.id_field is None]
    assert none_id_entries, "expected some bare-string-list entries folded in"
    assert all(entry.method for entry in none_id_entries)


def test_ecr_listers_emit_the_ccapi_primary_identifier():
    # A fast-scan lister's id_field must be the resource's CloudControl (CCAPI) primary
    # identifier, because that value flows straight into deletion:
    # cleanup handlers pass it as ``repositoryName`` and the CCAPI fallback passes it as
    # ``Identifier``. Emitting an ARN (repositoryArn) or an unrelated ARN (credentialArn,
    # customRoleArn) makes every ECR resource undeletable. Primary identifiers per the CFN
    # registry: Repository/PublicRepository -> RepositoryName (boto ``repositoryName``);
    # PullThroughCacheRule -> EcrRepositoryPrefix (boto ``ecrRepositoryPrefix``);
    # RepositoryCreationTemplate -> Prefix (boto ``prefix``).
    by_key = {(entry.service, entry.op): entry for entry in SIMPLE_LISTERS}
    assert by_key[("ecr", "DescribeRepositories")].id_field == "repositoryName"
    assert by_key[("ecr-public", "DescribeRepositories")].id_field == "repositoryName"
    assert by_key[("ecr", "DescribePullThroughCacheRules")].id_field == "ecrRepositoryPrefix"
    assert by_key[("ecr", "DescribeRepositoryCreationTemplates")].id_field == "prefix"


def test_rds_dbinstance_listers_emit_the_ccapi_primary_identifier():
    # AWS::RDS::DBInstance's CloudControl primary identifier is DBInstanceIdentifier (a name),
    # not an ARN — the delete handler calls delete_db_instance(DBInstanceIdentifier=...) and the
    # CCAPI fallback passes it as Identifier; an ARN is rejected (live: InvalidParameterValue).
    # Both the rds and docdb DescribeDBInstances listers are pinned to AWS::RDS::DBInstance, so
    # both must emit the name. (Neptune's DescribeDBInstances pins AWS::Neptune::DBInstance and is
    # audited separately.)
    by_key = {(entry.service, entry.op): entry for entry in SIMPLE_LISTERS}
    rds = by_key[("rds", "DescribeDBInstances")]
    docdb = by_key[("docdb", "DescribeDBInstances")]
    assert rds.cfn_type == "AWS::RDS::DBInstance"
    assert docdb.cfn_type == "AWS::RDS::DBInstance"
    assert rds.id_field == "DBInstanceIdentifier"
    assert docdb.id_field == "DBInstanceIdentifier"


def test_db_cluster_parameter_group_listers_emit_the_name_not_the_arn():
    # Every cluster-PG lister must emit the group NAME, not the ARN: the ``default.`` filter
    # matches on the name prefix, so an ARN would misflag reserved default groups as orphans (RC4).
    # DescribeDBClusterParameterGroups returns DBClusterParameterGroupArn too, so an ARN regression
    # here is silent without this guard.
    by_key = {(entry.service, entry.op): entry for entry in SIMPLE_LISTERS}
    cluster_pg_listers = [
        entry
        for entry in SIMPLE_LISTERS
        if entry.cfn_type
        in (
            "AWS::RDS::DBClusterParameterGroup",
            "AWS::Neptune::DBClusterParameterGroup",
            "AWS::DocDB::DBClusterParameterGroup",
        )
    ]
    assert cluster_pg_listers, "expected cluster parameter-group listers to exist"
    for entry in cluster_pg_listers:
        assert entry.id_field == "DBClusterParameterGroupName", (
            f"{entry.service}:{entry.op} pins {entry.cfn_type} but emits {entry.id_field!r} "
            f"(must be the name, not the ARN)"
        )
    # The DocDB-specific type must exist and be reachable for the service-API delete handler.
    assert ("docdb", "describe_db_cluster_parameter_groups") in by_key


def test_emr_cluster_has_one_lister_emitting_the_ccapi_primary_identifier():
    # AWS::EMR::Cluster's CloudControl primary identifier is Id (e.g. j-XXXX), not ClusterArn —
    # EMR's delete path uses describe_cluster(ClusterId=...) and the CCAPI fallback passes it as
    # Identifier (both reject an ARN, live: ValidationException). Two listers once called the same
    # emr list_clusters API and both fed AWS::EMR::Cluster: a pinned "list_clusters" emitting the
    # correct Id, and a hand-curated "ListClusters" emitting ClusterArn (double-detecting every
    # cluster, the ARN copy undeletable). Only the correct pinned Id lister must remain.
    emr = [
        entry
        for entry in SIMPLE_LISTERS
        if entry.service == "emr" and entry.method == "list_clusters"
    ]
    assert len(emr) == 1, "expected exactly one emr list_clusters lister"
    assert emr[0].id_field == "Id"
    assert emr[0].cfn_type == "AWS::EMR::Cluster"
    # Only LIVE clusters are surfaced. Terminal states (TERMINATING / TERMINATED /
    # TERMINATED_WITH_ERRORS) are excluded: a terminated cluster is un-deletable and lingers in
    # list_clusters for ~2 months, and anything a failed teardown leaves behind is caught by that
    # resource type's own lister (EC2 instances, EBS volumes, security groups, placement group).
    # Surfacing a terminal cluster only produced false-positive orphans.
    assert emr[0].status_field == "Status.State"
    assert emr[0].status_filter == (
        "STARTING",
        "BOOTSTRAPPING",
        "RUNNING",
        "WAITING",
    )


def test_emr_cluster_lister_excludes_all_terminal_clusters():
    # Drive the real EMR lister config through the runtime: only LIVE clusters survive. Every
    # terminal state — TERMINATING, cleanly TERMINATED, and TERMINATED_WITH_ERRORS — is dropped.
    # A terminated cluster is un-deletable and lingers in list_clusters for ~2 months; any fleet a
    # failed teardown leaves behind is caught by the EC2 instance lister, not this one.
    emr = next(e for e in SIMPLE_LISTERS if e.service == "emr" and e.method == "list_clusters")
    pages = [
        {
            "Clusters": [
                {"Id": "j-RUNNING", "Status": {"State": "RUNNING"}},
                {"Id": "j-WAITING", "Status": {"State": "WAITING"}},
                {"Id": "j-STARTING", "Status": {"State": "STARTING"}},
                {"Id": "j-BOOTSTRAP", "Status": {"State": "BOOTSTRAPPING"}},
                {"Id": "j-TERMINATING", "Status": {"State": "TERMINATING"}},
                {"Id": "j-TERMINATED", "Status": {"State": "TERMINATED"}},
                {"Id": "j-TERMERR", "Status": {"State": "TERMINATED_WITH_ERRORS"}},
            ]
        }
    ]
    client = _FakeClient({emr.op: pages})
    assert emr.status_filter is not None
    got = collect(
        client,
        emr.op,
        emr.result_path,
        emr.id_field,
        emr.status_field,
        list(emr.status_filter),
    )
    assert got == ["j-RUNNING", "j-WAITING", "j-STARTING", "j-BOOTSTRAP"]


def test_autoscaling_group_lister_emits_the_ccapi_primary_identifier():
    # AWS::AutoScaling::AutoScalingGroup's CloudControl primary identifier is AutoScalingGroupName,
    # not the ARN. The custom cleanup handler passes resource.identifier as AutoScalingGroupName to
    # suspend_processes / update_auto_scaling_group, and the CCAPI fallback passes it as Identifier;
    # an ARN is rejected there. The lister must emit the name.
    by_key = {(entry.service, entry.op): entry for entry in SIMPLE_LISTERS}
    assert by_key[("autoscaling", "DescribeAutoScalingGroups")].id_field == "AutoScalingGroupName"


# Representative listers whose id_field was corrected to the CFN/CloudControl primary identifier so
# the emitted value is usable for deletion (delete_resource Identifier / custom-handler API). Each
# was live round-trip verified (create -> scan -> the lister emits exactly the CCAPI Identifier ->
# delete) against real AWS. Covers the swap patterns: ARN->name, ARN->id, and wrong-field->name.
_PRIMARY_ID_LISTER_FIELDS = [
    ("lambda", "ListFunctions", "FunctionName"),
    ("cloudwatch", "DescribeAlarms", "AlarmName"),
    ("backup", "ListBackupVaults", "BackupVaultName"),
    ("rds", "DescribeDBParameterGroups", "DBParameterGroupName"),
    ("athena", "ListWorkGroups", "Name"),
    ("sns", "ListTopics", "TopicArn"),  # ARN-primary type: correctly stays an ARN
    ("appflow", "ListFlows", "flowName"),
    ("kinesisanalyticsv2", "ListApplications", "ApplicationName"),
    ("appstream", "DescribeImageBuilders", "Name"),
    # AWS::Kinesis::Stream primary id is the name (Ref → name); the ARN fails CCAPI with
    # HandlerInternalFailureException.
    ("kinesis", "ListStreams", "StreamName"),
    # Found by the live all-scenario cross-check: these emitted an ARN/wrong field where the CCAPI
    # primaryIdentifier is a bare id, so a detected orphan could not be CCAPI-deleted.
    ("ec2", "DescribeTransitGateways", "TransitGatewayId"),  # primary /properties/Id (bare tgw-…)
    ("ec2", "DescribeInstanceConnectEndpoints", "InstanceConnectEndpointId"),  # primary Id (eice-…)
    ("route53resolver", "list_resolver_query_log_configs", "Id"),  # primary Id (rqlc-…), not Arn
    ("appsync", "ListGraphqlApis", "apiId"),  # primary /properties/ApiId, not the arn
    # TenantDatabase delete handler filters by tenant-database-resource-id (≤63 chars); the ~78-char
    # ARN is rejected by that filter before the handler's ARN fallback runs. Emit the resource id.
    ("rds", "DescribeTenantDatabases", "TenantDatabaseResourceId"),
]


def test_corrected_listers_emit_the_ccapi_primary_identifier():
    # A lister's id_field must yield the CloudControl primary identifier — the value used for
    # deletion. Emitting an ARN where the primary id is a name (or an unrelated field) leaves the
    # resource undeletable. These were mass-corrected and live-verified; guard against regression.
    by_key = {(entry.service, entry.op): entry for entry in SIMPLE_LISTERS}
    for service, op, expected in _PRIMARY_ID_LISTER_FIELDS:
        entry = by_key.get((service, op))
        assert entry is not None, f"{service}:{op} lister missing"
        assert entry.id_field == expected, (
            f"{service}:{op} id_field={entry.id_field!r} != {expected!r}"
        )


def test_db_cluster_listers_emit_the_identifier_name_not_arn():
    # RDS/DocDB DBCluster delete goes through a custom handler that calls
    # delete_db_cluster(DBClusterIdentifier=<emitted>). A real cluster ARN is rejected there with
    # InvalidParameterValue (live-proven), so every DescribeDBClusters lister must emit the bare
    # DBClusterIdentifier NAME (the CFN primaryIdentifier), never DBClusterArn. Neptune already did;
    # rds and docdb (pinned AWS::RDS::DBCluster / AWS::DocDB::DBCluster) were emitting the ARN.
    rows = [e for e in SIMPLE_LISTERS if e.method == "describe_db_clusters"]
    assert rows, "no describe_db_clusters listers found"
    for e in rows:
        assert e.id_field == "DBClusterIdentifier", (
            f"{e.service}:{e.op} (cfn_type={e.cfn_type}) id_field={e.id_field!r} "
            "must be 'DBClusterIdentifier' — the handler's delete_db_cluster rejects an ARN"
        )


# Slow-delete listers (category 2 of the SR cleanup-failures report): each excludes only the
# transient deletion state(s) so a resource that is legitimately mid-deletion is not flagged as an
# orphan, while failed/other states still surface. (service, op) -> (status_field, status_exclude).
_SLOW_DELETE_EXCLUSIONS = {
    ("rds", "DescribeDBClusters"): ("Status", ("deleting",)),
    ("rds", "DescribeDBInstances"): ("DBInstanceStatus", ("deleting",)),
    ("neptune", "DescribeDBClusters"): ("Status", ("deleting",)),
    ("neptune", "DescribeDBInstances"): ("DBInstanceStatus", ("deleting",)),
    ("docdb", "DescribeDBClusters"): ("Status", ("deleting",)),
    ("docdb", "DescribeDBInstances"): ("DBInstanceStatus", ("deleting",)),
    ("elasticache", "DescribeCacheClusters"): ("CacheClusterStatus", ("deleting", "deleted")),
    ("elasticache", "DescribeReplicationGroups"): ("Status", ("deleting",)),
    ("batch", "DescribeComputeEnvironments"): ("status", ("DELETING", "DELETED")),
    ("batch", "DescribeJobQueues"): ("status", ("DELETING", "DELETED")),
    ("ec2", "DescribeTransitGateways"): ("State", ("deleting", "deleted")),
    ("ec2", "DescribeTransitGatewayAttachments"): (
        "State",
        ("deleting", "deleted", "failed", "rejected"),
    ),
    ("ec2", "DescribeTransitGatewayVpcAttachments"): (
        "State",
        ("deleting", "deleted", "failed", "rejected"),
    ),
    ("ec2", "DescribeTransitGatewayPeeringAttachments"): (
        "State",
        ("deleting", "deleted", "failed", "rejected"),
    ),
    ("ec2", "DescribeTransitGatewayConnects"): (
        "State",
        ("deleting", "deleted", "failed", "rejected"),
    ),
    ("ec2", "DescribeTransitGatewayConnectPeers"): ("State", ("deleting", "deleted")),
    ("ec2", "DescribeTransitGatewayRouteTables"): ("State", ("deleting", "deleted")),
    ("ec2", "DescribeTransitGatewayMulticastDomains"): ("State", ("deleting", "deleted")),
    ("ec2", "DescribeTransitGatewayPolicyTables"): ("State", ("deleting", "deleted")),
}


def test_slow_delete_listers_exclude_active_deletion_states():
    # Each slow-delete lister must carry the exact status_field + status_exclude blocklist, and
    # must NOT use status_filter (allowlist): the two are mutually exclusive per row, and an
    # allowlist over the RDS family's large state space would silently drop genuine orphans.
    by_key = {(entry.service, entry.op): entry for entry in SIMPLE_LISTERS}
    for key, (field, exclude) in _SLOW_DELETE_EXCLUSIONS.items():
        entry = by_key.get(key)
        assert entry is not None, f"{key} slow-delete lister missing"
        assert entry.status_field == field, f"{key} status_field={entry.status_field!r}"
        assert entry.status_exclude == exclude, f"{key} status_exclude={entry.status_exclude!r}"
        assert entry.status_filter is None, f"{key} must use status_exclude, not status_filter"


def test_slow_delete_lister_drops_deleting_keeps_failed_and_unknown():
    # Drive the real rds DescribeDBClusters row through the runtime: an actively-deleting cluster
    # is dropped, while available / failed / missing-status clusters still surface as orphans.
    entry = next(e for e in SIMPLE_LISTERS if (e.service, e.op) == ("rds", "DescribeDBClusters"))
    pages = [
        {
            "DBClusters": [
                {"DBClusterIdentifier": "cl-available", "Status": "available"},
                {"DBClusterIdentifier": "cl-deleting", "Status": "deleting"},
                {"DBClusterIdentifier": "cl-failed", "Status": "failed"},
                {"DBClusterIdentifier": "cl-unknown"},
            ]
        }
    ]
    client = _FakeClient({entry.method: pages})
    got = collect(
        client,
        entry.method,
        entry.result_path,
        entry.id_field,
        status_field=entry.status_field,
        status_exclude=list(entry.status_exclude or ()),
    )
    # emits DBClusterIdentifier (name), the CCAPI/handler primary id — not the ARN
    assert got == ["cl-available", "cl-failed", "cl-unknown"]


def test_transit_gateway_lister_drops_deleting_and_deleted_gateways():
    entry = next(
        e for e in SIMPLE_LISTERS if (e.service, e.op) == ("ec2", "DescribeTransitGateways")
    )
    pages = [
        {
            "TransitGateways": [
                {"TransitGatewayId": "tgw-available", "State": "available"},
                {"TransitGatewayId": "tgw-deleting", "State": "deleting"},
                {"TransitGatewayId": "tgw-deleted", "State": "deleted"},
                {"TransitGatewayId": "tgw-pending", "State": "pending"},
            ]
        }
    ]
    client = _FakeClient({entry.method: pages})
    got = collect(
        client,
        entry.method,
        entry.result_path,
        entry.id_field,
        status_field=entry.status_field,
        status_exclude=list(entry.status_exclude or ()),
    )
    # emits TransitGatewayId (CCAPI primaryIdentifier), not the ARN
    assert got == ["tgw-available", "tgw-pending"]


def test_transit_gateway_attachment_lister_drops_deleting_deleted_failed_rejected():
    entry = next(
        e
        for e in SIMPLE_LISTERS
        if (e.service, e.op) == ("ec2", "DescribeTransitGatewayVpcAttachments")
    )
    pages = [
        {
            "TransitGatewayVpcAttachments": [
                {"TransitGatewayAttachmentId": "tgw-attach-ok", "State": "available"},
                {"TransitGatewayAttachmentId": "tgw-attach-deleting", "State": "deleting"},
                {"TransitGatewayAttachmentId": "tgw-attach-deleted", "State": "deleted"},
                {"TransitGatewayAttachmentId": "tgw-attach-failed", "State": "failed"},
                {"TransitGatewayAttachmentId": "tgw-attach-rejected", "State": "rejected"},
            ]
        }
    ]
    client = _FakeClient({entry.method: pages})
    got = collect(
        client,
        entry.method,
        entry.result_path,
        entry.id_field,
        status_field=entry.status_field,
        status_exclude=list(entry.status_exclude or ()),
    )
    assert got == ["tgw-attach-ok"]
