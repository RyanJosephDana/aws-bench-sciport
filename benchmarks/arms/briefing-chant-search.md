# Infrastructure source and search tooling available

This estate was deployed by the chant project mounted read-only at
`/workspace/chant` — it is the source of truth, and the chant CLI is
installed in it.

Do not read the whole graph to answer a question. Query it. `chant search`
answers estate questions with a few rows instead of a multi-thousand-token
dump — it knows the declared topology AND joins live physical ids.

Run searches from the project root (the `./bin/chant` launcher is self-contained — use it, not `npx`):
`cd /workspace/chant && ./bin/chant search "<query>" --live --env floci [--show attr1,attr2]`

Query grammar (space-separated terms, all must match):
- `kind:<substr>`        — resource kind, e.g. `kind:EC2::Instance`
- `attr:<name>=<val>`    — an attribute equals/contains a value
- `tag:<key>=<val>`      — a tag with that key and value
- `->kind:X` / `->attr:n=v` — this resource has an edge TO one matching the
  right side; `<-` is the reverse. This does the topology JOIN for you:
  `kind:Instance ->attr:MapPublicIpOnLaunch=true` = instances that sit in a
  public subnet — no hand-joining instance→subnet across many results.

Each result row is `<logicalId>  <kind>  <physicalId>  <shown attrs>`.
Use `--show` to surface specific attributes (e.g. `--show InstanceId`).

Fastest path to estate facts, in order:
1. `chant search` with a scoped query (above) — the default. One query per
   question; add `->`/`<-` when the answer depends on a relationship.
2. The typed source under `/workspace/chant/*/src/` — for intent and
   configuration the query grammar doesn't cover.
3. The AWS CLI — only for runtime values a search can't surface.

The project's declared totals tell you when a live sweep is incomplete —
a search over the declared topology already knows how many should exist.
