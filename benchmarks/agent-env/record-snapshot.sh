#!/usr/bin/env bash
# Record chant's state snapshot into the exported workspace.
#
# `prepare.py --export` rebuilds the workspace from the arm image, which deletes
# whatever was there — including the `chant/lifecycle` orphan branch a snapshot
# lives on. Every re-export therefore silently invalidates the snapshot, and the
# next `search --at latest` fails with "No snapshots found". That is not visible
# as an error anywhere: the agent just falls back and the run looks like a bad
# score rather than a missing prerequisite.
#
# So this runs after every export, and `chant-source.sh` calls it.
#
#   ./benchmarks/agent-env/record-snapshot.sh [env]
set -euo pipefail

ENVIRONMENT="${1:-floci}"
EXPORTS="$HOME/.aws-bench/agent-env"
WORKSPACE="$EXPORTS/workspaces/chant"

[ -d "$WORKSPACE" ] || { echo "no exported chant workspace at $WORKSPACE" >&2; exit 1; }

echo "==> recording the $ENVIRONMENT snapshot into the exported workspace"
docker run --rm \
  -e AWS_ENDPOINT_URL="http://host.docker.internal:${FLOCI_PORT:-4566}" \
  -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test -e AWS_DEFAULT_REGION=us-east-1 \
  -v "$WORKSPACE:/w" awsbench-arm-chant:latest sh -c '
cd /w
# The workspace is owned by the host user and this container is root, so git
# refuses it as dubiously owned — and reports that as "not in a git directory",
# which reads like git init failed when it did not.
git config --global --add safe.directory "*" >/dev/null 2>&1 || true
rm -rf .git
git init -q
git config user.email bench@local
git config user.name bench
# node_modules is hundreds of megabytes and irrelevant to a state snapshot; the
# commit exists only to give the orphan branch a HEAD to hang off.
printf "node_modules/\n.runtime/\nvendor/\n" > .git/info/exclude
git add -A >/dev/null 2>&1
git commit -qm "estate at deploy" >/dev/null 2>&1
./node_modules/.bin/chant lifecycle snapshot '"$ENVIRONMENT"' --ambient 2>&1 | grep -cE "Snapshot saved" | sed "s/^/    stacks recorded: /"
'

# Prove it reads back, rather than assuming. A snapshot that cannot be replayed
# is the same as no snapshot, and it fails silently at query time.
echo "==> verifying the snapshot replays"
docker run --rm \
  -e AWS_ENDPOINT_URL=http://127.0.0.1:9999 \
  -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test -e AWS_DEFAULT_REGION=us-east-1 \
  -v "$WORKSPACE:/w:ro" awsbench-arm-chant:latest sh -c '
git config --global --add safe.directory "*" >/dev/null 2>&1 || true
cp -a /w /tmp/c && cd /tmp/c
./node_modules/.bin/chant search "kind:EC2::Instance" --at latest --env '"$ENVIRONMENT"' 2>/dev/null | tail -1
' | sed 's/^/    /'
