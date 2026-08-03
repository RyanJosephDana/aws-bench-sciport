"""Pre-invoke: count subnets holding no network interface, across reachable regions.

A subnet is "empty" here if no ENI reports it. That covers instances, load
balancer nodes, NAT and anything else that occupies an address, so the question
is about the subnet rather than about EC2 specifically.

Computed live rather than written down. The estate is redeployed before every
run and its subnet ids change each time, so a hardcoded count would track the
scenario only until someone edited it — which is the failure the sibling
security-group task already documents.

The scenario account is locked by a region-restrict SCP to its deploy regions,
so calls to any other region raise AccessDenied. We enumerate what the account
exposes and skip the denied ones, so the count covers exactly the regions a
restricted agent can reach.
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

RESULT_FILE = "/logs/pre_invoke/placeholder.json"
COUNT_KEY = "8f3c21d4-EmptySubnetCount"
REGION_KEY = "8f3c21d4-EmptySubnetRegionBreakdown"

_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthFailure",
}


def _enabled_regions(session):
    """Regions enabled for the account.

    Targets the us-east-1 endpoint, which the SCP allows, and returns metadata
    for every enabled region, so it is not itself blocked.
    """
    ec2 = session.client("ec2", region_name="us-east-1")
    return [r["RegionName"] for r in ec2.describe_regions(AllRegions=False)["Regions"]]


def _empty_in_region(session, region):
    """Subnets in `region` with no ENI in them, or None if the region is denied."""
    ec2 = session.client("ec2", region_name=region)
    try:
        subnets = set()
        for page in ec2.get_paginator("describe_subnets").paginate():
            for subnet in page["Subnets"]:
                subnets.add(subnet["SubnetId"])

        occupied = set()
        for page in ec2.get_paginator("describe_network_interfaces").paginate():
            for eni in page["NetworkInterfaces"]:
                if eni.get("SubnetId"):
                    occupied.add(eni["SubnetId"])
    except ClientError as e:
        if e.response["Error"]["Code"] in _DENIED_CODES:
            return None
        raise
    return sorted(subnets - occupied)


def run(session=None, region="us-east-1", **parameters):
    if not session:
        session = boto3.Session(region_name=region)

    total = 0
    breakdown = []
    for reg in _enabled_regions(session):
        empty = _empty_in_region(session, reg)
        if empty is None:
            continue
        total += len(empty)
        if empty:
            breakdown.append(f"{reg}: {len(empty)}")
    return {COUNT_KEY: str(total), REGION_KEY: ", ".join(breakdown) or "none"}


if __name__ == "__main__":
    try:
        placeholders = run()
    except Exception as e:
        print(f"pre_invoke failed: {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, "w") as f:
        json.dump(placeholders, f, indent=2)
