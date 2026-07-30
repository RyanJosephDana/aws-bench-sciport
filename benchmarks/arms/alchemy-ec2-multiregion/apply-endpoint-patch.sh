#!/usr/bin/env bash
# Point published Alchemy (async model, 0.93.12) at a local AWS endpoint (Floci).
#
# Alchemy has no endpoint override (upstream alchemy-run/alchemy#991). Two call
# sites hardcode https://<svc>.<region>.amazonaws.com and must be patched to
# honor AWS_ENDPOINT_URL:
#
#   1. src/aws/ec2/utils.ts        — the hand-rolled EC2 query client behind
#                                    Vpc/Subnet/InternetGateway/RouteTable/Route/
#                                    SecurityGroup(+Rule). Pins service:"ec2",
#                                    so SigV4 keeps the service in scope against
#                                    a localhost host and Floci routes correctly.
#   2. src/aws/control/client.ts   — the Cloud Control API client behind the
#                                    AWS.* proxy (AWS::EC2::Instance,
#                                    AWS::EC2::LaunchTemplate,
#                                    AWS::IAM::InstanceProfile). Pins
#                                    service:"cloudcontrolapi", same story.
#
# Everything else the estate touches needs NO patch: Role/Policy (IAM),
# SSMParameter, and AccountId go through AWS SDK v3 peer clients
# (@aws-sdk/client-iam/-ssm/-sts), which honor AWS_ENDPOINT_URL natively.
#
# One compat fix rides along (site 3): Policy's create-fallback catches
# error.name === "NoSuchEntity", but @aws-sdk/client-iam v3 throws
# NoSuchEntityException, so creating any policy that does not already exist
# fails against real AWS and Floci alike. Not endpoint-related — an upstream
# alchemy-vs-SDK drift bug.
#
# Bun resolves the package's "bun" export condition to src/*.ts, so the patch
# targets src, NOT lib/*.js.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
sites = [
    (
        "node_modules/alchemy/src/aws/ec2/utils.ts",
        'const url = `https://ec2.${client.region}.amazonaws.com/`;',
        'const url = process.env.AWS_ENDPOINT_URL '
        '? `${process.env.AWS_ENDPOINT_URL.replace(/\\/$/, "")}/` '
        ': `https://ec2.${client.region}.amazonaws.com/`;',
    ),
    (
        "node_modules/alchemy/src/aws/control/client.ts",
        '`https://cloudcontrolapi.${this.region}.amazonaws.com/?Action=${action}&Version=2021-09-30`,',
        '(process.env.AWS_ENDPOINT_URL '
        '? `${process.env.AWS_ENDPOINT_URL.replace(/\\/$/, "")}/?Action=${action}&Version=2021-09-30` '
        ': `https://cloudcontrolapi.${this.region}.amazonaws.com/?Action=${action}&Version=2021-09-30`),',
    ),
    (
        "node_modules/alchemy/src/aws/policy.ts",
        'if (error.name === "NoSuchEntity") {',
        'if (error.name === "NoSuchEntity" || error.name === "NoSuchEntityException") {',
    ),
]
for path, old, new in sites:
    s = open(path, encoding="utf-8").read()
    if new in s:
        print("already patched:", path)
        continue
    assert old in s, f"patch site not found in {path} — Alchemy version drift, re-locate it"
    open(path, "w", encoding="utf-8").write(s.replace(old, new))
    print("patched:", path)
PY
