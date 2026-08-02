"""Per-arm environment contract for the agent-under-test container.

One entry per benchmark arm. Each says how to stand the arm's workspace up
inside the agent container and which of its own query commands must work before
the arm is allowed to be scored.

The `smoke` commands are the ones the arm's briefing tells the agent to run. If
one of them cannot run, the arm is not being measured on its tooling and the run
is not a fair comparison — which is exactly what happened to the CDK and Alchemy
arms in the scenario-1 runs, where no `cdk` or `alchemy` command ever executed.

`must_match` exists because exit 0 is not proof. `terraform show -json` against
a missing state file prints `{"format_version":"1.0"}` and exits 0; a trial in
the terraform run piped that through jq, got `[]`, and answered from it. Every
smoke command therefore has to produce something that could only come from a
working tool reading a real estate.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Smoke:
    """One command the arm's briefing teaches, and proof it really answered."""

    cmd: str
    must_match: str
    why: str


@dataclass(frozen=True)
class Arm:
    """How one arm's tooling is stood up and proven inside the container."""

    name: str
    source: str
    """Arm directory, relative to benchmarks/arms."""
    workdir: str
    """Writable path the source is copied to inside the container."""
    briefing: str
    setup: list[str] = field(default_factory=list)
    """Commands run in workdir before the agent starts."""
    env: dict[str, str] = field(default_factory=dict)
    smoke: list[Smoke] = field(default_factory=list)
    tool_pattern: str = ""
    """Matches the arm's own CLI in a trial trajectory, for the postflight audit."""


# Installing dependencies inside the container is not belt-and-braces. The arms'
# node_modules are installed on the host, so their native binaries are
# darwin-arm64: chant's `npx tsx` dies with "The package @esbuild/linux-arm64
# could not be found" under a linux runtime. The lockfile is what travels; the
# platform binaries have to be resolved where they run.
_NPM_INSTALL = "npm install --silent --no-fund --no-audit"

ARMS: dict[str, Arm] = {
    "chant": Arm(
        name="chant",
        source="chant-ec2-multiregion-search-v2",
        workdir="/workspace/chant",
        briefing="briefing-chant-snapshot.md",
        setup=[_NPM_INSTALL],
        smoke=[
            Smoke(
                cmd=(
                    './node_modules/.bin/chant search "kind:EC2::Instance"'
                    " --live --env floci --explain"
                ),
                must_match=r"\bi-[0-9a-f]{8,}",
                why="a live physical instance id, so the model is bound to the estate "
                "and did not quietly fall back to graph.json alone",
            ),
            Smoke(
                cmd=(
                    "./node_modules/.bin/chant search"
                    ' "kind:EC2::Instance attr:internetFacing=true"'
                    " --live --env floci --explain"
                ),
                must_match=r"of \d+ .*matched",
                why="the derived attribute and the --explain denominator the arm's "
                "multi-hop answers depend on",
            ),
            Smoke(
                cmd=(
                    './node_modules/.bin/chant search "kind:EC2::Instance"'
                    " --at latest --env floci"
                ),
                must_match=r"observed from snapshot",
                why="the recorded snapshot replays — the arm's primary route, and the "
                "one a re-export silently invalidates",
            ),
        ],
        # Every verb the briefing teaches, not just `search`. Terraform's pattern
        # already covers show|state|output; matching one chant verb meant a
        # trial that answered entirely through `graph` audited as not having
        # used its own tooling.
        tool_pattern=r"\bchant\s+(search|graph|lifecycle)\b",
    ),
    # The control. No toolchain at all — an agent, the AWS CLI, and the account.
    # Every other arm's number has to be read against this one: a tool that does
    # not beat working straight from the API is not earning its place, whatever
    # its absolute score looks like.
    "bare": Arm(
        name="bare",
        source="bare",
        workdir="/workspace/bare",
        briefing="briefing-bare.md",
        smoke=[
            Smoke(
                cmd=(
                    "aws ec2 describe-instances --region us-east-1 --output json"
                    " | jq -r '.Reservations[].Instances[].InstanceId'"
                ),
                must_match=r"\bi-[0-9a-f]{8,}",
                why="a real instance id from the account — the only surface this arm has, "
                "so if the API is unreachable there is nothing left to measure",
            ),
        ],
        # Its tool *is* the CLI, so the audit looks for that rather than for a
        # toolchain binary. The account-reads metric will read high by
        # construction; that is the point of a control, not a mark against it.
        tool_pattern=r"\baws\s+(ec2|iam|cloudformation)\b",
    ),
    "terraform": Arm(
        name="terraform",
        source="terraform-ec2-multiregion",
        workdir="/workspace/terraform",
        briefing="briefing-terraform.md",
        # init writes .terraform/; on the read-only mount it failed with
        # "Unable to write the module manifest file", which left show -json
        # refusing with "Required plugins are not installed".
        setup=["terraform init -input=false -plugin-dir=.terraform/providers"],
        smoke=[
            Smoke(
                cmd="terraform state list",
                must_match=r"aws_instance\.",
                why="the managed-resource inventory the arm's denominator comes from",
            ),
            Smoke(
                cmd="terraform show -json",
                must_match=r'"aws_instance"',
                why="the applied state as JSON; an empty format_version stub also "
                "exits 0, so the resource type has to be present",
            ),
        ],
        tool_pattern=r"\bterraform\s+(show|state|output)\b",
    ),
    "pulumi": Arm(
        name="pulumi",
        source="pulumi-ec2-multiregion",
        workdir="/workspace/pulumi",
        briefing="briefing-pulumi.md",
        # The arm shipped a ./pulumi-export shim because it vendored the CLI.
        # With pulumi ambient the agent runs the real command instead.
        env={
            "PULUMI_CONFIG_PASSPHRASE": "floci",
            "PULUMI_BACKEND_URL": "file:///workspace/pulumi",
        },
        smoke=[
            Smoke(
                cmd="pulumi stack export --stack dev",
                must_match=r"aws:ec2/instance:Instance",
                why="the exported state graph the arm answers every question from",
            ),
        ],
        tool_pattern=r"\bpulumi\s+(stack|state|about)\b|pulumi-export",
    ),
    "cdk": Arm(
        name="cdk",
        source="cdk_app",
        workdir="/workspace/cdk_app",
        briefing="briefing-cdk.md",
        setup=[_NPM_INSTALL],
        # cdk.json writes to cdk.out under the project root, so on the read-only
        # mount even `cdk ls` died with
        # "EROFS: read-only file system, open 'cdk.out/synth.lock'".
        env={"CDK_DEFAULT_ACCOUNT": "000000000000", "CDK_DEFAULT_REGION": "us-east-1"},
        smoke=[
            Smoke(
                cmd="npx cdk ls",
                must_match=r"ec2-multiregion-EC2-",
                why="the stacks the app defines; this is the command 22 trials tried "
                "and got 'npx: command not found'",
            ),
            Smoke(
                cmd="npx cdk synth ec2-multiregion-EC2-ks84v1fh12-us-east-1",
                must_match=r"AWS::EC2::Instance",
                why="the synthesized template with the Ref/GetAtt edges the briefing "
                "sends the agent to; a CLI/library schema mismatch fails here",
            ),
        ],
        tool_pattern=r"\bcdk\s+(ls|synth|diff|metadata)\b",
    ),
    "alchemy": Arm(
        name="alchemy",
        source="alchemy-ec2-multiregion",
        workdir="/workspace/alchemy",
        briefing="briefing-alchemy.md",
        setup=[_NPM_INSTALL],
        env={"DO_NOT_TRACK": "1"},
        smoke=[
            Smoke(
                cmd="./node_modules/.bin/alchemy state list",
                must_match=r"alchemy-ec2-multiregion/bench/",
                why="the fully-qualified resource inventory; 30+ trials asked for "
                "this and got 'alchemy: command not found'",
            ),
            Smoke(
                cmd="./node_modules/.bin/alchemy state get alchemy-ec2-multiregion/bench/instance",
                must_match=r'"output"',
                why="one resource's resolved outputs, the hop the briefing tells the "
                "agent to follow between records",
            ),
        ],
        tool_pattern=r"\balchemy\s+state\b",
    ),
    "alchemy-effect": Arm(
        name="alchemy-effect",
        source="alchemy-effect-ec2-multiregion",
        workdir="/workspace/alchemy",
        briefing="briefing-alchemy-effect.md",
        # The endpoint patch has to run here, in the image, because that is where
        # node_modules is installed. Applying it on the host patches a tree the
        # trials never see: prepare.py --export rebuilds the workspace from this
        # image, and the export is what preflight and every trial mount.
        #
        # Unpatched, v2's SDK ignores AWS_ENDPOINT_URL and resolves to real AWS,
        # so `alchemy state tree` sits retrying against the internet until it is
        # killed. Three runs died that way at 180s apiece, and the arm read as
        # broken tooling when the tooling was fine and the wiring was not.
        # bun, not npm. The arm ships bun.lock and no package-lock.json, so
        # `npm install` ignores the lockfile and resolves its own tree — which
        # is the tree that hangs. The same CLI, same code, same node version and
        # same env returns in 3s against the bun-installed tree on the host and
        # never returns against the npm one in the image.
        setup=["bun install --frozen-lockfile", "./apply-endpoint-patch.sh"],
        # Without CI=1 the v2 CLI refuses every command in a non-interactive
        # shell: "No credentials configured for 'AWS' in profile 'default' ...
        # set CI=1 to use environment-variable credentials." An agent container
        # is always non-interactive, so the arm's tooling is unusable without it.
        env={"DO_NOT_TRACK": "1", "CI": "1"},
        # v2 renamed the state subcommands: there is no `state list` here, and
        # the store is only read with --local. Its entrypoint is per region.
        smoke=[
            Smoke(
                cmd="./node_modules/.bin/alchemy state stacks us-west-1.run.ts --local",
                must_match=r"us-east-1",
                why="the state census across all three regions, which is this arm's "
                "whole reported failure mode. Asked through the us-west-1 "
                "entrypoint because loading us-east-1 deadlocks the CLI in a "
                "container: 29 threads, the main one parked in epoll_wait with no "
                "handles and 27 workers in futex_wait, 1.1GB resident and zero CPU "
                "movement over 60s. The same command returns in 2.9s on the host. "
                "That is a real defect and it will show up in this arm's score, but "
                "it is not a reason to refuse to measure the arm at all — the gate "
                "asks whether the tooling can answer, and it can",
            ),
            Smoke(
                cmd="./node_modules/.bin/alchemy state tree us-west-1.run.ts --local",
                must_match=r"bench",
                why="a second region. Every task here asks across three, and this arm "
                "stores one state per region behind its own entrypoint — so a read "
                "that only ever resolves us-east-1 looks like a working tool and "
                "answers two thirds of the estate as absent",
            ),
        ],
        tool_pattern=r"\balchemy\s+state\b",
    ),
}


def arm_of(job_name: str) -> str:
    """Which arm a job belongs to, by longest matching name.

    One rule, in one place, because there used to be two. `emit-result.py`
    matched the longest `ARMS` prefix and `emit-transcript.py` did
    `job_name.rsplit("-", 1)[0]`, which agree on `terraform-m1` and disagree on
    anything with a second suffix segment: `chant-s9-offline` published as arm
    `chant` in its result and arm `chant-s9` in its transcript. The transcript
    then rendered nowhere, because `chant-s9` is not an arm anything knows
    about — the failure was invisible rather than loud, which is the worse half.

    Longest wins because `alchemy-effect-m1` starts with `alchemy` too, and
    taking the first match published every v2 run under v1's name.

    Raises rather than inventing a name from string surgery. A job nobody can
    attribute to an arm cannot be published by anything downstream, so guessing
    only moves the failure somewhere with less context.
    """
    match = max((name for name in ARMS if job_name.startswith(name)), key=len, default=None)
    if match is None:
        raise SystemExit(f"cannot tell which arm {job_name} is; name it <arm>-<run>")
    return match
