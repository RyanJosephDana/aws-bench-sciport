# Infrastructure source available

The AWS estate you are working with was provisioned from the AWS CDK
application mounted read-only at `/workspace/cdk_app`. It is the deployed
source of truth for this account's infrastructure: stacks, regions, VPCs,
subnets, instances, security groups, and their relationships are all defined
there.

Read it to understand the infrastructure before or instead of enumerating it
call-by-call. Use the AWS CLI to verify live state where the question depends
on runtime values (generated ids, IPs, states).
