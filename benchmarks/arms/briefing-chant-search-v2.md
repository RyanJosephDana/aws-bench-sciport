# Answer estate questions with `chant search` — it is the source of truth

This AWS estate was deployed from the chant project mounted read-only at
`/workspace/chant`, and the chant CLI is installed in it. chant holds a typed
model of the estate — resource kinds, attributes, tags and the edges between
resources — and joins it to live physical ids.

**Query the model rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the model
already holds the topology, and `--explain` reports the universe it matched
against, so you know the denominator.

Run from the project root:
`cd /workspace/chant && ./bin/chant search "<query>" --live --env floci [--explain] [--show attr1,attr2]`

Query grammar (space-separated terms, all must match):

- `kind:<substr>` — resource kind, e.g. `kind:EC2::Instance`
- `attr:<name>=<val>` — an attribute equals/contains a value
- `tag:<key>=<val>` — a tag with that key and value
- `->attr:n=v` / `->kind:X` — this resource has an edge TO one matching the
  right side; `<-` reverses it. This performs the join across the relationship,
  so `kind:EC2::Instance ->attr:MapPublicIpOnLaunch=true` selects instances by a
  property of their subnet.

Each result row is `<logicalId>  <kind>  <physicalId>  <shown attrs>`.
`--explain` adds a footer with the universe count ("4 of 6 Instances matched")
and, for each non-match, the term it failed.

## Derived attributes

Besides the attributes AWS returns directly, chant folds multi-hop topology onto
each instance and exposes the result as an attribute:

- `internetFacing` — whether the instance's subnet routes to an internet
  gateway, resolved through the route table, including a default VPC's main
  route-table association.
- `effectiveIngress` — ingress rules that reach the instance, resolved across
  both its directly attached security groups and any reached through its launch
  template. Values take the form `<proto>:<port>:<cidr>`.

## Path to estate facts, in order

1. `chant search "<query>" --live --env floci --explain` — the default, for
   every question. Add `->`/`<-` when the answer depends on a relationship.
2. The typed source under `/workspace/chant/*/src/` — for intent the grammar
   doesn't cover.
3. `aws ec2 …` — for runtime values the model does not carry.
