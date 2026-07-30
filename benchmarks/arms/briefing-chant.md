# Answer estate questions from the chant graph — it is the source of truth

This AWS estate was deployed from the chant project mounted read-only at
`/workspace/chant`. It holds typed declarations of every stack, region, VPC,
subnet, instance, security group, and their relationships.

**Read the graph rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the graph
already holds the topology and the declared totals, so you know the denominator.

- `/workspace/chant/graph.json` — the project's prebuilt resource graph: every
  node, its attributes, and the cross-resource edges between them, as declared.
  `jq` over this answers relationship questions without hand-joining CLI output.
  It carries the declared topology but not runtime-generated values.

## Path to estate facts, in order

1. `graph.json` for the declared topology and the relationships between
   resources — the default, for every question.
2. The typed source under `/workspace/chant/*/src/` — for intent and
   configuration the graph doesn't surface directly.
3. `aws ec2 …` — for the runtime-generated values that only exist live
   (instance ids, allocated IPs, instance states), reconciled against the
   graph's declared set.
