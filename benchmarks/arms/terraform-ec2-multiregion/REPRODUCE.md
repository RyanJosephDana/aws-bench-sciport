# Reproduce: the Terraform arm on ec2-multiregion

Stands up the same estate as the chant and CDK arms, authored in Terraform, on the
Floci AWS emulator — $0 of real AWS — and shows the two reachability facts the
benchmark turns on. Uses only released, public artifacts.

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
cd floci && git checkout scenario1-working
docker compose up -d            # AWS-shaped services at http://localhost:4566
cd ..
```

## 2. Get the estate
```sh
git clone https://github.com/lex00/aws-bench
cd aws-bench && git checkout scenario1-working
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
Tested with Terraform 1.15.8 + hashicorp/aws 6.56.0 against Floci `scenario1-working`.
