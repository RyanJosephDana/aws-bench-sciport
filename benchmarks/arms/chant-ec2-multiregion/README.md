# ec2-multiregion estate — chant project

This chant project is the deployed source of truth for this AWS account's
infrastructure. One module per region:

| Module | Region | Contents |
|---|---|---|
| `src/us-east-1.ts` | us-east-1 | Custom VPC (public + isolated private subnet), 4 instances (public web server, launch-template instance, private instance, default-VPC instance), open web security group, deliberately unused security group, IAM role/profile with deny policy, launch template, baked AMI |
| `src/us-west-1.ts` | us-west-1 | VPC with one public subnet, 1 public instance |
| `src/us-west-2.ts` | us-west-2 | VPC with one public subnet, 1 public instance |

The account operates ONLY in these three regions. Total estate: 6 EC2
instances across 4 VPCs (3 custom, plus the default VPC in us-east-1 that
hosts one instance).

Every resource is a typed declaration whose properties mirror the live
configuration; runtime-generated values (instance ids, allocated IPs) come
from the live AWS API.

`graph.json` is the prebuilt `chant graph --format ir` output: the typed
resource graph (every node, attribute, and cross-resource edge) of this
estate, ready to read without running any tooling.
