<!-- shared-lead -->
# Reproduce: the AWS CDK arm on aws-bench `ec2-multiregion`

## What the scenario is

One AWS estate, deployed six times over — once per arm (chant, Terraform, Pulumi, Alchemy, Alchemy v2 (Effect)) — onto the
Floci emulator, so no real AWS account and no spend is involved. Every arm
deploys the *same* estate and is asked the *same* questions; the only thing that
differs is the toolchain it answers with. This arm answers with `cdk ls` / `cdk synth` plus the CloudFormation API.

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

CDK has no state file of its own. Its deployed state *is* CloudFormation: the
synthesized template holds the declared shape and every `Ref`/`Fn::GetAtt` edge
between resources, and `describe-stack-resources` maps each logical id to the
physical id it deployed to. So this arm answers by joining two sources that no
other arm has to join — which is the interesting thing about it, and also its
cost: neither half is an answer alone.

The app is `lib/app.ts` (`npx ts-node lib/app.ts`, wired through `cdk.json`),
defining the estate across `stacks/ec2_ks84v1fh1.ts` (us-east-1),
`stacks/ec2_ls9fuhb52.ts` (us-west-1 and us-west-2) and
`stacks/qa_roles_stack.ts`, with shared constructs in `lib/shared.ts`.

Pinned to `aws-cdk-lib` ^2.260.0 and the `aws-cdk` CLI ^2.1112.0, both installed
into the arm rather than globally — the agent image deliberately ships no global
`cdk`, so a trial that reaches for one gets nothing and the postflight audit
catches it. The first scenario-1 runs reported CDK scores with no `cdk` command
ever executed.

## Prerequisites

Docker · Node 24 · AWS CLI v2 · Python 3 · git

## 1. Start the Floci emulator

```sh
git clone https://github.com/lex00/floci
cd floci && git checkout awsbench-integration-v2
docker compose up -d            # AWS-shaped services at http://localhost:4566
```

## 2. Point the toolchain at it

```sh
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1
```

## 3. Deploy the estate

Each region bootstraps independently, so bootstrap them in parallel — serially
this is most of the arm's deploy time.

```sh
cd benchmarks/arms/cdk_app
npm install

for r in us-east-1 us-west-1 us-west-2; do
  CDK_DEFAULT_ACCOUNT=000000000000 npx cdk bootstrap "aws://000000000000/$r" &
done
wait

CDK_DEFAULT_ACCOUNT=000000000000 npx cdk deploy --all \
  --require-approval never --concurrency 4
```

## 4. Confirm the arm can answer with its own tooling

These are the commands the briefing teaches and the preflight gate asserts. Both
must return something only a working CDK reading a real estate could produce.

```sh
npx cdk ls
# ec2-multiregion-EC2-ks84v1fh12-us-east-1
# ec2-multiregion-EC2-ls9fuhb522-us-west-1
# ec2-multiregion-EC2-ls9fuhb522-us-west-2
# ec2-multiregion-QARoles-us-east-1

npx cdk synth ec2-multiregion-EC2-ks84v1fh12-us-east-1 | head -40
# the synthesized template: resources, properties, and the Ref/GetAtt edges
```

## 5. The two reachability facts, by hand

The synthesized template carries the declared shape; the physical ids come from
CloudFormation. Neither alone answers the questions the benchmark turns on.

```sh
STACK=ec2-multiregion-EC2-ks84v1fh12-us-east-1

# logical id -> physical id
aws cloudformation describe-stack-resources --stack-name "$STACK" \
  --query 'StackResources[?ResourceType==`AWS::EC2::Instance`].[LogicalResourceId,PhysicalResourceId]' \
  --output text

# SSH reachability: the security group attached directly, AND the one reached
# through the launch template. The second hop is the one a flat read misses.
npx cdk synth "$STACK" > /tmp/t.json
jq -r '.Resources | to_entries[]
       | select(.value.Type=="AWS::EC2::LaunchTemplate")
       | .value.Properties.LaunchTemplateData.SecurityGroupIds' /tmp/t.json
```

Two instances take SSH from the internet — one only through its launch
template. Four security groups are attached to nothing; the template cannot tell
you that, because a resource nothing references is not reachable by walking
outward from what was declared.

## 6. Run the arm under the benchmark

```sh
./benchmarks/agent-env/run-arm.sh cdk
```

That wipes the emulator, redeploys this arm, re-exports its workspace, runs the
preflight gate, scores k=3 over the eight tasks, and runs the postflight audit.
