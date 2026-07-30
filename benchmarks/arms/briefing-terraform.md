# Infrastructure source available

The AWS estate you are working with was provisioned from the Terraform
configuration mounted read-only at `/workspace/terraform`, already applied. It
is the deployed source of truth for this account's infrastructure: stacks,
regions, VPCs, subnets, instances, security groups, and their relationships are
all defined there, and the applied Terraform state records the resolved live ids
and attributes.

Inspect it to understand the infrastructure before or instead of enumerating it
call-by-call. The Terraform CLI is vendored in the workspace — run it as
`./terraform` from `/workspace/terraform`:

- `cd /workspace/terraform && ./terraform show -json` — the full applied state,
  every resource with its resolved attributes and references.
- `cd /workspace/terraform && ./terraform state list` — the resources under
  management.
- The `.tf` source under `/workspace/terraform` — for declared intent.

Use the AWS CLI to verify live state where the question depends on runtime values
(generated ids, IPs, states) the state does not already carry.
