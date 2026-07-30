# Reproduce: the Pulumi arm on ec2-multiregion

Stands up the same estate as the chant, CDK, and Terraform arms, authored in
Pulumi (TypeScript), on the Floci AWS emulator — $0 of real AWS — and shows the
two reachability facts the benchmark turns on. Uses only released, public
artifacts.

Like Terraform, Pulumi's applied state carries every attribute, but the
reachability answers are a multi-hop join the state does not fold. You compute
them by hand below. That by-hand join is what a small model must do too — it wins
the launch-template SSH hop CDK's CLI sweep misses, but fumbles the route-table
walk for public subnets (1/3), where chant folds the same question into one query
and answers it 3/3.

## Prerequisites
Docker · Pulumi CLI · Node 18+ · AWS CLI v2 · Python 3 · git

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
cd benchmarks/arms/pulumi-ec2-multiregion
npm install
```

## 3. Deploy to Floci
Use a local file backend (no Pulumi Cloud account) and any passphrase:
```sh
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
export PULUMI_CONFIG_PASSPHRASE=floci PULUMI_BACKEND_URL="file://$PWD"
pulumi stack init dev
pulumi up --yes
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
There is no Pulumi command that answers "which instances are reachable via SSH
from the internet" — the state has the pieces, you assemble them. Export the state
and reduce it:
```sh
pulumi stack export --stack dev > /tmp/pstate.json
python3 - /tmp/pstate.json <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
res = d["deployment"]["resources"]
def out(r): return r.get("outputs", {})

insts = [r for r in res if r["type"] == "aws:ec2/instance:Instance"]
pub = {out(r).get("subnetId") for r in res if r["type"] == "aws:ec2/routeTableAssociation:RouteTableAssociation"}
open22 = set()
for r in res:
    if r["type"] == "aws:ec2/securityGroup:SecurityGroup":
        for ing in out(r).get("ingress", []):
            if ing.get("fromPort") == 22 and "0.0.0.0/0" in (ing.get("cidrBlocks") or []):
                open22.add(out(r).get("id"))
lt_sgs = {out(r).get("id"): set(out(r).get("vpcSecurityGroupIds") or [])
          for r in res if r["type"] == "aws:ec2/launchTemplate:LaunchTemplate"}

facing = ssh = 0
for i in insts:
    v = out(i)
    is_facing = v.get("subnetId") in pub or str(v.get("subnetId", "")).startswith("subnet-default")
    sgs = set(v.get("vpcSecurityGroupIds") or [])
    lt = v.get("launchTemplate")
    if isinstance(lt, dict):
        sgs |= lt_sgs.get(lt.get("id"), set())   # the launch-template hop
    facing += is_facing
    ssh += is_facing and bool(sgs & open22)
print("internet-facing (find-public):", facing)   # 5
print("ssh-reachable:", ssh)                       # 2
PY
```
Output: `internet-facing (find-public): 5` and `ssh-reachable: 2`.

The `sgs |= lt_sgs...` line is the launch-template hop — the security group reachable
only through a launch template. Drop it and the count falls to 1, which is what a CLI
sweep (and the CDK arm) returns.

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
Tested with Pulumi 3.255.0 + @pulumi/aws 6.x against Floci `awsbench-integration-v2`.
