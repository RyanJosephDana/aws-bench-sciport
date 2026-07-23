"""Behavior tests for the ported code listers (parent->child, discriminator, policy checks).

Each lister is exercised against fake boto3 clients to assert its exact emitted ids: the
composite ``|``-joined / ``_``-joined ids, the AWS-managed / default exclusions, the manual
pagination loops, and the per-parent error-swallowing branches. No AWS is touched.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from aws_bench.resource_management.fastscan.listers import custom_listers as cl
from tests.resource_management.fastscan.listers.test_custom_listers import (
    _FakeClient,
    _one_service,
    _session,
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "Op")


def _raising_child_client(paginated: dict, raise_ops: set[str]) -> _FakeClient:
    """A ``_FakeClient`` whose ``get_paginator`` raises for ``raise_ops`` (parent ops succeed).

    This drives the per-parent ``except (ClientError, BotoCoreError)`` swallow branches: the
    parent op paginates normally, then requesting a child paginator raises.
    """
    client = _FakeClient(paginated=paginated)
    base_get_paginator = client.get_paginator

    def get_paginator(op):
        if op in raise_ops:
            raise _client_error("AccessDenied")
        return base_get_paginator(op)

    client.get_paginator = get_paginator  # type: ignore[method-assign]
    return client


# --- cognito identity pools -------------------------------------------------------------


def test_cognito_identity_pools_returns_ids():
    client = _FakeClient(
        paginated={"list_identity_pools": [{"IdentityPools": [{"IdentityPoolId": "pool-1"}]}]}
    )
    assert cl.list_cognito_identity_pools(_one_service("cognito-identity", client)) == ["pool-1"]


# --- codestar-connections sync configurations (manual pagination + composite id) --------


def test_codestar_sync_configurations_composite_id_across_links():
    link_pages = iter(
        [
            {"RepositoryLinks": [{"RepositoryLinkId": "rl-1"}], "NextToken": "n"},
            {"RepositoryLinks": [{"RepositoryLinkId": "rl-2"}]},
        ]
    )
    client = _FakeClient(
        direct={
            "list_repository_links": lambda **_kw: next(link_pages),
            "list_sync_configurations": {
                "SyncConfigurations": [{"ResourceName": "stack-1", "SyncType": "CFN_STACK_SYNC"}]
            },
        }
    )
    out = cl.list_codestar_connections_sync_configurations(
        _one_service("codestar-connections", client)
    )
    assert out == ["stack-1|CFN_STACK_SYNC", "stack-1|CFN_STACK_SYNC"]


def test_codestar_sync_configurations_swallows_per_link_error():
    def list_sync_configurations(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        direct={
            "list_repository_links": {"RepositoryLinks": [{"RepositoryLinkId": "rl-1"}]},
            "list_sync_configurations": list_sync_configurations,
        }
    )
    assert (
        cl.list_codestar_connections_sync_configurations(
            _one_service("codestar-connections", client)
        )
        == []
    )


# --- datasync location-type listers -----------------------------------------------------


@pytest.mark.parametrize(
    ("fn", "describe_op"),
    [
        (cl.list_datasync_location_s3, "describe_location_s3"),
        (cl.list_datasync_location_efs, "describe_location_efs"),
        (cl.list_datasync_location_nfs, "describe_location_nfs"),
        (cl.list_datasync_location_smb, "describe_location_smb"),
        (cl.list_datasync_location_hdfs, "describe_location_hdfs"),
        (cl.list_datasync_location_object_storage, "describe_location_object_storage"),
        (cl.list_datasync_location_azure_blob, "describe_location_azure_blob"),
        (cl.list_datasync_location_fsx_lustre, "describe_location_fsx_lustre"),
        (cl.list_datasync_location_fsx_ontap, "describe_location_fsx_ontap"),
        (cl.list_datasync_location_fsx_openzfs, "describe_location_fsx_open_zfs"),
        (cl.list_datasync_location_fsx_windows, "describe_location_fsx_windows"),
    ],
)
def test_datasync_location_kept_when_describe_succeeds(fn, describe_op):
    client = _FakeClient(
        paginated={"list_locations": [{"Locations": [{"LocationArn": "arn:loc:1"}]}]},
        direct={describe_op: {}},
    )
    assert fn(_one_service("datasync", client)) == ["arn:loc:1"]


def test_datasync_location_excluded_when_describe_raises():
    def describe_location_s3(**_kw):
        raise _client_error("InvalidRequestException")

    client = _FakeClient(
        paginated={"list_locations": [{"Locations": [{"LocationArn": "arn:loc:1"}]}]},
        direct={"describe_location_s3": describe_location_s3},
    )
    assert cl.list_datasync_location_s3(_one_service("datasync", client)) == []


# --- omics workflow versions ------------------------------------------------------------


def test_omics_workflow_versions_emits_arns():
    client = _FakeClient(
        paginated={
            "list_workflows": [{"items": [{"id": "wf-1"}]}],
            "list_workflow_versions": [{"items": [{"arn": "arn:ver:1"}]}],
        }
    )
    assert cl.list_omics_workflow_versions(_one_service("omics", client)) == ["arn:ver:1"]


def test_omics_workflow_versions_swallows_per_workflow_error():
    def get_paginator(op):
        if op == "list_workflows":
            return _FakeClient(
                paginated={"list_workflows": [{"items": [{"id": "wf-1"}]}]}
            ).get_paginator(op)
        raise _client_error("AccessDenied")

    client = _FakeClient(paginated={"list_workflows": [{"items": [{"id": "wf-1"}]}]})
    client.get_paginator = get_paginator  # type: ignore[method-assign]
    assert cl.list_omics_workflow_versions(_one_service("omics", client)) == []


# --- _has_real_policy (direct) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (None, False),
        ("", False),
        ("{}", False),
        ('{"x": 1}', True),
        ("not-json", True),
    ],
)
def test_has_real_policy_branches(policy, expected):
    assert cl._has_real_policy(policy) is expected


# --- smsvoice resource policies ---------------------------------------------------------


def test_smsvoice_resource_policies_keeps_only_real_policy():
    def get_resource_policy(ResourceArn):  # noqa: N803
        return {"arn:phone": {"Policy": '{"a": 1}'}, "arn:pool": {"Policy": "{}"}}[ResourceArn]

    client = _FakeClient(
        paginated={
            "describe_phone_numbers": [{"PhoneNumbers": [{"PhoneNumberArn": "arn:phone"}]}],
            "describe_pools": [{"Pools": [{"PoolArn": "arn:pool"}]}],
            "describe_sender_ids": [{"SenderIds": []}],
            "describe_opt_out_lists": [{"OptOutLists": []}],
        },
        direct={"get_resource_policy": get_resource_policy},
    )
    assert cl.list_smsvoice_resource_policies(_one_service("pinpoint-sms-voice-v2", client)) == [
        "arn:phone"
    ]


def test_smsvoice_resource_policies_swallows_get_policy_error():
    def get_resource_policy(**_kw):
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={
            "describe_phone_numbers": [{"PhoneNumbers": [{"PhoneNumberArn": "arn:phone"}]}],
            "describe_pools": [{"Pools": []}],
            "describe_sender_ids": [{"SenderIds": []}],
            "describe_opt_out_lists": [{"OptOutLists": []}],
        },
        direct={"get_resource_policy": get_resource_policy},
    )
    assert cl.list_smsvoice_resource_policies(_one_service("pinpoint-sms-voice-v2", client)) == []


# --- servicecatalog associations --------------------------------------------------------


def test_servicecatalog_service_action_associations_composite_id():
    client = _FakeClient(
        paginated={
            "search_products_as_admin": [
                {"ProductViewDetails": [{"ProductViewSummary": {"ProductId": "prod-1"}}]}
            ],
            "list_service_actions_for_provisioning_artifact": [
                {"ServiceActionSummaries": [{"Id": "sa-1"}]}
            ],
        },
        direct={"list_provisioning_artifacts": {"ProvisioningArtifactDetails": [{"Id": "pa-1"}]}},
    )
    assert cl.list_servicecatalog_service_action_associations(
        _one_service("servicecatalog", client)
    ) == ["prod-1|pa-1|sa-1"]


def test_servicecatalog_tag_option_associations_composite_id():
    to_pages = iter([{"TagOptionDetails": [{"Id": "to-1"}]}])
    res_pages = iter([{"ResourceDetails": [{"Id": "res-1"}]}])
    client = _FakeClient(
        direct={
            "list_tag_options": lambda **_kw: next(to_pages),
            "list_resources_for_tag_option": lambda **_kw: next(res_pages),
        }
    )
    assert cl.list_servicecatalog_tag_option_associations(
        _one_service("servicecatalog", client)
    ) == ["to-1|res-1"]


def test_servicecatalog_tag_option_associations_not_migrated_returns_empty():
    def list_tag_options(**_kw):
        raise _client_error("TagOptionNotMigratedException")

    client = _FakeClient(direct={"list_tag_options": list_tag_options})
    assert (
        cl.list_servicecatalog_tag_option_associations(_one_service("servicecatalog", client)) == []
    )


# --- kinesisanalyticsv2 application outputs ---------------------------------------------


def test_kinesisanalyticsv2_application_outputs_composite_id():
    client = _FakeClient(
        paginated={"list_applications": [{"ApplicationSummaries": [{"ApplicationName": "app-1"}]}]},
        direct={
            "describe_application": {
                "ApplicationDetail": {
                    "ApplicationConfigurationDescription": {
                        "SqlApplicationConfigurationDescription": {
                            "OutputDescriptions": [{"OutputId": "out-1"}]
                        }
                    }
                }
            }
        },
    )
    assert cl.list_kinesisanalyticsv2_application_outputs(
        _one_service("kinesisanalyticsv2", client)
    ) == ["app-1|out-1"]


def test_kinesisanalyticsv2_application_outputs_swallows_describe_error():
    def describe_application(**_kw):
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={"list_applications": [{"ApplicationSummaries": [{"ApplicationName": "app-1"}]}]},
        direct={"describe_application": describe_application},
    )
    assert (
        cl.list_kinesisanalyticsv2_application_outputs(_one_service("kinesisanalyticsv2", client))
        == []
    )


# --- amplify domain associations --------------------------------------------------------


def test_amplify_domain_associations_emits_arns():
    client = _FakeClient(
        paginated={
            "list_apps": [{"apps": [{"appId": "app-1"}]}],
            "list_domain_associations": [
                {"domainAssociations": [{"domainAssociationArn": "arn:da:1"}]}
            ],
        }
    )
    assert cl.list_amplify_domain_associations(_one_service("amplify", client)) == ["arn:da:1"]


# --- config remediation configurations --------------------------------------------------


def test_config_remediation_configurations_skips_service_created():
    client = _FakeClient(
        paginated={
            "describe_config_rules": [
                {"ConfigRules": [{"ConfigRuleName": "rule-a"}, {"ConfigRuleName": "rule-b"}]}
            ]
        },
        direct={
            "describe_remediation_configurations": {
                "RemediationConfigurations": [
                    {"ConfigRuleName": "rule-a"},
                    {"ConfigRuleName": "rule-b", "CreatedByService": "config.amazonaws.com"},
                ]
            }
        },
    )
    assert cl.list_config_remediation_configurations(_one_service("config", client)) == ["rule-a"]


# --- global accelerator listeners / endpoint groups -------------------------------------


def test_globalaccelerator_listeners_emits_listener_arns():
    client = _FakeClient(
        paginated={
            "list_accelerators": [{"Accelerators": [{"AcceleratorArn": "arn:acc:1"}]}],
            "list_listeners": [{"Listeners": [{"ListenerArn": "arn:lis:1"}]}],
        }
    )
    assert cl.list_globalaccelerator_listeners(_one_service("globalaccelerator", client)) == [
        "arn:lis:1"
    ]


def test_globalaccelerator_endpoint_groups_emits_group_arns():
    client = _FakeClient(
        paginated={
            "list_accelerators": [{"Accelerators": [{"AcceleratorArn": "arn:acc:1"}]}],
            "list_listeners": [{"Listeners": [{"ListenerArn": "arn:lis:1"}]}],
            "list_endpoint_groups": [{"EndpointGroups": [{"EndpointGroupArn": "arn:eg:1"}]}],
        }
    )
    assert cl.list_globalaccelerator_endpoint_groups(_one_service("globalaccelerator", client)) == [
        "arn:eg:1"
    ]


# --- guardduty child listers (DetectorId parent) ----------------------------------------


def _guardduty_client(**extra_paginated):
    return _FakeClient(
        paginated={"list_detectors": [{"DetectorIds": ["d1"]}], **extra_paginated},
    )


def test_guardduty_filters_composite_id():
    client = _guardduty_client(list_filters=[{"FilterNames": ["f1"]}])
    assert cl.list_guardduty_filters(_one_service("guardduty", client)) == ["d1|f1"]


def test_guardduty_ip_sets_composite_id():
    client = _guardduty_client(list_ip_sets=[{"IpSetIds": ["ip1"]}])
    assert cl.list_guardduty_ip_sets(_one_service("guardduty", client)) == ["ip1|d1"]


def test_guardduty_threat_intel_sets_composite_id():
    client = _guardduty_client(list_threat_intel_sets=[{"ThreatIntelSetIds": ["ti1"]}])
    assert cl.list_guardduty_threat_intel_sets(_one_service("guardduty", client)) == ["ti1|d1"]


def test_guardduty_threat_entity_sets_composite_id():
    client = _guardduty_client(list_threat_entity_sets=[{"ThreatEntitySetIds": ["te1"]}])
    assert cl.list_guardduty_threat_entity_sets(_one_service("guardduty", client)) == ["te1|d1"]


def test_guardduty_trusted_entity_sets_composite_id():
    client = _guardduty_client(list_trusted_entity_sets=[{"TrustedEntitySetIds": ["tr1"]}])
    assert cl.list_guardduty_trusted_entity_sets(_one_service("guardduty", client)) == ["tr1|d1"]


def test_guardduty_members_composite_id():
    client = _guardduty_client(list_members=[{"Members": [{"AccountId": "111"}]}])
    assert cl.list_guardduty_members(_one_service("guardduty", client)) == ["d1|111"]


def test_guardduty_filters_swallows_child_error():
    def get_paginator(op):
        if op == "list_detectors":
            return _FakeClient(
                paginated={"list_detectors": [{"DetectorIds": ["d1"]}]}
            ).get_paginator(op)
        raise _client_error("AccessDenied")

    client = _FakeClient(paginated={"list_detectors": [{"DetectorIds": ["d1"]}]})
    client.get_paginator = get_paginator  # type: ignore[method-assign]
    assert cl.list_guardduty_filters(_one_service("guardduty", client)) == []


def test_guardduty_publishing_destinations_manual_pagination():
    dest_pages = iter(
        [
            {"Destinations": [{"DestinationId": "x"}], "NextToken": "n"},
            {"Destinations": [{"DestinationId": "y"}]},
        ]
    )
    client = _FakeClient(
        paginated={"list_detectors": [{"DetectorIds": ["d1"]}]},
        direct={"list_publishing_destinations": lambda **_kw: next(dest_pages)},
    )
    assert cl.list_guardduty_publishing_destinations(_one_service("guardduty", client)) == [
        "d1|x",
        "d1|y",
    ]


def test_guardduty_publishing_destinations_swallows_error():
    def list_publishing_destinations(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_detectors": [{"DetectorIds": ["d1"]}]},
        direct={"list_publishing_destinations": list_publishing_destinations},
    )
    assert cl.list_guardduty_publishing_destinations(_one_service("guardduty", client)) == []


def test_guardduty_masters_composite_id():
    client = _FakeClient(
        paginated={"list_detectors": [{"DetectorIds": ["d1"]}]},
        direct={"get_master_account": {"Master": {"AccountId": "111"}}},
    )
    assert cl.list_guardduty_masters(_one_service("guardduty", client)) == ["d1|111"]


def test_guardduty_masters_swallows_error_and_skips_empty():
    def get_master_account(**_kw):
        raise _client_error("BadRequestException")

    client = _FakeClient(
        paginated={"list_detectors": [{"DetectorIds": ["d1"]}]},
        direct={"get_master_account": get_master_account},
    )
    assert cl.list_guardduty_masters(_one_service("guardduty", client)) == []


# --- location tracker consumers ---------------------------------------------------------


def test_location_tracker_consumers_composite_id():
    client = _FakeClient(
        paginated={
            "list_trackers": [{"Entries": [{"TrackerName": "trk-1"}]}],
            "list_tracker_consumers": [{"ConsumerArns": ["arn:con:1"]}],
        }
    )
    assert cl.list_location_tracker_consumers(_one_service("location", client)) == [
        "trk-1|arn:con:1"
    ]


# --- organizations OUs (BFS recursion) --------------------------------------------------


def test_organizations_organizational_units_recurses():
    def get_paginator(op):
        pages = {
            "list_roots": [{"Roots": [{"Id": "r-1"}]}],
        }
        if op == "list_roots":
            return _FakeClient(paginated=pages).get_paginator(op)
        # list_organizational_units_for_parent: r-1 -> ou-1, ou-1 -> ou-2, ou-2 -> none.
        return _OuPaginator()

    class _OuPaginator:
        _by_parent = {
            "r-1": [{"OrganizationalUnits": [{"Id": "ou-1"}]}],
            "ou-1": [{"OrganizationalUnits": [{"Id": "ou-2"}]}],
            "ou-2": [{"OrganizationalUnits": []}],
        }

        def paginate(self, ParentId):  # noqa: N803
            return iter(self._by_parent[ParentId])

    client = _FakeClient()
    client.get_paginator = get_paginator  # type: ignore[method-assign]
    out = cl.list_organizations_organizational_units(_one_service("organizations", client))
    assert sorted(out) == ["ou-1", "ou-2"]


# --- connect per-instance listers -------------------------------------------------------


def test_connect_approved_origins_composite_id():
    client = _FakeClient(
        paginated={
            "list_instances": [{"InstanceSummaryList": [{"Id": "inst-1"}]}],
            "list_approved_origins": [{"Origins": ["https://a.example"]}],
        }
    )
    assert cl.list_connect_approved_origins(_one_service("connect", client)) == [
        "inst-1|https://a.example"
    ]


def test_connect_approved_origins_swallows_error():
    def get_paginator(op):
        if op == "list_instances":
            return _FakeClient(
                paginated={"list_instances": [{"InstanceSummaryList": [{"Id": "inst-1"}]}]}
            ).get_paginator(op)
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_instances": [{"InstanceSummaryList": [{"Id": "inst-1"}]}]}
    )
    client.get_paginator = get_paginator  # type: ignore[method-assign]
    assert cl.list_connect_approved_origins(_one_service("connect", client)) == []


def test_connect_security_keys_composite_id():
    client = _FakeClient(
        paginated={
            "list_instances": [{"InstanceSummaryList": [{"Id": "inst-1"}]}],
            "list_security_keys": [{"SecurityKeys": [{"AssociationId": "assoc-1"}]}],
        }
    )
    assert cl.list_connect_security_keys(_one_service("connect", client)) == ["inst-1|assoc-1"]


def test_connect_instance_storage_configs_composite_id():
    client = _FakeClient(
        paginated={
            "list_instances": [{"InstanceSummaryList": [{"Id": "inst-1", "Arn": "arn:inst:1"}]}]
        },
        direct={"list_instance_storage_configs": {"StorageConfigs": [{"AssociationId": "sc-1"}]}},
    )
    out = cl.list_connect_instance_storage_configs(_one_service("connect", client))
    # One config emitted per resource-type that returns it (13 resource types).
    assert out
    assert all(item == "arn:inst:1|sc-1" for item in out)


def test_connect_instance_storage_configs_swallows_error():
    def list_instance_storage_configs(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={
            "list_instances": [{"InstanceSummaryList": [{"Id": "inst-1", "Arn": "arn:inst:1"}]}]
        },
        direct={"list_instance_storage_configs": list_instance_storage_configs},
    )
    assert cl.list_connect_instance_storage_configs(_one_service("connect", client)) == []


# --- EC2 route/route-server/endpoint listers --------------------------------------------


def test_ec2_local_gateway_routes_composite_id():
    client = _FakeClient(
        paginated={
            "describe_local_gateway_route_tables": [
                {"LocalGatewayRouteTables": [{"LocalGatewayRouteTableId": "lgw-rtb-1"}]}
            ]
        },
        direct={
            "search_local_gateway_routes": {"Routes": [{"DestinationCidrBlock": "10.0.0.0/8"}]}
        },
    )
    assert cl.list_ec2_local_gateway_routes(_one_service("ec2", client)) == ["10.0.0.0/8|lgw-rtb-1"]


def test_ec2_local_gateway_routes_swallows_search_error():
    def search_local_gateway_routes(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={
            "describe_local_gateway_route_tables": [
                {"LocalGatewayRouteTables": [{"LocalGatewayRouteTableId": "lgw-rtb-1"}]}
            ]
        },
        direct={"search_local_gateway_routes": search_local_gateway_routes},
    )
    assert cl.list_ec2_local_gateway_routes(_one_service("ec2", client)) == []


def test_ec2_route_server_associations_composite_id():
    client = _FakeClient(
        paginated={"describe_route_servers": [{"RouteServers": [{"RouteServerId": "rs-1"}]}]},
        direct={
            "get_route_server_associations": {
                "RouteServerAssociations": [{"RouteServerId": "rs-1", "VpcId": "vpc-1"}]
            }
        },
    )
    assert cl.list_ec2_route_server_associations(_one_service("ec2", client)) == ["rs-1|vpc-1"]


def test_ec2_route_server_propagations_composite_id():
    client = _FakeClient(
        paginated={"describe_route_servers": [{"RouteServers": [{"RouteServerId": "rs-1"}]}]},
        direct={
            "get_route_server_propagations": {
                "RouteServerPropagations": [{"RouteServerId": "rs-1", "RouteTableId": "rtb-1"}]
            }
        },
    )
    assert cl.list_ec2_route_server_propagations(_one_service("ec2", client)) == ["rs-1|rtb-1"]


def test_ec2_vpc_endpoint_service_permissions_emits_service_id_with_allowlist():
    def describe_vpc_endpoint_service_permissions(ServiceId):  # noqa: N803
        return {
            "svc-1": {"AllowedPrincipals": [{"Principal": "arn:aws:iam::1:root"}]},
            "svc-2": {"AllowedPrincipals": []},
        }[ServiceId]

    client = _FakeClient(
        paginated={
            "describe_vpc_endpoint_service_configurations": [
                {"ServiceConfigurations": [{"ServiceId": "svc-1"}, {"ServiceId": "svc-2"}]}
            ]
        },
        direct={
            "describe_vpc_endpoint_service_permissions": describe_vpc_endpoint_service_permissions
        },
    )
    assert cl.list_ec2_vpc_endpoint_service_permissions(_one_service("ec2", client)) == ["svc-1"]


def test_ec2_security_group_ingress_and_egress_split_by_is_egress():
    paginated = {
        "describe_security_group_rules": [
            {
                "SecurityGroupRules": [
                    {"SecurityGroupRuleId": "sgr-1", "IsEgress": False},
                    {"SecurityGroupRuleId": "sgr-2", "IsEgress": True},
                ]
            }
        ]
    }
    ingress = cl.list_ec2_security_group_ingress_rules(
        _one_service("ec2", _FakeClient(paginated=paginated))
    )
    egress = cl.list_ec2_security_group_egress_rules(
        _one_service("ec2", _FakeClient(paginated=paginated))
    )
    assert ingress == ["sgr-1"]
    assert egress == ["sgr-2"]


# --- greengrass logger definition versions ----------------------------------------------


def test_greengrass_logger_definition_versions_emits_version_ids():
    client = _FakeClient(
        paginated={
            "list_logger_definitions": [{"Definitions": [{"Id": "def-1"}]}],
            "list_logger_definition_versions": [{"Versions": [{"Id": "ver-1"}]}],
        }
    )
    assert cl.list_greengrass_logger_definition_versions(_one_service("greengrass", client)) == [
        "ver-1"
    ]


# --- verifiedpermissions per-store listers ----------------------------------------------


def test_verifiedpermissions_identity_sources_composite_id():
    client = _FakeClient(
        paginated={
            "list_policy_stores": [{"policyStores": [{"policyStoreId": "ps-1"}]}],
            "list_identity_sources": [{"identitySources": [{"identitySourceId": "is-1"}]}],
        }
    )
    assert cl.list_verifiedpermissions_identity_sources(
        _one_service("verifiedpermissions", client)
    ) == ["is-1|ps-1"]


def test_verifiedpermissions_policies_composite_id():
    client = _FakeClient(
        paginated={
            "list_policy_stores": [{"policyStores": [{"policyStoreId": "ps-1"}]}],
            "list_policies": [{"policies": [{"policyId": "pol-1"}]}],
        }
    )
    assert cl.list_verifiedpermissions_policies(_one_service("verifiedpermissions", client)) == [
        "pol-1|ps-1"
    ]


def test_verifiedpermissions_policies_swallows_error():
    def get_paginator(op):
        if op == "list_policy_stores":
            return _FakeClient(
                paginated={"list_policy_stores": [{"policyStores": [{"policyStoreId": "ps-1"}]}]}
            ).get_paginator(op)
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_policy_stores": [{"policyStores": [{"policyStoreId": "ps-1"}]}]}
    )
    client.get_paginator = get_paginator  # type: ignore[method-assign]
    assert cl.list_verifiedpermissions_policies(_one_service("verifiedpermissions", client)) == []


# --- pca-connector-ad -------------------------------------------------------------------


def test_pcaconnectorad_service_principal_names_composite_id():
    client = _FakeClient(
        paginated={
            "list_directory_registrations": [{"DirectoryRegistrations": [{"Arn": "arn:reg:1"}]}],
            "list_service_principal_names": [
                {"ServicePrincipalNames": [{"ConnectorArn": "arn:conn:1"}]}
            ],
        }
    )
    assert cl.list_pcaconnectorad_service_principal_names(
        _one_service("pca-connector-ad", client)
    ) == ["arn:conn:1|arn:reg:1"]


def test_pcaconnectorad_template_group_access_control_entries_composite_id():
    client = _FakeClient(
        paginated={
            "list_connectors": [{"Connectors": [{"Arn": "arn:conn:1"}]}],
            "list_templates": [{"Templates": [{"Arn": "arn:tmpl:1"}]}],
            "list_template_group_access_control_entries": [
                {"AccessControlEntries": [{"GroupSecurityIdentifier": "S-1-5"}]}
            ],
        }
    )
    assert cl.list_pcaconnectorad_template_group_access_control_entries(
        _one_service("pca-connector-ad", client)
    ) == ["S-1-5|arn:tmpl:1"]


# --- rtbfabric link routing rules -------------------------------------------------------


def test_rtbfabric_link_routing_rules_composite_id():
    client = _FakeClient(
        paginated={
            "list_requester_gateways": [{"gatewayIds": ["gw-1"]}],
            "list_responder_gateways": [{"gatewayIds": []}],
            "list_links": [{"links": [{"linkId": "link-1"}]}],
            "list_link_routing_rules": [{"rules": [{"ruleId": "rule-1"}]}],
        }
    )
    assert cl.list_rtbfabric_link_routing_rules(_one_service("rtbfabric", client)) == [
        "gw-1|link-1|rule-1"
    ]


# --- logs transformers ------------------------------------------------------------------


def test_logs_transformers_keeps_groups_with_transformer():
    def get_transformer(logGroupIdentifier):  # noqa: N803
        if logGroupIdentifier == "/has/transformer":
            return {"transformerConfig": [{"type": "x"}]}
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={
            "describe_log_groups": [
                {"logGroups": [{"logGroupName": "/has/transformer"}, {"logGroupName": "/none"}]}
            ]
        },
        direct={"get_transformer": get_transformer},
    )
    assert cl.list_logs_transformers(_one_service("logs", client)) == ["/has/transformer"]


def test_logs_transformers_logs_unexpected_client_error(caplog):
    import logging

    def get_transformer(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"describe_log_groups": [{"logGroups": [{"logGroupName": "/g"}]}]},
        direct={"get_transformer": get_transformer},
    )
    with caplog.at_level(logging.DEBUG):
        assert cl.list_logs_transformers(_one_service("logs", client)) == []


# --- networkmanager attachments + registrations -----------------------------------------


@pytest.mark.parametrize(
    ("fn", "attachment_type"),
    [
        (cl.list_networkmanager_connect_attachments, "CONNECT"),
        (cl.list_networkmanager_site_to_site_vpn_attachments, "SITE_TO_SITE_VPN"),
        (cl.list_networkmanager_vpc_attachments, "VPC"),
        (cl.list_networkmanager_direct_connect_gateway_attachments, "DIRECT_CONNECT_GATEWAY"),
        (
            cl.list_networkmanager_transit_gateway_route_table_attachments,
            "TRANSIT_GATEWAY_ROUTE_TABLE",
        ),
    ],
)
def test_networkmanager_attachments_filters_by_type(fn, attachment_type):
    client = _FakeClient(
        paginated={
            "list_attachments": [
                {
                    "Attachments": [
                        {"AttachmentType": attachment_type, "AttachmentId": "attach-1"},
                        {"AttachmentType": "OTHER", "AttachmentId": "attach-2"},
                    ]
                }
            ]
        }
    )
    assert fn(_one_service("networkmanager", client)) == ["attach-1"]


def test_networkmanager_transit_gateway_registrations_composite_id():
    client = _FakeClient(
        paginated={
            "describe_global_networks": [{"GlobalNetworks": [{"GlobalNetworkId": "gn-1"}]}],
            "get_transit_gateway_registrations": [
                {"TransitGatewayRegistrations": [{"TransitGatewayArn": "arn:tgw:1"}]}
            ],
        }
    )
    assert cl.list_networkmanager_transit_gateway_registrations(
        _one_service("networkmanager", client)
    ) == ["gn-1|arn:tgw:1"]


# --- iam user-to-group additions --------------------------------------------------------


def test_iam_user_to_group_additions_composite_id():
    client = _FakeClient(
        paginated={
            "list_groups": [{"Groups": [{"GroupName": "grp-1"}]}],
            "get_group": [{"Users": [{"UserName": "alice"}]}],
        }
    )
    assert cl.list_iam_user_to_group_additions(_one_service("iam", client)) == ["grp-1/alice"]


# --- medialive channel placement groups -------------------------------------------------


def test_medialive_channel_placement_groups_composite_id():
    client = _FakeClient(
        paginated={
            "list_clusters": [{"Clusters": [{"Id": "clu-1"}]}],
            "list_channel_placement_groups": [{"ChannelPlacementGroups": [{"Id": "cpg-1"}]}],
        }
    )
    assert cl.list_medialive_channel_placement_groups(_one_service("medialive", client)) == [
        "cpg-1|clu-1"
    ]


# --- qbusiness data sources -------------------------------------------------------------


def test_qbusiness_data_sources_composite_id():
    client = _FakeClient(
        paginated={
            "list_applications": [{"applications": [{"applicationId": "app-1"}]}],
            "list_indices": [{"indices": [{"indexId": "idx-1"}]}],
            "list_data_sources": [{"dataSources": [{"dataSourceId": "ds-1"}]}],
        }
    )
    assert cl.list_qbusiness_data_sources(_one_service("qbusiness", client)) == ["app-1|ds-1|idx-1"]


# --- s3files file system policies -------------------------------------------------------


def test_s3files_file_system_policies_keeps_only_with_policy():
    def get_file_system_policy(fileSystemId):  # noqa: N803
        if fileSystemId == "fs-1":
            return {"policy": "{}"}
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={
            "list_file_systems": [
                {"fileSystems": [{"fileSystemId": "fs-1"}, {"fileSystemId": "fs-2"}]}
            ]
        },
        direct={"get_file_system_policy": get_file_system_policy},
    )
    assert cl.list_s3files_file_system_policies(_one_service("s3files", client)) == ["fs-1"]


# --- secretsmanager ---------------------------------------------------------------------


def test_secretsmanager_resource_policies_keeps_only_with_policy():
    def get_resource_policy(SecretId):  # noqa: N803
        if SecretId == "arn:sec:1":
            return {"ResourcePolicy": "{}"}
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={"list_secrets": [{"SecretList": [{"ARN": "arn:sec:1"}, {"ARN": "arn:sec:2"}]}]},
        direct={"get_resource_policy": get_resource_policy},
    )
    assert cl.list_secretsmanager_resource_policies(_one_service("secretsmanager", client)) == [
        "arn:sec:1"
    ]


def test_secretsmanager_rotation_schedules_keeps_rotation_enabled():
    client = _FakeClient(
        paginated={
            "list_secrets": [
                {
                    "SecretList": [
                        {"ARN": "arn:sec:1", "RotationEnabled": True},
                        {"ARN": "arn:sec:2", "RotationEnabled": False},
                    ]
                }
            ]
        }
    )
    assert cl.list_secretsmanager_rotation_schedules(_one_service("secretsmanager", client)) == [
        "arn:sec:1"
    ]


# --- qconnect ---------------------------------------------------------------------------


def test_qconnect_ai_agents_composite_id():
    client = _FakeClient(
        paginated={
            "list_assistants": [{"assistantSummaries": [{"assistantId": "asst-1"}]}],
            "list_ai_agents": [{"aiAgentSummaries": [{"aiAgentId": "agent-1"}]}],
        }
    )
    assert cl.list_qconnect_ai_agents(_one_service("qconnect", client)) == ["agent-1|asst-1"]


def test_qconnect_ai_agent_versions_composite_id():
    client = _FakeClient(
        paginated={
            "list_assistants": [{"assistantSummaries": [{"assistantId": "asst-1"}]}],
            "list_ai_agents": [{"aiAgentSummaries": [{"aiAgentId": "agent-1"}]}],
            "list_ai_agent_versions": [{"aiAgentVersionSummaries": [{"versionNumber": 3}]}],
        }
    )
    assert cl.list_qconnect_ai_agent_versions(_one_service("qconnect", client)) == [
        "asst-1|agent-1|3"
    ]


# --- apigatewayv2 -----------------------------------------------------------------------


def test_apigatewayv2_integration_responses_composite_id():
    client = _FakeClient(
        paginated={
            "get_apis": [{"Items": [{"ApiId": "api-1"}]}],
            "get_integrations": [{"Items": [{"IntegrationId": "int-1"}]}],
            "get_integration_responses": [{"Items": [{"IntegrationResponseId": "ir-1"}]}],
        }
    )
    assert cl.list_apigatewayv2_integration_responses(_one_service("apigatewayv2", client)) == [
        "api-1|int-1|ir-1"
    ]


def test_apigatewayv2_route_responses_composite_id():
    client = _FakeClient(
        paginated={
            "get_apis": [{"Items": [{"ApiId": "api-1"}]}],
            "get_routes": [{"Items": [{"RouteId": "route-1"}]}],
            "get_route_responses": [{"Items": [{"RouteResponseId": "rr-1"}]}],
        }
    )
    assert cl.list_apigatewayv2_route_responses(_one_service("apigatewayv2", client)) == [
        "api-1|route-1|rr-1"
    ]


# --- backup restore-testing selections --------------------------------------------------


def test_backup_restore_testing_selections_composite_id():
    client = _FakeClient(
        paginated={
            "list_restore_testing_plans": [
                {"RestoreTestingPlans": [{"RestoreTestingPlanName": "plan-1"}]}
            ],
            "list_restore_testing_selections": [
                {"RestoreTestingSelections": [{"RestoreTestingSelectionName": "sel-1"}]}
            ],
        }
    )
    assert cl.list_backup_restore_testing_selections(_one_service("backup", client)) == [
        "plan-1|sel-1"
    ]


# --- iotsitewise projects ---------------------------------------------------------------


def test_iotsitewise_projects_across_portals():
    client = _FakeClient(
        paginated={
            "list_portals": [{"portalSummaries": [{"id": "portal-1"}]}],
            "list_projects": [{"projectSummaries": [{"id": "proj-1"}]}],
        }
    )
    assert cl.list_iotsitewise_projects(_one_service("iotsitewise", client)) == ["proj-1"]


# --- lambda urls ------------------------------------------------------------------------


def test_lambda_urls_emits_function_arns():
    client = _FakeClient(
        paginated={
            "list_functions": [{"Functions": [{"FunctionName": "fn-1"}]}],
            "list_function_url_configs": [{"FunctionUrlConfigs": [{"FunctionArn": "arn:fn:1"}]}],
        }
    )
    assert cl.list_lambda_urls(_one_service("lambda", client)) == ["arn:fn:1"]


# --- mediapackagev2 origin endpoint policies --------------------------------------------


def test_mediapackagev2_origin_endpoint_policies_composite_id():
    client = _FakeClient(
        paginated={
            "list_channel_groups": [{"Items": [{"ChannelGroupName": "grp-1"}]}],
            "list_channels": [{"Items": [{"ChannelName": "chan-1"}]}],
            "list_origin_endpoints": [{"Items": [{"OriginEndpointName": "ep-1"}]}],
        },
        direct={"get_origin_endpoint_policy": {"Policy": "{}"}},
    )
    assert cl.list_mediapackagev2_origin_endpoint_policies(
        _one_service("mediapackagev2", client)
    ) == ["grp-1|chan-1|ep-1"]


def test_mediapackagev2_has_policy_resource_not_found_returns_false():
    def get_origin_endpoint_policy(**_kw):
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(direct={"get_origin_endpoint_policy": get_origin_endpoint_policy})
    assert cl._mediapackagev2_has_policy(client, "grp", "chan", "ep") is False


def test_mediapackagev2_has_policy_other_error_returns_false():
    def get_origin_endpoint_policy(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(direct={"get_origin_endpoint_policy": get_origin_endpoint_policy})
    assert cl._mediapackagev2_has_policy(client, "grp", "chan", "ep") is False


# --- pinpoint channels / settings / templates -------------------------------------------


@pytest.mark.parametrize(
    ("fn", "get_op", "response_key"),
    [
        (cl.list_pinpoint_sms_channels, "get_sms_channel", "SMSChannelResponse"),
        (
            cl.list_pinpoint_apns_sandbox_channels,
            "get_apns_sandbox_channel",
            "APNSSandboxChannelResponse",
        ),
        (cl.list_pinpoint_apns_voip_channels, "get_apns_voip_channel", "APNSVoipChannelResponse"),
        (
            cl.list_pinpoint_apns_voip_sandbox_channels,
            "get_apns_voip_sandbox_channel",
            "APNSVoipSandboxChannelResponse",
        ),
    ],
)
def test_pinpoint_channels_emit_channel_id(fn, get_op, response_key):
    client = _FakeClient(
        direct={
            "get_apps": {"ApplicationsResponse": {"Item": [{"Id": "app-1"}]}},
            get_op: {response_key: {"Id": "chan-1"}},
        }
    )
    assert fn(_one_service("pinpoint", client)) == ["chan-1"]


def test_pinpoint_channels_skip_not_found():
    def get_sms_channel(**_kw):
        raise _client_error("NotFoundException")

    client = _FakeClient(
        direct={
            "get_apps": {"ApplicationsResponse": {"Item": [{"Id": "app-1"}]}},
            "get_sms_channel": get_sms_channel,
        }
    )
    assert cl.list_pinpoint_sms_channels(_one_service("pinpoint", client)) == []


def test_pinpoint_application_settings_emit_application_id():
    client = _FakeClient(
        direct={
            "get_apps": {"ApplicationsResponse": {"Item": [{"Id": "app-1"}]}},
            "get_application_settings": {"ApplicationSettingsResource": {"ApplicationId": "app-1"}},
        }
    )
    assert cl.list_pinpoint_application_settings(_one_service("pinpoint", client)) == ["app-1"]


def test_pinpoint_in_app_templates_filters_by_type():
    client = _FakeClient(
        direct={
            "list_templates": {
                "TemplatesResponse": {
                    "Item": [
                        {"TemplateName": "t-inapp", "TemplateType": "INAPP"},
                        {"TemplateName": "t-email", "TemplateType": "EMAIL"},
                    ]
                }
            }
        }
    )
    assert cl.list_pinpoint_in_app_templates(_one_service("pinpoint", client)) == ["t-inapp"]


# --- s3tables table bucket policies -----------------------------------------------------


def test_s3tables_table_bucket_policies_keeps_only_with_policy():
    def get_table_bucket_policy(tableBucketARN):  # noqa: N803
        if tableBucketARN == "arn:tb:1":
            return {"resourcePolicy": "{}"}
        raise _client_error("NotFoundException")

    client = _FakeClient(
        direct={
            "list_table_buckets": {"tableBuckets": [{"arn": "arn:tb:1"}, {"arn": "arn:tb:2"}]},
            "get_table_bucket_policy": get_table_bucket_policy,
        }
    )
    assert cl.list_s3tables_table_bucket_policies(_one_service("s3tables", client)) == ["arn:tb:1"]


# --- sso-admin --------------------------------------------------------------------------


def test_sso_application_assignments_composite_id():
    client = _FakeClient(
        paginated={
            "list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}],
            "list_applications": [{"Applications": [{"ApplicationArn": "arn:app:1"}]}],
            "list_application_assignments": [
                {
                    "ApplicationAssignments": [
                        {
                            "ApplicationArn": "arn:app:1",
                            "PrincipalType": "USER",
                            "PrincipalId": "u-1",
                        }
                    ]
                }
            ],
        }
    )
    assert cl.list_sso_application_assignments(_one_service("sso-admin", client)) == [
        "arn:app:1|USER|u-1"
    ]


def test_sso_instance_access_control_attribute_configs_kept_when_present():
    client = _FakeClient(
        paginated={"list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}]},
        direct={"describe_instance_access_control_attribute_configuration": {}},
    )
    assert cl.list_sso_instance_access_control_attribute_configs(
        _one_service("sso-admin", client)
    ) == ["arn:inst:1"]


def test_sso_instance_access_control_attribute_configs_skips_not_found():
    def describe(**_kw):
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={"list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}]},
        direct={"describe_instance_access_control_attribute_configuration": describe},
    )
    assert (
        cl.list_sso_instance_access_control_attribute_configs(_one_service("sso-admin", client))
        == []
    )


# --- amp resource policies --------------------------------------------------------------


def test_amp_resource_policies_keeps_only_with_policy():
    def describe_resource_policy(workspaceId):  # noqa: N803
        if workspaceId == "ws-1":
            return {}
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={
            "list_workspaces": [
                {
                    "workspaces": [
                        {"workspaceId": "ws-1", "arn": "arn:ws:1"},
                        {"workspaceId": "ws-2", "arn": "arn:ws:2"},
                    ]
                }
            ]
        },
        direct={"describe_resource_policy": describe_resource_policy},
    )
    assert cl.list_amp_resource_policies(_one_service("amp", client)) == ["arn:ws:1"]


# --- kinesis stream consumers -----------------------------------------------------------


def test_kinesis_stream_consumers_manual_pagination():
    consumer_pages = iter(
        [
            {"Consumers": [{"ConsumerARN": "arn:con:1"}], "NextToken": "n"},
            {"Consumers": [{"ConsumerARN": "arn:con:2"}]},
        ]
    )
    client = _FakeClient(
        paginated={"list_streams": [{"StreamSummaries": [{"StreamARN": "arn:stream:1"}]}]},
        direct={"list_stream_consumers": lambda **_kw: next(consumer_pages)},
    )
    assert cl.list_kinesis_stream_consumers(_one_service("kinesis", client)) == [
        "arn:con:1",
        "arn:con:2",
    ]


def test_kinesis_stream_consumers_swallows_error():
    def list_stream_consumers(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_streams": [{"StreamSummaries": [{"StreamARN": "arn:stream:1"}]}]},
        direct={"list_stream_consumers": list_stream_consumers},
    )
    assert cl.list_kinesis_stream_consumers(_one_service("kinesis", client)) == []


# --- neptune-graph private endpoints ----------------------------------------------------


def test_neptunegraph_private_graph_endpoints_composite_id():
    client = _FakeClient(
        paginated={
            "list_graphs": [{"graphs": [{"id": "g-1"}]}],
            "list_private_graph_endpoints": [{"privateGraphEndpoints": [{"vpcId": "vpc-1"}]}],
        }
    )
    assert cl.list_neptunegraph_private_graph_endpoints(_one_service("neptune-graph", client)) == [
        "g-1_vpc-1"
    ]


# --- route53 DNSSEC + key signing keys --------------------------------------------------


def test_route53_dnssec_emits_signing_zones_and_skips_private():
    def get_dnssec(HostedZoneId):  # noqa: N803
        return {
            "Zpub": {"Status": {"ServeSignature": "SIGNING"}, "KeySigningKeys": []},
        }[HostedZoneId]

    client = _FakeClient(
        paginated={
            "list_hosted_zones": [
                {
                    "HostedZones": [
                        {"Id": "/hostedzone/Zpub", "Config": {"PrivateZone": False}},
                        {"Id": "/hostedzone/Zpriv", "Config": {"PrivateZone": True}},
                    ]
                }
            ]
        },
        direct={"get_dnssec": get_dnssec},
    )
    assert cl.list_route53_dnssec(_one_service("route53", client)) == ["Zpub"]


def test_route53_key_signing_keys_composite_id():
    client = _FakeClient(
        paginated={
            "list_hosted_zones": [
                {"HostedZones": [{"Id": "/hostedzone/Zpub", "Config": {"PrivateZone": False}}]}
            ]
        },
        direct={"get_dnssec": {"KeySigningKeys": [{"Name": "ksk-1"}]}},
    )
    assert cl.list_route53_key_signing_keys(_one_service("route53", client)) == ["Zpub|ksk-1"]


def test_route53_hosted_zones_emit_bare_ids_including_private():
    """HostedZone lister emits the bare CCAPI id (``Z...``) for BOTH public and private zones."""
    client = _FakeClient(
        paginated={
            "list_hosted_zones": [
                {
                    "HostedZones": [
                        {"Id": "/hostedzone/Zpublic", "Config": {"PrivateZone": False}},
                        {"Id": "/hostedzone/Zprivate", "Config": {"PrivateZone": True}},
                    ]
                }
            ]
        },
    )
    assert cl.list_route53_hosted_zones(_one_service("route53", client)) == [
        "Zpublic",
        "Zprivate",
    ]


# --- datazone project profiles ----------------------------------------------------------


def test_datazone_project_profiles_composite_id():
    client = _FakeClient(
        paginated={
            "list_domains": [{"items": [{"id": "dom-1"}]}],
            "list_project_profiles": [{"items": [{"id": "prof-1"}]}],
        }
    )
    assert cl.list_datazone_project_profiles(_one_service("datazone", client)) == ["dom-1|prof-1"]


# --- bedrock-agentcore ------------------------------------------------------------------


def test_bedrockagentcore_gateway_targets_composite_id():
    client = _FakeClient(
        paginated={
            "list_gateways": [{"items": [{"gatewayId": "gw-1"}]}],
            "list_gateway_targets": [{"items": [{"targetId": "tgt-1"}]}],
        }
    )
    assert cl.list_bedrockagentcore_gateway_targets(
        _one_service("bedrock-agentcore-control", client)
    ) == ["gw-1|tgt-1"]


def test_bedrockagentcore_browser_custom_emits_ids():
    client = _FakeClient(
        paginated={"list_browsers": [{"browserSummaries": [{"browserId": "br-1"}]}]}
    )
    assert cl.list_bedrockagentcore_browser_custom(
        _one_service("bedrock-agentcore-control", client)
    ) == ["br-1"]


def test_bedrockagentcore_code_interpreter_custom_emits_ids():
    client = _FakeClient(
        paginated={
            "list_code_interpreters": [
                {"codeInterpreterSummaries": [{"codeInterpreterId": "ci-1"}]}
            ]
        }
    )
    assert cl.list_bedrockagentcore_code_interpreter_custom(
        _one_service("bedrock-agentcore-control", client)
    ) == ["ci-1"]


# --- s3vectors vector bucket policies ---------------------------------------------------


def test_s3vectors_vector_bucket_policies_keeps_only_with_policy():
    def get_vector_bucket_policy(vectorBucketName):  # noqa: N803
        if vectorBucketName == "vb-1":
            return {}
        raise _client_error("NotFoundException")

    client = _FakeClient(
        direct={
            "list_vector_buckets": {
                "vectorBuckets": [
                    {"vectorBucketName": "vb-1", "vectorBucketArn": "arn:vb:1"},
                    {"vectorBucketName": "vb-2", "vectorBucketArn": "arn:vb:2"},
                ]
            },
            "get_vector_bucket_policy": get_vector_bucket_policy,
        }
    )
    assert cl.list_s3vectors_vector_bucket_policies(_one_service("s3vectors", client)) == [
        "arn:vb:1"
    ]


# --- cleanroomsml configured-model-algorithm associations -------------------------------


def test_cleanroomsml_configured_model_algorithm_associations_emits_arns():
    cleanrooms = _FakeClient(
        paginated={"list_memberships": [{"membershipSummaries": [{"id": "mem-1"}]}]}
    )
    cleanroomsml = _FakeClient(
        paginated={
            "list_configured_model_algorithm_associations": [
                {
                    "configuredModelAlgorithmAssociations": [
                        {"configuredModelAlgorithmAssociationArn": "arn:cmaa:1"}
                    ]
                }
            ]
        }
    )
    session = _session({"cleanrooms": cleanrooms, "cleanroomsml": cleanroomsml})
    assert cl.list_cleanroomsml_configured_model_algorithm_associations(session) == ["arn:cmaa:1"]


def test_cleanroomsml_swallows_per_membership_error():
    cleanrooms = _FakeClient(
        paginated={"list_memberships": [{"membershipSummaries": [{"id": "mem-1"}]}]}
    )

    def get_paginator(op):
        raise _client_error("AccessDenied")

    cleanroomsml = _FakeClient()
    cleanroomsml.get_paginator = get_paginator  # type: ignore[method-assign]
    session = _session({"cleanrooms": cleanrooms, "cleanroomsml": cleanroomsml})
    assert cl.list_cleanroomsml_configured_model_algorithm_associations(session) == []


# --- per-parent error-swallow branches (child paginator raises; parent succeeds) --------
# Each case pins one lister's ``except (ClientError, BotoCoreError)`` continue/log branch:
# the parent op paginates one item, then the child paginator raises and the item is dropped.


@pytest.mark.parametrize(
    ("fn", "service", "parent_pages", "raise_ops"),
    [
        (
            cl.list_amplify_domain_associations,
            "amplify",
            {"list_apps": [{"apps": [{"appId": "app-1"}]}]},
            {"list_domain_associations"},
        ),
        (
            cl.list_globalaccelerator_listeners,
            "globalaccelerator",
            {"list_accelerators": [{"Accelerators": [{"AcceleratorArn": "arn:acc:1"}]}]},
            {"list_listeners"},
        ),
        (
            cl.list_globalaccelerator_endpoint_groups,
            "globalaccelerator",
            {"list_accelerators": [{"Accelerators": [{"AcceleratorArn": "arn:acc:1"}]}]},
            {"list_listeners"},
        ),
        (
            cl.list_guardduty_ip_sets,
            "guardduty",
            {"list_detectors": [{"DetectorIds": ["d1"]}]},
            {"list_ip_sets"},
        ),
        (
            cl.list_guardduty_threat_intel_sets,
            "guardduty",
            {"list_detectors": [{"DetectorIds": ["d1"]}]},
            {"list_threat_intel_sets"},
        ),
        (
            cl.list_guardduty_threat_entity_sets,
            "guardduty",
            {"list_detectors": [{"DetectorIds": ["d1"]}]},
            {"list_threat_entity_sets"},
        ),
        (
            cl.list_guardduty_trusted_entity_sets,
            "guardduty",
            {"list_detectors": [{"DetectorIds": ["d1"]}]},
            {"list_trusted_entity_sets"},
        ),
        (
            cl.list_guardduty_members,
            "guardduty",
            {"list_detectors": [{"DetectorIds": ["d1"]}]},
            {"list_members"},
        ),
        (
            cl.list_location_tracker_consumers,
            "location",
            {"list_trackers": [{"Entries": [{"TrackerName": "trk-1"}]}]},
            {"list_tracker_consumers"},
        ),
        (
            cl.list_organizations_organizational_units,
            "organizations",
            {"list_roots": [{"Roots": [{"Id": "r-1"}]}]},
            {"list_organizational_units_for_parent"},
        ),
        (
            cl.list_connect_security_keys,
            "connect",
            {"list_instances": [{"InstanceSummaryList": [{"Id": "inst-1"}]}]},
            {"list_security_keys"},
        ),
        (
            cl.list_greengrass_logger_definition_versions,
            "greengrass",
            {"list_logger_definitions": [{"Definitions": [{"Id": "def-1"}]}]},
            {"list_logger_definition_versions"},
        ),
        (
            cl.list_verifiedpermissions_identity_sources,
            "verifiedpermissions",
            {"list_policy_stores": [{"policyStores": [{"policyStoreId": "ps-1"}]}]},
            {"list_identity_sources"},
        ),
        (
            cl.list_pcaconnectorad_service_principal_names,
            "pca-connector-ad",
            {"list_directory_registrations": [{"DirectoryRegistrations": [{"Arn": "arn:reg:1"}]}]},
            {"list_service_principal_names"},
        ),
        (
            cl.list_medialive_channel_placement_groups,
            "medialive",
            {"list_clusters": [{"Clusters": [{"Id": "clu-1"}]}]},
            {"list_channel_placement_groups"},
        ),
        (
            cl.list_qconnect_ai_agents,
            "qconnect",
            {"list_assistants": [{"assistantSummaries": [{"assistantId": "asst-1"}]}]},
            {"list_ai_agents"},
        ),
        (
            cl.list_backup_restore_testing_selections,
            "backup",
            {
                "list_restore_testing_plans": [
                    {"RestoreTestingPlans": [{"RestoreTestingPlanName": "plan-1"}]}
                ]
            },
            {"list_restore_testing_selections"},
        ),
        (
            cl.list_iotsitewise_projects,
            "iotsitewise",
            {"list_portals": [{"portalSummaries": [{"id": "portal-1"}]}]},
            {"list_projects"},
        ),
        (
            cl.list_lambda_urls,
            "lambda",
            {"list_functions": [{"Functions": [{"FunctionName": "fn-1"}]}]},
            {"list_function_url_configs"},
        ),
        (
            cl.list_sso_application_assignments,
            "sso-admin",
            {"list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}]},
            {"list_applications"},
        ),
        (
            cl.list_networkmanager_transit_gateway_registrations,
            "networkmanager",
            {"describe_global_networks": [{"GlobalNetworks": [{"GlobalNetworkId": "gn-1"}]}]},
            {"get_transit_gateway_registrations"},
        ),
        (
            cl.list_iam_user_to_group_additions,
            "iam",
            {"list_groups": [{"Groups": [{"GroupName": "grp-1"}]}]},
            {"get_group"},
        ),
        (
            cl.list_datazone_project_profiles,
            "datazone",
            {"list_domains": [{"items": [{"id": "dom-1"}]}]},
            {"list_project_profiles"},
        ),
        (
            cl.list_bedrockagentcore_gateway_targets,
            "bedrock-agentcore-control",
            {"list_gateways": [{"items": [{"gatewayId": "gw-1"}]}]},
            {"list_gateway_targets"},
        ),
        (
            cl.list_neptunegraph_private_graph_endpoints,
            "neptune-graph",
            {"list_graphs": [{"graphs": [{"id": "g-1"}]}]},
            {"list_private_graph_endpoints"},
        ),
        (
            cl.list_apigatewayv2_integration_responses,
            "apigatewayv2",
            {"get_apis": [{"Items": [{"ApiId": "api-1"}]}]},
            {"get_integrations"},
        ),
        (
            cl.list_apigatewayv2_route_responses,
            "apigatewayv2",
            {"get_apis": [{"Items": [{"ApiId": "api-1"}]}]},
            {"get_routes"},
        ),
        (
            cl.list_qbusiness_data_sources,
            "qbusiness",
            {"list_applications": [{"applications": [{"applicationId": "app-1"}]}]},
            {"list_indices"},
        ),
        (
            cl.list_pcaconnectorad_template_group_access_control_entries,
            "pca-connector-ad",
            {"list_connectors": [{"Connectors": [{"Arn": "arn:conn:1"}]}]},
            {"list_templates"},
        ),
        (
            cl.list_rtbfabric_link_routing_rules,
            "rtbfabric",
            {
                "list_requester_gateways": [{"gatewayIds": ["gw-1"]}],
                "list_responder_gateways": [{"gatewayIds": []}],
            },
            {"list_links"},
        ),
    ],
)
def test_parent_child_lister_swallows_child_error(fn, service, parent_pages, raise_ops):
    client = _raising_child_client(parent_pages, raise_ops)
    assert fn(_one_service(service, client)) == []


def test_rtbfabric_swallows_gateway_list_error():
    """A raising gateway-list op is swallowed, leaving no gateways to walk."""
    client = _raising_child_client({}, {"list_requester_gateways", "list_responder_gateways"})
    assert cl.list_rtbfabric_link_routing_rules(_one_service("rtbfabric", client)) == []


def test_qconnect_ai_agent_versions_swallows_agent_list_error():
    client = _raising_child_client(
        {"list_assistants": [{"assistantSummaries": [{"assistantId": "asst-1"}]}]},
        {"list_ai_agents"},
    )
    assert cl.list_qconnect_ai_agent_versions(_one_service("qconnect", client)) == []


def test_config_remediation_configurations_swallows_batch_error():
    def describe_remediation_configurations(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"describe_config_rules": [{"ConfigRules": [{"ConfigRuleName": "rule-a"}]}]},
        direct={"describe_remediation_configurations": describe_remediation_configurations},
    )
    assert cl.list_config_remediation_configurations(_one_service("config", client)) == []


def test_ec2_route_server_associations_swallows_error():
    def get_route_server_associations(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"describe_route_servers": [{"RouteServers": [{"RouteServerId": "rs-1"}]}]},
        direct={"get_route_server_associations": get_route_server_associations},
    )
    assert cl.list_ec2_route_server_associations(_one_service("ec2", client)) == []


def test_ec2_route_server_propagations_swallows_error():
    def get_route_server_propagations(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"describe_route_servers": [{"RouteServers": [{"RouteServerId": "rs-1"}]}]},
        direct={"get_route_server_propagations": get_route_server_propagations},
    )
    assert cl.list_ec2_route_server_propagations(_one_service("ec2", client)) == []


def test_ec2_vpc_endpoint_service_permissions_swallows_error():
    def describe_vpc_endpoint_service_permissions(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={
            "describe_vpc_endpoint_service_configurations": [
                {"ServiceConfigurations": [{"ServiceId": "svc-1"}]}
            ]
        },
        direct={
            "describe_vpc_endpoint_service_permissions": describe_vpc_endpoint_service_permissions
        },
    )
    assert cl.list_ec2_vpc_endpoint_service_permissions(_one_service("ec2", client)) == []


def test_verifiedpermissions_identity_sources_swallows_error():
    client = _raising_child_client(
        {"list_policy_stores": [{"policyStores": [{"policyStoreId": "ps-1"}]}]},
        {"list_identity_sources"},
    )
    assert (
        cl.list_verifiedpermissions_identity_sources(_one_service("verifiedpermissions", client))
        == []
    )


def test_secretsmanager_resource_policies_swallows_error():
    def get_resource_policy(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_secrets": [{"SecretList": [{"ARN": "arn:sec:1"}]}]},
        direct={"get_resource_policy": get_resource_policy},
    )
    assert cl.list_secretsmanager_resource_policies(_one_service("secretsmanager", client)) == []


def test_pinpoint_application_settings_swallows_error():
    def get_application_settings(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        direct={
            "get_apps": {"ApplicationsResponse": {"Item": [{"Id": "app-1"}]}},
            "get_application_settings": get_application_settings,
        }
    )
    assert cl.list_pinpoint_application_settings(_one_service("pinpoint", client)) == []


def test_pinpoint_channels_logs_non_notfound_error():
    def get_sms_channel(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        direct={
            "get_apps": {"ApplicationsResponse": {"Item": [{"Id": "app-1"}]}},
            "get_sms_channel": get_sms_channel,
        }
    )
    assert cl.list_pinpoint_sms_channels(_one_service("pinpoint", client)) == []


def test_s3files_file_system_policies_logs_unexpected_error(caplog):
    import logging

    def get_file_system_policy(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_file_systems": [{"fileSystems": [{"fileSystemId": "fs-1"}]}]},
        direct={"get_file_system_policy": get_file_system_policy},
    )
    with caplog.at_level(logging.WARNING):
        assert cl.list_s3files_file_system_policies(_one_service("s3files", client)) == []
    assert "s3files.get_file_system_policy skipped" in caplog.text


def test_s3tables_table_bucket_policies_logs_unexpected_error():
    def get_table_bucket_policy(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        direct={
            "list_table_buckets": {"tableBuckets": [{"arn": "arn:tb:1"}]},
            "get_table_bucket_policy": get_table_bucket_policy,
        }
    )
    assert cl.list_s3tables_table_bucket_policies(_one_service("s3tables", client)) == []


def test_sso_application_assignments_swallows_assignment_error():
    client = _raising_child_client(
        {
            "list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}],
            "list_applications": [{"Applications": [{"ApplicationArn": "arn:app:1"}]}],
        },
        {"list_application_assignments"},
    )
    assert cl.list_sso_application_assignments(_one_service("sso-admin", client)) == []


def test_sso_instance_access_control_configs_logs_unexpected_error():
    def describe(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={"list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}]},
        direct={"describe_instance_access_control_attribute_configuration": describe},
    )
    assert (
        cl.list_sso_instance_access_control_attribute_configs(_one_service("sso-admin", client))
        == []
    )


def test_amp_resource_policies_swallows_describe_error():
    def describe_resource_policy(**_kw):
        raise _client_error("ResourceNotFoundException")

    client = _FakeClient(
        paginated={
            "list_workspaces": [{"workspaces": [{"workspaceId": "ws-1", "arn": "arn:ws:1"}]}]
        },
        direct={"describe_resource_policy": describe_resource_policy},
    )
    assert cl.list_amp_resource_policies(_one_service("amp", client)) == []


def test_route53_dnssec_swallows_get_dnssec_error():
    def get_dnssec(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={
            "list_hosted_zones": [
                {"HostedZones": [{"Id": "/hostedzone/Zpub", "Config": {"PrivateZone": False}}]}
            ]
        },
        direct={"get_dnssec": get_dnssec},
    )
    assert cl.list_route53_dnssec(_one_service("route53", client)) == []


def test_route53_key_signing_keys_swallows_get_dnssec_error():
    def get_dnssec(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={
            "list_hosted_zones": [
                {"HostedZones": [{"Id": "/hostedzone/Zpub", "Config": {"PrivateZone": False}}]}
            ]
        },
        direct={"get_dnssec": get_dnssec},
    )
    assert cl.list_route53_key_signing_keys(_one_service("route53", client)) == []


def test_s3vectors_vector_bucket_policies_swallows_error():
    def get_vector_bucket_policy(**_kw):
        raise _client_error("NotFoundException")

    client = _FakeClient(
        direct={
            "list_vector_buckets": {
                "vectorBuckets": [{"vectorBucketName": "vb-1", "vectorBucketArn": "arn:vb:1"}]
            },
            "get_vector_bucket_policy": get_vector_bucket_policy,
        }
    )
    assert cl.list_s3vectors_vector_bucket_policies(_one_service("s3vectors", client)) == []


def test_mediapackagev2_endpoints_swallows_list_channels_error():
    """A raising list_channels leaves the group with no endpoints (outer lister returns [])."""
    client = _raising_child_client(
        {"list_channel_groups": [{"Items": [{"ChannelGroupName": "grp-1"}]}]},
        {"list_channels"},
    )
    assert (
        cl.list_mediapackagev2_origin_endpoint_policies(_one_service("mediapackagev2", client))
        == []
    )


def test_mediapackagev2_endpoints_swallows_list_origin_endpoints_error():
    client = _raising_child_client(
        {
            "list_channel_groups": [{"Items": [{"ChannelGroupName": "grp-1"}]}],
            "list_channels": [{"Items": [{"ChannelName": "chan-1"}]}],
        },
        {"list_origin_endpoints"},
    )
    assert (
        cl.list_mediapackagev2_origin_endpoint_policies(_one_service("mediapackagev2", client))
        == []
    )


def test_mediapackagev2_skips_group_without_name():
    """A channel group with no ChannelGroupName is skipped before any child call."""
    client = _FakeClient(paginated={"list_channel_groups": [{"Items": [{"other": "x"}]}]})
    assert (
        cl.list_mediapackagev2_origin_endpoint_policies(_one_service("mediapackagev2", client))
        == []
    )


# --- remaining inner-branch coverage: nested error swallows + BotoCoreError paths --------


def test_servicecatalog_service_action_swallows_artifact_error():
    def list_provisioning_artifacts(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        paginated={
            "search_products_as_admin": [
                {"ProductViewDetails": [{"ProductViewSummary": {"ProductId": "prod-1"}}]}
            ]
        },
        direct={"list_provisioning_artifacts": list_provisioning_artifacts},
    )
    assert (
        cl.list_servicecatalog_service_action_associations(_one_service("servicecatalog", client))
        == []
    )


def test_servicecatalog_service_action_swallows_inner_paginator_error():
    client = _raising_child_client(
        {
            "search_products_as_admin": [
                {"ProductViewDetails": [{"ProductViewSummary": {"ProductId": "prod-1"}}]}
            ]
        },
        {"list_service_actions_for_provisioning_artifact"},
    )
    client._direct["list_provisioning_artifacts"] = {  # type: ignore[attr-defined]
        "ProvisioningArtifactDetails": [{"Id": "pa-1"}]
    }
    assert (
        cl.list_servicecatalog_service_action_associations(_one_service("servicecatalog", client))
        == []
    )


def test_servicecatalog_tag_option_swallows_resource_error():
    def list_resources_for_tag_option(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(
        direct={
            "list_tag_options": {"TagOptionDetails": [{"Id": "to-1"}]},
            "list_resources_for_tag_option": list_resources_for_tag_option,
        }
    )
    assert (
        cl.list_servicecatalog_tag_option_associations(_one_service("servicecatalog", client)) == []
    )


def test_servicecatalog_tag_option_reraises_other_client_error():
    def list_tag_options(**_kw):
        raise _client_error("AccessDenied")

    client = _FakeClient(direct={"list_tag_options": list_tag_options})
    with pytest.raises(ClientError):
        cl.list_servicecatalog_tag_option_associations(_one_service("servicecatalog", client))


def test_smsvoice_swallows_describe_paginator_error():
    """A raising describe paginator for one resource kind is swallowed; no arns collected."""
    client = _raising_child_client(
        {},
        {
            "describe_phone_numbers",
            "describe_pools",
            "describe_sender_ids",
            "describe_opt_out_lists",
        },
    )
    assert cl.list_smsvoice_resource_policies(_one_service("pinpoint-sms-voice-v2", client)) == []


def test_qbusiness_data_sources_swallows_inner_error():
    client = _raising_child_client(
        {
            "list_applications": [{"applications": [{"applicationId": "app-1"}]}],
            "list_indices": [{"indices": [{"indexId": "idx-1"}]}],
        },
        {"list_data_sources"},
    )
    assert cl.list_qbusiness_data_sources(_one_service("qbusiness", client)) == []


def test_qconnect_ai_agent_versions_swallows_inner_error():
    client = _raising_child_client(
        {
            "list_assistants": [{"assistantSummaries": [{"assistantId": "asst-1"}]}],
            "list_ai_agents": [{"aiAgentSummaries": [{"aiAgentId": "agent-1"}]}],
        },
        {"list_ai_agent_versions"},
    )
    assert cl.list_qconnect_ai_agent_versions(_one_service("qconnect", client)) == []


def test_apigatewayv2_integration_responses_swallows_inner_error():
    client = _raising_child_client(
        {
            "get_apis": [{"Items": [{"ApiId": "api-1"}]}],
            "get_integrations": [{"Items": [{"IntegrationId": "int-1"}]}],
        },
        {"get_integration_responses"},
    )
    assert cl.list_apigatewayv2_integration_responses(_one_service("apigatewayv2", client)) == []


def test_apigatewayv2_route_responses_swallows_inner_error():
    client = _raising_child_client(
        {
            "get_apis": [{"Items": [{"ApiId": "api-1"}]}],
            "get_routes": [{"Items": [{"RouteId": "route-1"}]}],
        },
        {"get_route_responses"},
    )
    assert cl.list_apigatewayv2_route_responses(_one_service("apigatewayv2", client)) == []


def test_pcaconnectorad_template_entries_swallows_inner_error():
    client = _raising_child_client(
        {
            "list_connectors": [{"Connectors": [{"Arn": "arn:conn:1"}]}],
            "list_templates": [{"Templates": [{"Arn": "arn:tmpl:1"}]}],
        },
        {"list_template_group_access_control_entries"},
    )
    assert (
        cl.list_pcaconnectorad_template_group_access_control_entries(
            _one_service("pca-connector-ad", client)
        )
        == []
    )


def test_rtbfabric_swallows_inner_routing_rules_error():
    client = _raising_child_client(
        {
            "list_requester_gateways": [{"gatewayIds": ["gw-1"]}],
            "list_responder_gateways": [{"gatewayIds": []}],
            "list_links": [{"links": [{"linkId": "link-1"}]}],
        },
        {"list_link_routing_rules"},
    )
    assert cl.list_rtbfabric_link_routing_rules(_one_service("rtbfabric", client)) == []


def _botocore_error():
    from botocore.exceptions import BotoCoreError

    return BotoCoreError()


def test_logs_transformers_swallows_botocore_error():
    def get_transformer(**_kw):
        raise _botocore_error()

    client = _FakeClient(
        paginated={"describe_log_groups": [{"logGroups": [{"logGroupName": "/g"}]}]},
        direct={"get_transformer": get_transformer},
    )
    assert cl.list_logs_transformers(_one_service("logs", client)) == []


def test_s3files_file_system_policies_swallows_botocore_error():
    def get_file_system_policy(**_kw):
        raise _botocore_error()

    client = _FakeClient(
        paginated={"list_file_systems": [{"fileSystems": [{"fileSystemId": "fs-1"}]}]},
        direct={"get_file_system_policy": get_file_system_policy},
    )
    assert cl.list_s3files_file_system_policies(_one_service("s3files", client)) == []


def test_s3tables_table_bucket_policies_swallows_botocore_error():
    def get_table_bucket_policy(**_kw):
        raise _botocore_error()

    client = _FakeClient(
        direct={
            "list_table_buckets": {"tableBuckets": [{"arn": "arn:tb:1"}]},
            "get_table_bucket_policy": get_table_bucket_policy,
        }
    )
    assert cl.list_s3tables_table_bucket_policies(_one_service("s3tables", client)) == []


def test_sso_instance_access_control_configs_swallows_botocore_error():
    def describe(**_kw):
        raise _botocore_error()

    client = _FakeClient(
        paginated={"list_instances": [{"Instances": [{"InstanceArn": "arn:inst:1"}]}]},
        direct={"describe_instance_access_control_attribute_configuration": describe},
    )
    assert (
        cl.list_sso_instance_access_control_attribute_configs(_one_service("sso-admin", client))
        == []
    )


def test_pinpoint_channels_swallows_botocore_error():
    def get_sms_channel(**_kw):
        raise _botocore_error()

    client = _FakeClient(
        direct={
            "get_apps": {"ApplicationsResponse": {"Item": [{"Id": "app-1"}]}},
            "get_sms_channel": get_sms_channel,
        }
    )
    assert cl.list_pinpoint_sms_channels(_one_service("pinpoint", client)) == []


def test_mediapackagev2_has_policy_botocore_error_returns_false():
    def get_origin_endpoint_policy(**_kw):
        raise _botocore_error()

    client = _FakeClient(direct={"get_origin_endpoint_policy": get_origin_endpoint_policy})
    assert cl._mediapackagev2_has_policy(client, "grp", "chan", "ep") is False


def test_pinpoint_in_app_templates_manual_pagination():
    template_pages = iter(
        [
            {
                "TemplatesResponse": {
                    "Item": [{"TemplateName": "t-1", "TemplateType": "INAPP"}],
                    "NextToken": "n",
                }
            },
            {"TemplatesResponse": {"Item": [{"TemplateName": "t-2", "TemplateType": "INAPP"}]}},
        ]
    )
    client = _FakeClient(direct={"list_templates": lambda **_kw: next(template_pages)})
    assert cl.list_pinpoint_in_app_templates(_one_service("pinpoint", client)) == ["t-1", "t-2"]


# --- defensive skip / multi-page-continuation branches inside target listers ------------


def test_codestar_sync_configurations_multi_page_marker():
    """A second sync-configurations page (via NextToken) is walked."""
    sync_pages = iter(
        [
            {
                "SyncConfigurations": [{"ResourceName": "s1", "SyncType": "CFN_STACK_SYNC"}],
                "NextToken": "n",
            },
            {"SyncConfigurations": [{"ResourceName": "s2", "SyncType": "CFN_STACK_SYNC"}]},
        ]
    )
    client = _FakeClient(
        direct={
            "list_repository_links": {"RepositoryLinks": [{"RepositoryLinkId": "rl-1"}]},
            "list_sync_configurations": lambda **_kw: next(sync_pages),
        }
    )
    assert cl.list_codestar_connections_sync_configurations(
        _one_service("codestar-connections", client)
    ) == ["s1|CFN_STACK_SYNC", "s2|CFN_STACK_SYNC"]


def test_servicecatalog_service_action_skips_products_and_artifacts_without_id():
    """Products with no ProductId and artifacts with no Id are skipped."""
    client = _FakeClient(
        paginated={
            "search_products_as_admin": [{"ProductViewDetails": [{"ProductViewSummary": {}}]}]
        },
    )
    assert (
        cl.list_servicecatalog_service_action_associations(_one_service("servicecatalog", client))
        == []
    )


def test_servicecatalog_service_action_skips_artifact_without_id():
    client = _FakeClient(
        paginated={
            "search_products_as_admin": [
                {"ProductViewDetails": [{"ProductViewSummary": {"ProductId": "prod-1"}}]}
            ]
        },
        direct={"list_provisioning_artifacts": {"ProvisioningArtifactDetails": [{"other": "x"}]}},
    )
    assert (
        cl.list_servicecatalog_service_action_associations(_one_service("servicecatalog", client))
        == []
    )


def test_servicecatalog_tag_option_multi_page_marker():
    """A second resources page (via PageToken) is walked for one tag option."""
    resource_pages = iter(
        [
            {"ResourceDetails": [{"Id": "r1"}], "PageToken": "n"},
            {"ResourceDetails": [{"Id": "r2"}]},
        ]
    )
    client = _FakeClient(
        direct={
            "list_tag_options": {"TagOptionDetails": [{"Id": "to-1"}]},
            "list_resources_for_tag_option": lambda **_kw: next(resource_pages),
        }
    )
    assert cl.list_servicecatalog_tag_option_associations(
        _one_service("servicecatalog", client)
    ) == ["to-1|r1", "to-1|r2"]


def test_globalaccelerator_endpoint_groups_swallows_endpoint_group_error():
    client = _raising_child_client(
        {
            "list_accelerators": [{"Accelerators": [{"AcceleratorArn": "arn:acc:1"}]}],
            "list_listeners": [{"Listeners": [{"ListenerArn": "arn:lis:1"}]}],
        },
        {"list_endpoint_groups"},
    )
    assert (
        cl.list_globalaccelerator_endpoint_groups(_one_service("globalaccelerator", client)) == []
    )


def test_connect_instance_storage_configs_skips_instance_without_arn():
    client = _FakeClient(
        paginated={"list_instances": [{"InstanceSummaryList": [{"Id": "inst-1"}]}]}
    )
    assert cl.list_connect_instance_storage_configs(_one_service("connect", client)) == []


def test_networkmanager_transit_gateway_registrations_skips_network_without_id():
    client = _FakeClient(
        paginated={"describe_global_networks": [{"GlobalNetworks": [{"other": "x"}]}]}
    )
    assert (
        cl.list_networkmanager_transit_gateway_registrations(_one_service("networkmanager", client))
        == []
    )


def test_iam_user_to_group_additions_skips_group_without_name():
    client = _FakeClient(paginated={"list_groups": [{"Groups": [{"other": "x"}]}]})
    assert cl.list_iam_user_to_group_additions(_one_service("iam", client)) == []


def test_medialive_channel_placement_groups_skips_cluster_without_id():
    client = _FakeClient(paginated={"list_clusters": [{"Clusters": [{"other": "x"}]}]})
    assert cl.list_medialive_channel_placement_groups(_one_service("medialive", client)) == []


def test_amp_resource_policies_skips_workspace_without_ids():
    client = _FakeClient(paginated={"list_workspaces": [{"workspaces": [{"other": "x"}]}]})
    assert cl.list_amp_resource_policies(_one_service("amp", client)) == []
