"""Pre-invoke: count VPCs running no EC2 instance, across reachable regions.

A VPC counts as empty when no instance in a non-terminated state reports it.
Terminated instances are excluded deliberately: they linger in the API for a
while after they stop existing, and a VPC whose only instance is terminated is
empty in every sense a practitioner cares about.

Computed live. The estate is redeployed before every run and its VPC ids change
each time, so a written-down count would track the scenario only until someone
edited it.

The scenario account is locked by a region-restrict SCP to its deploy regions,
so regions that come back denied are skipped rather than failing the run.
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

RESULT_FILE = "/logs/pre_invoke/placeholder.json"
COUNT_KEY = "b71ea095-EmptyVpcCount"
TOTAL_KEY = "b71ea095-TotalVpcCount"

_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthFailure",
}

#: An instance in one of these is gone, or on its way out, and does not make a
#: VPC occupied.
_DEAD = {"terminated", "shutting-down"}


def _enabled_regions(session):
    """Regions enabled for the account, asked through an allowed endpoint."""
    ec2 = session.client("ec2", region_name="us-east-1")
    return [r["RegionName"] for r in ec2.describe_regions(AllRegions=False)["Regions"]]


def _vpcs_in_region(session, region):
    """(all VPC ids, VPC ids with a live instance), or None if the region is denied."""
    ec2 = session.client("ec2", region_name=region)
    try:
        vpcs = set()
        for page in ec2.get_paginator("describe_vpcs").paginate():
            for vpc in page["Vpcs"]:
                vpcs.add(vpc["VpcId"])

        occupied = set()
        for page in ec2.get_paginator("describe_instances").paginate():
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    if inst["State"]["Name"] not in _DEAD and inst.get("VpcId"):
                        occupied.add(inst["VpcId"])
    except ClientError as e:
        if e.response["Error"]["Code"] in _DENIED_CODES:
            return None
        raise
    return vpcs, occupied


def run(session=None, region="us-east-1", **parameters):
    if not session:
        session = boto3.Session(region_name=region)

    empty = total = 0
    for reg in _enabled_regions(session):
        found = _vpcs_in_region(session, reg)
        if found is None:
            continue
        vpcs, occupied = found
        total += len(vpcs)
        empty += len(vpcs - occupied)
    return {COUNT_KEY: str(empty), TOTAL_KEY: str(total)}


if __name__ == "__main__":
    try:
        placeholders = run()
    except Exception as e:
        print(f"pre_invoke failed: {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, "w") as f:
        json.dump(placeholders, f, indent=2)
