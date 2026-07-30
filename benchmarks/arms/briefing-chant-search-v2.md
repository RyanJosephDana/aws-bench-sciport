# Answer estate questions with `chant search` — it is the source of truth

This AWS estate was deployed from the chant project mounted read-only at
`/workspace/chant`, and the chant CLI is installed in it. chant knows the
declared topology — every subnet's public/private status, every security-group
rule, every instance's placement — and joins it to live physical ids.

**Use `chant search` FIRST for every estate question. Do not start with raw
`aws ec2` sweeps** — they return per-resource facts with no topology, so it is
easy to mislabel (e.g. calling a private-subnet or default-VPC instance
"public"). chant has already resolved the topology; trust its result.

Run from the project root:
`cd /workspace/chant && ./bin/chant search "<query>" --live --env floci --explain [--show attr1,attr2]`

Query grammar (space-separated terms, all must match):
- `kind:<substr>`        — resource kind, e.g. `kind:EC2::Instance`
- `attr:<name>=<val>`    — an attribute equals/contains a value
- `tag:<key>=<val>`      — a tag with that key and value
- `->attr:n=v` / `->kind:X` — this resource has an edge TO one matching the
  right side (`<-` reverses it). This does the topology JOIN for you:
  `kind:EC2::Instance ->attr:MapPublicIpOnLaunch=true` = instances in a PUBLIC
  subnet — no hand-joining instance→subnet, and no risk of miscounting.

**Derived reachability attributes (use these — a raw CLI sweep gets them wrong).**
chant folds multi-hop topology onto each instance so you don't hand-join it:
- `attr:internetFacing=true` — the instance's subnet routes to an Internet
  Gateway (this is what "public subnet" means — not `MapPublicIpOnLaunch`).
- `attr:effectiveIngress=<proto>:<port>:<cidr>` — an ingress rule reachable from
  the instance, resolved across BOTH its direct security groups AND any reached
  through its launch template. A CLI sweep misses the launch-template hop and
  under-counts.

"which instances are in a public subnet" is one query (internet-facing covers
managed subnets AND the account's default VPC):
`./bin/chant search "kind:EC2::Instance attr:internetFacing=true" --live --env floci --explain --show InstanceId`
`chant`'s `internetFacing` is COMPLETE — it resolves the route-table→IGW path
including the account's default VPC, whose main-route-table association
`describe-route-tables` makes hard to confirm. `--explain` names the route
table and IGW for each match. Do NOT re-derive this with `describe-route-tables`
and do NOT drop any instance chant marks internet-facing; that is the exact case
a manual CLI check gets wrong.

So "which instances are reachable via SSH from the internet" is one query:
`./bin/chant search "kind:EC2::Instance attr:internetFacing=true attr:effectiveIngress=tcp:22:0.0.0.0/0" --live --env floci --explain --show InstanceId`
Trust the result — do not re-derive it with `describe-instances`/`describe-security-groups`; those miss launch-template SGs and route-table joins.

**Always pass `--explain`.** It prints a footer with the universe count
("4 of 6 Instances matched") and, for each near-miss, WHY it was excluded
(which term it fails). Use it to confirm your answer is complete and correct:
if your count disagrees with the footer's universe, chant is right — the graph
knows the denominator a live sweep cannot.

Each result row is `<logicalId>  <kind>  <physicalId>  <shown attrs>`.

Order of tools:
1. `chant search "<query>" --live --env floci --explain` — the default, for
   every question. Add `->`/`<-` when the answer depends on a relationship
   (public/private, which SG, which VPC).
2. The typed source under `/workspace/chant/*/src/` — for intent the grammar
   doesn't cover (e.g. security-group rule details).
3. `aws ec2 …` — only for a runtime value no search surfaces, and never to
   re-derive a classification chant already gave you.
