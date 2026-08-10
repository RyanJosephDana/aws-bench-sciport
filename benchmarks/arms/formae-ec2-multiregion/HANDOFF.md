# Handoff: build the Formae arm for aws-bench scenario 1 (ec2-multiregion)

## Goal
Add Formae as an arm next to chant / Terraform / Pulumi / CDK / Alchemy, on the
Floci emulator ($0), and produce its X/24 score on the board plus the negative
set. Same estate, same model, same k=3. Wire it into `../REPRODUCE-BENCHMARK.md`.

## Why
Formae (Platform Engineering Labs) is the closest architectural neighbour chant
has on the board: compiled configuration (Pkl), no state file, an always-on
agent that observes and reconciles. Its AWS plugin speaks **only Cloud Control**
(`formae-plugin-aws` is literally "AWS CloudControl resource plugin for
formae"). The claim under test: does an agent-holding-Formae answer estate
questions from the datastore (`formae inventory`), and does the always-on
reconciler close the gap that sank the record-of-own-deployments arms on the
negative set?

## Status: gate OPEN — emulator side resolved, deploy proof not yet run
The blocker was Floci serving Cloud Control *writes*. That is resolved:

- `ghcr.io/lex00/floci:awsbench-f670952` (branch `awsbench-integration-v2` at
  `f6709527`) serves `CreateResource` / `DeleteResource` / `GetResource` /
  `GetResourceRequestStatus` / `ListResources`. The floating `:awsbench` tag
  points at the same image.
- Upstream this is floci-io/floci#2037 — review blockers fixed, maintainer
  sign-off on everything else, not yet merged. The fork's integration line
  carries it either way, plus upstream's EC2-tags-in-CC-reads (#1933).

The remaining gate is the same one Alchemy had: deploy a VPC + subnet into
Floci with `formae apply` and read them back with `formae inventory`. Nobody
has run that yet. Do it before authoring the estate.

## Facts that shape the build (verified in source, 2026-08-09)
- **No endpoint patch needed.** The plugin builds every client through
  aws-sdk-go-v2 `config.LoadDefaultConfig` (`pkg/config/config.go`) and never
  sets `BaseEndpoint`, so the SDK's native `AWS_ENDPOINT_URL` (and
  `AWS_ENDPOINT_URL_CLOUDCONTROL`) env handling applies. This is the patch-free
  story Alchemy did not have (alchemy-run/alchemy#991).
- **The plugin's Cloud Control surface** (`pkg/ccx/client.go`): CreateResource,
  UpdateResource, DeleteResource, GetResource, GetResourceRequestStatus,
  ListResources.
- **Floci does not implement `UpdateResource`.** A first apply is create-only,
  so the gate proof should pass without it — but Formae's reconcile mode exists
  to converge drift, and convergence updates. If the estate deploy or the
  reconciler calls it, file in the fork with a fix (pattern: #2037's
  `provisionStandalone`, an update path through the CFN provisioner).
- **`ListResources` covers 9 types**: S3 Bucket, EC2 VPC / Subnet /
  SecurityGroup / Instance / LaunchTemplate, IAM Role / User / InstanceProfile.
  Anything else Formae creates reads back through the create-time record
  (`GetResource` fallback), but **discovery of resources Formae did not create
  is limited to the listed types**. The default VPC is listable — the
  default-VPC question stays answerable. RouteTable / IGW / associations are
  not listable; if Formae's discovery sweep needs them, that is fork work
  (floci-io/floci#2043 is the upstream half).
- **Install and run**: `curl -fsSL https://hub.platform.engineering/get/formae.sh | bash`,
  then `formae agent start` (local agent, own datastore), `formae apply
  --mode reconcile <forma>.pkl`, `formae inventory resources --query "..."`.
  License is FSL-1.1 — fine for benchmarking, say so in REPRODUCE.

## Build steps
1. **Gate proof** (`proof/`): minimal forma declaring a VPC + subnet, applied
   against the new image with `AWS_ENDPOINT_URL=http://localhost:4566`,
   confirmed via `aws --endpoint-url ... ec2 describe-vpcs` *and*
   `formae inventory resources`. Capture whether UpdateResource ever fires.
2. **Author the estate** in Pkl, matching `../terraform-ec2-multiregion/`
   exactly — 6 instances across 3 regions, and the discriminating detail: the
   SSH-open SG attached via a **launch template**, not the instance. Regions
   are per-target in Formae; the estate is one forma with region-scoped stacks.
3. **`../briefing-formae.md`** — mirror `briefing-pulumi.md`'s shape; the
   sanctioned read is `formae inventory` / the agent's datastore.
4. **`REPRODUCE.md`** for the arm; row + link in `../REPRODUCE-BENCHMARK.md`.
5. **Run** k=3 on both question sets when the harness owner says go.

## Pointers
- Emulator: `ghcr.io/lex00/floci:awsbench-f670952`, fork branch
  `awsbench-integration-v2`, upstream PR floci-io/floci#2037.
- Plugin source: github.com/platform-engineering-labs/formae-plugin-aws.
- Topology to match: `../terraform-ec2-multiregion/` (+ its REPRODUCE.md).
- Harness runbook: `../REPRODUCE-BENCHMARK.md`.
