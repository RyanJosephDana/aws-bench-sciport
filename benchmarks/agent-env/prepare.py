#!/usr/bin/env python3
"""Bake each arm's workspace into an image, once, so nothing installs per run.

Installing an arm's dependencies inside every container is the slow part: the
Alchemy v2 arm's tree alone takes minutes, and it repeats for every trial and
every preflight. Baking it into a layer makes it a one-off that Docker caches,
and it makes every later container start from an identical, already-working
workspace.

    python3 benchmarks/agent-env/prepare.py            # every arm
    python3 benchmarks/agent-env/prepare.py chant cdk  # named arms
    python3 benchmarks/agent-env/prepare.py --rebuild  # ignore the layer cache

Produces one image per arm, `awsbench-arm-<name>`, holding the toolchain plus the
arm's workspace at its briefing's path with dependencies resolved for linux.

The arm's own node_modules is deliberately not copied in. Those are installed on
the host, so their native binaries are darwin-arm64 and chant's `npx tsx` dies
with "The package @esbuild/linux-arm64 could not be found". The lockfile travels;
the platform binaries get resolved here.
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from arms import ARMS, Arm  # noqa: E402

TOOLS_IMAGE = "awsbench-agent-tools:latest"
ARMS_DIR = Path(__file__).resolve().parents[1] / "arms"

# Exports go beside the dataset cache, not into the repo. They are gigabytes of
# platform binaries and resolved dependencies — the toolchain alone is 714MB and
# the six workspaces are 2.5GB — and ruff walks anything inside the tree,
# including the Python that ships in Node's own share/doc.
EXPORT_ROOT = Path.home() / ".aws-bench" / "agent-env"

# Copied into each arm's build context. Keeping host-installed dependencies and
# local state out of the context also keeps the build fast — the CDK arm's
# node_modules is most of a gigabyte.
DOCKERIGNORE = "node_modules\n.git\ncdk.out\n.terraform/modules\n"


def image_for(arm: Arm) -> str:
    """Image name holding this arm's prepared workspace."""
    return f"awsbench-arm-{arm.name}:latest"


def dockerfile_for(arm: Arm) -> str:
    """A layer per setup step, so an edit to one arm rebuilds only what changed."""
    lines = [
        f"FROM {TOOLS_IMAGE}",
        f"WORKDIR {arm.workdir}",
        f"COPY . {arm.workdir}",
        # The arm's own CLI, on PATH under the name its briefing teaches.
        #
        # chant's briefing says `chant search …` and alchemy's says `alchemy
        # state list`; neither was on PATH, so the agent's first call died with
        # `command not found` and it spent turns rediscovering `npx <tool>`.
        # Every trial of a chant run did this, and the audit — correctly — reads
        # a missing CLI as "this run did not measure the arm" and invalidates
        # the whole thing.
        #
        # This is not a thumb on the scale for the arms that have a local CLI.
        # The briefing is part of the experiment, and an environment that does
        # not provide the command its own instruction names is measuring the
        # agent's recovery from a broken setup. terraform, pulumi and cdk are
        # unaffected — theirs are already on PATH or invoked through npx.
        f'ENV PATH="{arm.workdir}/node_modules/.bin:${{PATH}}"',
    ]
    for key, value in arm.env.items():
        lines.append(f"ENV {key}={value}")
    for step in arm.setup:
        lines.append(f"RUN {step}")
    return "\n".join(lines) + "\n"


def prepare(arm: Arm, rebuild: bool) -> tuple[str, bool, str]:
    """Build one arm's image. Returns (name, ok, message)."""
    source = ARMS_DIR / arm.source
    if not source.is_dir():
        return arm.name, False, f"{arm.source} is not in benchmarks/arms"

    dockerfile = source / "Dockerfile.awsbench-arm"
    dockerignore = source / ".dockerignore"
    wrote_dockerignore = not dockerignore.exists()
    try:
        dockerfile.write_text(dockerfile_for(arm))
        if wrote_dockerignore:
            dockerignore.write_text(DOCKERIGNORE)
        cmd = [
            "docker",
            "build",
            "-t",
            image_for(arm),
            "-f",
            str(dockerfile),
            str(source),
        ]
        if rebuild:
            cmd.insert(2, "--no-cache")
        proc = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        dockerfile.unlink(missing_ok=True)
        if wrote_dockerignore:
            dockerignore.unlink(missing_ok=True)

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
        return arm.name, False, "\n".join(f"    | {line}" for line in tail)
    return arm.name, True, image_for(arm)


def workspace_fingerprint(root: Path) -> str:
    """A short digest of what a workspace contains, by path and size.

    Recorded so a result can say which workspace produced it. The briefing hash
    and the harness commit already travel with every run; the third thing that
    decides what an arm could answer is the tree its trials were handed, and
    that was the one nobody could name.

    Stat only, no file contents: 62k files in about a second, and it still moves
    when a dependency is added, removed, patched or half-installed. It will not
    notice an edit that preserves length, which is a real gap and a cheap price
    for a check that runs on every export.

    Deliberately not a comparison against the image. The export is `cp -a` out of
    that image, so the two are equal by construction, and the host tree
    legitimately differs from both — node_modules is dockerignored and is darwin
    on the host, linux in the image. A diff there is noise.
    """
    entries = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and not f.is_symlink():
            entries.append(f"{f.relative_to(root)}:{f.stat().st_size}")
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()[:12]


def export(arm: Arm, dest_root: Path) -> tuple[str, bool, str]:
    """Copy an arm's prepared workspace and the toolchain out to the host.

    A trial's image comes from the dataset, so the prepared image cannot be the
    container the agent runs in. Exporting lets the same bytes be bind-mounted
    into whatever image the harness builds — the arm's workspace with its linux
    dependencies, and the shared toolchain, both resolved once.
    """
    toolchain = dest_root / "toolchain"
    workspace = dest_root / "workspaces" / arm.name
    workspace.parent.mkdir(parents=True, exist_ok=True)
    toolchain.mkdir(parents=True, exist_ok=True)

    script = f"cp -a {shlex.quote(arm.workdir)}/. /out/workspace/"
    if not any(toolchain.iterdir()):
        script += " && cp -a /opt/node /opt/bun /opt/terraform-bin /opt/pulumi /out/toolchain/"

    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workspace}:/out/workspace",
            "-v",
            f"{toolchain}:/out/toolchain",
            image_for(arm),
            "sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return arm.name, False, (proc.stderr or proc.stdout).strip()[-400:]

    # Written beside the workspace rather than inside it: anything in there is
    # handed to the agent, and the harness has no business putting files in a
    # tree it is asking a tool to describe.
    fingerprint = workspace_fingerprint(workspace)
    (workspace.parent / f"{arm.name}.fingerprint").write_text(fingerprint + "\n")
    return arm.name, True, f"{workspace}  ({fingerprint})"


def ensure_tools_image(context: Path, rebuild: bool = False) -> None:
    """Build the shared toolchain image if it is missing, or if asked to rebuild.

    It used to build only when absent, so an edit to the Dockerfile never took
    effect: `prepare.py --rebuild` rebuilt each arm on top of a toolchain image
    that was whatever had been built first. Adding `column` to the image and
    watching it still not exist is the cheap version of that; the expensive
    version is a pinned tool version that silently is not the pinned version.
    """
    present = subprocess.run(["docker", "image", "inspect", TOOLS_IMAGE], capture_output=True).returncode == 0
    if present and not rebuild:
        return
    print(f"building {TOOLS_IMAGE} ...", flush=True)
    subprocess.run(["docker", "build", "-t", TOOLS_IMAGE, str(context)], check=True)


def main() -> int:
    """Prepare, and optionally export, the requested arms."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arms", nargs="*", help="arms to prepare (default: all)")
    parser.add_argument("--rebuild", action="store_true", help="ignore the layer cache")
    parser.add_argument("--jobs", type=int, default=4, help="arms to build at once")
    parser.add_argument(
        "--export",
        action="store_true",
        help="also copy the prepared workspaces and toolchain out for bind-mounting into trials",
    )
    args = parser.parse_args()

    names = args.arms or list(ARMS)
    unknown = [n for n in names if n not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {', '.join(unknown)}. known: {', '.join(ARMS)}")

    ensure_tools_image(Path(__file__).parent, rebuild=args.rebuild)

    print(f"preparing {len(names)} arm(s) ...", flush=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda n: prepare(ARMS[n], args.rebuild), names))

    failed = [n for n, ok, _ in results if not ok]
    for name, ok, message in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  -> {message}" if ok else ""))
        if not ok:
            print(message)

    if args.export:
        built = [n for n, ok, _ in results if ok]
        print(f"\nexporting {len(built)} workspace(s) ...", flush=True)
        # Serial: the first export also writes the shared toolchain.
        for name in built:
            _, ok, message = export(ARMS[name], EXPORT_ROOT)
            print(f"  {'ok  ' if ok else 'FAIL'}  {name}  {message}")
            if not ok:
                failed.append(name)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
