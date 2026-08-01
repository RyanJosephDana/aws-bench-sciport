# Answer estate questions from Terraform state — it is the source of truth

This AWS estate was deployed from the Terraform configuration mounted read-only
at `/workspace/terraform`, already applied, and the Terraform CLI is vendored in
the workspace. The applied state records every managed resource with its
resolved live ids, its attributes, and the references between resources.

**Query the state rather than enumerating the account resource by resource.** A
raw `aws ec2` sweep returns per-resource facts with no relationships; the state
already holds how resources reference one another, and `state list` gives you
the complete set under management, so you know the denominator.

A security group can reach an instance indirectly: a launch template can carry
security-group ids that the instance's own record never lists. Anything you
conclude about what reaches an instance has to account for both the groups
attached to it directly and any it picks up from a template it was launched
from.

Run from the project root (use the vendored binary, `./terraform`):

- `cd /workspace/terraform && ./terraform state list` — every resource address
  under management, one per line. This is the full inventory.
- `cd /workspace/terraform && ./terraform state show <address>` — one resource
  with all of its resolved attributes.
- `cd /workspace/terraform && ./terraform show -json` — the whole applied state
  as JSON. Resources live under `.values.root_module` (recurse
  `child_modules`); each has `type`, `address`, and a `values` object with the
  resolved attributes. `jq` over this answers relationship questions without
  hand-joining CLI output.
- `cd /workspace/terraform && ./terraform output -json` — the declared outputs.

Path to estate facts, in order:

1. `./terraform show -json` or `state show` — the default, for every question.
   Follow attribute references (subnet ids, security-group ids, launch-template
   ids) between resources to answer questions that span them.
2. The `.tf` source under `/workspace/terraform` — for intent and configuration
   the state doesn't surface directly.
3. `aws ec2 …` — for runtime values the state does not carry (instance states,
   allocated addresses).
