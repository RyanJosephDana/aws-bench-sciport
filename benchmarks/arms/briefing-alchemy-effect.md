# Answer estate questions from the Alchemy state — it is the source of truth

This AWS estate was deployed from the Alchemy program mounted read-only at
`/workspace/alchemy`, already applied, and the Alchemy CLI is installed in it.
The applied state records every resource with its resolved live ids and
attributes. This estate is deployed as one stack per region, with an entrypoint
each: `us-east-1.run.ts`, `us-west-1.run.ts`, `us-west-2.run.ts`.

**Query the state rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the state
already holds each resource's resolved attributes and the ids it references, and
`state export` returns every record in the store as one JSON document, so you
know the denominator from a single call.

A security group can reach an instance indirectly: a launch template can carry
security-group ids that the instance's own record never lists. Anything you
conclude about what reaches an instance has to account for both the groups
attached to it directly and any it picks up from a template it was launched
from.

Run from the project root with `--local`, which reads the on-disk store under
`.alchemy/state`. That store holds all three regions, and one entrypoint reaches
every stack in it — `--stack` is what selects the region, not the entrypoint. Use
`us-west-1.run.ts` as the handle throughout:

- `alchemy state export us-west-1.run.ts --local` — **every resource in every
  stack as one JSON document**: a flat `resources` array of
  `{stack, stage, fqn, state}`, where `state` is the same record `state get`
  prints. One call covers all three regions; filter it with `jq`. This answers
  most questions by itself.
- `alchemy state export --stack <stack> us-west-1.run.ts --local` — the same,
  narrowed to one region's stack.
- `alchemy state tree us-west-1.run.ts --local` — every stack and stage with the
  resources under it, when you want the census without the records.
- `alchemy state stacks us-west-1.run.ts --local` and
  `alchemy state stages us-west-1.run.ts --local` — the stacks and stages present.
- `alchemy state get --stack <stack> --stage <stage> --fqn <fqn> us-west-1.run.ts --local`
  — one resource with its resolved attributes, when you already know its name.

`alchemy state stacks` lists all three region stacks whichever entrypoint you
name, so one command per question covers the estate. The same records are on disk
under `/workspace/alchemy/.alchemy/state/*/bench/*.json` — one stack directory
per region, one JSON file per resource, each with a `resourceType`, a `props`
object holding the declared configuration and an `attr` object holding the
resolved attributes — if you would rather `jq` or grep the files directly.

Path to estate facts, in order:

1. `alchemy state export … --local` piped through `jq` — the default, for every
   question. The whole estate is in one document, so relationship questions are
   a join over the array rather than a walk between commands.
2. The `*.run.ts` stacks and `src/` under `/workspace/alchemy` — for intent the
   state doesn't surface directly.
3. `aws ec2 …` — Alchemy treats cloud state as authoritative, so use it for
   runtime values the state does not carry (instance states, allocated
   addresses).
