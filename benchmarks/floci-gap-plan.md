# Floci gap plan — remaining 7 scenarios

Planned upstream filings only — nothing filed yet. Derived from the corrected
gap matrix (140 missing CFN types v2, minus the two ec2-multiregion types now
in PR) cross-referenced against upstream floci-io/floci open work as of
2026-07-28. Filing pattern per the ec2-multiregion batch: companion
[FEAT]/[BUG] issue in their template → one capability per branch →
conventional-commit PR with tests → aws-bench context comment → merge into
the fork's awsbench-integration line immediately, upstream at their pace.

## Reuse — upstream work we ride, not reimplement

| Upstream | Covers | Scenarios unblocked |
|---|---|---|
| PR #1949 (+#1945) | SageMaker service (Docker-backed training) | troubleshooting |
| PR #1914 (+#1915) | Kinesis Analytics V2 / Managed Flink | compute-and-data task |
| PR #1804 | CFN `SecretsManager::SecretTargetAttachment` | data, refe, serv, stre, trou (5!) |
| PR #1807 / #1794 | CFN `Events::EventBus` | streaming-and-iot |
| PR #1760 | CFN `ApiGatewayV2::Authorizer` | api-and-observability |
| PR #1809 | CFN dynamic references (ssm/secretsmanager) | several, deploy-time |
| PR #1965 / #1966 | UpdateStack reconcile / DeleteStack idempotency | reset loops, all |
| Ours in flight | #1969/#1972/#1973/#1975/#1977/#1979 + #1982–#1984 | ec2-multiregion done |

## Horizontal batches — types spanning 3+ scenarios (file first, highest leverage)

1. **CFN EC2 security-group rule resources** — `SecurityGroupIngress`,
   `SecurityGroupEgress` (5–6 scenarios). Ec2Service rule APIs exist; pure
   provisioner mapping.
2. **CFN `EC2::VPCEndpoint`** (5 scenarios). Endpoint model likely thin/new.
3. **CFN Lambda addressing trio** — `Permission`, `Version`, `Alias`
   (6/2/2 scenarios). Lambda service models exist.
4. **CFN ECS capacity pair** — `CapacityProvider`,
   `ClusterCapacityProviderAssociations` (6 scenarios).
5. **CFN EC2 NACL family** — `NetworkAcl`, `NetworkAclEntry`,
   `SubnetNetworkAclAssociation`, plus `NetworkInterface(+Attachment)`
   (databases + troubleshooting).
6. **CFN small metadata types** — `ApiGateway::Account` (4),
   `CloudWatch::Dashboard` (3), `AutoScaling::LifecycleHook` (3),
   `EC2::FlowLog` (3). One issue per service, trivial provisioners.
7. **Custom:: CDK resources — verify, then file only real bugs.**
   `Custom::AWS` worked on ec2-multiregion once its SDK call existed;
   `Custom::CDKBucketDeployment`, `Custom::LogRetention`,
   `Custom::S3AutoDeleteObjects`, `Custom::S3BucketNotifications`,
   `Custom::VpcRestrictDefaultSG`, `Custom::CloudwatchLogResourcePolicy`,
   `Custom::OpenSearchAccessPolicy` need a verification pass per scenario.

## Thin service modules — no upstream work exists (PR #15 pattern, one each)

s3tables (CFN `S3Tables::TableBucket` + API; data+stre), imagebuilder (CFN ×5
+ API; compute), medialive (API; compute), customer-profiles (CFN Domain +
API; streaming), iotsitewise (API; streaming), amplify (API; compute),
appstream (API; compute). Each scoped to the operations its tasks/verifiers
actually call.

## Vertical remainders per scenario (file when that scenario is next)

Recommended order = ascending gap count = ascending effort:

| Order | Scenario | CFN gaps left after horizontals/reuse | Notable verticals |
|---|---|---|---|
| 1 | compute-and-data | ~10 | IPAM×3, `EC2::Volume`, `EKS::Addon`, RedshiftServerless×2, CloudFront OAI+Distribution |
| 2 | serverless-apps | ~10 | DocDB×3, `Athena::WorkGroup`, `Logs::LogStream`, `CloudWatch::MetricStream` |
| 3 | streaming-and-iot | ~12 | MSK Cluster+Topic, Neptune×3, `IoT::Thing`, CustomerProfiles::Domain |
| 4 | databases-and-storage | ~14 | NACL family lands here, S3Tables, storage misc |
| 5 | api-and-observability | ~20 | AppConfig×6, `OpenSearchService::Domain`, Redshift×3, CloudFront, WAFv2 |
| 6 | troubleshooting-multiservice | ~35 | TransitGateway×6, Redshift ra3, NetworkFirewall, 30-stack breadth |
| 7 | reference-architectures | ~55 | ServiceCatalog×9, Route53Resolver×6, AppSync×5, Lex×3, Transfer, Backup×3, Cognito×3 |

## Fidelity loop (unplannable specifics, budgeted pattern)

Each scenario's live run will surface runtime fidelity bugs invisible in
templates (the ec2-multiregion set: #1982 port collisions, #1983 private-IP
split, #1984 public-IP 127.0.0.1). Budget one discover→file→fix cycle per
scenario. Two known candidates from task-validity triage, deliberately
unfiled pending design thought: per-region default-VPC opt-out (breaks the
"regions without default VPC" task genre) and account region-restriction
emulation (SCP-shaped; the unused-SG count genre). Both are config-flag-sized
asks at best; may stay excluded-task categories instead.

## Tally

~13 horizontal/service issues + ~7 vertical batch issues + verification
passes + ride 8 upstream PRs. Every filing gets the aws-bench context
comment; fork integration line stays continuously deployable.
