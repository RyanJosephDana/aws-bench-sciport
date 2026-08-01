# Answer estate questions from the Alchemy state — it is the source of truth

This AWS estate was deployed from the Alchemy program mounted read-only at
`/workspace/alchemy`, already applied, and the Alchemy CLI is installed in it.
The applied state records every resource with its resolved live ids and
attributes. This estate is deployed as one stack per region, with an entrypoint
each: `us-east-1.run.ts`, `us-west-1.run.ts`, `us-west-2.run.ts`.

**Query the state rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the state
already holds each resource's resolved attributes and the ids it references, and
`state resources` is the complete set per stack, so you know the denominator.

A security group can reach an instance indirectly: a launch template can carry
security-group ids that the instance's own record never lists. Anything you
conclude about what reaches an instance has to account for both the groups
attached to it directly and any it picks up from a template it was launched
from.

Run from the project root, naming the entrypoint for the region you are asking
about and adding `--local` to read the on-disk store under `.alchemy/state`:

- `alchemy state tree <entrypoint> --local` — every stack and stage with the
  resources under it.
- `alchemy state stacks <entrypoint> --local` and
  `alchemy state stages <entrypoint> --local` — the stacks and stages present.
- `alchemy state resources --stack <stack> --stage <stage> <entrypoint> --local`
  — the fully-qualified name of every resource there. This is the full
  inventory for that stack.
- `alchemy state get --stack <stack> --stage <stage> --fqn <fqn> <entrypoint> --local`
  — one resource with its resolved attributes, including physical ids and the
  subnet and security-group ids it references.

Repeat per entrypoint to cover all three regions. The same records are on disk
under `/workspace/alchemy/.alchemy/state/*/bench/*.json` — one stack directory
per region, one JSON file per resource, each with a `resourceType`, a `props`
object holding the declared configuration and an `attr` object holding the
resolved attributes — if you would rather `jq` or grep the files directly.

Path to estate facts, in order:

1. `alchemy state resources` / `alchemy state get` per entrypoint — the default,
   for every question. Follow referenced ids between records when the answer
   spans resources.
2. The `*.run.ts` stacks and `src/` under `/workspace/alchemy` — for intent the
   state doesn't surface directly.
3. `aws ec2 …` — Alchemy treats cloud state as authoritative, so use it for
   runtime values the state does not carry (instance states, allocated
   addresses).
