# Questions aws-bench does not ask

Two introspection questions of a shape the upstream set contains exactly one of.
They are not aws-bench's. They are ours, and anything published from them has to
say so.

## Why these two

`list-unused-security-groups-all-regions` is the most interesting result on the
board and the least representative. Across every valid run:

| arm | gets it |
|---|--:|
| chant | 51% |
| No tool (AWS CLI) | 28% |
| Alchemy | 5% |
| Pulumi, Terraform, AWS CDK | **0 of 66** |

Every arm that keeps a state file is at zero, and an agent with no
infrastructure tooling beats all three of them. The explanation is structural
rather than incidental: the answer is a negative about things a state file does
not contain. Four groups are attached to nothing, so nothing the tool created
references them, and reading your own state cannot find what nothing points at.

That is one question out of eight. One question is an anecdote, and the whole
result currently rests on it — so the question worth asking is whether the
finding is about the *shape* or about that one question.

## The two

| task | the negative | today's answer |
|---|---|--:|
| `subnets-with-no-network-interfaces` | subnets nothing occupies | 8 of 13 |
| `vpcs-with-no-running-instances` | VPCs running nothing | 2 of 6 |

Both share the property that makes the security-group question hard. The
account's default VPCs and their subnets were created by no deployment, so an
arm reading only its own state sees a subset and cannot know what it is missing.

**Neither is hard for an agent that reads the account.** The first run of these
put the no-tool baseline at 6 of 6. That is worth stating plainly, because it
makes these weaker than the security-group question they are modelled on, where
even account-reading agents manage only 28% — there you must cross-reference
every network interface, and here a sweep of two API calls is enough.

So these do not test the same difficulty. What they test is the same
*structure*: all eight empty subnets are in the account's default VPCs, which no
deployment created, so an arm reading only the state it wrote cannot find any of
them. Whether the state-file arms still answer 0 the way they do on security
groups is the open question, and it is the only reason to run these.

Nor are they a gimme for chant, which reads a recorded snapshot rather than the
live account and has to have swept the right things when the snapshot was
taken.

## First results

| arm | score | account reads | how it answered |
|---|--:|--:|---|
| No tool (AWS CLI) | **6/6** | 16 | swept the account |
| Terraform | **0/6** | 1 | read `terraform.tfstate` |
| chant | **0/6** | 0 | read its recorded snapshot |

k=3, one run each, on an estate holding 13 subnets (8 empty) and 6 VPCs (2 empty).
All three runs passed the audit; every arm used its own tooling throughout.

**chant failed these too, and the diagnosis splits in two.**

*The subnet half was a real gap.* Its snapshot held five of the estate's seven
subnets, and the two it dropped were the two with nothing in them. chant's
ambient observer (chant #1278) enumerates kinds the project manages that exist
without being declared — the mechanism built for exactly this question — but its
`ENUMERABLE` table listed security groups, VPCs and network interfaces and *not*
subnets. So a subnet was recorded only when something in it was recorded, which
is backwards for a question about subnets holding nothing. Fixed in chant by
adding the table entry.

*The VPC half is an emulator artifact, and not chant's.* Floci assigns literally
identical ids across regions — `vpc-default` and `subnet-default-a/b/c` exist
under those exact names in all three. Real AWS never does this; VPC and subnet
ids are globally unique. Ambient observation keys results by physical id, so the
three distinct default VPCs collapse to one entry and the survivor is the
occupied us-east-1 one. chant's VPC enumeration is correct and recorded every
unique id the account exposes.

That makes the VPC question ill-posed on this emulator rather than hard: the
ground truth counts per region, and no id-keyed store can reconstruct that while
the ids collide. It needs either region-qualified identity in the question or
unique ids in floci, and the second is the better fix — id reuse across regions
will quietly distort anything that counts resources account-wide.

Terraform's failure is neither of these and stands unchanged: it does not record
what it did not create, and said so itself.

## The conditions these were written under

**Written before any arm ran them.** The estate was queried to check the answers
are non-trivial — a question whose answer is "none" measures nothing — and then
the tasks were written. Nothing was tuned to a result, because there were no
results.

**Five other candidates were discarded**, because the estate does not support
them: unattached network interfaces (0), route tables with no association (0),
security groups referenced only by other groups (0), unattached volumes (0), and
security groups with no ingress rules (7 of 8, which is "almost all of them" and
so discriminates nothing). Recorded here because the ones that survived look
cherry-picked without the ones that did not.

**Published either way.** A question set added by the author of one of the tools
is worth nothing unless the arm that author builds can lose on it. chant's score
on these goes on the site whatever it is.

**One defect found by running them before publishing them.** The first version
of the subnet reference answer claimed the empty subnets included "unused
subnets in the deployed VPCs". They do not — all eight are in default VPCs. A
trial answered "8 — us-east-1: 2, us-west-1: 3, us-west-2: 3", which is exactly
right, and the judge failed it for contradicting the reference on infrastructure
state. Both references now assert only what `pre_invoke` computes. A question
that fails correct answers is worse than no question, and this one shipped that
way for exactly one run.

**Scored separately.** The eight-question board is over eight questions. Folding
two more in would silently change every arm's denominator and make the new
numbers incomparable with the published ones. These get their own set.

## Ground truth

Computed live by each task's `pre_invoke.py`, sweeping the account at run time
and substituting the result into the reference answer. The estate is redeployed
before every run with fresh resource ids, so a written-down count would track
the scenario only until someone edited it. Verified against the deployed estate:
8 empty subnets (us-east-1: 2, us-west-1: 3, us-west-2: 3) and 2 empty VPCs of 6.

## Running them

```sh
./benchmarks/agent-env/run-arm.sh chant          # deploy the estate, score the eight
./benchmarks/agent-env/run-negatives.sh chant    # score these two on the same estate
```

`run-negatives.sh` neither wipes nor deploys — it scores whatever is up, so the
estate has to be the one that arm deployed. The estate gate runs first for that
reason.
