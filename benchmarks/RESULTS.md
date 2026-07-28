# aws-bench results log

Tracking chant vs CDK on aws-bench, run locally on the Floci emulator (zero AWS cost).
Fixed control: **Haiku 4.5**. The variable is the tooling. Primary comparator: **CDK**.
Append new runs at the top of the Run log. Keep the numbers honest — note exclusions and noise.

## Headline (2026-07-28)

On the fair valid set, the smallest model on chant **beats CDK on accuracy and costs ~30% less**.

| arm | valid | ssh-reach | input tok | output tok | $ |
|---|---|---|---|---|---|
| Haiku + CDK | 9/12 | 0/3 | 7.88M | 82k | 1.87 |
| Haiku + chant-search (v3, effective-SG) | **11/12** | **3/3** | 5.87M | 51k | 1.28 |

The entire accuracy delta is the ssh-reach swing (0/3 → 3/3). chant's one miss is a single
`list-all-regions` trial (a task everyone saturates) — adoption noise, not the feature.

## Scenario: ec2-multiregion (quickstart, 6 EC2 instances / 3 regions)

### Fair valid set — 4 tasks × k=3 = 12
- `describe-ec-instances-cross-region-connectivity`
- `list-ec-instances-all-regions`
- `list-ec-instances-by-vpc-across-regions`
- `list-ec-instances-all-regions-1` (**ssh-reachability**) — the only discriminating task

### Excluded (see EXCLUSIONS.md), with reasons
- `describe-cloudformation-stack-resources` — CDK-shaped ground truth counts L2 cruft; measures the IaC generator, not the agent (0/3 for every arm).
- `find-ec-instances-in-public-subnets` — **estate confound**: our chant port over-built the west stacks as internet-facing (IGW + route + public subnet), so the estate has more public-subnet instances than the CDK ground truth. Not a fair comparison until the estate matches CDK's west topology.
- `list-ec-private-ips-all-regions` — Floci reported the Docker bridge IP as PrivateIpAddress (#1983). Now un-excludable by setting `ec2.aws-faithful-private-ip=true` (toggle shipped) + redeploy.
- `ssh-reachability`-adjacent notes: valid on the #1984 image (public IP gated on subnet); before that every instance reported 127.0.0.1.

## Run log

### 2026-07-28 — v3 effective-SG enrichment (the win)
Full table (valid = fair set /12):

| arm | pass | valid | in tok | out tok | $ |
|---|---|---|---|---|---|
| Sonnet bare | 16 | 9/12 | 2.66M | 39k | 2.02 |
| Haiku bare | 12 | 9/12 | 2.85M | 44k | 0.81 |
| Haiku+CDK | 14 | 9/12 | 7.88M | 82k | 1.87 |
| Haiku+chant-map | 12 | 9/12 | 6.67M | 59k | 2.12 |
| Haiku+chant-tools (overlay dump) | 10 | 7/12 | 11.16M | 82k | 2.71 |
| chant-search unfaithful Floci | 11 | 9/12 | 4.90M | 50k | 1.07 |
| chant-search #2014 (faithful subnet) | 13 | 9/12 | 5.11M | 53k | 1.07 |
| chant-search +#1984 (public IP) | 13 | 8/12 | 5.84M | 56k | 1.19 |
| chant-search v2 (chant-first briefing + --explain) | 12 | 8/12 | 6.09M | 57k | 1.30 |
| **chant-search v3 (effective-SG enrichment)** | **13** | **11/12** | 5.87M | 51k | 1.28 |

ssh-reach per arm (k=3): CDK 0/3 · unfaithful 0/3 · #2014 0/3 · #1984 0/3 · v2 0/3 · **v3 3/3**.

v3 ssh-reach trials: agent ran `chant search "kind:EC2::Instance attr:internetFacing=true attr:effectiveIngress=tcp:22:0.0.0.0/0"`, zero AWS-CLI fallback, correct 2 instances including the launch-template one every CLI sweep drops.

## Findings

1. **Efficiency is the robust, repeatable win.** chant-search uses ~5–6M input tokens vs CDK's 7.9M (~30% less) and the lowest output of any arm. It scales with estate size: CLI cost grows with the graph, one scoped query stays flat.
2. **Adoption follows capability, not prompting.** v2 (firmer briefing + --explain) did **not** lift accuracy — the gap was query expressiveness. Once the tool could answer ssh-reach (v3), the agent used it every trial with no CLI fallback. Don't tune prompts to force a tool that can't answer the question.
3. **The discriminating tasks are multi-hop joins.** find-public-subnets = subnet→route→IGW; ssh-reach = that AND (direct SG OR launch-template SG). CLI agents botch these (over- or under-count). chant wins them by folding the join into one queryable predicate — `effectiveIngress` + `internetFacing` on the instance node (chant `enrichEffectiveTopology`).
4. **Small benchmark tier caveat.** 3 of 4 fair tasks saturate for everyone; one `list-all-regions` trial flips on noise. The signal is real but thin here — real differentiation needs harder scenarios where multi-hop joins are the norm.

## Feature/emulator changes behind these runs
- chant: `chant search` (#1139), `--explain` footer, multi-stack live identity (#1162), `enrichEffectiveTopology` (effective SGs + internet-facing).
- Floci (fork integration image): #2014 subnet MapPublicIpOnLaunch, #1984 public-IP gated on subnet, #1667 cidr-block filter (upstream #1864), #1983 `ec2.aws-faithful-private-ip` toggle.

## Next
- Apply enrichment on a harder scenario (compute-and-data / serverless) where joins dominate and the cost gap should widen.
- Optional: flip `ec2.aws-faithful-private-ip=true` to reclaim `list-ec-private-ips` as a 5th valid task; rebuild west stacks to match CDK to reclaim find-public-subnets.
