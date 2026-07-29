# Reproduce: chant resolves internet SSH-reachability where a CLI sweep under-counts

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
cd floci && git checkout scenario1-working
docker compose up -d            # AWS-shaped services at http://localhost:4566
cd ..
```
On Apple Silicon / arm64 you can skip the build and pull the prebuilt image
instead:
```sh
docker run -d -p 4566:4566 -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/lex00/floci:scenario1-arm64
```

## 2. Get the estate (a chant project)
```sh
git clone https://github.com/lex00/aws-bench
cd aws-bench && git checkout scenario1-working
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
