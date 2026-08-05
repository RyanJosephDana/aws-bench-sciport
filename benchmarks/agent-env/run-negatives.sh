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

# The arm's own CLI, on PATH under the name its briefing uses.
#
# chant's briefing says `chant search …` and alchemy's says `alchemy state list`.
# Neither is on PATH in the trial container, so the agent's first call dies with
# `command not found` and it spends turns rediscovering `npx <tool>`. The audit
# — correctly — reads a missing CLI as "this run did not measure the arm" and
# voids the whole run.
#
# Whether that happens is a coin flip on what the agent types first. Three
# g-series chant runs never typed the bare form and published; the next two both
# did, and both were voided at 21/24 and 5/24 with the tooling otherwise healthy
# (72 invocations, 1% failure). A gate that fires on the model's phrasing rather
# than on the environment is not measuring the environment.
#
# It goes here rather than in the arm image because the arm image is not what a
# trial runs — prepare.py exports the workspace and the harness bind-mounts it
# into a container built from the dataset's own image, so an ENV baked into
# awsbench-arm-* never reaches a trial.
#
# The base is read from the tools image rather than written down, so this cannot
# drift from the Dockerfile that sets it.
BASE_PATH="$(docker run --rm awsbench-agent-tools:latest sh -c 'printf %s "$PATH"')"
AGENT_ENV+=(--ae "PATH=$TARGET/node_modules/.bin:$BASE_PATH")

# No deploy and no wipe. This scores an estate that is already up, which is what
# makes the run cheap enough to be worth doing — but it means the estate has to
# be the one this arm deployed, so the gate that decides that runs first.
echo "==> [$ARM] estate: is the deployed estate intact?"
python3 benchmarks/agent-env/estate-check.py

# Which version of its own tool this arm is about to be measured on, read from
# the export the trials mount. Same stamp `run-arm.sh` takes, for the same
# reason: a scenario whose records cannot name the tool is a scenario whose
# numbers cannot be attributed to one, and these are the runs most likely to be
# repeated across builds.
#
# `if` rather than `[ … ] && …` — under `set -e` a false bare test takes the
# script with it, and an arm declining to name its version would kill a run it
# was only annotating.
# Which harness produced this run, captured at the start line rather than read
# at emit time.
#
# `emit-result.py` called `git_commit(REPO)` when the record was written, so a
# job emitted after any later commit or edit was stamped with a harness it never
# ran under. That is the same fault the `workspace` stamp was added to fix in
# 09075af, still live on the one field INTENTIUS/chant-bench#26 is actually
# about. Matches `git_commit`'s semantics: short HEAD, `-dirty` when the tree
# carries anything uncommitted.
HARNESS_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"
if [ -n "$HARNESS_COMMIT" ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  HARNESS_COMMIT="${HARNESS_COMMIT}-dirty"
fi

TOOL_VERSION="$(python3 benchmarks/agent-env/tool-version.py "$ARM" || true)"
if [ -n "$TOOL_VERSION" ]; then
  echo "==> [$ARM] tool under test: $TOOL_VERSION"
fi

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

# These are two questions, not eight, so the record has to say which set it is.
# Emitted under the board's scenario name it would sit beside 24-trial runs with
# a denominator of 6, and `validate_results.py` rejects the whole group.
echo ec2-multiregion-negatives > "jobs/$JOB/scenario"

# Which workspace this run was handed, captured NOW rather than derived later.
#
# `emit-result.py` read `<arm>.fingerprint`, which `prepare.py --export`
# overwrites. Emit a run after the next export and it is stamped with a
# workspace it never saw — silently, and most easily when several runs are
# ingested together after the rebuild that follows them. Three runs published
# that way claimed a build that postdated them, which is the same shape as the
# `harness_commit` fault in INTENTIUS/chant-bench#26.
cp "$EXPORTS/workspaces/$ARM.fingerprint" "jobs/$JOB/workspace" 2>/dev/null || true

# And which harness ran it, captured before it started.
if [ -n "$HARNESS_COMMIT" ]; then
  printf '%s\n' "$HARNESS_COMMIT" > "jobs/$JOB/harness_commit"
fi

# And which tool the run was measuring, captured before it started.
if [ -n "$TOOL_VERSION" ]; then
  printf '%s\n' "$TOOL_VERSION" > "jobs/$JOB/tool_version"
fi

echo "==> [$ARM] audit: did the arm use its own tooling?"
python3 benchmarks/agent-env/audit.py "jobs/$JOB"
