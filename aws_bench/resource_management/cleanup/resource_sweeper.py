"""Delete a set of resources (grouped by CloudFormation type) via the shared pipeline."""

from __future__ import annotations

import boto3

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.manager import Resource
from aws_bench.resource_management.ccapi.models import DeletionFailureEvent
from aws_bench.resource_management.cleanup.models import StackResource
from aws_bench.resource_management.cleanup.resource_cleaner import ResourceCleaner

logger = get_logger(__name__)


class ResourceSweeper:
    """Deletes resources grouped by CFN type using ResourceCleaner (custom handlers + CCAPI)."""

    def __init__(self, session: boto3.Session) -> None:
        """Initialize with a boto3 session scoped to the target account/region."""
        self._session = session

    async def delete(
        self, resources_by_type: dict[str, list[dict]]
    ) -> dict[Resource, DeletionFailureEvent]:
        """Delete every resource in ``resources_by_type``; return per-resource failures.

        ``resources_by_type`` maps a CloudFormation type name to a list of resources,
        each a dict with an ``"Identifier"`` key (the shape produced by a scan and by
        ``find_new_resources``). Deletion goes through the shared ``ResourceCleaner``
        pipeline: service-API custom handlers first, then the CloudControl fallback for
        types without a handler. Returns a dict of the resources that could not be
        deleted; an empty dict means everything was removed.

        Synthetic catch-all types (``AWS::<service>::*``) emitted by fast-scan are
        skipped and warned: CCAPI cannot delete a wildcard type, so passing one is a
        guaranteed no-op — the warning names it so a real orphan bucketed under a
        catch-all surfaces rather than vanishing silently.
        """
        catch_all_types = [rtype for rtype in resources_by_type if rtype.endswith("::*")]
        if catch_all_types:
            logger.warning(
                f"Skipping {len(catch_all_types)} catch-all type(s) CCAPI cannot delete: "
                f"{', '.join(sorted(catch_all_types))}"
            )

        stack_resources = [
            StackResource(
                logical_id=item["Identifier"],
                physical_id=item["Identifier"],
                resource_type=resource_type,
                status="",
            )
            for resource_type, items in resources_by_type.items()
            if not resource_type.endswith("::*")
            for item in items
        ]
        if not stack_resources:
            return {}

        count = len(stack_resources)
        logger.debug(f"Sweeping {count} resource(s) via the shared cleanup pipeline")
        return await ResourceCleaner(self._session).cleanup(
            stack_resources,
            prepare=True,
            custom_delete=True,
            ccapi_fallback=True,
        )
