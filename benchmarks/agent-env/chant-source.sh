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
    # npm pack prints the tarball name on stdout; build noise goes to stderr.
    core=$(cd "$SRC/packages/core" && npm pack --pack-destination "$VENDOR" --silent | tail -1)
    lex=$(cd "$SRC/lexicons/aws" && npm pack --pack-destination "$VENDOR" --silent | tail -1)
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

echo "==> installed in the image:"
docker run --rm awsbench-arm-chant:latest sh -c \
  'cd /workspace/chant && node -e "console.log(\"  chant\", require(\"./node_modules/@intentius/chant/package.json\").version, \"| lexicon-aws\", require(\"./node_modules/@intentius/chant-lexicon-aws/package.json\").version)"'
