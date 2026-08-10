#!/usr/bin/env python3
"""Which version of its own tool an arm is about to be measured on.

    python3 benchmarks/agent-env/tool-version.py chant
    @intentius/chant 0.41.0

`run-arm.sh` calls this before the run and stamps the answer into the job, so
`emit-result.py` can put it in the published record.

Every other thing that decides what an arm could answer is recorded — the
harness commit, the briefing hash, the workspace fingerprint — and the tool
under test was the one left to be inferred. It was inferred wrongly: the chant
arm's committed pin said 0.33.1 for weeks while the runs that produced the
published board were measured against 0.41.0, and nothing in a record could
have shown that. A fingerprint says two runs differ. It does not say what
differed, and a reader cannot look up a hash.

The version is read where a trial runs, from the workspace a trial mounts,
rather than from the arm directory on the host — which the next
`prepare.py --export` overwrites. That is the same mistake the `workspace`
stamp was added to stop making.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from arms import ARMS, Arm  # noqa: E402
from prepare import image_for  # noqa: E402

#: Seconds the version command gets. It reads a file or prints a constant; a
#: tool that cannot say its own version in this long is not going to.
TIMEOUT = 60


def container_script(arm: Arm) -> str:
    """Stand the exported workspace up and ask the tool its version.

    The materialisation matches `preflight.container_script` deliberately: the
    dependency trees are symlinked in from the mount, which is what puts
    `node_modules` where the version command looks for it.
    """
    return "\n".join(
        [
            'git config --global --add safe.directory "*" >/dev/null 2>&1 || true',
            f"if [ -d /opt/awsbench-arm ]; then rm -rf {shlex.quote(arm.workdir)} && "
            f"mkdir -p {shlex.quote(arm.workdir)} && "
            'for e in /opt/awsbench-arm/* /opt/awsbench-arm/.[!.]*; do [ -e "$e" ] || continue; '
            'case "${e##*/}" in node_modules|vendor|vendor-local|.runtime) '
            f'ln -s "$e" {shlex.quote(arm.workdir)}/"${{e##*/}}" ;; '
            f'*) cp -a "$e" {shlex.quote(arm.workdir)}/ ;; esac; done; fi',
            f"cd {shlex.quote(arm.workdir)}",
            arm.version_cmd,
        ]
    )


def tool_version(arm: Arm) -> str | None:
    """The version string, or None if it could not be established."""
    if not arm.version_cmd:
        return None

    image = image_for(arm)
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        print(f"not prepared: {image} does not exist", file=sys.stderr)
        return None

    cmd = ["docker", "run", "--rm"]
    for key, value in arm.env.items():
        cmd += ["-e", f"{key}={value}"]
    export = Path.home() / ".aws-bench" / "agent-env" / "workspaces" / arm.name
    if export.is_dir():
        cmd += ["-v", f"{export}:/opt/awsbench-arm:ro"]
    cmd += [image, "sh", "-c", container_script(arm)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"timed out after {TIMEOUT}s asking {arm.name} its version", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(proc.stderr.strip()[-400:] or "version command failed", file=sys.stderr)
        return None

    # Last non-empty line: npm and some CLIs print notices ahead of the answer,
    # and a notice recorded as a version would be worse than no version at all.
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    # `pulumi version` says v3.255.0 and everything else says 3.255.0. One
    # leading v is the only normalisation done here — anything more ambitious
    # would eventually mangle a suffix like 2.0.0-beta.65.
    return lines[-1].removeprefix("v")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=sorted(ARMS))
    args = parser.parse_args()

    arm = ARMS[args.arm]
    version = tool_version(arm)
    if version is None:
        return 1
    print(f"{arm.tool_name} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
