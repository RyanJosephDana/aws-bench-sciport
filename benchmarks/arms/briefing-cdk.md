# Answer estate questions from the CDK app and its stacks — they are the source of truth

This AWS estate was deployed from the AWS CDK application mounted read-only at
`/workspace/cdk_app`, and the CDK CLI is installed in it. CDK's deployed state
is CloudFormation: the synthesized templates hold the complete declared shape,
and the CloudFormation API maps each logical id to the physical id it deployed
to.

**Query the templates and the stacks rather than enumerating the account
resource by resource.** A raw `aws ec2` sweep returns per-resource facts with no
relationships; a synthesized template holds every resource, its properties, and
its `Ref`/`Fn::GetAtt` references to other resources — including the resources
L2 constructs generate that the source never names, so it is the complete
inventory and tells you the denominator.

Run from the project root:

- `cd /workspace/cdk_app && npx cdk ls` — every stack the app defines.
- `cd /workspace/cdk_app && npx cdk synth <stack>` — the synthesized
  CloudFormation template: all resources with their properties, logical ids, and
  the `Ref`/`Fn::GetAtt` edges between them. `jq` over this answers relationship
  questions without hand-joining CLI output. Templates are also written to
  `cdk.out/`.
- `aws cloudformation describe-stack-resources --stack-name <stack> --region <region>`
  — the deployed logical id → physical id mapping for that stack.
- `aws cloudformation describe-stacks --stack-name <stack> --region <region>` —
  the stack's outputs and status.

Path to estate facts, in order:

1. `npx cdk synth` (or the templates in `cdk.out/`) for the declared shape and
   the relationships, joined to `describe-stack-resources` for the physical ids
   — the default, for every question. The app spans several stacks and regions;
   cover each.
2. `lib/`, `stacks/` and `environment.ts` under `/workspace/cdk_app` — for
   intent the template doesn't make obvious.
3. `aws ec2 …` — for runtime values the templates do not carry (instance states,
   allocated addresses).
