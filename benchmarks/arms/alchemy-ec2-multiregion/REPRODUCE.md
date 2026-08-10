<!-- shared-lead -->
# Reproduce: the Alchemy arm on aws-bench `ec2-multiregion`

## What the scenario is

One AWS estate, deployed six times over — once per arm (chant, Terraform, Pulumi, AWS CDK, Alchemy v2 (Effect)) — onto the
Floci emulator, so no real AWS account and no spend is involved. Every arm
deploys the *same* estate and is asked the *same* questions; the only thing that
differs is the toolchain it answers with. This arm answers with `alchemy state list` / `state get` over the applied state.

The estate spans three regions and four stacks:

| | |
|---|---|
| stacks | `ec2-multiregion-EC2-…-us-east-1`, `…-us-west-1`, `…-us-west-2`, `ec2-multiregion-QARoles-us-east-1` |
| instances | 6 — four in us-east-1, one in us-west-1, one in us-west-2 |
| VPCs | 4, one of which is the account's default VPC (declared by nothing) |
| security groups | 6 distinct, of which 4 are attached to nothing |
| reachability | 2 instances take SSH from the internet; one of them only through its launch template |

Those last two rows are the point. The estate is built so that the questions
cannot be answered by listing resources — they need the relationships between
them. An instance whose security group is attached via a launch template is
reachable, but nothing about the instance record says so. A security group
nothing references cannot be found by walking outward from what was deployed.

## What the agent is asked

Eight questions, each run three times (k=3) — 24 trials per arm. They are
natural-language estate questions, not commands:

| task | the fact |
|---|---|
| `list-ec-instances-all-regions` | 6 instance ids across 3 regions |
| `list-ec-instances-all-regions-1` | which instances take SSH from the internet — **2** |
| `find-ec-instances-in-public-subn` | instances in a public subnet — **5** |
| `list-ec-instances-by-vpc-across` | which instances sit in which of the 4 VPCs |
| `ec-instances-without-default-vpc` | instances outside the default VPC — **5** |
| `describe-ec-instances-cross-regi` | per-region counts, and shared networking in us-east-1 |
| `list-ec-private-ips-all-regions` | 6 instances and their private IPs |
| `list-unused-security-groups-all` | security groups attached to nothing — **4** |

Answers are graded by an LLM judge against a reference answer whose placeholders
are resolved against the live estate. The judge and the placeholder resolver
always retain AWS access, independently of what the agent is given.

## How the agent environment is configured

Identical for every arm, so a difference in score is a difference in tooling:

| | |
|---|---|
| agent | `claude-code`, model `claude-haiku-4-5-20251001` |
| base image | `public.ecr.aws/docker/library/python:3.13-slim` |
| pinned toolchain | node 24.13.1, bun 1.3.6, terraform 1.15.8, pulumi 3.255.0, plus `awscli`, `jq`, `git` |
| AWS endpoint | the Floci emulator at `host.docker.internal:4566`; credentials are staged placeholders |
| arm workspace | mounted read-only at `/opt/awsbench-arm`, copied to a writable workdir before the agent starts |
| briefing | one per arm, appended to the task prompt — teaches that arm's own read commands and nothing about any answer |
| excluded | `describe-cloudformation-stack-resources`, so no arm can shortcut through CloudFormation |

Two gates run around every scored run, and both stop it:

- **preflight** — each arm's own read commands must run *and* return something
  only a working tool reading a real estate could produce. Exit 0 is not proof:
  `terraform show -json` against a missing state file prints
  `{"format_version":"1.0"}` and exits 0, and a trial once answered from that.
- **postflight audit** — every trial's trajectory must show the arm's own CLI
  actually being invoked. The first scenario-1 runs reported CDK and Alchemy
  scores with no `cdk` or `alchemy` command ever executed.

---
## This arm in particular

Like Terraform and Pulumi, Alchemy's applied state carries every attribute,
but the reachability answers are a multi-hop join the state does not fold. You
compute them by hand below. That by-hand join is what a small model must do
too.

## What is and is not stock Alchemy

Three things about this arm are ours, all in this directory, all documented so
the arm stays reproducible and honest:

- **Endpoint patch** (`apply-endpoint-patch.sh`). Alchemy has no endpoint
  override (upstream `alchemy-run/alchemy#991`); two call sites hardcode
  `https://<svc>.<region>.amazonaws.com` and are patched to honor
  `AWS_ENDPOINT_URL`. The same script fixes one upstream compat bug: Policy's
  create-fallback catches `error.name === "NoSuchEntity"` where
  `@aws-sdk/client-iam` v3 throws `NoSuchEntityException`, so creating a
  not-yet-existing policy fails against real AWS and Floci alike.
- **Custom resources** (`src/aws-extra.ts`). The published async Alchemy ships
  no EC2 Instance, Launch Template, or IAM Instance Profile resource — those
  types are reachable only through its Cloud Control API path
  (`AWS::EC2::Instance` etc.), and Floci's Cloud Control is read-only
  (`CreateResource` is unsupported). The arm authors them the same way the
  framework's own AWS resources are written: an async lifecycle function per
  resource, on AWS SDK v3 clients, which honor `AWS_ENDPOINT_URL` natively.
  A region-scoped SSM parameter resource rides along because Alchemy's
  `SSMParameter` can only write to the process region.
- **State fidelity.** The custom Instance echoes only its declared props into
  state plus runtime ids/IPs. An instance launched via launch template records
  the template reference, not the security groups it resolves to — the same
  shape Terraform and Pulumi state gives this estate. The `ssh-reachable`
  task turns on that hop.

Two known live-API divergences from the Terraform/Pulumi arms, neither read by
any task's ground truth: security groups keep their default allow-all egress
rule (Alchemy does not manage egress on create the way the Terraform lineage
revokes it), and subnets carry explicit availability zones (Alchemy requires
one).

## Prerequisites
Docker · Bun · AWS CLI v2 · Python 3 · git

## 1. Start the Floci emulator
```sh
git clone https://github.com/lex00/floci
cd floci && git checkout awsbench-integration-v2
docker compose up -d            # AWS-shaped services at http://localhost:4566
cd ..
```

## 2. Get the estate
```sh
git clone https://github.com/lex00/aws-bench
cd aws-bench && git checkout feat/emulator-floci   # feat/emulator-floci carries the arms
cd benchmarks/arms/alchemy-ec2-multiregion
bun install
./apply-endpoint-patch.sh
```

## 3. Deploy to Floci
Alchemy keeps its state in local JSON files under `.alchemy/` — no cloud
account, no backend:
```sh
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_REGION=us-east-1 AWS_ACCOUNT_ID=000000000000 ALCHEMY_TELEMETRY_DISABLED=1
bun alchemy.run.ts
```
Creates 6 EC2 instances across 3 regions (plus the account default VPC) and
writes the export contract to SSM `/exports`. Confirm the estate:
```sh
for r in us-east-1 us-west-1 us-west-2; do
  aws ec2 describe-instances --region $r \
    --filters Name=instance-state-name,Values=running \
    --query 'length(Reservations[].Instances[])' --output text
done                            # → 4, 1, 1  (6 running, 0 terminated)
```

## 4. The two facts, computed by hand from state
There is no Alchemy command that answers "which instances are reachable via
SSH from the internet" — the state has the pieces, you assemble them. Reduce
the per-resource state files:
```sh
python3 - .alchemy/alchemy-ec2-multiregion/bench <<'PY'
import json, glob, sys
root = sys.argv[1]
res = [json.load(open(p)) for p in glob.glob(f"{root}/*.json")]
def kind(r): return r.get("kind")
def out(r): return {k: v for k, v in r.get("output", {}).items() if not k.startswith("Symbol")}

insts = [out(r) for r in res if kind(r) == "aws::Instance"]
pub = {out(r).get("subnetId") for r in res if kind(r) == "aws::RouteTableAssociation"}
open22 = set()
for r in res:
    if kind(r) == "aws::SecurityGroupRule":
        o = out(r)
        if o.get("type") == "ingress" and o.get("fromPort") == 22 and "0.0.0.0/0" in (o.get("cidrBlocks") or []):
            sg = o.get("securityGroup")
            open22.add(sg.get("groupId") if isinstance(sg, dict) else sg)
lt_sgs = {out(r)["launchTemplateId"]: set(out(r)["launchTemplateData"].get("SecurityGroupIds") or [])
          for r in res if kind(r) == "aws::LaunchTemplate"}

facing = ssh = 0
for i in insts:
    is_facing = i.get("subnetId") in pub or str(i.get("subnetId", "")).startswith("subnet-default")
    sgs = set(i.get("securityGroupIds") or [])
    lt = i.get("launchTemplate")
    if isinstance(lt, dict):
        sgs |= lt_sgs.get(lt.get("launchTemplateId"), set())   # the launch-template hop
    facing += is_facing
    ssh += is_facing and bool(sgs & open22)
print("internet-facing (find-public):", facing)   # 5
print("ssh-reachable:", ssh)                       # 2
PY
```
Output: `internet-facing (find-public): 5` and `ssh-reachable: 2`.

The `sgs |= lt_sgs...` line is the launch-template hop — the security group
reachable only through a launch template. Drop it and the count falls to 1,
which is what a CLI sweep (and the CDK arm) returns.

## Expected outcome
The reproduction succeeds when both hold:
- The instance check prints `4`, `1`, `1` — 6 running, 0 terminated.
- The derivation prints exactly `internet-facing (find-public): 5` and
  `ssh-reachable: 2`.

Any other counts (terminated instances present, or 5/2 not matching) is a
failure.

## Scope
This proves the estate is faithful — internet-facing = 5, ssh-reachable = 2.
It does not reproduce the win-rate or cost; those need the aws-bench harness
(Haiku 4.5, k=3, judged), not the derivation above. To run the harness and
tally the scores across the arms, see `../REPRODUCE-BENCHMARK.md`.

---
Tested with Bun 1.3.6 + alchemy 0.93.12 + @aws-sdk v3 against Floci
`ghcr.io/lex00/floci:awsbench`.
