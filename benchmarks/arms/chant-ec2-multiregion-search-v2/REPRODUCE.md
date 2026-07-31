<!-- shared-lead -->
# Reproduce: the chant arm on aws-bench `ec2-multiregion`

## What the scenario is

One AWS estate, deployed six times over — once per arm (Terraform, Pulumi, AWS CDK, Alchemy, Alchemy v2 (Effect)) — onto the
Floci emulator, so no real AWS account and no spend is involved. Every arm
deploys the *same* estate and is asked the *same* questions; the only thing that
differs is the toolchain it answers with. This arm answers with `chant search` / `chant graph` over a recorded state snapshot.

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

Recreates the discriminating result from the aws-bench `ec2-multiregion` scenario.
"Which EC2 instances are reachable via SSH from the internet?" is a multi-hop
join, and chant folds it into one query a small model can run — returning the
correct **2** instances (including the one reachable only through its launch
template), the answer a raw `aws ec2` sweep drops to 1. Everything runs on the
Floci AWS emulator — $0 of real AWS. Uses only released, public artifacts.

## Prerequisites
Docker · Node 20+ · AWS CLI v2 · git

## 1. Start the Floci emulator
Build it from the pinned public tag (works on any architecture):
```sh
git clone https://github.com/lex00/floci
cd floci && git checkout awsbench-integration-v2
docker compose up -d            # AWS-shaped services at http://localhost:4566
cd ..
```
On Apple Silicon / arm64 you can skip the build and pull the prebuilt image
instead:
```sh
docker run -d -p 4566:4566 -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/lex00/floci:awsbench
```

## 2. Get the estate (a chant project)
```sh
git clone https://github.com/lex00/aws-bench
cd aws-bench && git checkout awsbench-integration-v2
cd benchmarks/arms/chant-ec2-multiregion-search-v2
npm install                     # pulls released @intentius/chant + lexicon-aws (0.33.0)
```

## 3. Deploy the estate to Floci
```sh
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
./deploy.sh
```
`deploy.sh` runs `chant build` on each stack and creates 6 EC2 instances across
3 regions (plus the account default VPC). Expected outcome: four `deployed <stack>`
lines, ending with a running-instance tally of `6 running`.

## 4. The discriminating query
"Which instances are SSH-reachable from the internet?" — the subnet must route to
an Internet Gateway AND a security group must allow tcp/22 from 0.0.0.0/0, whether
attached directly or through the launch template:
```sh
npx chant search "kind:EC2::Instance attr:internetFacing=true attr:effectiveIngress=tcp:22:0.0.0.0/0" \
  --live --env floci --explain --show InstanceId
```
Expected — **2**: `webServer` and `launchTemplateServer`. `--explain` names the
route-table → IGW path for each match and why the other four are excluded.

Sanity check — "which instances are in a public subnet" is **5**, including the
account default-VPC instance chant resolves from live route tables:
```sh
npx chant search "kind:EC2::Instance attr:internetFacing=true" --live --env floci --explain --show InstanceId
```

## 5. Why it separates the tools
`launchTemplateServer` is reachable through a security group attached via its
launch template, not directly. A raw `describe-instances` + `describe-security-groups`
sweep misses that hop and returns 1. chant folds both hops (route-table→IGW and
the launch-template SG) into `internetFacing` / `effectiveIngress`, so the count
is right — and the same query holds as the estate grows.

## Expected outcome
The reproduction succeeds when both hold:
- ssh-reach query returns exactly `webServer` + `launchTemplateServer` — 2 rows.
- public-subnet query returns 5 rows, including `defaultVpcServer`.

Anything else (0, 1, 3+, or a missing `defaultVpcServer`) is a failure.

## Scope
This proves the discriminating result — SSH-reachable = 2, public = 5. It does not
reproduce the full win-rate or cost figures; those need the aws-bench harness
(Haiku 4.5, k=3, judged), not the two queries above. To run the harness and tally
the scores across all four arms, see `../REPRODUCE-BENCHMARK.md`.

---
Tested with `@intentius/chant@0.33.0` + `@intentius/chant-lexicon-aws@0.33.0`.
