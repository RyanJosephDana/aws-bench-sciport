# Answer estate questions from the Pulumi state — it is the source of truth

This AWS estate was deployed from the Pulumi program mounted read-only at
`/workspace/pulumi`, already applied. The exported state records every resource
with its resolved live ids, its inputs and outputs, and the dependency edges
between resources.

**Query the state rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the state
export already holds the graph, and it is the complete set of managed resources,
so you know the denominator.

A security group can reach an instance indirectly: a launch template can carry
security-group ids that the instance's own record never lists. Anything you
conclude about what reaches an instance has to account for both the groups
attached to it directly and any it picks up from a template it was launched
from.

Run from the project root:

- `cd /workspace/pulumi && ./pulumi-export` — the whole applied state as JSON.
  Each entry under `.deployment.resources[]` has:
  - `type` — the resource type, e.g. `aws:ec2/instance:Instance`
  - `urn` — its unique name
  - `inputs` — what was declared
  - `outputs` — the resolved attributes, including physical ids
  - `parent` and `dependencies` — the edges to other resources

  `jq` over `.deployment.resources[]` answers relationship questions without
  hand-joining CLI output — filter by `type`, then follow `dependencies` or an
  output id into the resources that reference it.

Path to estate facts, in order:

1. `./pulumi-export` piped through `jq` — the default, for every question. Use
   `dependencies`/`parent` and output ids when the answer spans resources.
2. The `index.ts` source under `/workspace/pulumi` — for intent and
   configuration the export doesn't surface directly.
3. `aws ec2 …` — for runtime values the state does not carry (instance states,
   allocated addresses).
