# Handoff: build the Alchemy arm for aws-bench scenario 1 (ec2-multiregion)

## Goal
Add Alchemy as a fifth arm next to chant / Terraform / Pulumi / CDK, on the Floci
emulator ($0), and produce its X/15 score. Same estate, same model (Haiku 4.5),
same 5-task valid set. Wire it into `../REPRODUCE-BENCHMARK.md`.

## Why
James Ward called "Alchemy Effect" the best in this space. The claim under test is
that Alchemy is executed TypeScript with an owned state file — the Pulumi lineage —
not infra-as-data. The benchmark is the demonstration. **Expected result: it
clusters with Pulumi/CDK (≈11–13/15), not near chant's 15/15**, because the agent
still derives reachability by hand from state or the live API. If it lands there,
it proves the point; report the number honestly either way.

## Status: COMPLETE (2026-07-29) — scored 10/15
All five build steps are done: extended patch (`apply-endpoint-patch.sh`),
estate (`alchemy.run.ts` + `src/aws-extra.ts`), `../briefing-alchemy.md`,
`REPRODUCE.md`, runbook wiring, and the k=3 run. Estate verified on Floci:
4/1/1 running, internet-facing 5 / ssh-reachable 2 derived from state.
Recorded score **10/15** (ssh-reachable 1/3, cross-region 3/3, list regions
3/3, by-VPC 2/3, public subnets 1/3), cost $1.59 — clusters with Pulumi 12 /
CDK 11, as predicted, not with chant's 15. One deviation from the plan below:
Floci's Cloud Control API is read-only and the published async Alchemy has no
Instance/LaunchTemplate/InstanceProfile resources, so those are custom
resources in `src/aws-extra.ts` (SDK-based, endpoint via env) — see REPRODUCE
for the full accounting.

## Status: gate CLEARED
The blocker was whether Alchemy can target Floci at all. It can, with a small patch.
Proven: `proof/alchemy.run.ts` deployed a VPC + subnet into Floci
(`vpc-…`, `subnet-…` confirmed via `aws --endpoint-url`). Reproduce:
```sh
cd proof && bun add alchemy effect @distilled.cloud/aws
bash apply-endpoint-patch.sh
AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  AWS_REGION=us-east-1 ALCHEMY_TELEMETRY_DISABLED=1 bun alchemy.run.ts
```

## Facts that shape the build (learned the hard way)
- **Published `alchemy@0.93.12` is the async-function model, not Effect.** Exports are
  `alchemy/aws/ec2` etc. A resource is `Resource("aws::Vpc", async (id, props) => …)`.
  The Effect line (`alchemy/AWS`, `AWS.Endpoint.of()`) is the repo main-branch rewrite
  and is **not published**. Decide which to benchmark; this handoff targets the
  published async version (what `npm i` gives you). If you must test the Effect line
  to answer James precisely, build from the `alchemy-run/alchemy` main branch instead —
  it has a native endpoint layer, but it's an unreleased in-progress rewrite.
- **No endpoint override upstream.** Filed `alchemy-run/alchemy#991`. Each service
  hardcodes `https://<svc>.<region>.amazonaws.com` and nothing reads `AWS_ENDPOINT_URL`.
- **Patch `src/*.ts`, not `lib/*.js`.** Bun uses the `"bun"` export condition → source.
  `apply-endpoint-patch.sh` does the EC2 site (proven). **Extend it** to IAM, SSM, and
  LaunchTemplate — grep `node_modules/alchemy/src/aws` for `amazonaws.com` and wrap each
  URL build the same way. Capture the final patch reproducibly (`bun patch`, or a
  `lex00/alchemy` fork) and document it in this arm's REPRODUCE, like the Floci/Formae
  patches.
- **SigV4 is fine.** EC2's client pins `service: "ec2"`, so signing keeps the service
  in scope against a localhost host and Floci routes correctly. Verify the same for
  IAM/SSM clients (they should pin their service too).
- **Deploy:** `bun alchemy.run.ts`, phase defaults to `up`, creds/region/endpoint via env.

## Remaining build
1. **Extend the patch** to every service the estate touches (IAM, SSM, LaunchTemplate).
2. **Author the estate** in `alchemy.run.ts` (+ `src/`), matching the other arms exactly.
   Use `terraform-ec2-multiregion` / `pulumi-ec2-multiregion` as the topology spec:
   - 6 EC2 instances across us-east-1 (×4), us-west-1 (×1), us-west-2 (×1).
   - public and private subnets, route tables, an internet gateway on the public side.
   - **the discriminating detail:** the SSH-open (`tcp:22 0.0.0.0/0`) security group is
     attached via a **launch template**, not directly on the instance. This is what the
     `ssh-reachable` task turns on — get it right or the arm's score is meaningless.
   - IAM role/instance-profile; the SSM `/exports` contract the tasks read.
   - Multi-region = region-scoped resources (region via `AwsClientProps`/scope per region).
     This is the one non-trivial modeling piece; the other arms deploy per-region stacks.
3. **`briefing-alchemy.md`** — mirror `briefing-pulumi.md` (agent reads state or the live
   API; there is no folded query). Keep task instructions identical to the other arms.
4. **`REPRODUCE.md`** for this arm; add an Alchemy row + link to `../REPRODUCE-BENCHMARK.md`.
5. **Run** k=3 × 5 tasks (`describe-cfn-stack-resources` excluded), tally `reward.txt`.
   Needs a real Anthropic credential.

## Pointers
- Proof + patch: `proof/`.
- Topology to match: `../terraform-ec2-multiregion/`, `../pulumi-ec2-multiregion/` (+ their REPRODUCE.md).
- Harness runbook: `../REPRODUCE-BENCHMARK.md` (pins, Floci setup, run invocation, tally).
- Upstream gap: `alchemy-run/alchemy#991`.
