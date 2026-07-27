# Infrastructure source and tooling available

This estate was deployed by the chant project mounted read-only at
`/workspace/chant` — it is the source of truth, and the chant CLI is
installed in it.

Fastest path to estate facts, in order:

1. One command reconciles what's declared with what's live:
   `cd /workspace/chant && npx chant graph --format ir --live --env floci --overlay`
   Every resource, its attributes, cross-resource edges, and live physical
   ids (instance ids, etc.) in one JSON document. Prefer extracting what you
   need from it (jq or targeted reads) over re-running AWS CLI sweeps.
2. `/workspace/chant/graph.json` — the prebuilt declared-only graph, when
   live values don't matter.
3. The typed source under `/workspace/chant/*/src/` — for intent and
   configuration details.
4. The AWS CLI — only for runtime values the overlay doesn't carry.

The project's declared totals (instances per region, VPCs, security groups)
tell you when a live enumeration is incomplete — reconcile counts before
answering.
