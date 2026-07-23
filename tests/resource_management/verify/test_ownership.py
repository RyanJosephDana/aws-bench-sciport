"""Tests for the live AWS-managed ownership probe."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.verify.ownership import AwsManagedOwnershipProbe


def _client_error(code: str) -> ClientError:
    """A botocore ClientError, the error type the probes fail open on."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "DescribeNetworkInterfaces")


def _session_with(kms=None, ec2=None) -> MagicMock:
    session = MagicMock()
    clients = {"kms": kms or MagicMock(), "ec2": ec2 or MagicMock()}
    session.client.side_effect = lambda name, **kwargs: clients[name]
    return session


def _ec2_paginating(network_interfaces) -> MagicMock:
    """An ec2 mock whose describe_network_interfaces paginator yields one page."""
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [
        {"NetworkInterfaces": network_interfaces}
    ]
    return ec2


# -- KMS keys --


def test_excludes_aws_managed_kms_key():
    kms = MagicMock()
    kms.describe_key.return_value = {"KeyMetadata": {"KeyManager": "AWS"}}
    probe = AwsManagedOwnershipProbe(_session_with(kms=kms), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::KMS::Key": [{"Identifier": "key-aws"}]})
    assert out == {}


def test_keeps_customer_kms_key():
    kms = MagicMock()
    kms.describe_key.return_value = {"KeyMetadata": {"KeyManager": "CUSTOMER"}}
    probe = AwsManagedOwnershipProbe(_session_with(kms=kms), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::KMS::Key": [{"Identifier": "key-cust"}]})
    assert out == {"AWS::KMS::Key": [{"Identifier": "key-cust"}]}


def test_keeps_kms_key_when_probe_errors():
    kms = MagicMock()
    kms.describe_key.side_effect = _client_error("AccessDeniedException")
    probe = AwsManagedOwnershipProbe(_session_with(kms=kms), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::KMS::Key": [{"Identifier": "key-x"}]})
    assert out == {"AWS::KMS::Key": [{"Identifier": "key-x"}]}


# -- ENIs --


def test_excludes_requester_managed_eni():
    ec2 = _ec2_paginating(
        [{"NetworkInterfaceId": "eni-1", "RequesterManaged": True, "Description": "x"}]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-1"}]})
    assert out == {}


def test_excludes_service_described_eni():
    ec2 = _ec2_paginating(
        [
            {
                "NetworkInterfaceId": "eni-2",
                "RequesterManaged": False,
                "Description": "Amazon EKS abc-cluster",
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-2"}]})
    assert out == {}


def test_keeps_customer_eni():
    ec2 = _ec2_paginating(
        [
            {
                "NetworkInterfaceId": "eni-3",
                "RequesterManaged": False,
                "InterfaceType": "interface",
                "Description": "my app eni",
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-3"}]})
    assert out == {"AWS::EC2::NetworkInterface": [{"Identifier": "eni-3"}]}


def test_excludes_lambda_eni_by_interface_type():
    """A Lambda-in-VPC ENI reports RequesterManaged=False + InterfaceType='lambda'.

    Observed live: it dodges both the
    RequesterManaged flag and the description denylist, so reset looped on it.
    The InterfaceType != 'interface' signal is what catches it.
    """
    ec2 = _ec2_paginating(
        [
            {
                "NetworkInterfaceId": "eni-lambda",
                "RequesterManaged": False,
                "InterfaceType": "lambda",
                "Description": "AWS Lambda VPC ENI: my-fn",
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-lambda"}]})
    assert out == {}


def test_excludes_vpc_endpoint_eni_by_requester_managed():
    """A VPC-endpoint ENI is RequesterManaged=True (InterfaceType='vpc_endpoint').

    Live-observed shape: the flag alone is enough here.
    """
    ec2 = _ec2_paginating(
        [
            {
                "NetworkInterfaceId": "eni-vpce",
                "RequesterManaged": True,
                "InterfaceType": "vpc_endpoint",
                "Description": "VPC Endpoint Interface vpce-x",
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-vpce"}]})
    assert out == {}


def test_excludes_rds_eni_by_requester_managed_despite_interface_type():
    """An RDS ENI is RequesterManaged=True but reports InterfaceType='interface'.

    Observed live: proves an InterfaceType allowlist alone
    is unsafe (it would keep this) — RequesterManaged must remain the primary gate.
    """
    ec2 = _ec2_paginating(
        [
            {
                "NetworkInterfaceId": "eni-rds",
                "RequesterManaged": True,
                "InterfaceType": "interface",
                "Description": "RDSNetworkInterface",
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-rds"}]})
    assert out == {}


def test_keeps_customer_eni_missing_interface_type():
    """A customer ENI with RequesterManaged=False and no InterfaceType is kept.

    Absent InterfaceType must not be treated as 'not interface' (which would
    wrongly drop customer ENIs). Defaults to the customer type.
    """
    ec2 = _ec2_paginating(
        [{"NetworkInterfaceId": "eni-bare", "RequesterManaged": False, "Description": "app"}]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-bare"}]})
    assert out == {"AWS::EC2::NetworkInterface": [{"Identifier": "eni-bare"}]}


def test_keeps_eni_when_not_found():
    ec2 = _ec2_paginating([])
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed({"AWS::EC2::NetworkInterface": [{"Identifier": "eni-4"}]})
    assert out == {"AWS::EC2::NetworkInterface": [{"Identifier": "eni-4"}]}


def test_batches_all_enis_into_one_describe_call():
    """A set of ENIs is resolved in ONE batched describe (one paginate call)."""
    ec2 = _ec2_paginating(
        [
            {"NetworkInterfaceId": "eni-svc", "RequesterManaged": True, "Description": "x"},
            {"NetworkInterfaceId": "eni-cust", "RequesterManaged": False, "Description": "app"},
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed(
        {
            "AWS::EC2::NetworkInterface": [
                {"Identifier": "eni-svc"},
                {"Identifier": "eni-cust"},
            ]
        }
    )
    # Only the service-owned ENI is dropped; the customer ENI is kept.
    assert out == {"AWS::EC2::NetworkInterface": [{"Identifier": "eni-cust"}]}
    # One batched paginate call carrying every id — not one call per item.
    paginate = ec2.get_paginator.return_value.paginate
    paginate.assert_called_once()
    _, kwargs = paginate.call_args
    # The id filter (not NetworkInterfaceIds=) is used so a stale id can't fail the batch.
    assert kwargs["Filters"][0]["Name"] == "network-interface-id"
    assert kwargs["Filters"][0]["Values"] == ["eni-svc", "eni-cust"]


def test_keeps_all_enis_when_batched_describe_errors():
    """Fail-open: a describe error keeps EVERY ENI in the set (none are dropped)."""
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.side_effect = _client_error("RequestLimitExceeded")
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed(
        {
            "AWS::EC2::NetworkInterface": [
                {"Identifier": "eni-a"},
                {"Identifier": "eni-b"},
            ]
        }
    )
    assert out == {"AWS::EC2::NetworkInterface": [{"Identifier": "eni-a"}, {"Identifier": "eni-b"}]}


def test_ec2_client_built_with_adaptive_retry_config():
    """The ownership probe's ec2 client carries adaptive retry, not boto3's weak default.

    This probe is the one that throttled and made reset loop (RC10/RC6-B).
    """
    session = _session_with(ec2=_ec2_paginating([]))
    probe = AwsManagedOwnershipProbe(session, "us-east-1")
    _ = probe._ec2  # trigger lazy build
    ec2_calls = [c for c in session.client.call_args_list if c.args and c.args[0] == "ec2"]
    assert len(ec2_calls) == 1
    config = ec2_calls[0].kwargs.get("config")
    assert config is not None, "ec2 client built without a botocore Config"
    assert config.retries.get("mode") == "adaptive"
    assert config.retries.get("max_attempts", 0) >= 8


def test_reuses_single_ec2_client_across_eni_items():
    """The EC2 client is built once and reused, not one per item."""
    ec2 = _ec2_paginating([])
    session = _session_with(ec2=ec2)
    probe = AwsManagedOwnershipProbe(session, "us-east-1")
    probe.exclude_aws_managed(
        {"AWS::EC2::NetworkInterface": [{"Identifier": "eni-a"}, {"Identifier": "eni-b"}]}
    )
    ec2_calls = [c for c in session.client.call_args_list if c.args[0] == "ec2"]
    assert len(ec2_calls) == 1


# -- ENI attachments (resolved back to parent ENI ownership) --


def test_excludes_service_owned_eni_attachment():
    # The attachment-id filter resolves to a service-managed ENI whose attachment
    # id maps back to the requested attachment.
    ec2 = _ec2_paginating(
        [
            {
                "RequesterManaged": True,
                "Description": "Amazon EKS x",
                "Attachment": {"AttachmentId": "ela-attach-1"},
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed(
        {"AWS::EC2::NetworkInterfaceAttachment": [{"Identifier": "ela-attach-1"}]}
    )
    assert out == {}
    # Confirms it filtered by attachment-id, not raw id.
    _, kwargs = ec2.get_paginator.return_value.paginate.call_args
    assert kwargs["Filters"][0]["Name"] == "attachment.attachment-id"
    assert kwargs["Filters"][0]["Values"] == ["ela-attach-1"]


def test_keeps_customer_eni_attachment():
    ec2 = _ec2_paginating(
        [
            {
                "RequesterManaged": False,
                "Description": "my app eni",
                "Attachment": {"AttachmentId": "eni-attach-cust"},
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed(
        {"AWS::EC2::NetworkInterfaceAttachment": [{"Identifier": "eni-attach-cust"}]}
    )
    assert out == {"AWS::EC2::NetworkInterfaceAttachment": [{"Identifier": "eni-attach-cust"}]}


def test_keeps_eni_attachment_when_unresolvable():
    ec2 = _ec2_paginating([])
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed(
        {"AWS::EC2::NetworkInterfaceAttachment": [{"Identifier": "eni-attach-x"}]}
    )
    assert out == {"AWS::EC2::NetworkInterfaceAttachment": [{"Identifier": "eni-attach-x"}]}


def test_batches_all_eni_attachments_into_one_describe_call():
    """A set of attachments is resolved in ONE batched describe (one paginate call)."""
    ec2 = _ec2_paginating(
        [
            {
                "RequesterManaged": True,
                "Description": "x",
                "Attachment": {"AttachmentId": "ela-attach-svc"},
            }
        ]
    )
    probe = AwsManagedOwnershipProbe(_session_with(ec2=ec2), "us-east-1")
    out = probe.exclude_aws_managed(
        {
            "AWS::EC2::NetworkInterfaceAttachment": [
                {"Identifier": "ela-attach-svc"},
                {"Identifier": "eni-attach-cust"},
            ]
        }
    )
    assert out == {"AWS::EC2::NetworkInterfaceAttachment": [{"Identifier": "eni-attach-cust"}]}
    paginate = ec2.get_paginator.return_value.paginate
    paginate.assert_called_once()
    _, kwargs = paginate.call_args
    assert kwargs["Filters"][0]["Values"] == ["ela-attach-svc", "eni-attach-cust"]


# -- pass-through --


def test_other_types_pass_through_untouched():
    probe = AwsManagedOwnershipProbe(_session_with(), "us-east-1")
    payload = {"AWS::S3::Bucket": [{"Identifier": "b1"}]}
    assert probe.exclude_aws_managed(payload) == payload


def test_mixed_set_only_filters_probe_types():
    kms = MagicMock()
    kms.describe_key.return_value = {"KeyMetadata": {"KeyManager": "AWS"}}
    probe = AwsManagedOwnershipProbe(_session_with(kms=kms), "us-east-1")
    out = probe.exclude_aws_managed(
        {
            "AWS::KMS::Key": [{"Identifier": "key-aws"}],
            "AWS::S3::Bucket": [{"Identifier": "b1"}],
        }
    )
    assert out == {"AWS::S3::Bucket": [{"Identifier": "b1"}]}
