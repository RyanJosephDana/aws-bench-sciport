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
        briefing="briefing-chant-search-v2.md",
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
        ],
        tool_pattern=r"\bchant\s+search\b",
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
        setup=[_NPM_INSTALL],
        # Without CI=1 the v2 CLI refuses every command in a non-interactive
        # shell: "No credentials configured for 'AWS' in profile 'default' ...
        # set CI=1 to use environment-variable credentials." An agent container
        # is always non-interactive, so the arm's tooling is unusable without it.
        env={"DO_NOT_TRACK": "1", "CI": "1"},
        # v2 renamed the state subcommands: there is no `state list` here, and
        # the store is only read with --local. Its entrypoint is per region.
        smoke=[
            Smoke(
                cmd="./node_modules/.bin/alchemy state tree us-east-1.run.ts --local",
                must_match=r"bench",
                why="the stack/stage tree; the v2 arm's whole reported failure mode "
                "is a state census, so the census has to be readable",
            ),
        ],
        tool_pattern=r"\balchemy\s+state\b",
    ),
}
