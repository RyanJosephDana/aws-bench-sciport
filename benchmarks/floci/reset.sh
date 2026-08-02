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

# `floci-ec2-*` is not a name only this emulator uses. Any Floci on the machine
# creates siblings under it, so an unscoped `docker rm -f` reaches into whatever
# else the developer is running — it deleted a live instance belonging to
# another Floci that had nothing to do with the benchmark. A reset may destroy
# the estate it is resetting and nothing else.
#
# Ownership cannot be read off a container. Floci sets no labels, and it puts
# EC2 containers on the default bridge rather than its own network, so neither
# `--filter label=` nor `--filter network=` can tell two emulators apart.
#
# So ask this emulator which instances are its own, while it is still up —
# `docker compose down` happens further below. Anything it does not claim is
# either an orphan from a previous run of ours, recognised by the host port it
# holds, or somebody else's and left alone.
#
# The prefix over-matches, incidentally. This scenario's stacks are named
# `ec2-multiregion`, so a Lambda container for one of them is `floci-` plus a
# function name beginning `ec2-` and matches `^floci-ec2` without being an
# instance at all. They hold no host ports, which is what this reap is for, so
# they are left to compose.
FLOCI_PORT="${FLOCI_PORT:-4566}"
SSH_START="${FLOCI_SSH_PORT_START:-2300}"; SSH_END="${FLOCI_SSH_PORT_END:-2399}"
APP_START="${FLOCI_APP_PORT_START:-31000}"; APP_END=$((APP_START + 999))

echo "==> removing this emulator's instance containers (compose down does not)"
ours=""
REGIONS="${FLOCI_ALLOWED_REGIONS:-us-east-1,us-west-1,us-west-2}"
for region in ${REGIONS//,/ }; do
  ids=$(AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
        aws --endpoint-url "http://localhost:$FLOCI_PORT" --region "$region" \
            ec2 describe-instances --query 'Reservations[].Instances[].InstanceId' \
            --output text 2>/dev/null) || true
  for id in $ids; do
    # The instance id alone: ids are unique, and it matches both the instance
    # container and its `-fwd-` port sidecars.
    ours="$ours $(docker ps -aq --filter "name=$id" 2>/dev/null)"
  done
done

# Orphans of ours: no emulator left to claim them, but they hold a host port in
# the range this benchmark configured, so nothing else could have created them.
orphans_and_foreign=$(docker ps -a --filter "name=^floci-ec2" --format '{{.ID}} {{.Names}}')
foreign=""
while read -r cid cname; do
  [ -n "$cid" ] || continue
  case " $ours " in *" $cid "*) continue ;; esac
  port=$(docker inspect "$cid" --format \
    '{{range $p, $c := .NetworkSettings.Ports}}{{range $c}}{{.HostPort}} {{end}}{{end}}' 2>/dev/null)
  mine=""
  for p in $port; do
    if { [ "$p" -ge "$SSH_START" ] && [ "$p" -le "$SSH_END" ]; } ||
       { [ "$p" -ge "$APP_START" ] && [ "$p" -le "$APP_END" ]; }; then mine=1; fi
  done
  if [ -n "$mine" ]; then ours="$ours $cid"; elif [ -n "$port" ]; then foreign="$foreign $cname"; fi
done <<EOF
$orphans_and_foreign
EOF

[ -n "${ours// /}" ] && docker rm -f $ours >/dev/null 2>&1 || true

# Somebody else's, holding host ports. Named rather than removed, and named
# loudly: Floci's defaults are 2200-2299 and 30000+, and if this benchmark is
# still configured for those, a foreign instance holding one is a deploy that
# fails with "port is already allocated" and an instance that goes straight to
# `terminated`. That is exactly how a run once scored 14/24 on five instances
# where the scenario defines six, with every gate passing it.
if [ -n "${foreign// /}" ]; then
  echo "    another Floci has instances up; leaving them alone:"
  printf '      %s\n' $foreign
  if [ "$SSH_START" -lt 2300 ] || [ "$APP_START" -lt 31000 ]; then
    echo "    WARNING: this benchmark is using Floci's default port ranges, which those" >&2
    echo "    instances also use. Set FLOCI_SSH_PORT_START / FLOCI_APP_PORT_START." >&2
  fi
fi

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
