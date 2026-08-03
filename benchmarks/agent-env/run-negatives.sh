#!/usr/bin/env bash
# Score one arm against the negative-question set, on an estate it already deployed.
#
#   ./benchmarks/agent-env/run-negatives.sh chant
#   ./benchmarks/agent-env/run-negatives.sh cdk cdk-neg-1
#
# These questions are not aws-bench's. They are ours, they live in
# benchmarks/tasks/ec2-multiregion-negatives, and they exist because the one
# question of that shape upstream already asks — which security groups are
# unused — is the only place on the board where every arm that keeps a state
# file scores zero. Pulumi, Terraform and CDK are 0 for 66 attempts between
# them; the agent holding nothing but the AWS CLI gets it 28% of the time.
#
# One question is an anecdote. The point of adding two more of the same shape is
# to find out whether that result is about the shape or about that question, and
# the honest version of that experiment requires publishing the answer either
# way — including chant scoring badly, which it does on the existing one about
# half the time.
#
# Written against the estate before any arm ran them, so nothing here is tuned
# to a result. Scored separately from the eight-question board rather than
# folded in: a different question set is a different experiment, and the arms'
# existing numbers are over eight questions, not ten.
set -euo pipefail

ARM="${1:?usage: run-negatives.sh <arm> [job-name]}"
JOB="${2:-${ARM}-neg-1}"

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
EXPORTS="$HOME/.aws-bench/agent-env"
cd "$REPO"

export FLOCI_PORT="${FLOCI_PORT:-4566}"
export AWS_ENDPOINT_URL="http://localhost:${FLOCI_PORT}"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1

# Same arm contract the scored runs use, read from one place so the two cannot
# drift. An arm named here that arms.py does not know fails loudly.
read -r SRC TARGET BRIEFING < <(python3 - "$ARM" <<'PY'
import sys
sys.path.insert(0, "benchmarks/agent-env")
from arms import ARMS
arm = ARMS.get(sys.argv[1])
if arm is None:
    sys.exit(f"unknown arm: {sys.argv[1]}")
print(arm.source, arm.workdir, arm.briefing)
PY
)

AGENT_ENV=()
case "$ARM" in
  pulumi)  AGENT_ENV=(--ae PULUMI_CONFIG_PASSPHRASE=floci
                      --ae "PULUMI_BACKEND_URL=file://$TARGET") ;;
  cdk)     AGENT_ENV=(--ae CDK_DEFAULT_ACCOUNT=000000000000
                      --ae CDK_DEFAULT_REGION=us-east-1) ;;
  alchemy) AGENT_ENV=(--ae DO_NOT_TRACK=1) ;;
  alchemy-effect) AGENT_ENV=(--ae DO_NOT_TRACK=1 --ae CI=1) ;;
esac

# No deploy and no wipe. This scores an estate that is already up, which is what
# makes the run cheap enough to be worth doing — but it means the estate has to
# be the one this arm deployed, so the gate that decides that runs first.
echo "==> [$ARM] estate: is the deployed estate intact?"
python3 benchmarks/agent-env/estate-check.py

echo "==> [$ARM] scoring the negative questions, k=3"
AWS_BENCH_EMULATOR=floci \
AWS_BENCH_EMULATOR_ENDPOINT="http://localhost:${FLOCI_PORT}" \
AWS_BENCH_EMULATOR_CONTAINER_ENDPOINT="http://host.docker.internal:${FLOCI_PORT}" \
AWSBENCH_SCAN_METHOD=fastscan \
uv run aws-bench run --env-name awsbench \
  -p benchmarks/tasks/ec2-multiregion-negatives \
  --scenario-path benchmarks/scenarios \
  -a claude-code -m claude-haiku-4-5-20251001 -k 3 -n "${N_CONCURRENT:-6}" \
  --job-name "$JOB" \
  --extra-instruction-path "benchmarks/arms/$BRIEFING" \
  --mounts '[
    {"type":"bind","source":"'"$EXPORTS"'/toolchain","target":"/opt/awsbench-toolchain","read_only":true},
    {"type":"bind","source":"'"$EXPORTS"'/workspaces/'"$ARM"'","target":"/opt/awsbench-arm","read_only":true}
  ]' \
  --ak toolchain=/opt/awsbench-toolchain \
  --ak arm_src=/opt/awsbench-arm \
  --ak "arm_workdir=$TARGET" \
  ${AGENT_ENV[@]+"${AGENT_ENV[@]}"} \
  --no-verify-env --yes

echo "==> [$ARM] audit: did the arm use its own tooling?"
python3 benchmarks/agent-env/audit.py "jobs/$JOB"
