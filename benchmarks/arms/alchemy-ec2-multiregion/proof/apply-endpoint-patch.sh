#!/usr/bin/env bash
# Alchemy (published async, 0.93.12) hardcodes https://<svc>.<region>.amazonaws.com
# at each service call site and reads no AWS_ENDPOINT_URL (upstream alchemy#991).
# Bun runs the package's TypeScript source (the "bun" export condition -> src/*.ts),
# so the patch must target src/*.ts, NOT lib/*.js.
#
# PROVEN: the EC2 site below deploys a VPC+subnet into Floci. The EC2 client pins
# service:"ec2", so SigV4 keeps the service in scope against a localhost endpoint
# and Floci routes correctly.
#
# TODO for the full estate: apply the SAME override pattern to the other services
# the estate touches — grep for `amazonaws.com` under node_modules/alchemy/src/aws:
#   - IAM (role, instance-profile)   - SSM (ssm-parameter, the /exports contract)
#   - LaunchTemplate (lives under AutoScaling in the codegen'd Effect line; confirm
#     the async build's site)
set -euo pipefail
F=node_modules/alchemy/src/aws/ec2/utils.ts
python3 - "$F" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
old = 'const url = `https://ec2.${client.region}.amazonaws.com/`;'
new = ('const url = process.env.AWS_ENDPOINT_URL '
       '? `${process.env.AWS_ENDPOINT_URL.replace(/\\/$/, "")}/` '
       ': `https://ec2.${client.region}.amazonaws.com/`;')
if new.split("?")[0] in s:
    print("already patched"); sys.exit(0)
assert old in s, "EC2 url site not found — Alchemy version drift, re-locate the endpoint site"
open(p, "w", encoding="utf-8").write(s.replace(old, new))
print("patched", p)
PY
