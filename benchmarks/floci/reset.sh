#!/usr/bin/env bash
# Reset the emulator and one arm's IaC state so the next deploy starts from nothing.
#
#   ./benchmarks/floci/reset.sh              # emulator only
#   ./benchmarks/floci/reset.sh terraform    # emulator + that arm's state
#   ./benchmarks/floci/reset.sh pulumi|cdk|chant|alchemy|alchemy-effect
#
# Three things bite if you skip any of them, and each fails in a way that blames
# the arm rather than the reset:
#
#   1. `docker compose down -v` does NOT remove the floci-ec2-* containers. Floci
#      creates them as siblings through the Docker socket, outside the compose
#      project, so they survive and keep host ports 2200-2299 and 30000+. The next
#      arm's instances then fail to launch with "port is already allocated" and go
#      straight to `terminated` — which Terraform reports as "unexpected state
#      'terminated', wanted target 'running'".
#   2. An arm's IaC state survives the wipe. Terraform tries to reconcile against
#      resources that no longer exist; Pulumi tries to *update* a launch template
#      that is gone.
#   3. A failed deploy leaves partial state behind (SSM /exports parameters, named
#      IAM policies, launch templates). Retrying without a full wipe collides with
#      the previous attempt's leftovers.
set -euo pipefail
cd "$(dirname "$0")"
ARM="${1:-}"
ARMS_DIR="$(cd ../arms && pwd)"

echo "==> removing floci-ec2-* containers (compose down does not)"
docker ps -aq --filter "name=^floci-ec2" | xargs -r docker rm -f >/dev/null 2>&1 || true

# Every trial creates its own docker network and they are not always reaped.
# Docker's default address pool is finite: after a few hundred trials it is
# fully subnetted and the next `docker compose up` fails with "all predefined
# address pools have been fully subnetted". That surfaces as 22 of 24 trials
# raising RuntimeError, which reads as the arm collapsing rather than as the
# machine running out of subnets. Only unused networks are removed, so anything
# running is untouched.
echo "==> reclaiming unused docker networks"
docker network prune -f >/dev/null 2>&1 || true

echo "==> recreating the emulator"
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d >/dev/null
for _ in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' floci-floci-1 2>/dev/null || echo starting)
  [ "$status" = healthy ] && break
  sleep 2
done
echo "    emulator: ${status:-unknown}"
# Stop here rather than deploying into an emulator that never came up. It
# printed "unhealthy" and carried on, so every arm downstream failed against a
# dead endpoint and reported it as the tool's problem — the emulator had died on
# a full Docker disk and nothing said so.
if [ "${status:-unknown}" != healthy ]; then
  echo "    the emulator is not healthy; refusing to continue" >&2
  docker logs floci-floci-1 --tail 20 2>&1 | sed 's/^/      /' >&2 || true
  exit 1
fi

case "$ARM" in
  "") ;;
  terraform)
    echo "==> clearing terraform state"
    rm -f "$ARMS_DIR/terraform-ec2-multiregion"/terraform.tfstate*
    ;;
  pulumi)
    echo "==> clearing pulumi stack"
    ( cd "$ARMS_DIR/pulumi-ec2-multiregion"
      export PULUMI_CONFIG_PASSPHRASE=floci PULUMI_BACKEND_URL="file://$PWD"
      pulumi stack rm dev --yes --force >/dev/null 2>&1 || true
      pulumi stack init dev >/dev/null 2>&1 || true )
    ;;
  cdk)
    echo "==> clearing cdk synth output"
    rm -rf "$ARMS_DIR/cdk_app/cdk.out"
    ;;
  chant)
    # chant's state is the emulator's CloudFormation stacks — wiped above.
    echo "==> chant keeps no local state"
    ;;
  bare)
    echo "==> bare keeps no local state (it has no toolchain)"
    ;;
  alchemy|alchemy-effect)
    echo "==> clearing alchemy state"
    rm -rf "$ARMS_DIR/${ARM}-ec2-multiregion/.alchemy"
    ;;
  *)
    echo "unknown arm: $ARM" >&2; exit 1 ;;
esac

echo "==> ready — deploy the arm now (see its REPRODUCE.md)"
