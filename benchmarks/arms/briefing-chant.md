# Infrastructure source available

The AWS estate you are working with was provisioned from the chant project
mounted read-only at `/workspace/chant`. It is the deployed source of truth
for this account's infrastructure: typed declarations of every stack, region,
VPC, subnet, instance, security group, and their relationships.

Read it to understand the infrastructure before or instead of enumerating it
call-by-call. Use the AWS CLI to verify live state where the question depends
on runtime values (generated ids, IPs, states).
