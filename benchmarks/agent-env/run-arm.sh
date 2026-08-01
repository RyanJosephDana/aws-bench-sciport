#!/usr/bin/env bash
# Run one arm end to end: wipe, deploy its estate, prove its tooling, score it,
# then check the tooling was actually used.
#
#   ./benchmarks/agent-env/run-arm.sh chant
#   ./benchmarks/agent-env/run-arm.sh terraform my-job-name
#
# Arms share one emulator and deploy identically-named stacks, so they have to go
# one at a time and the wipe is not optional — see benchmarks/floci/reset.sh for
# what survives a plain `docker compose down -v`.
#
# The preflight and audit are the point. A trial whose tool is missing does not
# error; it answers from jq and still earns a reward, which is how the first
# scenario-1 runs reported CDK and Alchemy numbers with no `cdk` or `alchemy`
# command ever executed. Both gates exit nonzero and stop the run.
set -euo pipefail

ARM="${1:?usage: run-arm.sh <arm> [job-name]}"
JOB="${2:-${ARM}-s2-fixed}"

# 24 trials at -n 4 is three quarters of an hour of waiting across five arms, and
# concurrency does not change scores — each trial gets its own container. 8 puts
# an arm in the 5-10 minute range. Override with N_CONCURRENT for a smaller box.
# Trial containers have no memory reservation of their own — they share the
# Docker VM, so concurrency decides how much each can actually take. CDK's
# `cdk ls` runs `ts-node lib/app.ts` and peaks near 1.4GB; eight of those at once
# needs ~11.8GB with the emulator, which overflowed the 8GB VM this was first
# run against and got them SIGKILLed.
#
# The failure hid itself: with `cdk` dead the agent read the cdk.out templates
# the deploy had left behind and still answered, so four trials scored 1.0
# having never run CDK once. Only the postflight audit caught it.
#
# Resolved by giving Docker 20GB rather than by special-casing the arm, so every
# arm runs at the same concurrency again.
N_CONCURRENT="${N_CONCURRENT:-8}"

# Everything this script prints — wipe, deploy, export, preflight, k=3, audit —
# is captured and filed with the job at the end. `job.log` covers only the
# scored run, so the two gates that decide whether a run counts at all left no
# record beside the result. A published number should link to the evidence that
# it was allowed to stand.
RUN_LOG="$(mktemp -t run-arm)"
exec > >(tee -a "$RUN_LOG") 2>&1
park_run_log() {
  if [ -d "jobs/${JOB}" ]; then
    cp "$RUN_LOG" "jobs/${JOB}/run-arm.log" 2>/dev/null || true
  fi
  rm -f "$RUN_LOG" 2>/dev/null || true
}
trap park_run_log EXIT

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ARMS="$REPO/benchmarks/arms"
EXPORTS="$HOME/.aws-bench/agent-env"
cd "$REPO"

export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1

case "$ARM" in
  chant)          SRC=chant-ec2-multiregion-search-v2; TARGET=/workspace/chant
                  BRIEFING=briefing-chant-snapshot.md ;;
  # Same source, same briefing, same recorded snapshot — the agent's AWS
  # endpoint points at a closed port, so the estate is unreachable to it. Every
  # other arm keeps live access. The question is whether an answer built from a
  # recording holds up against tools reading the account as they answer.
  chant-offline)  SRC=chant-ec2-multiregion-search-v2; TARGET=/workspace/chant
                  BRIEFING=briefing-chant-snapshot.md ;;
  terraform)      SRC=terraform-ec2-multiregion;       TARGET=/workspace/terraform
                  BRIEFING=briefing-terraform.md ;;
  pulumi)         SRC=pulumi-ec2-multiregion;          TARGET=/workspace/pulumi
                  BRIEFING=briefing-pulumi.md ;;
  cdk)            SRC=cdk_app;                          TARGET=/workspace/cdk_app
                  BRIEFING=briefing-cdk.md ;;
  alchemy)        SRC=alchemy-ec2-multiregion;         TARGET=/workspace/alchemy
                  BRIEFING=briefing-alchemy.md ;;
  alchemy-effect) SRC=alchemy-effect-ec2-multiregion;  TARGET=/workspace/alchemy
                  BRIEFING=briefing-alchemy-effect.md ;;
  *) echo "unknown arm: $ARM" >&2; exit 2 ;;
esac

# Per-arm agent environment. Expanded below as ${AGENT_ENV[@]+...}: bash 3.2 —
# still what macOS ships — treats an empty array as unset under `set -u`, and
# chant is the only arm that needs no extra env, so it was the only one that
# died, after a full wipe, deploy and preflight had already succeeded. Without CI=1 the Alchemy v2 CLI refuses every
# command in a non-interactive shell, which an agent container always is.
AGENT_ENV=()
case "$ARM" in
  pulumi)  AGENT_ENV=(--ae PULUMI_CONFIG_PASSPHRASE=floci
                      --ae "PULUMI_BACKEND_URL=file://$TARGET") ;;
  cdk)     AGENT_ENV=(--ae CDK_DEFAULT_ACCOUNT=000000000000
                      --ae CDK_DEFAULT_REGION=us-east-1) ;;
  alchemy) AGENT_ENV=(--ae DO_NOT_TRACK=1) ;;
  alchemy-effect) AGENT_ENV=(--ae DO_NOT_TRACK=1 --ae CI=1) ;;
  # Denial happens in aws-bench (AWS_BENCH_DENY_AGENT_LIVE, exported below), not
  # here: the trial sets AWS_ENDPOINT_URL from the emulator AFTER applying --ae,
  # so doing it through the agent env looks like it worked and does nothing.
  chant-offline)  AGENT_ENV=() ;;
esac

# Everything below — reset, deploy, export, snapshot, audit — is chant's; only
# the agent's environment differs.
BASE_ARM="${ARM%-offline}"

echo "==> [$ARM] wiping the emulator and this arm's state"
./benchmarks/floci/reset.sh "$BASE_ARM"

echo "==> [$ARM] deploying the estate"
(
  cd "$ARMS/$SRC"
  case "$BASE_ARM" in
    chant)     ./deploy.sh ;;
    # Deployed inside the arm's own image, which is where its vendored provider
    # works. The provider is linux_arm64 and the host is darwin_arm64, so on the
    # host both routes fail for opposite reasons: a bare `init` fetches a darwin
    # build whose checksum is not in .terraform.lock.hcl, and `-plugin-dir`
    # finds no darwin package at all. In-container the deploy uses exactly the
    # terraform the agent will use, which is what the run is meant to measure.
    terraform) docker run --rm \
                 -e AWS_ENDPOINT_URL=http://host.docker.internal:4566 \
                 -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test \
                 -e AWS_DEFAULT_REGION=us-east-1 -e AWS_REGION=us-east-1 \
                 -e TF_VAR_floci_endpoint=http://host.docker.internal:4566 \
                 -v "$PWD:/w" -w /w awsbench-arm-terraform:latest sh -c '
                   terraform init -input=false -plugin-dir=.terraform/providers >/dev/null &&
                   terraform apply -auto-approve' ;;
    pulumi)    export PULUMI_CONFIG_PASSPHRASE=floci PULUMI_BACKEND_URL="file://$PWD"
               pulumi up --yes ;;
    cdk)       # The three regions bootstrap independently; serially they are
               # most of this arm's deploy time. Each gets its own synth output
               # directory: they all synthesize on the way to bootstrapping, and
               # three processes writing one cdk.out collide on asset paths —
               # `EEXIST: file already exists, mkdir cdk.out/asset.…` killed the
               # deploy before the estate existed.
               for r in us-east-1 us-west-1 us-west-2; do
                 CDK_DEFAULT_ACCOUNT=000000000000 \
                   npx cdk bootstrap "aws://000000000000/$r" \
                     --output "cdk.out.bootstrap.$r" >/dev/null &
               done
               wait
               rm -rf cdk.out.bootstrap.*
               CDK_DEFAULT_ACCOUNT=000000000000 npx cdk deploy --all \
                 --require-approval never --concurrency 4 ;;
    alchemy)   AWS_ACCOUNT_ID=000000000000 ALCHEMY_TELEMETRY_DISABLED=1 \
                 bun alchemy.run.ts ;;
    # v2's AWSEnvironment carries a single region, so this arm is three
    # independent per-region entrypoints rather than one — the same shape as
    # the CDK arm. Deployed as its REPRODUCE.md documents it.
    alchemy-effect)
               # Idempotent, and nothing else applies it: the patch lives in
               # node_modules, so a fresh `npm install` or a rebuilt arm image
               # silently loses it and the deploy goes to real AWS URLs.
               ./apply-endpoint-patch.sh >/dev/null
               export CI=true AWS_ACCOUNT_ID=000000000000 ALCHEMY_TELEMETRY_DISABLED=1
               for r in us-west-1 us-west-2; do
                 AWS_REGION=$r bunx alchemy deploy "$r.run.ts" --stage bench --yes &
               done
               wait
               AWS_REGION=us-east-1 bunx alchemy deploy us-east-1.run.ts --stage bench --yes ;;
  esac
)

echo "==> [$ARM] re-exporting the workspace so trials get the deployed state"
# The estate deploy writes state into the arm directory — terraform.tfstate, the
# Pulumi stack, .alchemy/. Trials mount the export, not that directory, so an
# export from before the deploy would hand the agent an empty state file.
python3 benchmarks/agent-env/prepare.py "$BASE_ARM" --export

# The export rebuilds the workspace from the arm image, which deletes the orphan
# branch chant records its state snapshot on. Without re-recording, every trial's
# `search --at latest` fails with "No snapshots found" — and fails quietly, since
# the agent just falls back and the run scores as bad answers rather than a
# missing prerequisite.
if [ "$BASE_ARM" = chant ]; then
  ./benchmarks/agent-env/record-snapshot.sh floci
fi

echo "==> [$ARM] preflight: can it answer with its own tooling?"
python3 benchmarks/agent-env/preflight.py "$BASE_ARM" --keep-going

echo "==> [$ARM] running k=3"
# The estate is unreachable to the agent, and only to the agent — the verifier
# still resolves the reference answer's placeholders against it.
if [ "$ARM" = chant-offline ]; then export AWS_BENCH_DENY_AGENT_LIVE=1; fi

AWS_BENCH_EMULATOR=floci \
AWS_BENCH_EMULATOR_ENDPOINT=http://localhost:4566 \
AWS_BENCH_EMULATOR_CONTAINER_ENDPOINT=http://host.docker.internal:4566 \
AWSBENCH_SCAN_METHOD=fastscan \
uv run aws-bench run --env-name awsbench -d ec2-multiregion \
  -a claude-code -m claude-haiku-4-5-20251001 -k 3 -n "$N_CONCURRENT" \
  -x 'describe-cloudformation-stack-resources' \
  --job-name "$JOB" \
  --extra-instruction-path "benchmarks/arms/$BRIEFING" \
  --mounts '[
    {"type":"bind","source":"'"$EXPORTS"'/toolchain","target":"/opt/awsbench-toolchain","read_only":true},
    {"type":"bind","source":"'"$EXPORTS"'/workspaces/'"$BASE_ARM"'","target":"/opt/awsbench-arm","read_only":true}
  ]' \
  --ak toolchain=/opt/awsbench-toolchain \
  --ak arm_src=/opt/awsbench-arm \
  --ak "arm_workdir=$TARGET" \
  ${AGENT_ENV[@]+"${AGENT_ENV[@]}"} \
  --no-verify-env --yes

echo "==> [$ARM] audit: did every trial use the tool?"
python3 benchmarks/agent-env/audit.py "jobs/$JOB"
