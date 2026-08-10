# Handoff: build the ConfigHub arm for aws-bench scenario 1 (ec2-multiregion)

## Goal
Add ConfigHub as an arm on the Floci emulator and produce its score on both
question sets. Same estate, same model, same k=3.

## Why
ConfigHub is the config-as-data corner of the comparison: configuration lives
as units in a store, applied by workers, queried through `cub`. The claim under
test is whether an agent holding the store answers estate questions better than
one holding a state file — and what happens on the negative set, where the
store only knows what was put in it.

## Status: bridge BUILT (2026-08-09, see bridge/) — the SaaS half needs a human
ConfigHub cannot apply an AWS estate out of the box. The SDK's toolchain types
(`confighub/sdk`, `core/workerapi/types.go`) are `ConfigHub/YAML`,
`Kubernetes/YAML`, and the `AppConfig/*` family — application config and
Kubernetes, no AWS. The arm therefore needs a **custom bridge worker** before
it has a deploy story at all.

## The path
1. **The bridge exists** — `bridge/` in this directory: apply / refresh /
   import / destroy over Cloud Control, tested against an in-process fake.
   What remains is running it against hosted ConfigHub (`cub auth login`,
   `cub worker run`) — the SaaS credential is the one piece only a human
   holds.
2. **Emulator side is already resolved** — the same surface the Formae arm
   uses: `ghcr.io/lex00/floci:awsbench-e0eb525` (fork branch
   `awsbench-integration-v2`, upstream PR floci-io/floci#2037) serves Cloud
   Control create/read/delete/status/list. See the Formae HANDOFF for the
   coverage caveats (no UpdateResource; ListResources limited to 9 types —
   `import` leans on exactly that op, so import breadth is bounded by it).
3. **Author the estate as units** matching `../terraform-ec2-multiregion/`,
   including the SSH-open SG attached via launch template.
4. **Briefing**: the sanctioned read is `cub unit`/`cub function` queries
   against the store; mirror `briefing-pulumi.md`'s shape.
5. **REPRODUCE.md** + runbook row, then k=3 when the harness owner says go.

## Honesty items the writeup must carry
- **The bridge is ours.** Every other arm deploys with its vendor's own apply
  path; this one deploys through a bridge we wrote because the vendor has no
  AWS path. The score measures ConfigHub-the-store plus our bridge, and the
  writeup has to say so plainly — same discipline as the Alchemy custom
  resources note.
- **The control plane is SaaS.** `cub auth login` talks to hosted ConfigHub;
  there is no self-hosted control plane. The arm depends on an external
  service and a per-run account, unlike every other arm. Say it in
  prerequisites, like the Anthropic-credential note.
- **Worker connectivity**: the worker dials out to the SaaS and in to the
  emulator; runs are reproducible only while the hosted API is stable. Pin the
  `cub` and SDK versions in REPRODUCE.

## Open questions (answer before building)
- Can a unit's live state be refreshed through the bridge so `cub` reflects
  the estate post-apply? (The refresh op exists; verify the round-trip.)
- Does ConfigHub's function machinery (`cub function`) give the agent a query
  surface beyond raw unit YAML — and is using it in the briefing fair, or the
  arm's equivalent of chant's folded search?

## Pointers
- Bridge guide: `confighub/examples` → `custom-workers/hello-world-bridge`.
- SDK: github.com/confighub/sdk (`cmd/cub-worker`, `core/workerapi`).
- Emulator + coverage caveats: `../formae-ec2-multiregion/HANDOFF.md`.
- Topology: `../terraform-ec2-multiregion/` (+ REPRODUCE.md).
