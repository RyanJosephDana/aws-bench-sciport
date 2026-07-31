#!/usr/bin/env bash
# Point the chant arm at a local chant checkout, or back at a published version.
#
#   ./benchmarks/agent-env/chant-source.sh local [/path/to/chant]
#   ./benchmarks/agent-env/chant-source.sh published 0.33.0
#   ./benchmarks/agent-env/chant-source.sh show
#
# Local mode packs the working tree the same way a release would — `npm pack`
# runs `prepack`, which builds — so what the agent runs is the package, not a
# symlink into a monorepo with devDependencies on PATH. The tarballs live in the
# arm directory because that directory is the Docker build context; a `file:`
# dependency pointing outside it would not resolve during `prepare.py`.
#
# Changing the source rewrites the arm, so it re-prepares and re-exports. Run
# preflight afterwards before trusting anything.
set -euo pipefail

MODE="${1:?usage: chant-source.sh local|published|show [path|version]}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ARM="$REPO/benchmarks/arms/chant-ec2-multiregion-search-v2"
VENDOR="$ARM/vendor-local"
# Where prepare.py --export writes; the workspace a trial actually mounts.
EXPORTS="$HOME/.aws-bench/agent-env"

show() {
  python3 - "$ARM/package.json" <<'PY'
import json, sys
deps = json.load(open(sys.argv[1]))["dependencies"]
for name, spec in deps.items():
    kind = "LOCAL" if spec.startswith("file:") else "published"
    print(f"  {kind:<9} {name}  {spec}")
PY
}

case "$MODE" in
  show)
    show; exit 0 ;;

  local)
    SRC="$(cd "${2:-$HOME/Documents/checkouts/intentius/chant}" && pwd)"
    [ -f "$SRC/packages/core/package.json" ] || {
      echo "not a chant checkout: $SRC" >&2; exit 2; }

    echo "==> packing local chant from $SRC"
    rm -rf "$VENDOR"; mkdir -p "$VENDOR"
    # Do NOT parse npm pack's stdout for the tarball name. `prepack` builds, and
    # the aws lexicon's build writes its progress to stdout too, so the last line
    # is whatever the generator happened to print last ("All validation checks
    # passed."). That produced a file: dependency pointing at a tarball that does
    # not exist, and the failure surfaced later as a stale package rather than as
    # a packing error.
    ( cd "$SRC/packages/core" && npm pack --pack-destination "$VENDOR" >/dev/null )
    ( cd "$SRC/lexicons/aws" && npm pack --pack-destination "$VENDOR" >/dev/null )
    core=$(cd "$VENDOR" && ls intentius-chant-*.tgz | grep -v lexicon | head -1)
    lex=$(cd "$VENDOR" && ls intentius-chant-lexicon-aws-*.tgz | head -1)
    [ -n "$core" ] && [ -n "$lex" ] || { echo "npm pack produced no tarballs in $VENDOR" >&2; exit 1; }
    echo "    $core"
    echo "    $lex"

    python3 - "$ARM/package.json" "$core" "$lex" <<'PY'
import json, sys
path, core, lex = sys.argv[1:4]
pkg = json.load(open(path))
pkg["dependencies"]["@intentius/chant"] = f"file:vendor-local/{core}"
pkg["dependencies"]["@intentius/chant-lexicon-aws"] = f"file:vendor-local/{lex}"
json.dump(pkg, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY
    # The lockfile pins registry tarball URLs and integrity hashes for the
    # published packages; keeping it makes npm resolve those instead.
    rm -f "$ARM/package-lock.json"
    ;;

  published)
    VERSION="${2:?published needs a version, e.g. 0.33.0}"
    echo "==> restoring published chant $VERSION"
    rm -rf "$VENDOR"
    python3 - "$ARM/package.json" "$VERSION" <<'PY'
import json, sys
path, version = sys.argv[1:3]
pkg = json.load(open(path))
pkg["dependencies"]["@intentius/chant"] = f"{version}"
pkg["dependencies"]["@intentius/chant-lexicon-aws"] = f"{version}"
json.dump(pkg, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY
    rm -f "$ARM/package-lock.json"
    ;;

  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

echo "==> arm now depends on:"
show

echo "==> re-preparing and exporting the chant arm"
python3 "$REPO/benchmarks/agent-env/prepare.py" chant --export

# An export wipes the workspace, taking the recorded snapshot with it. Without
# this, `search --at latest` fails on the very next run and the failure looks
# like a bad answer rather than a missing prerequisite.
"$REPO/benchmarks/agent-env/record-snapshot.sh" floci || {
  echo "snapshot not recorded — --at queries will fail" >&2; exit 1; }

echo "==> installed in the image:"
docker run --rm awsbench-arm-chant:latest sh -c \
  'cd /workspace/chant && node -e "console.log(\"  chant\", require(\"./node_modules/@intentius/chant/package.json\").version, \"| lexicon-aws\", require(\"./node_modules/@intentius/chant-lexicon-aws/package.json\").version)"'

# Prove the export is the source that was just packed, rather than assuming it.
#
# The version number cannot tell you this: a local build carries the same
# 0.33.1 as the published release and as whatever was exported an hour ago.
# When the packing step failed, the previous export stayed in place and the next
# container ran the OLD build — a verification against it passed while measuring
# code that had already been replaced.
#
# Content is the only honest check. Hash the source trees on both sides,
# path-independently, and compare.
if [ "$MODE" = "local" ]; then
  fingerprint() {
    # Contents only, in a stable order: the two trees live at different paths,
    # so anything including filenames would never match.
    ( cd "$1" 2>/dev/null && find . -type f -name '*.ts' | LC_ALL=C sort | xargs cat 2>/dev/null ) | shasum -a 256 | cut -d' ' -f1
  }
  echo "==> verifying the export is this working tree"
  mismatch=0
  for pair in "packages/core:@intentius/chant" "lexicons/aws:@intentius/chant-lexicon-aws"; do
    local_dir="$SRC/${pair%%:*}/src"
    export_dir="$EXPORTS/workspaces/chant/node_modules/${pair##*:}/src"
    if [ ! -d "$export_dir" ]; then
      echo "    MISSING  ${pair##*:} is not in the exported workspace" >&2
      mismatch=1; continue
    fi
    if [ "$(fingerprint "$local_dir")" = "$(fingerprint "$export_dir")" ]; then
      echo "    ok    ${pair##*:}"
    else
      echo "    STALE  ${pair##*:} in the export does not match $local_dir" >&2
      mismatch=1
    fi
  done
  [ "$mismatch" -eq 0 ] || {
    echo "The exported workspace is not the code you just built — do not trust a run against it." >&2
    exit 1
  }
fi
