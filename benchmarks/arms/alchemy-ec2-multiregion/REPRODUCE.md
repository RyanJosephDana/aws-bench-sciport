# Reproduce: the Alchemy arm on ec2-multiregion

Stands up the same estate as the chant, CDK, Terraform, and Pulumi arms,
authored in Alchemy (published async model, `alchemy@0.93.12`), on the Floci
AWS emulator — $0 of real AWS — and shows the two reachability facts the
benchmark turns on. Uses only released, public artifacts plus the local patch
in this directory.

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
cd floci && git checkout scenario1-working
docker compose up -d            # AWS-shaped services at http://localhost:4566
cd ..
```

## 2. Get the estate
```sh
git clone https://github.com/lex00/aws-bench
cd aws-bench && git checkout feat/emulator-floci   # the scenario1-working tag predates this arm
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
