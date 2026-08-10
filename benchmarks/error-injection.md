# Error-injection cost overlay

Scenario family measuring the cost of late failure: each task stages a change
containing an injected error that stock CDK cannot catch pre-synth (it fails at
CloudFormation deploy or at runtime) while chant catches it at build — or makes
it unrepresentable. Metric: tokens + wall-clock to green, per arm.

## Injection catalog (buildable order)

| # | Injection | CDK arm experience | chant arm experience |
|---|---|---|---|
| 1 | GetAtt on nonexistent attribute | synth clean; deploy fails downstream of the cause (proven on Floci — unresolved literal propagates into a consuming resource) | `cf-refs` build diagnostic naming resource + attribute |
| 2 | Cross-stack export/import typo | deploy: "export not found" | unrepresentable — typed refs cannot point at nothing |
| 3 | Duplicate export / name collision | deploy collision | intra-template uniqueness rule (chant#1140) |
| 4 | Policy-violating change | no stock gate; verifier or worse | WAW audit rule at build, with fix hint |
| 5 | Subnet CIDR outside VPC CIDR | deploy failure | containment rule (chant#1140) + Floci CreateSubnet validation for fidelity |
| 6 | Deny-policy contradiction | deploy succeeds; runtime 403; expensive live diagnosis | IAM-reasoning rule (chant#1140): deny intersects a required action |

## CDK-tuned arm

Per the with/without rule, CDK is also measured with the ecosystem's pre-synth
toolchain in the workspace (cdk-nag, cfn-lint, cfn-guard) and a briefing that
names them. This changes the expected outcomes honestly:

- Injections 1 and 3 become catchable pre-deploy IF the agent assembles and
  runs the pipeline (synth, then cfn-lint on the output) — the measurement then
  captures the token cost of that discipline vs chant's single build command.
- Injection 2 (typed-ref unrepresentability) and 6 (deny-policy contradiction)
  remain uncatchable by any CFN linter — template-level tools cannot see intent
  or reason across IAM semantics.
- Injection 4 becomes a cdk-nag-vs-WAW comparison: both catch it; cost and
  fix-hint quality differ.

The tuned arm's own overhead is a first-class measurement, not a confound: if
the pipeline is expensive (orchestration turns, tool wall-clock, and reading
verbose linter output), the accuracy it buys comes at a token price chant does
not pay. Report per arm: tokens spent invoking + reading analysis tooling, and
tokens-per-actionable-finding (diagnostic signal-to-noise). Expected shape:
stock CDK is cheap upfront and pays at deploy; tuned CDK pays upfront in
orchestration and noise-filtering; chant pays neither.

## Dependencies

- chant-deployed estate (tasks are mutation-style; both arms attempt the same
  staged change)
- INTENTIUS/chant#1139 (composites), INTENTIUS/chant#1140 (new static rules)
- Floci CreateSubnet CIDR validation (fidelity PR, for injection 5)
