"""Drops residuals the account can't delete (AWS-managed KMS keys, service ENIs).

These look account-owned by identifier alone, so the scan flags them and reset
loops forever trying to delete them. One describe call tells them apart.
"""

from __future__ import annotations

from typing import cast

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import THROTTLE_ERROR_CODES
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

# Adaptive retry (8 attempts) for the ownership probes. This probe fans
# describe_network_interfaces across every candidate ENI and gets throttled; under boto3's weak
# default the throttle trips fail-open, which keeps ALL ENIs and makes reset loop on service ENIs.
_OWNERSHIP_RETRY_CONFIG = Config(retries={"max_attempts": 8, "mode": "adaptive"})

_KMS_KEY_TYPE = "AWS::KMS::Key"
_ENI_TYPE = "AWS::EC2::NetworkInterface"
_ENI_ATTACHMENT_TYPE = "AWS::EC2::NetworkInterfaceAttachment"

# Look ENIs up by the id the scan reported (own id, or attachment id). A filter
# (not NetworkInterfaceIds=) tolerates a since-deleted id instead of failing the batch.
_ENI_ID_FILTER = "network-interface-id"
_ATTACHMENT_ID_FILTER = "attachment.attachment-id"

# Customer-creatable, customer-deletable ENI types. Everything else (lambda, nat_gateway,
# vpc_endpoint, …) is service-injected and undeletable by us. Catches Lambda ENIs
# (RequesterManaged=False, type='lambda') the flag misses; not sufficient alone since RDS ENIs
# are 'interface' with RequesterManaged=True, so it's OR'd with RequesterManaged. efa/efa-only
# are customer-owned HPC adapters — kept, not dropped. Verified live.
_CUSTOMER_INTERFACE_TYPES = frozenset({"interface", "efa", "efa-only"})

# Tertiary signal when both RequesterManaged and InterfaceType are unset.
_SERVICE_ENI_DESCRIPTION_PREFIXES = ("Amazon EKS", "DMSNetworkInterface", "ELB ")


class AwsManagedOwnershipProbe:
    """Drops confirmed AWS-managed KMS keys and service-owned ENIs from a scan set."""

    def __init__(self, session: boto3.Session, region: str | None = None) -> None:
        """Initialize with a session scoped to the region being verified."""
        self._session = session
        self._region = region
        # Built lazily and reused so a large set doesn't spin up a client per item.
        self._kms_client: BaseClient | None = None
        self._ec2_client: BaseClient | None = None

    @property
    def _kms(self) -> BaseClient:
        """The KMS client, built once and reused."""
        if self._kms_client is None:
            self._kms_client = cast(
                BaseClient,
                build_client(
                    self._session, "kms", region_name=self._region, config=_OWNERSHIP_RETRY_CONFIG
                ),
            )
        return self._kms_client

    @property
    def _ec2(self) -> BaseClient:
        """The EC2 client, built once and reused."""
        if self._ec2_client is None:
            self._ec2_client = cast(
                BaseClient,
                build_client(
                    self._session, "ec2", region_name=self._region, config=_OWNERSHIP_RETRY_CONFIG
                ),
            )
        return self._ec2_client

    def exclude_aws_managed(self, resources: dict[str, list[dict]]) -> dict[str, list[dict]]:
        """Return ``resources`` minus the undeletable ones; other types pass through."""
        kept: dict[str, list[dict]] = {}
        for resource_type, items in resources.items():
            if resource_type == _KMS_KEY_TYPE:
                surviving = [
                    r for r in items if not self._is_aws_managed_kms_key(r.get("Identifier", ""))
                ]
            elif resource_type == _ENI_TYPE:
                surviving = self._keep_deletable_enis(items, _ENI_ID_FILTER)
            elif resource_type == _ENI_ATTACHMENT_TYPE:
                surviving = self._keep_deletable_enis(items, _ATTACHMENT_ID_FILTER)
            else:
                surviving = items
            if surviving:
                kept[resource_type] = surviving
        return kept

    def _is_aws_managed_kms_key(self, key_id: str) -> bool:
        """True if the key is AWS-managed (undeletable). Fail-open: keep on error."""
        if not key_id:
            return False
        try:
            metadata = self._kms.describe_key(KeyId=key_id)["KeyMetadata"]
        except (ClientError, BotoCoreError) as e:
            # Best-effort fail-open filter (keep-on-error); a probe miss degrades safely.
            logger.debug(f"KMS ownership probe failed for '{key_id}'; keeping it: {e}")
            return False
        return metadata.get("KeyManager") == "AWS"

    def _keep_deletable_enis(self, items: list[dict], filter_name: str) -> list[dict]:
        """Return ``items`` (ENIs or attachments) minus service-owned. Fail-open: keep on error."""
        identifiers = [i for r in items if (i := r.get("Identifier", ""))]
        if not identifiers:
            return items
        try:
            service_managed = self._service_managed_enis(identifiers, filter_name)
        except (ClientError, BotoCoreError) as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in THROTTLE_ERROR_CODES:
                # Fail-open on a throttle keeps every ENI, so reset may loop on service ENIs.
                logger.warning(
                    f"ENI ownership probe THROTTLED for {identifiers}; keeping all "
                    f"(reset may re-flag service ENIs): {e}"
                )
            else:
                # Best-effort fail-open filter (keep-on-error); a probe miss degrades safely.
                logger.debug(f"ENI ownership probe failed for {identifiers}; keeping them: {e}")
            return items
        return [r for r in items if r.get("Identifier", "") not in service_managed]

    def _service_managed_enis(self, identifiers: list[str], filter_name: str) -> set[str]:
        """The subset of ``identifiers`` whose ENI EC2 reports as service-managed."""
        service_managed: set[str] = set()
        pages = self._ec2.get_paginator("describe_network_interfaces").paginate(
            Filters=[{"Name": filter_name, "Values": identifiers}]
        )
        for page in pages:
            for eni in page.get("NetworkInterfaces", []):
                if self._is_service_managed(eni):
                    identifier = self._reported_identifier(eni, filter_name)
                    if identifier:
                        service_managed.add(identifier)
        return service_managed

    @staticmethod
    def _reported_identifier(eni: dict, filter_name: str) -> str:
        """The id the ENI was scanned under: its attachment id, or its own id."""
        if filter_name == _ATTACHMENT_ID_FILTER:
            return eni.get("Attachment", {}).get("AttachmentId", "")
        return eni.get("NetworkInterfaceId", "")

    @staticmethod
    def _is_service_managed(eni: dict | None) -> bool:
        """True if the ENI is service-managed (undeletable by us).

        Union of three signals: RequesterManaged (most managed ENIs), an InterfaceType outside
        the customer set (catches Lambda/NAT/TGW that RequesterManaged misses; absent type =>
        customer, kept), and a service Description prefix (last resort).
        """
        if not eni:
            return False
        if eni.get("RequesterManaged"):
            return True
        interface_type = eni.get("InterfaceType")
        if interface_type and interface_type not in _CUSTOMER_INTERFACE_TYPES:
            return True
        return (eni.get("Description") or "").startswith(_SERVICE_ENI_DESCRIPTION_PREFIXES)
