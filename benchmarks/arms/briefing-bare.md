# Answer estate questions from the AWS API

There is no infrastructure toolchain here — no state file, no synthesized
template, no recorded snapshot. The AWS CLI is installed and configured against
the account, and that is the whole surface.

**Every answer has to be assembled from API calls.** `describe-instances`,
`describe-security-groups`, `describe-subnets`, `describe-route-tables` and
friends each return one slice; a question that spans resources means calling
several and joining the results yourself.

A security group can reach an instance indirectly: a launch template can carry
security-group ids that the instance's own record never lists. Anything you
conclude about what reaches an instance has to account for both the groups
attached to it directly and any it picks up from a template it was launched
from.

The account spans **us-east-1**, **us-west-1** and **us-west-2**. Most EC2 calls
are regional, so a question about "all regions" means asking each one — pass
`--region` explicitly rather than relying on the default.

`--output json` piped through `jq` is usually easier to join than the table
output. `--query` filters server-side if you would rather narrow before it
reaches you.

Path to estate facts, in order:

1. `aws ec2 …`, `aws iam …` — the default, for every question. Join across calls
   when the answer spans resources.
2. `aws cloudformation describe-stack-resources` / `describe-stacks` — if the
   estate was deployed from a stack, this maps logical ids to physical ones.
