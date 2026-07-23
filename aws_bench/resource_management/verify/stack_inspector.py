"""Stack inspection operations for verification."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import boto3
import botocore.exceptions

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.snapshot.models import StackMetadata
from aws_bench.resource_management.verify.models import (
    StackStatusCheckResult,
    TemplateHashCheckResult,
)
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)


class StackInspector:
    """Handles CloudFormation stack inspection and validation."""

    def __init__(self, session: boto3.Session):
        """Initialize with boto3 session.

        Args:
            session: boto3 Session for AWS operations
        """
        self._cfn_client = build_client(session, "cloudformation")

    def check_stack_status(
        self,
        stack_metadata: dict[str, StackMetadata],
    ) -> StackStatusCheckResult:
        """Check that all stacks exist and have correct status.

        Args:
            stack_metadata: Expected stack metadata from baseline

        Returns:
            StackStatusCheckResult with success status and error details
        """
        logger.debug("Checking stack statuses")
        try:
            current_stacks = {s["StackName"]: s for s in self._list_cloudformation_stacks()}
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list stacks: {e}")
            return StackStatusCheckResult(
                success=False,
                error_reason="Failed to list CloudFormation stacks",
                error_details={"error": str(e)},
            )

        status_failures = {}

        for stack_name, baseline_metadata in stack_metadata.items():
            current_stack = current_stacks.get(stack_name)

            if not current_stack:
                status_failures[stack_name] = {
                    "expected": baseline_metadata.status,
                    "actual": "MISSING",
                }
                continue

            # Compare status to baseline
            if current_stack["StackStatus"] != baseline_metadata.status:
                status_failures[stack_name] = {
                    "expected": baseline_metadata.status,
                    "actual": current_stack["StackStatus"],
                }

        if status_failures:
            return StackStatusCheckResult(
                success=False,
                error_reason=f"{len(status_failures)} stack(s) have status mismatch",
                error_details=status_failures,
            )

        return StackStatusCheckResult(success=True, error_reason="", error_details=None)

    def check_template_hash(
        self,
        stack_metadata: dict[str, StackMetadata],
    ) -> TemplateHashCheckResult:
        """Check that all stack templates match baseline hashes.

        Args:
            stack_metadata: Expected stack metadata from baseline

        Returns:
            TemplateHashCheckResult with success status and error details
        """
        logger.debug("Verifying template hashes")

        mismatched: list[str] = []
        for stack_name, baseline_metadata in stack_metadata.items():
            # Skip CDKToolkit - it's a bootstrap stack managed by CDK
            if stack_name == "CDKToolkit":
                logger.debug(f"Skipping template verification for {stack_name} (bootstrap stack)")
                continue
            try:
                template_resp = self._cfn_client.get_template(StackName=stack_name)
                current_template = template_resp["TemplateBody"]
                current_hash = self._compute_template_hash(current_template)

                if current_hash != baseline_metadata.template_hash:
                    mismatched.append(stack_name)
            except botocore.exceptions.ClientError as e:
                # A read failure is not a template change: keep it non-remediable (string
                # details, no stack list) so reset never deletes a stack it couldn't inspect.
                logger.error(f"Failed to get template for {stack_name}: {e}")
                return TemplateHashCheckResult(
                    success=False,
                    error_reason=f"Failed to verify template for {stack_name}",
                    error_details=str(e),
                )

        if mismatched:
            # error_details carries the mismatched stack NAMES so reset can delete+redeploy them.
            return TemplateHashCheckResult(
                success=False,
                error_reason=f"Stack {mismatched[0]} template changed"
                + (f" (+{len(mismatched) - 1} more)" if len(mismatched) > 1 else ""),
                error_details={"template_mismatch_stacks": mismatched},
            )

        return TemplateHashCheckResult(success=True, error_reason="", error_details=None)

    def _compute_template_hash(self, template: dict[str, Any] | str) -> str:
        """Compute SHA256 hash of CloudFormation template.

        Args:
            template: CloudFormation template (dict or JSON string)

        Returns:
            Hash string in format "sha256:<hex>"
        """
        if isinstance(template, str):
            # If it's a string, try to parse as JSON for consistent formatting
            try:
                template = json.loads(template)
            except json.JSONDecodeError:
                # If not JSON, hash the string directly
                hash_hex = hashlib.sha256(template.encode()).hexdigest()
                return f"sha256:{hash_hex}"

        # Convert dict to sorted JSON string for consistent hashing
        template_json = json.dumps(template, sort_keys=True)
        hash_hex = hashlib.sha256(template_json.encode()).hexdigest()
        return f"sha256:{hash_hex}"

    def _list_cloudformation_stacks(self) -> list[dict[str, Any]]:
        """List all non-deleted, root CloudFormation stacks.

        Returns:
            List of stack summaries (excludes DELETE_COMPLETE and nested stacks)
        """
        stacks = []
        paginator = self._cfn_client.get_paginator("list_stacks")

        for page in paginator.paginate():
            for stack in page["StackSummaries"]:
                # Filter out deleted stacks
                if stack["StackStatus"] == "DELETE_COMPLETE":
                    continue

                # Filter out nested stacks (they have ParentId)
                if "ParentId" in stack:
                    continue

                stacks.append(stack)

        return stacks
