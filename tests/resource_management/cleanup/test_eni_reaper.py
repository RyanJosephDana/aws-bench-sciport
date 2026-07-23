from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from aws_bench.resource_management.cleanup.handlers.cross_service import (
    EniReapResult,
    reap_vpc_enis,
    reap_vpc_security_groups,
)


def _session_with_ec2() -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    ec2 = MagicMock()
    session.client.return_value = ec2
    return session, ec2


def _avail(nid: str) -> dict:
    return {"NetworkInterfaceId": nid, "Status": "available"}


def _in_use(nid: str, *, requester: bool = False, attach_id: str = "att-1") -> dict:
    eni = {"NetworkInterfaceId": nid, "Status": "in-use", "RequesterManaged": requester}
    if attach_id:
        eni["Attachment"] = {"AttachmentId": attach_id, "Status": "attached"}
    return eni


def _describe_sequence(ec2: MagicMock, *calls: list[dict]) -> None:
    """Each positional arg is the ENI list one describe call should return."""
    ec2.get_paginator.return_value.paginate.side_effect = [
        [{"NetworkInterfaces": list(nics)}] for nics in calls
    ]


def _describe_always(ec2: MagicMock, nics: list[dict]) -> None:
    """Every describe call returns the same ENI list (for deadline-bounded tests)."""
    ec2.get_paginator.return_value.paginate.side_effect = lambda *a, **k: [
        {"NetworkInterfaces": list(nics)}
    ]


def test_no_vpc_ids_returns_empty_without_building_client():
    session, _ = _session_with_ec2()
    result = reap_vpc_enis(session, [])
    assert result.deleted == [] and result.detached == [] and result.remaining == []
    assert result.reaped_any is False
    session.client.assert_not_called()


def test_deletes_available_enis():
    session, ec2 = _session_with_ec2()
    _describe_sequence(ec2, [_avail("eni-1"), _avail("eni-2")], [])
    result = reap_vpc_enis(session, ["vpc-1"])
    assert result.deleted == ["eni-1", "eni-2"]
    assert result.remaining == []
    assert result.reaped_any is True
    assert ec2.delete_network_interface.call_count == 2


@patch("aws_bench.resource_management.cleanup.handlers.cross_service.time.sleep")
def test_force_detaches_then_deletes_customer_managed(_sleep):
    session, ec2 = _session_with_ec2()
    _describe_sequence(
        ec2,
        [_in_use("eni-1", requester=False)],  # pass 1: force-detach
        [_avail("eni-1")],  # pass 2: now available -> delete
        [],  # final sweep
    )
    result = reap_vpc_enis(session, ["vpc-1"], interval=0)
    ec2.detach_network_interface.assert_called_once_with(AttachmentId="att-1", Force=True)
    assert result.detached == ["eni-1"]
    assert result.deleted == ["eni-1"]
    assert result.remaining == []


def test_skips_requester_managed_and_reports_remaining():
    session, ec2 = _session_with_ec2()
    _describe_always(ec2, [_in_use("eni-eks", requester=True)])
    result = reap_vpc_enis(session, ["vpc-1"], timeout=0)
    ec2.delete_network_interface.assert_not_called()
    ec2.detach_network_interface.assert_not_called()
    assert result.deleted == [] and result.detached == []
    assert result.remaining == ["eni-eks"]
    assert result.reaped_any is False


def _session_with_ec2_efs() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Session whose .client() routes to distinct ec2 and efs mocks by service name."""
    session = MagicMock()
    ec2 = MagicMock()
    efs = MagicMock()
    session.client.side_effect = lambda service, **kwargs: {"ec2": ec2, "efs": efs}[service]
    return session, ec2, efs


def _efs_eni(nid: str, mt_id: str) -> dict:
    return {
        "NetworkInterfaceId": nid,
        "Status": "in-use",
        "RequesterManaged": True,
        "InterfaceType": "efs",
        "Description": f"EFS mount target for fs-0367f8d26e (fsmt-{mt_id})",
    }


@patch("aws_bench.resource_management.cleanup.handlers.cross_service.time.sleep")
def test_efs_mount_target_eni_released_by_deleting_mount_target(_sleep):
    """An EFS mount-target ENI is released by deleting its mount target, not skipped.

    Unlike EKS/ELB requester-managed ENIs, an EFS ENI unpins the subnet/VPC in the
    same run: the reaper parses the mount-target id from the ENI description and
    deletes it, then the ENI disappears.
    """
    session, ec2, efs = _session_with_ec2_efs()
    # pass 1: EFS ENI present -> delete its mount target; pass 2: gone (poll-until-gone);
    # final sweep: gone.
    _describe_sequence(ec2, [_efs_eni("eni-efs", "00b6b841d214708d9")], [], [])
    result = reap_vpc_enis(session, ["vpc-1"], interval=0)
    efs.delete_mount_target.assert_called_once_with(MountTargetId="fsmt-00b6b841d214708d9")
    ec2.delete_network_interface.assert_not_called()  # can't delete an EFS ENI directly
    assert result.deleted == ["eni-efs"]
    assert result.remaining == []
    assert result.reaped_any is True


@patch("aws_bench.resource_management.cleanup.handlers.cross_service.time.sleep")
def test_efs_polls_until_mount_target_eni_actually_gone(_sleep):
    """The reaper waits out the async mount-target deletion (poll-until-gone).

    The mount-target delete is issued once; the ENI lingers for a poll, then
    disappears. The reaper must keep polling (not return it as 'remaining' or defer)
    and only count it reaped once it is actually gone.
    """
    session, ec2, efs = _session_with_ec2_efs()
    eni = _efs_eni("eni-efs", "00b6b841d214708d9")
    # present, still present (releasing), gone, final sweep gone
    _describe_sequence(ec2, [eni], [eni], [], [])
    result = reap_vpc_enis(session, ["vpc-1"], interval=0)
    # Issued exactly once despite the ENI appearing in two polls.
    efs.delete_mount_target.assert_called_once_with(MountTargetId="fsmt-00b6b841d214708d9")
    assert result.deleted == ["eni-efs"]
    assert result.remaining == []
    assert result.reaped_any is True


@patch("aws_bench.resource_management.cleanup.handlers.cross_service.time.sleep")
def test_efs_mount_target_not_found_is_treated_as_released(_sleep):
    """A MountTargetNotFound (already deleting) still counts as released."""
    from botocore.exceptions import ClientError as _CE

    session, ec2, efs = _session_with_ec2_efs()
    efs.delete_mount_target.side_effect = _CE(
        {"Error": {"Code": "MountTargetNotFound", "Message": "gone"}}, "DeleteMountTarget"
    )
    _describe_sequence(ec2, [_efs_eni("eni-efs", "0c904c043d2009839")], [], [])
    result = reap_vpc_enis(session, ["vpc-1"], interval=0)
    assert result.deleted == ["eni-efs"]
    assert result.remaining == []


def test_delete_not_found_is_treated_as_gone():
    session, ec2 = _session_with_ec2()
    _describe_sequence(ec2, [_avail("eni-1")], [])
    ec2.delete_network_interface.side_effect = ClientError(
        {"Error": {"Code": "InvalidNetworkInterfaceID.NotFound", "Message": "gone"}},
        "DeleteNetworkInterface",
    )
    result = reap_vpc_enis(session, ["vpc-1"])
    assert result.deleted == ["eni-1"]


def test_describe_error_yields_empty_result():
    session, ec2 = _session_with_ec2()
    ec2.get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "no"}},
        "DescribeNetworkInterfaces",
    )
    result = reap_vpc_enis(session, ["vpc-1"])
    assert result == EniReapResult()


@patch("aws_bench.resource_management.cleanup.handlers.cross_service.time.sleep")
def test_blank_vpc_ids_filtered_out(_sleep):
    session, ec2 = _session_with_ec2()
    result = reap_vpc_enis(session, ["", None])  # type: ignore[list-item]
    assert result == EniReapResult()
    session.client.assert_not_called()


@patch("aws_bench.resource_management.cleanup.handlers.cross_service.time.sleep")
def test_wait_for_release_false_returns_without_polling(_sleep):
    # Only a requester-managed ENI remains; with wait_for_release=False the reaper
    # must NOT idle-poll for the owner (even with a large timeout) — it reports it
    # and returns immediately. This is the VPC pre-delete hook's mode.
    session, ec2 = _session_with_ec2()
    _describe_always(ec2, [_in_use("eni-eks", requester=True)])
    result = reap_vpc_enis(session, ["vpc-1"], timeout=9999, wait_for_release=False)
    assert result.remaining == ["eni-eks"]
    assert result.reaped_any is False
    _sleep.assert_not_called()


# ── reap_vpc_security_groups ─────────────────────────────────────────


def _sg_paginators(ec2: MagicMock, security_groups: list[dict]) -> None:
    """Route get_paginator: SGs for describe_security_groups, no ENIs for the drain."""
    sg_pag = MagicMock()
    sg_pag.paginate.return_value = [{"SecurityGroups": security_groups}]
    eni_pag = MagicMock()
    eni_pag.paginate.return_value = [{"NetworkInterfaces": []}]
    ec2.get_paginator.side_effect = lambda op: (
        sg_pag if op == "describe_security_groups" else eni_pag
    )


def test_reap_vpc_security_groups_deletes_non_default_keeps_default():
    session, ec2 = _session_with_ec2()
    _sg_paginators(
        ec2,
        [
            {"GroupId": "sg-default", "GroupName": "default"},
            {
                "GroupId": "sg-leftover",
                "GroupName": "sagemaker-efs",
                "IpPermissions": [],
                "IpPermissionsEgress": [],
            },
        ],
    )
    remaining = reap_vpc_security_groups(session, ["vpc-1"])
    ec2.delete_security_group.assert_called_once_with(GroupId="sg-leftover")
    assert remaining == []


def test_reap_vpc_security_groups_skips_cfn_owned_sg():
    """A stack-owned SG (has aws:cloudformation:stack-id) is left to its own stack.

    Blast-radius guard: only service-created orphan SGs are deleted; a SG owned by
    the failing stack or any other still-deleting stack must not be pre-empted here.
    """
    session, ec2 = _session_with_ec2()
    _sg_paginators(
        ec2,
        [
            {"GroupId": "sg-default", "GroupName": "default"},
            {
                "GroupId": "sg-owned",
                "GroupName": "app",
                "IpPermissions": [],
                "IpPermissionsEgress": [],
                "Tags": [
                    {"Key": "aws:cloudformation:stack-id", "Value": "arn:aws:...:stack/other/abc"}
                ],
            },
        ],
    )
    remaining = reap_vpc_security_groups(session, ["vpc-1"])
    ec2.delete_security_group.assert_not_called()
    assert remaining == []


def test_reap_vpc_security_groups_reports_undeletable():
    session, ec2 = _session_with_ec2()
    _sg_paginators(
        ec2,
        [
            {
                "GroupId": "sg-stuck",
                "GroupName": "app",
                "IpPermissions": [],
                "IpPermissionsEgress": [],
            }
        ],
    )
    ec2.delete_security_group.side_effect = ClientError(
        {"Error": {"Code": "DependencyViolation", "Message": "in use"}}, "DeleteSecurityGroup"
    )
    remaining = reap_vpc_security_groups(session, ["vpc-1"])
    assert remaining == ["sg-stuck"]


def test_reap_vpc_security_groups_not_found_is_treated_as_gone():
    session, ec2 = _session_with_ec2()
    _sg_paginators(
        ec2,
        [{"GroupId": "sg-x", "GroupName": "app", "IpPermissions": [], "IpPermissionsEgress": []}],
    )
    ec2.delete_security_group.side_effect = ClientError(
        {"Error": {"Code": "InvalidGroup.NotFound", "Message": "gone"}}, "DeleteSecurityGroup"
    )
    assert reap_vpc_security_groups(session, ["vpc-1"]) == []


def test_reap_vpc_security_groups_no_vpc_ids_no_client():
    session, _ = _session_with_ec2()
    assert reap_vpc_security_groups(session, []) == []
    session.client.assert_not_called()
