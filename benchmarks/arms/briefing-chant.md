# Infrastructure source available

The AWS estate you are working with was provisioned from the chant project
mounted read-only at `/workspace/chant`. It is the deployed source of truth
for this account's infrastructure: typed declarations of every stack, region,
VPC, subnet, instance, security group, and their relationships.
`/workspace/chant/graph.json` is the project's resource graph — every node,
attribute, and cross-resource edge — prebuilt for reading.

Work the way chant users do: the project tells you what exists BY DESIGN;
the AWS API tells you what exists LIVE. Answer questions by reconciling the
two — enumerate live state with the AWS CLI, then check it against the
project (counts, placements, and relationships must match; the project's
totals tell you when your live sweep is incomplete). Runtime-generated
values (instance ids, allocated IPs, states) only exist live.
