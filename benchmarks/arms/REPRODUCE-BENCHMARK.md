# Reproduce: the full five-arm benchmark on ec2-multiregion

The per-arm `REPRODUCE.md` files prove each estate is faithful and derive the two
reachability facts by hand. They stop short of the win-rate and cost table. This
runbook covers the rest: running the aws-bench agent trials against each estate and
tallying the scores the report turns on.

Everything here is $0 of real AWS — the estates live on the Floci emulator and the
only paid call is the agent-under-test (Haiku 4.5).

## What this reproduces

| Task | chant | Terraform | Pulumi | CDK | Alchemy | Alchemy v2 (Effect) |
|---|---|---|---|---|---|---|
| ssh-reachable from internet | 3/3 | 3/3 | 2/3 | 0/3 | 0/3 | 2/3 |
| cross-region connectivity | 3/3 | 3/3 | 3/3 | 3/3 | 2/3 | 1/3 |
| list all regions | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 0/2 |
| instances by VPC | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 0/3 |
| in public subnets | 3/3 | 1/3 | 1/3 | 2/3 | 2/3 | 0/3 |
| **Total (valid)** | **15/15** | **13/15** | **12/15** | **11/15** | **10/15** | **3/15** |

Cost per arm lands near chant $1.33 / Terraform $2.17 / Pulumi $1.76 /
CDK $1.87 / Alchemy $1.55 / Alchemy v2 $1.12.

The Alchemy arm (`alchemy-ec2-multiregion`, published async `alchemy@0.93.12`
with a local endpoint patch) clusters with the other state-file arms, as the
program-vs-data claim predicts. Its ssh-reachable failures are instructive: the
agent finds both open instances — including the launch-template hop — from
state, then discounts them because Floci's emulated public IPs read
`127.0.0.1`. The join the state does not fold still has to be argued by hand,
and that is where the answer decays.

The Alchemy v2 Effect arm (`alchemy-effect-ec2-multiregion`, pinned
`alchemy@2.0.0-beta.65`) scores far below its v1 sibling, and the failure mode
is specific: v2 has no native way to launch an instance from a launch template,
so that one instance is a custom resource with its own `resourceType` — and
agents that census instances from state by exact type
(`"resourceType":"AWS.EC2.Instance"`) silently miss the sixth instance. Every
counting task decays with it. Trials that fell through to the live API scored;
trials that trusted the state census did not. The per-task numbers are one k=3 run;
three runs of this arm landed 6/15, 4/15 and 3/15 with the same signature every
time, so the low cluster is stable even though the number moves. One valid-set
trial in the recorded run was lost to a verifier error, making the total 3 of 14
scored trials.

## Pin these first

The one thing that makes results drift is an unpinned toolchain. Fix all of it:

| Piece | Pin |
|---|---|
| chant | `@intentius/chant@0.33.0` + `@intentius/chant-lexicon-aws@0.33.0` (released on npm) |
| chant arm | `benchmarks/arms/chant-ec2-multiregion-search-v2` + `briefing-chant-search-v2.md` |
| Floci | `github.com/lex00/floci` branch `feat/emulator-floci` (or `ghcr.io/lex00/floci:awsbench`) |
| aws-bench | `github.com/lex00/aws-bench` branch `feat/emulator-floci` |
| Agent-under-test | `claude-code` on `claude-haiku-4-5-20251001`, k=3 |
| Valid set | all `ec2-multiregion` tasks **except** `describe-cfn-stack-resources` (its CFN-shaped ground truth measures the generator, not the agent) |

There is no "v3" chant. The effective-SG enrichment the scores rely on is the
`attr:effectiveIngress` / `attr:internetFacing` search in 0.33.0, exercised by
`briefing-chant-search-v2.md`. The older `chant-ec2-multiregion-search` arm (vendored
0.28.0) is superseded — do not use it.

## Prerequisites
Docker · Node 20+ · uv · git · a real Anthropic credential
(`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or `ANTHROPIC_API_KEY`)

## 1. Deploy each arm's estate

Follow each arm's own `REPRODUCE.md` for the estate (chant, terraform, pulumi,
alchemy, cdk).
The report runs one Floci at a time and **wipes between arms** so no estate bleeds
into the next:

```sh
cd benchmarks/floci
docker compose down -v && docker compose up -d   # fresh estate, port 4566
```

That compose file carries the emulator-fidelity settings the run depends on —
faithful private IP, AWS-shaped public DNS, and the region allow-list standing in
for the account's region-restriction SCP. Starting Floci without them changes what
the arms read from the live estate and therefore what they score; see the comments
in `benchmarks/floci/docker-compose.yml` for why each one is set.

Two Floci fidelity gaps to expect while deploying — neither is a code fix, and
neither affects the scores:

- **Leftover orphans on retry.** A partial deploy can leave a named IAM policy or
  launch template behind, so a rerun fails with "already exists" / "already in use".
  Delete the named orphan (`aws iam delete-policy` / `aws ec2 delete-launch-template`)
  and rerun only the failing `create-stack`. Floci does not clean up on rollback the
  way real CloudFormation does.
- **Cosmetic cleanup errors.** `ChangeSetNotFoundException` (and cdk-bootstrap's
  changeset noise) come from Floci not retaining executed changesets. Confirm the real
  state with `describe-stacks` before treating it as a failure.

The CDK arm has no per-arm `REPRODUCE.md` — its estate ships inside the aws-bench
scenario, so get it from the cache and deploy it directly:

```sh
uv run aws-bench env init --env-name awsbench-cdk -d ec2-multiregion   # populates ~/.aws-bench/cache
cp -r ~/.aws-bench/cache/scenarios/*/ec2-multiregion/scenario/cdk_app ./cdk_app
cd cdk_app && npm install
```
Two source edits are Floci-sandbox workarounds, not methodology changes — both remove
Lambda-backed custom resources Floci can't run in a cgroup-limited sandbox:
- `cdk.json`: set `@aws-cdk/aws-ec2:restrictDefaultSecurityGroup` to `false`.
- `stacks/ec2_*.ts`: delete the `CreateAmi` `cr.AwsCustomResource` block, then bake the
  AMI by hand after deploy (`aws ec2 create-image` against the web instance).
```sh
npm run build
export AWS_ENDPOINT_URL=http://localhost:4567 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 CDK_DEFAULT_ACCOUNT=000000000000
for r in us-east-1 us-west-1 us-west-2; do npx cdk bootstrap "aws://000000000000/$r"; done
npx cdk deploy --all --require-approval never --concurrency 4
```

Optional speedup — run two arms at once on two Floci instances. They deploy
identically-named stacks, so isolate them by port and by the EC2 SSH-forward range or
the backing containers collide:

```yaml
# docker-compose.yml for the second instance (port 4567)
services:
  floci:
    image: ghcr.io/lex00/floci:awsbench
    ports: ["4567:4566"]
    volumes: ["/var/run/docker.sock:/var/run/docker.sock", "./data:/app/data"]
    environment:
      FLOCI_HOSTNAME: floci-b
      FLOCI_BASE_URL: http://floci-b:4566
      FLOCI_SERVICES_EC2_SSH_PORT_RANGE_START: "2300"
      FLOCI_SERVICES_EC2_SSH_PORT_RANGE_END: "2399"
      FLOCI_SERVICES_EC2_AWS_FAITHFUL_PRIVATE_IP: "true"
      FLOCI_SERVICES_EC2_AWS_FAITHFUL_PUBLIC_DNS: "true"
      FLOCI_ALLOWED_REGIONS: "us-east-1,us-west-1,us-west-2"
    networks: { floci_b_default: { aliases: [localhost.floci.io] } }
networks: { floci_b_default: { name: floci_b_default } }
```
```sh
docker compose -p floci-b up -d   # -> localhost:4567
```

## 2. Run the trials

One job per arm, k=3. Swap the three per-arm values (endpoint, briefing, mount) and
keep everything else fixed:

The agent authenticates with a Claude Code OAuth token. Put it in `~/.anthropic`
(what `claude setup-token` writes) and the run picks it up — there is no need to
write it into the repo, and a token file inside the working tree is one
`git add -A` away from being published.

```sh
cd aws-bench
claude setup-token          # writes ~/.anthropic, once per machine

# --- chant arm (repeat the block per arm with the table below) ---
AWS_BENCH_EMULATOR=floci \
AWS_BENCH_EMULATOR_ENDPOINT=http://localhost:4566 \
AWS_BENCH_EMULATOR_CONTAINER_ENDPOINT=http://host.docker.internal:4566 \
AWSBENCH_SCAN_METHOD=fastscan \
uv run aws-bench run --env-name awsbench -d ec2-multiregion \
  -a claude-code -m claude-haiku-4-5-20251001 -k 3 \
  --extra-instruction-path benchmarks/arms/briefing-chant-search-v2.md \
  --mounts '[{"type":"bind","source":"'"$PWD"'/benchmarks/arms/chant-ec2-multiregion-search-v2","target":"/workspace/chant","read_only":true}]' \
  --no-verify-env --yes
```

Per-arm values:

| Arm | briefing | mount source → target |
|---|---|---|
| chant | `briefing-chant-search-v2.md` | `benchmarks/arms/chant-ec2-multiregion-search-v2` → `/workspace/chant` |
| terraform | `briefing-terraform.md` | `benchmarks/arms/terraform-ec2-multiregion` → `/workspace/terraform` |
| pulumi | `briefing-pulumi.md` | `benchmarks/arms/pulumi-ec2-multiregion` → `/workspace/pulumi` |
| alchemy | `briefing-alchemy.md` | `benchmarks/arms/alchemy-ec2-multiregion` → `/workspace/alchemy` |
| alchemy-effect | `briefing-alchemy-effect.md` | `benchmarks/arms/alchemy-effect-ec2-multiregion` → `/workspace/alchemy` |
| cdk | `briefing-cdk.md` | the scenario's `cdk_app` (from `~/.aws-bench/cache`) → `/workspace/cdk_app` |

Notes:
- Exclude `describe-cfn-stack-resources` from the run — it is not in the valid set.
- `--no-verify-env` is required: the estate was deployed by hand, so the harness has
  no POST_SETUP baseline snapshot to verify against.
- `-n <N>` runs trials concurrently to cut wall-clock; it does not change scores.
- Set `CLAUDE_CODE_OAUTH_TOKEN` in the environment instead if you would rather not
  use `~/.anthropic`; it takes precedence. Do not put it in a file under the repo.
- Point the CDK arm at port 4567 if you deployed it on the second Floci instance.

## 3. Tally

Each job writes `jobs/<timestamp>/result.json` (aggregate pass rate, tokens, cost)
and one `jobs/<timestamp>/<task>__<id>/verifier/reward.txt` (1.0 or 0.0) per trial.
Sum the `reward.txt` values per task across the three rounds for the X/3 breakdown,
then across tasks for the arm total.

## Expected outcome

The reproduction succeeds when the per-task and total columns match the table at the
top: chant 15/15, Terraform 13/15, Pulumi 12/15, CDK 11/15, Alchemy 10/15,
Alchemy v2 3/15 (expect wobble on individual v2 rows; the low cluster is the
signal; v1 has hit exactly 10/15 on two independent runs with rows
redistributing), with chant answering ssh-reachable 3/3 and public-subnet 3/3 where the
other arms fall to 0–2/3.
Token and cost totals land within run-to-run variance of the figures above.
