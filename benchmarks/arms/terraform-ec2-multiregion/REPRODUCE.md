<!-- shared-lead -->
# Reproduce: the Terraform arm on aws-bench `ec2-multiregion`

## What the scenario is

One AWS estate, deployed six times over — once per arm (chant, Pulumi, AWS CDK, Alchemy, Alchemy v2 (Effect)) — onto the
Floci emulator, so no real AWS account and no spend is involved. Every arm
deploys the *same* estate and is asked the *same* questions; the only thing that
differs is the toolchain it answers with. This arm answers with `terraform show -json` / `state list` over the applied state.

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

The point of contrast: Terraform's applied state carries every attribute, but the
reachability answers are a multi-hop join the state does not fold. You compute
them by hand below. That by-hand join is what a small model must do too — it nails
the launch-template SSH hop (3/3 on the benchmark) but fumbles the route-table
walk for public subnets (1/3), where chant folds the same question into one query
and answers it 3/3.

## Prerequisites
Docker · Terraform ≥ 1.5 · AWS CLI v2 · Python 3 · git

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
cd aws-bench && git checkout awsbench-integration-v2
cd benchmarks/arms/terraform-ec2-multiregion
```

## 3. Deploy to Floci
```sh
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
terraform init
terraform apply -auto-approve
```
Creates 6 EC2 instances across 3 regions (plus the account default VPC) and writes
the export contract to SSM `/exports`. Confirm the estate:
```sh
for r in us-east-1 us-west-1 us-west-2; do
  aws ec2 describe-instances --region $r \
    --filters Name=instance-state-name,Values=running \
    --query 'length(Reservations[].Instances[])' --output text
done                            # → 4, 1, 1  (6 running, 0 terminated)
```

## 4. The two facts, computed by hand from state
There is no `terraform` sub-command that answers "which instances are reachable via
SSH from the internet" — the state has the pieces, you assemble them. Save the state
and reduce it:
```sh
terraform show -json > /tmp/tfstate.json
python3 - /tmp/tfstate.json <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
res = []
def walk(m):
    res.extend(m.get("resources", []))
    for c in m.get("child_modules", []): walk(c)
walk(d["values"]["root_module"])

insts = [r for r in res if r["type"] == "aws_instance"]
pub_subnets = {r["values"]["subnet_id"] for r in res if r["type"] == "aws_route_table_association"}
open22 = set()
for r in res:
    if r["type"] == "aws_security_group":
        for ing in r["values"].get("ingress", []):
            if ing.get("from_port") == 22 and "0.0.0.0/0" in (ing.get("cidr_blocks") or []):
                open22.add(r["values"]["id"])
lt_sgs = {r["values"]["id"]: set(r["values"].get("vpc_security_group_ids") or [])
          for r in res if r["type"] == "aws_launch_template"}

facing = ssh = 0
for i in insts:
    v = i["values"]
    is_facing = v.get("subnet_id") in pub_subnets or str(v.get("subnet_id", "")).startswith("subnet-default")
    sgs = set(v.get("vpc_security_group_ids") or [])
    for lt in (v.get("launch_template") or []):
        sgs |= lt_sgs.get(lt.get("id"), set())   # the launch-template hop
    facing += is_facing
    ssh += is_facing and bool(sgs & open22)
print("internet-facing (find-public):", facing)   # 5
print("ssh-reachable:", ssh)                       # 2
PY
```
Output: `internet-facing (find-public): 5` and `ssh-reachable: 2`.

The `sgs |= lt_sgs...` line is the launch-template hop — the security group reachable
only through a launch template, not attached to the instance directly. Drop it and the
count falls to 1, which is exactly what a CLI sweep (and the CDK arm) returns.

## Expected outcome
The reproduction succeeds when both hold:
- The instance check prints `4`, `1`, `1` — 6 running, 0 terminated.
- The derivation prints exactly `internet-facing (find-public): 5` and
  `ssh-reachable: 2`.

Any other counts (terminated instances present, or 5/2 not matching) is a failure.

## Scope
This proves the estate is faithful — internet-facing = 5, ssh-reachable = 2. It
does not reproduce the full win-rate or cost; those need the aws-bench harness
(Haiku 4.5, k=3, judged), not the derivation above. To run the harness and tally
the scores across all four arms, see `../REPRODUCE-BENCHMARK.md`.

---
Tested with Terraform 1.15.8 + hashicorp/aws 6.56.0 against Floci `awsbench-integration-v2`.
