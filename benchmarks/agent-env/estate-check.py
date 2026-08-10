#!/usr/bin/env python3
"""Check, after a deploy, that the estate is the one the scenario defines.

    python3 benchmarks/agent-env/estate-check.py
    python3 benchmarks/agent-env/estate-check.py --regions us-east-1,us-west-1

The two existing gates ask about the tool. Preflight asks whether the arm can
answer with its own tooling; the audit asks whether it did. Neither asks whether
the thing it answered *about* was intact, and a degraded estate is invisible to
both: the tool runs perfectly and reports what is there.

cdk-m3 is the whole argument. A second Floci on the machine held host port
30000, so this estate's us-west-2 instance could not publish its app ports —
"Bind for 0.0.0.0:30000 failed: port is already allocated" — and went straight
to `terminated`. Five instances deployed where the scenario defines six. The run
completed 24 trials with no exceptions, the audit passed, `cdk` ran in all 24,
and it scored 14/24 against 17-19/24 for the same arm on the same briefing.

Nothing in the harness could tell that from CDK having a bad day. The number was
publishable and wrong, which is the exact failure the other two gates exist to
prevent, arriving through the one door they do not cover.

No ground truth is encoded here. The estate publishes its own, and this reads
it back:

  * `…-NUMEC2Running` — a CloudFormation export the scenario's stacks write,
    saying how many instances that region is supposed to be running. Compared
    against how many there are. This is the check that decides the verdict, and
    it is the one that would have named the cdk-m3 failure outright: us-west-2
    declared 1 and was running 0.
  * every export naming an instance must name one that exists and is alive. A
    dangling export is the same fault seen from the other side, and it survives
    cases where the count happens to come out right.
  * nothing is terminated or shutting down. The emulator was wiped minutes ago,
    so the deploy created everything in it, and no deploy means to create a
    terminated instance.
  * the emulator logged no failure to publish an instance's ports — the
    collision above, caught at its source rather than through its symptom.

The first two come from the scenario. If it changes shape, they follow it
without anything here being edited, which is the property the hardcoded version
of this check would not have had.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

#: States a deploy never means to produce. Anything here was created and lost.
DEAD = {"terminated", "shutting-down"}

#: The scenario's own declaration of how many instances a region should run.
#: Matched on the suffix so it follows the stack's naming rather than fixing it.
DECLARED_COUNT = re.compile(r"NUMEC2Running$")

#: An instance id anywhere in an export's value.
INSTANCE_ID = re.compile(r"\bi-[0-9a-f]{8,}\b")

#: The emulator's own report that it could not give an instance its host ports.
PUBLISH_FAILED = "Failed to publish EC2 instance"


def run_aws(args: list[str], endpoint: str) -> str:
    """One AWS call against the emulator, or a hard stop saying why not."""
    out = subprocess.run(
        ["aws", "--endpoint-url", endpoint, *args],
        capture_output=True, text=True,
        env={**os.environ, "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"},
    )
    if out.returncode != 0:
        raise SystemExit(f"cannot read the estate: {out.stderr.strip()[:200]}")
    return out.stdout


def instances(region: str, endpoint: str) -> list[tuple[str, str]]:
    """(id, state) for every instance in one region."""
    out = run_aws(["--region", region, "ec2", "describe-instances", "--output", "json"], endpoint)
    try:
        data = json.loads(out)
    except ValueError:
        raise SystemExit(f"cannot read {region}: the emulator did not return JSON")
    return [
        (i["InstanceId"], i["State"]["Name"])
        for r in data.get("Reservations", [])
        for i in r.get("Instances", [])
    ]


def exports(region: str, endpoint: str) -> list[tuple[str, str]]:
    """(name, value) for every CloudFormation export in one region.

    These are the scenario's contract with itself. The verifier resolves the
    reference answers' placeholders against them, so an estate whose exports do
    not describe it is one whose ground truth does not either.
    """
    out = run_aws(
        ["--region", region, "cloudformation", "list-exports", "--output", "json"],
        endpoint,
    )
    return [(e["Name"], e["Value"]) for e in json.loads(out).get("Exports", [])]


def emulator_log(container: str) -> list[str]:
    """Every line the emulator has written. Empty if it cannot be read."""
    out = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    if out.returncode != 0:
        # Not fatal on its own. The check that decides the verdict is the state
        # of the instances, and that came from the API.
        return []
    return (out.stdout + out.stderr).splitlines()


def publish_failures(lines: list[str]) -> list[str]:
    """Lines where the emulator could not publish an instance's ports."""
    return [" ".join(l.split())[:180] for l in lines if PUBLISH_FAILED in l]


def complaints(lines: list[str]) -> list[str]:
    """Everything the emulator logged at WARN or ERROR, deduplicated by shape.

    Printed even when the run is allowed to proceed, because the failure that
    invalidated a run had already been written here — at WARN, while an instance
    was going to `terminated` — and the reset that followed took the log with
    it. A gate only catches what it was told to look for. This is where anything
    it was not told about will have been said.

    Deduplicated on the message minus its timestamp and ids: one bad deploy
    produces the same complaint once per instance per region, and eighteen
    copies of one sentence is how a reader learns to skip the section.
    """
    seen: dict[str, int] = {}
    for line in lines:
        if " WARN " not in line and " ERROR " not in line:
            continue
        shape = re.sub(r"\b(i-[0-9a-f]+|[0-9a-f]{12,})\b", "…", " ".join(line.split()))
        shape = re.sub(r"^\S+ \S+ ", "", shape)[:200]
        seen[shape] = seen.get(shape, 0) + 1
    return [f"{msg}" + (f"   (x{n})" if n > 1 else "") for msg, n in seen.items()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--endpoint",
        default=f"http://localhost:{os.environ.get('FLOCI_PORT', '4566')}",
        help="the emulator, on the host",
    )
    parser.add_argument(
        "--regions",
        default=os.environ.get("FLOCI_ALLOWED_REGIONS", "us-east-1,us-west-1,us-west-2"),
        help="comma-separated regions the scenario deploys to",
    )
    parser.add_argument("--container", default="floci-floci-1", help="the emulator container")
    args = parser.parse_args()

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    problems: list[str] = []
    alive = 0
    declared_total = 0

    for region in regions:
        found = instances(region, args.endpoint)
        living = {i for i, s in found if s not in DEAD}
        alive += len(living)
        for instance, state in found:
            if state in DEAD:
                problems.append(f"{region}: {instance} is {state}")

        # What the scenario's own stacks say this region should be running.
        # Several stacks can declare it; they are summed, because a region with
        # two stacks running one instance each should have two.
        region_exports = exports(region, args.endpoint)
        declared = sum(
            int(value)
            for name, value in region_exports
            if DECLARED_COUNT.search(name) and value.strip().lstrip("-").isdigit()
        )
        said = any(DECLARED_COUNT.search(name) for name, _ in region_exports)
        declared_total += declared

        # An export that names an instance has to name a living one. Catches the
        # same fault from the other side, in the case where one instance died
        # and another was created and the count comes out right anyway.
        for name, value in region_exports:
            for ref in INSTANCE_ID.findall(value):
                if ref not in living:
                    problems.append(f"{region}: export {name} names {ref}, which is not a running instance")

        note = f"    {region}: {len(living)} instance(s)"
        if said:
            note += f", {declared} declared"
            if declared != len(living):
                problems.append(
                    f"{region}: the scenario's stacks declare {declared} running instance(s), found {len(living)}"
                )
        if len(found) != len(living):
            note += f", {len(found) - len(living)} dead"
        print(note)

    log = emulator_log(args.container)
    for line in publish_failures(log):
        problems.append(f"emulator could not publish an instance's ports: {line}")

    if not alive:
        problems.append("no instances at all — the deploy did not land")

    # Said whether or not the estate passed. A clean deploy prints nothing here.
    grumbles = complaints(log)
    if grumbles:
        print(f"    the emulator complained {len(grumbles)} time(s) during the deploy:")
        for line in grumbles[:12]:
            print(f"      {line}")
        if len(grumbles) > 12:
            print(f"      … {len(grumbles) - 12} more, in the emulator log filed with the job")

    if not problems:
        against = f", matching the {declared_total} its stacks declare" if declared_total else ""
        print(f"    estate intact: {alive} instance(s) across {len(regions)} region(s){against}")
        return 0

    print("", file=sys.stderr)
    print("the estate is not what the scenario defines:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Scoring this would measure a broken estate, not the arm. The usual cause"
        " is another Floci on the machine holding the instance host ports — see"
        " FLOCI_SSH_PORT_START and FLOCI_APP_PORT_START in"
        " benchmarks/floci/docker-compose.yml.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
