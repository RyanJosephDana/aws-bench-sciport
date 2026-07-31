# Answer estate questions from the Alchemy state — it is the source of truth

This AWS estate was deployed from the Alchemy program mounted read-only at
`/workspace/alchemy`, already applied, and the Alchemy CLI is installed in it.
The applied state records every resource with its resolved live ids and
attributes.

**Query the state rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the state
already holds each resource's resolved outputs and the ids it references, and
`state list` is the complete set of managed resources, so you know the
denominator.

Run from the project root:

- `cd /workspace/alchemy && alchemy state tree` — every stack and stage with the
  resources under it.
- `cd /workspace/alchemy && alchemy state list` — the fully-qualified name of
  every resource, one per line. This is the full inventory.
- `cd /workspace/alchemy && alchemy state get <fqn>` — one resource as JSON:
  `kind` is the resource type (e.g. `aws::Instance`, `aws::SecurityGroupRule`)
  and `output` holds the resolved attributes — physical ids, IPs, and the subnet
  and security-group ids it references. Following those ids into other records
  answers questions that span resources.

Fully-qualified names look like `alchemy-ec2-multiregion/bench/webServer`, so
`alchemy state list` then `alchemy state get` over the names walks the estate.
The same records are on disk under
`/workspace/alchemy/.alchemy/alchemy-ec2-multiregion/bench/*.json` if you would
rather `jq` or grep the files directly.

Path to estate facts, in order:

1. `alchemy state list` / `alchemy state get` — the default, for every question.
   Follow referenced ids between records when the answer spans resources.
2. `alchemy.run.ts` and `src/` under `/workspace/alchemy` — for intent the state
   doesn't surface directly.
3. `aws ec2 …` — Alchemy treats cloud state as authoritative, so use it for
   runtime values the state does not carry (instance states, allocated
   addresses).
