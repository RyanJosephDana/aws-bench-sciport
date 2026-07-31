# Answer estate questions with `chant search` — the recorded state is the source of truth

This AWS estate was deployed from the chant project mounted at
`/workspace/chant`, and the chant CLI is installed in it. A state snapshot was
recorded at deploy time: it holds every managed resource with its resolved
physical id, the resources the estate depends on but does not declare, and the
edges between them. chant folds that graph into typed answers.

**Query the recorded state rather than enumerating the account resource by
resource.** A raw `aws ec2` sweep returns per-resource facts with no
relationships; the snapshot already holds the topology, and `--explain` reports
the universe it matched against, so you know the denominator.

Run from the project root. Three read commands, each answering a different
shape of question:

**`chant lifecycle show floci`** — the complete recorded inventory: every
managed resource with its logical name, type, physical id and status, plus the
resources the estate depends on. This is the census, so you know the
denominator before you filter.

**`chant search "<query>" --at latest --env floci [--explain] [--show a,b]`** —
filter and join over that inventory. The main tool for any question narrower
than "list everything".

**`chant graph --format ir --live --env floci`** — the whole graph as JSON on
stdout. `nodes` carry `id`, `kind`, `physicalId` and `attrs`; `edges` carry
`from`, `to` and `viaAttr` (the attribute the reference travels through). For a
question about how resources relate rather than about one resource's properties.

Warnings go to stderr, so stdout is already valid JSON — redirect with
`2>/dev/null`, not `2>&1`, or the warnings land in the JSON and break the parse.
`graph` reads the estate now; it has no `--at`.

Add `--ambient` to any `search` to include resources of a kind this estate
manages that exist in the account without being declared or referenced — a
default security group, something left behind. They are marked distinctly, and
without the flag they are not reported at all.

Every answer states what backed it — `— observed from snapshot <commit> taken
<time> · bound N/M` — so you can see the estate has already been read, and how
completely, without re-reading it yourself.

Values match exactly or by substring — there is no wildcard, so `attr:x=*foo`
matches nothing. When a query returns no matches, the footer names the
attributes the queried kind carries, and for an attribute you did query it lists
the values actually present. A miss is worth reading rather than working around.

Query grammar (space-separated terms, all must match):

- `kind:<substr>` — resource kind, e.g. `kind:EC2::Instance`
- `attr:<name>=<val>` — an attribute equals/contains a value
- `tag:<key>=<val>` — a tag with that key and value
- `!<term>` — prefix any term to require its ABSENCE. `!<-kind:X` selects
  nodes nothing of kind X references, which is how you ask what is unreferenced.
- `->attr:n=v` / `->kind:X` — this resource has an edge TO one matching the
  right side; `<-` reverses it. This performs the join across the relationship,
  so `kind:EC2::Instance ->attr:MapPublicIpOnLaunch=true` selects instances by a
  property of their subnet.

Terms compose, and the flags compose with them — asking what is unreferenced is
usually also asking about things nothing declared, so those two go together:

    chant search "kind:EC2::Subnet !<-kind:EC2::Instance" --at latest --ambient
    chant search "kind:EC2::Instance" --at latest --show VpcId,PrivateIpAddress

Each result row is `<logicalId>  <kind>  <physicalId>  <shown attrs>`. `--show`
takes the resource's own property names as the account reports them.
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

1. `chant search "<query>" --at latest --env floci --explain` — the default, for
   every question. Add `->`/`<-` when the answer depends on a relationship.
2. `chant search "<query>" --live --env floci` — the same query against the
   estate as it is right now, for a value that may have changed since the
   snapshot was taken.
3. `chant lifecycle show floci` — when the question is "list all of my X" and a
   census answers it more directly than a filter.
4. `chant graph --format ir --live --env floci` — when the question is about
   relationships between resources.
5. The typed source under `/workspace/chant/*/src/` — for intent the grammar
   doesn't cover.
6. `aws ec2 …` — for runtime values none of the above carries.
