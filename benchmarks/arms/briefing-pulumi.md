# Infrastructure source available

The AWS estate you are working with was provisioned from the Pulumi program
mounted read-only at `/workspace/pulumi`, already applied. It is the deployed
source of truth for this account's infrastructure: stacks, regions, VPCs,
subnets, instances, security groups, and their relationships are all defined
there, and the applied Pulumi state records the resolved live ids and attributes.

Inspect it to understand the infrastructure before or instead of enumerating it
call-by-call:

- `cd /workspace/pulumi && ./pulumi-export` — the full applied state as JSON. Each
  entry under `.deployment.resources[]` has a `type` (e.g. `aws:ec2/instance:Instance`)
  and an `outputs` object with the resource's resolved attributes and references.
- The `index.ts` source under `/workspace/pulumi` — for declared intent.

Use the AWS CLI to verify live state where the question depends on runtime values
(generated ids, IPs, states) the state does not already carry.
