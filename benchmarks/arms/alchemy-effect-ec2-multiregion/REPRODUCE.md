# Reproduce: the Alchemy v2 (Effect) arm on ec2-multiregion

Stands up the same estate as the chant, CDK, Terraform, Pulumi, and Alchemy-v1
arms, authored in the Alchemy v2 Effect line ("Infrastructure-as-Effects"), on
the Floci AWS emulator — $0 of real AWS — and shows the two reachability facts
the benchmark turns on. Uses only released, public artifacts (the npm `next`
channel) plus the local patch in this directory.

This is the line James Ward praised as "Alchemy Effect." It is a different
product surface from the published v1 async package the `alchemy-ec2-multiregion`
arm benchmarks: Effect programs, codegen'd resources over the Effect-native
`@distilled.cloud/aws` SDK, a CLI (`alchemy deploy`), and a per-stack local
state store. What it shares with v1 — and with Terraform and Pulumi — is the
epistemics: an owned state file that carries every attribute but folds neither
reachability question. You compute them by hand below.

## What is and is not stock Alchemy v2

- **Pinned beta.** `alchemy@2.0.0-beta.65` (npm dist-tag `next`; `latest` is
  still the v1 async 0.93.x line) + `effect@4.0.0-beta.102`. The Effect line is
  released but moving fast — expect drift on unpinned installs.
- **Endpoint patch** (`apply-endpoint-patch.sh`). v2 has a first-class endpoint
  layer (every SDK call resolves `AWSEnvironment.endpoint`), but nothing
  populates it — the env-var auth path drops the endpoint. One logical patch
  carries `AWS_ENDPOINT_URL` into the resolved environment. Two Floci-compat
  fixes ride along: tolerate the unimplemented `TagInstanceProfile` operation,
  and launch instances with top-level `SubnetId`/`SecurityGroupIds` instead of
  a primary `NetworkInterfaces[0]` spec, which Floci ignores (silently dropping
  subnet placement and SGs). The header of the patch script documents each.
- **One custom resource** (`src/LtInstance.ts`). v2's native `AWS.EC2.Instance`
  requires `imageId` + `instanceType` and attaches security groups directly;
  `AWS.AutoScaling.LaunchTemplate` exists but nothing launches a bare instance
  from it. The instance-launched-from-a-launch-template is authored in the
  framework's own provider style (`Provider.effect` + reconcile/delete on the
  `@distilled.cloud/aws` SDK). Its state records the launch-template REFERENCE,
  not the security groups it resolves to — the same shape every other
  state-file arm gives this estate. The `ssh-reachable` task turns on that hop.
- **Three stacks, one per region.** v2's `AWSEnvironment` carries a single
  region, so the estate deploys as `us-east-1.run.ts` / `us-west-1.run.ts` /
  `us-west-2.run.ts` (like the CDK arm's per-region stacks), each a
  self-contained `Alchemy.Stack` with local state.

Known live divergences from the Terraform/Pulumi arms, none read by any task's
ground truth: v2's launch template carries no block-device or IMDSv2 metadata
options (its props don't model them), security groups keep default egress, and
subnets carry explicit availability zones.

## Prerequisites
Docker · Bun · AWS CLI v2 · Python 3 · git

## 1. Start the Floci emulator
```sh
git clone https://github.com/lex00/floci
cd floci && git checkout scenario1-working
docker compose up -d            # AWS-shaped services at http://localhost:4566
cd ..
```

## 2. Get the estate
```sh
git clone https://github.com/lex00/aws-bench
cd aws-bench && git checkout scenario1-working
cd benchmarks/arms/alchemy-effect-ec2-multiregion
bun install
./apply-endpoint-patch.sh
```

## 3. Deploy to Floci
State is local JSON under `.alchemy/state/` — no cloud account, no login. The
`CI=true` + env-var combination selects v2's non-interactive env auth path.
The three stacks are independent; deploy them in parallel or in any order:
```sh
export CI=true AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_ACCOUNT_ID=000000000000 AWS_ENDPOINT_URL=http://localhost:4566
AWS_REGION=us-west-1 bunx alchemy deploy us-west-1.run.ts --stage bench --yes &
AWS_REGION=us-west-2 bunx alchemy deploy us-west-2.run.ts --stage bench --yes &
wait
AWS_REGION=us-east-1 bunx alchemy deploy us-east-1.run.ts --stage bench --yes
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
the per-resource state files across the three stacks:
```sh
python3 - .alchemy/state <<'PY'
import json, glob, sys
res = [json.load(open(p)) for p in glob.glob(f"{sys.argv[1]}/*/bench/*.json")]
def rt(r): return r.get("resourceType")

insts = [r for r in res if rt(r) in ("AWS.EC2.Instance", "Bench.EC2.LaunchTemplateInstance")]
pub = {(r.get("attr") or {}).get("subnetId") or (r.get("props") or {}).get("subnetId")
       for r in res if rt(r) == "AWS.EC2.RouteTableAssociation"}
open22 = set()
for r in res:
    if rt(r) == "AWS.EC2.SecurityGroup":
        for ing in (r.get("props", {}).get("ingress") or []):
            if ing.get("fromPort") == 22 and ing.get("cidrIpv4") == "0.0.0.0/0":
                open22.add(r["attr"]["groupId"])
lt_sgs = {r["attr"]["launchTemplateId"]: set(r["props"].get("securityGroupIds") or [])
          for r in res if rt(r) == "AWS.AutoScaling.LaunchTemplate"}

facing = ssh = 0
for r in insts:
    a, p = r.get("attr") or {}, r.get("props") or {}
    subnet = a.get("subnetId") or p.get("subnetId") or ""
    is_facing = subnet in pub or str(subnet).startswith("subnet-default")
    sgs = set(a.get("securityGroupIds") or [])
    lt = a.get("launchTemplateId") or p.get("launchTemplateId")
    if lt:
        sgs |= lt_sgs.get(lt, set())   # the launch-template hop
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
Tested with Bun 1.3.6 + alchemy 2.0.0-beta.65 + effect 4.0.0-beta.102 against
Floci `ghcr.io/lex00/floci:awsbench`.
