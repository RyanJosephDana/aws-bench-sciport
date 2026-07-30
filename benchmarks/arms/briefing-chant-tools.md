# Answer estate questions from the chant graph — it is the source of truth

This AWS estate was deployed from the chant project mounted read-only at
`/workspace/chant`, and the chant CLI is installed in it.

**Query the graph rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the graph
already holds the topology and the declared totals, so you know the denominator.

- `cd /workspace/chant && npx chant graph --format ir --live --env floci --overlay`
  — one JSON document holding every resource, its attributes, the cross-resource
  edges between them, and the live physical ids overlaid onto each node
  (instance ids and the like). `jq` over this answers relationship questions
  without hand-joining CLI output.

## Path to estate facts, in order

1. The `--overlay` graph above — the default, for every question. Follow the
   edges when the answer spans resources.
2. `/workspace/chant/graph.json`, the same graph as declared without live
   values, and the typed source under `/workspace/chant/*/src/` — for intent and
   configuration details.
3. `aws ec2 …` — for runtime values the overlay does not carry.
