# Infrastructure source available

The AWS estate you are working with was provisioned from the Alchemy program
mounted read-only at `/workspace/alchemy`, already applied. It is the deployed
source of truth for this account's infrastructure: regions, VPCs, subnets,
instances, security groups, and their relationships are all defined there, and
the applied Alchemy state records the resolved live ids and attributes.

Inspect it to understand the infrastructure before or instead of enumerating it
call-by-call:

- `/workspace/alchemy/.alchemy/alchemy-ec2-multiregion/bench/*.json` — the full
  applied state, one JSON file per resource. Each file has a `kind` (e.g.
  `aws::Ec2Instance`, `aws::SecurityGroupRule`) and an `output` object with the
  resource's resolved attributes and references.
- `alchemy.run.ts` and `src/` under `/workspace/alchemy` — for declared intent.

Use the AWS CLI to verify live state where the question depends on runtime
values (generated ids, IPs, states) the state does not already carry.
