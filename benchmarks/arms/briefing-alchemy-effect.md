# Infrastructure source available

The AWS estate you are working with was provisioned from the Alchemy program
mounted read-only at `/workspace/alchemy`, already applied. It is the deployed
source of truth for this account's infrastructure: regions, VPCs, subnets,
instances, security groups, and their relationships are all defined there, and
the applied Alchemy state records the resolved live ids and attributes.

This estate is deployed as one stack per region, with an entrypoint each:
`us-east-1.run.ts`, `us-west-1.run.ts`, `us-west-2.run.ts`.

## Query the applied state with the `alchemy state` CLI

Alchemy ships a state inspector. Use it before enumerating the account
call-by-call — it reads the same applied state the deploy wrote, already keyed
by resource. Run from `/workspace/alchemy`, naming the entrypoint for the region
you are asking about, and add `--local` to read the on-disk store under
`.alchemy/state`:

- `alchemy state tree <entrypoint> --local` — every stack/stage and the
  resources under it.
- `alchemy state stacks <entrypoint> --local` and
  `alchemy state stages <entrypoint> --local` — the stacks and stages present.
- `alchemy state resources --stack <stack> --stage <stage> <entrypoint> --local`
  — the fully-qualified name of every resource in that stack and stage.
- `alchemy state get --stack <stack> --stage <stage> --fqn <fqn> <entrypoint> --local`
  — the full record for one resource, including its resolved live attributes.

Repeat per entrypoint to cover all three regions.

The same records are on disk under
`/workspace/alchemy/.alchemy/state/*/bench/*.json` if you would rather read or
grep the files directly — one stack directory per region, one JSON file per
resource, each with a `resourceType`, a `props` object holding the declared
configuration, and an `attr` object holding the resolved attributes. The
`*.run.ts` stacks and `src/` under `/workspace/alchemy` carry the declared
intent behind them.

## Cloud state is authoritative

Alchemy's own guidance for agents is that applied state records what was
deployed and is not a substitute for the cloud: read current state from the
provider via describe/get APIs rather than trusting cached output attributes.
Use the AWS CLI to confirm anything that depends on runtime values, and where
the two disagree, the live API wins.
