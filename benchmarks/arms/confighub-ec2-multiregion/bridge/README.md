# The Cloud Control bridge

ConfigHub applies units through workers, and its SDK ships no AWS toolchain —
units are ConfigHub/YAML, Kubernetes/YAML or AppConfig/*. This bridge is the
missing piece the confighub arm deploys with: units carrying CFN-shaped
resources, applied through the Cloud Control API. **The bridge is ours, not
the vendor's, and every number this arm earns is a measurement of
ConfigHub-the-store plus this bridge.** The arm's writeup carries that
sentence, not the fine print.

## The unit shape

```yaml
resources:
  - label: gateVpc
    typeName: AWS::EC2::VPC
    desiredState:
      CidrBlock: 10.42.0.0/16
```

Apply creates each resource without an `identifier`, polls the request token,
and returns the unit with `identifier` and `live` filled per resource — that
round-trips through ConfigHub as the unit's live state. Refresh re-reads every
identified resource and reports drift by the desired-subset rule: every
declared key must match live; live-only keys (ids, service defaults) are not
drift. Import sweeps `ListResources` for the unit's types — including what no
deployment created. Destroy deletes in reverse declaration order.

Deliberately absent: convergence of a changed `desiredState`. Cloud Control
updates go through `UpdateResource`, which Floci does not implement yet, so
Apply treats an identified resource as read-only. The run deploys once and
answers questions; that is all the scenario needs.

## Running it

```sh
go build -o confighub-cc-bridge .

# Hosted control plane, local apply target:
cub auth login
BRIDGE_REGIONS="us-east-1,us-west-1,us-west-2" \
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
AWS_ENDPOINT_URL=http://localhost:4566 \
  cub worker run --space $SPACE --executable ./confighub-cc-bridge confighub-cc
```

One target per region; the region is the target's BridgeHandle. The AWS client
comes from `LoadDefaultConfig`, so `AWS_ENDPOINT_URL` is the only emulator
wiring — the same story as the Formae arm, deliberately.

The control plane is SaaS: `cub auth login` and the worker's connection both
talk to hosted ConfigHub. The arm depends on an external service in a way no
other arm does, and its REPRODUCE must say so beside the credential note.

## Tests

`go test ./...` — the Cloud Control surface is faked in-process; nothing here
needs an account or an emulator.
