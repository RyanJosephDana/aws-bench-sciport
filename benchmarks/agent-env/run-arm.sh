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
N_CONCURRENT="${N_CONCURRENT:-8}"

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
    # Same init the arm's own contract uses (arms.py). Without -plugin-dir this
    # fetches from the registry and the download does not match the checksum
    # recorded in .terraform.lock.hcl, so the deploy dies before the estate
    # exists — the arm was vendoring a provider that only the agent's setup
    # actually used.
    terraform) terraform init -input=false -plugin-dir=.terraform/providers >/dev/null \
               && terraform apply -auto-approve ;;
    pulumi)    export PULUMI_CONFIG_PASSPHRASE=floci PULUMI_BACKEND_URL="file://$PWD"
               pulumi up --yes ;;
    cdk)       # The three regions bootstrap independently; serially they are
               # most of this arm's deploy time.
               for r in us-east-1 us-west-1 us-west-2; do
                 CDK_DEFAULT_ACCOUNT=000000000000 \
                   npx cdk bootstrap "aws://000000000000/$r" >/dev/null &
               done
               wait
               CDK_DEFAULT_ACCOUNT=000000000000 npx cdk deploy --all \
                 --require-approval never --concurrency 4 ;;
    alchemy|alchemy-effect)
               AWS_ACCOUNT_ID=000000000000 ALCHEMY_TELEMETRY_DISABLED=1 \
                 bun alchemy.run.ts ;;
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
