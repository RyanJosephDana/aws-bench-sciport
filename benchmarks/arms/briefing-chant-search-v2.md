# Infrastructure source available

This AWS estate was deployed from the chant project mounted read-only at
`/workspace/chant`, and the chant CLI is installed in it. chant holds a typed
model of the estate — resource kinds, attributes, tags and the edges between
resources — and joins it to live physical ids.

## Query the model with `chant search`

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
`--explain` adds a footer with the universe count and, for each non-match, the
term it failed.

## Derived attributes

Besides the attributes AWS returns directly, chant folds multi-hop topology onto
each instance and exposes the result as an attribute:

- `internetFacing` — whether the instance's subnet routes to an internet
  gateway, resolved through the route table including a default VPC's main
  route-table association.
- `effectiveIngress` — ingress rules that reach the instance, resolved across
  both its directly attached security groups and any reached through its launch
  template. Values take the form `<proto>:<port>:<cidr>`.

## Other sources

The typed source under `/workspace/chant/*/src/` carries declared intent the
grammar doesn't cover. Use the AWS CLI for runtime values the model does not
already hold.
