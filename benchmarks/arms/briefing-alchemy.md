# Infrastructure source available

The AWS estate you are working with was provisioned from the Alchemy program
mounted read-only at `/workspace/alchemy`, already applied. It is the deployed
source of truth for this account's infrastructure: regions, VPCs, subnets,
instances, security groups, and their relationships are all defined there, and
the applied Alchemy state records the resolved live ids and attributes.

## Query the applied state with the `alchemy state` CLI

Alchemy ships a state inspector. Use it before enumerating the account
call-by-call — it reads the same applied state the deploy wrote, already keyed
by resource. Run from `/workspace/alchemy`:

- `alchemy state tree` — every stack/stage and the resources under it.
- `alchemy state list` — the fully-qualified name of every resource, one per
  line, suitable for scripting.
- `alchemy state get <fqn>` — the full record for one resource as JSON. `kind`
  is the resource type (e.g. `aws::Instance`, `aws::SecurityGroupRule`) and the
  `output` object holds the resolved live attributes: physical ids, IPs, and
  subnet and security-group references.

Fully-qualified names look like `alchemy-ec2-multiregion/bench/webServer`, so
`alchemy state list` followed by `alchemy state get` on the names you care about
walks the whole estate.

The same records are on disk under
`/workspace/alchemy/.alchemy/alchemy-ec2-multiregion/bench/*.json` if you would
rather read or grep the files directly, and `alchemy.run.ts` and `src/` under
`/workspace/alchemy` carry the declared intent behind them.

## Cloud state is authoritative

Alchemy's own guidance for agents is that applied state records what was
deployed and is not a substitute for the cloud: read current state from the
provider via describe/get APIs rather than trusting cached output attributes.
Use the AWS CLI to confirm anything that depends on runtime values, and where
the two disagree, the live API wins.
