# Gate proof: formae deploys into Floci through Cloud Control

Proven 2026-08-09. One VPC, declared in `gate.pkl`, applied by the published
formae agent image against the CC-capable emulator, confirmed live by the AWS
CLI and present in `formae inventory` — the two reads the arm's briefing will
sanction.

No patch, no host install. The agent runs from
`ghcr.io/platform-engineering-labs/formae` (0.85.2 at proof time, aws plugin
v0.1.7) and the CLI runs inside the same container, so the arm needs neither
the `/opt/pel` installer nor an endpoint patch — the plugin builds its clients
with aws-sdk-go-v2 `LoadDefaultConfig`, and `AWS_ENDPOINT_URL` in the
container's environment is all the wiring there is.

## Reproduce

```sh
# The emulator (any port; the bench harness derives its own):
docker run -d --name floci-cc-lab -p 14566:4566 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/lex00/floci:awsbench-277aad0

# The agent, pointed at it:
docker run -d --name formae-agent-lab -v "$PWD:/work" \
  -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test \
  -e AWS_REGION=us-east-1 -e AWS_DEFAULT_REGION=us-east-1 \
  -e AWS_ENDPOINT_URL=http://host.docker.internal:14566 \
  ghcr.io/platform-engineering-labs/formae:latest

# Resolve the Pkl deps once (formae schema comes from their hub; the aws
# schema resolves from the copy the image bundles), then apply:
docker exec -w /work formae-agent-lab pkl project resolve
docker exec -w /work formae-agent-lab sh -c 'echo Y | formae apply --mode reconcile gate.pkl'

# Both sides of the claim:
docker exec formae-agent-lab formae inventory resources     # the tool's record
aws --endpoint-url http://localhost:14566 ec2 describe-vpcs \
  --region us-east-1 --filters Name=cidr,Values=10.42.0.0/16  # the live estate
```

Observed: apply Success (stack + target + VPC, 3/3), `vpc-…` in the inventory,
and the same id from `describe-vpcs`.

## What this does not prove yet

- The full estate: instances, launch templates, IAM, SSM — the types the
  eight questions need. `PklProject` here deps the aws schema by the path the
  image bundles it at; the estate build does the same.
- Reconcile-mode drift convergence, which calls `UpdateResource` — Floci does
  not implement it yet. First apply is create-only, so the estate deploy is
  expected to clear; the reconciler's behaviour on drift is to be established.
- Discovery breadth: `ListResources` covers 9 types (see the HANDOFF).
