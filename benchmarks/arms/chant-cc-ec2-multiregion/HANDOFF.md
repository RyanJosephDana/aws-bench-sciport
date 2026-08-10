# Handoff: the chant config-controller arm for aws-bench scenario 1

## Goal
A chant arm that runs on the config-controller path — observe / status /
target over the same Cloud Control surface the Formae and ConfigHub arms use —
so the three tools are compared on the same transport.

## Why this arm exists when a chant arm already does
`chant-ec2-multiregion-search-v2` pins chant 0.33.0, which predates the CC
lane (INTENTIUS/chant#1198, closed 2026-08-02; the e2e is #1335,
`just aws-cc-e2e`). Its numbers stand, but they were earned on the snapshot /
search path. Formae's AWS plugin is Cloud Control end to end, and the
ConfigHub arm's bridge will be too. Comparing those against a chant arm that
never touches Cloud Control invites the objection that the boards measure
different transports, not different tools. A `chant-cc` arm closes it: same
estate, same questions, chant's reads on the applier's Cloud Control client.

## What the CC lane gives the arm (from the epic's close-out)
- Both read paths (thin `describeResources()` and `deep-observe`) on the
  applier's native Cloud Control transport (#1297).
- Drift: clean apply is quiet; an out-of-band SG rule surfaces named (#1207).
- The round-trip e2e on Floci already exists upstream: `just aws-cc-e2e`
  (apply → observe → mutate → reconcile → rollback). The arm is a benchmark
  packaging of a path chant already proves in CI.

## Build steps
1. **Pin a released chant** ≥ the CC lane (0.44.3 at time of writing; any
   release after 2026-08-02 carries #1335). Install from npm like the
   existing arm — the reader must be able to install what scored.
2. **Estate**: reuse the existing chant arm's estate source; topology is
   unchanged. What changes is the lifecycle the briefing sanctions.
3. **Briefing** (`../briefing-chant-cc.md`): the sanctioned reads are the CC
   observe/status surface (`chant lifecycle diff --live`, observe output) —
   not the folded snapshot search. That difference is the experiment.
4. **Emulator**: `ghcr.io/lex00/floci:awsbench-e0eb525` — same image as the
   other CC arms. The observe path is proven against it: deploy → `lifecycle
   snapshot --deep` → `diff --live` is quiet except one read-only-attribute
   classification (INTENTIUS/chant#1641). Getting there took three emulator
   fixes now in the image: IAM read models a drift engine can diff, intrinsic
   resolution in stored policy documents, and ManagedPolicy on the CC read
   side. Remaining caveats: no UpdateResource; ListResources covers 10 types.
5. **REPRODUCE.md** + runbook row. Keep `search-v2` published and untouched —
   two chant arms, two briefings, one estate, and the board says which is
   which, same as Alchemy v1/v2.

## Fairness note for the writeup
The point of this arm is symmetry: when the Formae and ConfigHub numbers land,
every store-or-controller tool on the board answers over Cloud Control, and
chant's snapshot-search numbers stay published as the separate experiment they
are. Do not fold the two chant arms into one row.
