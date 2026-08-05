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
    # The emulator's own log, filed with the run. It is the only account of what
    # happened to the estate, and it lived nowhere: the emulator that produced a
    # result gets torn down by the next run's reset, taking its log with it.
    #
    # It had already reported the failure that invalidated a run — "Bind for
    # 0.0.0.0:30000 failed: port is already allocated", at WARN, while an
    # instance was going to `terminated` — and nobody was reading it. A gate
    # catches that now, but a gate only catches what it was told to look for,
    # and this is where anything else will have been written down.
    docker logs floci-floci-1 > "jobs/${JOB}/emulator.log" 2>&1 || true
  fi
  rm -f "$RUN_LOG" 2>/dev/null || true
}
trap park_run_log EXIT

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ARMS="$REPO/benchmarks/arms"
EXPORTS="$HOME/.aws-bench/agent-env"
cd "$REPO"

# 4566 is the port every Floci wants, so a developer already running one for
# something else has it. Everything downstream — the deploy, the snapshot, the
# preflight, the endpoints handed to trial containers — is derived from this one
# value rather than repeating the number, which is how five of them would
# otherwise have to be found and changed together.
#
#   FLOCI_PORT=4577 ./benchmarks/agent-env/run-arm.sh cdk
#
# The container side is untouched. Only the host binding moves.
export FLOCI_PORT="${FLOCI_PORT:-4566}"
export FLOCI_TLS_PORT="${FLOCI_TLS_PORT:-443}"
HOST_ENDPOINT="http://localhost:${FLOCI_PORT}"
CONTAINER_ENDPOINT="http://host.docker.internal:${FLOCI_PORT}"

export AWS_ENDPOINT_URL="$HOST_ENDPOINT"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1

case "$ARM" in
  chant)          SRC=chant-ec2-multiregion-search-v2; TARGET=/workspace/chant
                  BRIEFING=briefing-chant-snapshot.md ;;
  # The control: an agent, the AWS CLI, and the account. Its workspace is empty
  # by design, so the estate is deployed by another arm's tooling — the estate
  # is the scenario's, not any one tool's, and nothing about who applied it
  # reaches the agent.
  bare)           SRC=bare; TARGET=/workspace/bare
                  BRIEFING=briefing-bare.md ;;
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

# Everything below — reset, deploy, export, snapshot, audit — is chant's; only
# the agent's environment differs.
BASE_ARM="${ARM%-offline}"

echo "==> [$ARM] wiping the emulator and this arm's state"
./benchmarks/floci/reset.sh "$BASE_ARM"

# Can Docker actually give this run the networks it needs? Every trial brings up
# its own compose project, so N_CONCURRENT networks exist at once on top of the
# emulator's.
#
# When the address pool is exhausted, compose fails in the trial's _prepare —
# before the agent starts — and the harness records a RuntimeError and moves on
# to the next trial, which fails the same way. terraform-m1, chant-m1 and
# pulumi-m1 each lost 22 of 24 trials to this and each still produced a
# result.json. An hour per arm, three arms, and what came out looked like scores.
#
# So find out in two seconds instead. This creates what the run needs and
# removes it again; if the pool is full it is full now, not forty minutes in.
echo "==> [$ARM] checking docker can allocate $N_CONCURRENT trial networks"
probe_nets=()
probe_failed=""
for i in $(seq 1 "$N_CONCURRENT"); do
  if net=$(docker network create "awsbench-probe-$$-$i" 2>&1); then
    probe_nets+=("awsbench-probe-$$-$i")
  else
    probe_failed="$net"
    break
  fi
done
[ ${#probe_nets[@]} -gt 0 ] && docker network rm "${probe_nets[@]}" >/dev/null 2>&1
if [ -n "$probe_failed" ]; then
  echo "" >&2
  echo "docker cannot create $N_CONCURRENT networks — only ${#probe_nets[@]} available:" >&2
  echo "  $probe_failed" >&2
  echo "" >&2
  echo "Every trial would fail in setup and the run would still emit a result." >&2
  echo "Reclaim them first:  docker network prune -f" >&2
  echo "Or run with fewer at once:  N_CONCURRENT=4 $0 $ARM" >&2
  exit 1
fi
echo "    $N_CONCURRENT networks available"

echo "==> [$ARM] deploying the estate"
(
  cd "$ARMS/$SRC"
  case "$BASE_ARM" in
    chant)     ./deploy.sh ;;
    # Deployed from the chant arm's directory: the estate is defined by the
    # scenario and is identical whoever applies it, and the bare agent never
    # sees the project that did.
    bare)      (cd "$ARMS/chant-ec2-multiregion-search-v2" && ./deploy.sh) ;;
    # Deployed inside the arm's own image, which is where its vendored provider
    # works. The provider is linux_arm64 and the host is darwin_arm64, so on the
    # host both routes fail for opposite reasons: a bare `init` fetches a darwin
    # build whose checksum is not in .terraform.lock.hcl, and `-plugin-dir`
    # finds no darwin package at all. In-container the deploy uses exactly the
    # terraform the agent will use, which is what the run is meant to measure.
    terraform) docker run --rm \
                 -e AWS_ENDPOINT_URL="$CONTAINER_ENDPOINT" \
                 -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test \
                 -e AWS_DEFAULT_REGION=us-east-1 -e AWS_REGION=us-east-1 \
                 -e TF_VAR_floci_endpoint="$CONTAINER_ENDPOINT" \
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

# Before anything asks the arm a question, check the estate it will be asked
# about. Preflight and the audit are both about the tool, and neither can see a
# deploy that came up short — the tool runs fine and faithfully reports five
# instances where the scenario defines six.
echo "==> [$ARM] estate: did the deploy land intact?"
python3 benchmarks/agent-env/estate-check.py

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

# Which version of its own tool this arm is about to be measured on, read from
# the export the trials mount. Captured here rather than after the run because
# `jobs/$JOB` does not exist yet and the export can be rebuilt the moment this
# returns; the stamp below just writes down what was true at the start line.
#
# Not fatal. A run whose tool would not say its version is still a run, and
# `emit-result.py` omits the field rather than guessing.
#
# `if` rather than `[ ... ] && ...`: under `set -e` a bare test that comes back
# false takes the whole script with it, and an arm that declined to name its
# version would kill the run it was only annotating.
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

TOOL_VERSION="$(python3 benchmarks/agent-env/tool-version.py "$BASE_ARM" || true)"
if [ -n "$TOOL_VERSION" ]; then
  echo "==> [$ARM] tool under test: $TOOL_VERSION"
fi

echo "==> [$ARM] running k=3"
# The estate is unreachable to the agent, and only to the agent — the verifier
# still resolves the reference answer's placeholders against it.
if [ "$ARM" = chant-offline ]; then export AWS_BENCH_DENY_AGENT_LIVE=1; fi

AWS_BENCH_EMULATOR=floci \
AWS_BENCH_EMULATOR_ENDPOINT="$HOST_ENDPOINT" \
AWS_BENCH_EMULATOR_CONTAINER_ENDPOINT="$CONTAINER_ENDPOINT" \
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

# Which question set this job scored. The emitter cannot work it out from the
# job — result.json names the tasks, not the set — and a 6-trial negative run
# labelled as the 24-trial board is exactly the denominator mixing the negative
# set exists to avoid.
echo ec2-multiregion > "jobs/$JOB/scenario"

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

echo "==> [$ARM] audit: did every trial use the tool?"
python3 benchmarks/agent-env/audit.py "jobs/$JOB"
