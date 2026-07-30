# Answer estate questions with `chant search` — it is the source of truth

This AWS estate was deployed from the chant project mounted read-only at
`/workspace/chant`, and the chant CLI is installed in it. chant holds a typed
model of the estate — resource kinds, attributes, tags and the edges between
resources — and joins it to live physical ids.

**Query the model rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships and a
multi-thousand-token dump; a scoped query returns a few rows, and the model
already holds the declared totals, so you know the denominator.

Run from the project root (the `./bin/chant` launcher is self-contained — use
it, not `npx`):
`cd /workspace/chant && ./bin/chant search "<query>" --live --env floci [--show attr1,attr2]`

Query grammar (space-separated terms, all must match):

- `kind:<substr>` — resource kind, e.g. `kind:EC2::Instance`
- `attr:<name>=<val>` — an attribute equals/contains a value
- `tag:<key>=<val>` — a tag with that key and value
- `->kind:X` / `->attr:n=v` — this resource has an edge TO one matching the
  right side; `<-` reverses it. This performs the join across the relationship,
  so `kind:Instance ->attr:MapPublicIpOnLaunch=true` selects instances by a
  property of their subnet.

Each result row is `<logicalId>  <kind>  <physicalId>  <shown attrs>`. `--show`
surfaces specific attributes (e.g. `--show InstanceId`).

## Path to estate facts, in order

1. `chant search "<query>" --live --env floci` — the default, for every
   question. Add `->`/`<-` when the answer depends on a relationship.
2. `/workspace/chant/graph.json`, the prebuilt declared-only graph, and the
   typed source under `/workspace/chant/*/src/` — for intent and configuration
   the grammar doesn't cover.
3. `aws ec2 …` — for runtime values the model does not carry.
