#!/usr/bin/env python3
"""Prove an arm's own tooling works in the agent container before it is scored.

Run this against every arm before a benchmark job. It stands the arm's workspace
up the way a trial does, runs the commands the arm's briefing teaches, and fails
if any of them cannot answer.

    python3 benchmarks/agent-env/preflight.py                # every arm
    python3 benchmarks/agent-env/preflight.py chant cdk      # named arms

Exit status is nonzero if any arm's tooling is broken, so it can gate a run:

    python3 benchmarks/agent-env/preflight.py cdk && uv run aws-bench run ...

The scenario-1 runs are what this is for. CDK and Alchemy were scored 11/15 and
10/15 without a single `cdk` or `alchemy` command ever executing, and Terraform's
`show -json` was refusing while the agent answered from `cat terraform.tfstate`.
None of that surfaced in a result.json, because a trial that quietly falls back
to jq still produces a reward.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from arms import ARMS, Arm  # noqa: E402
from prepare import TOOLS_IMAGE as IMAGE  # noqa: E402
from prepare import image_for  # noqa: E402

ARMS_DIR = Path(__file__).resolve().parents[1] / "arms"

# The container reaches the emulator on the host's published port.
DEFAULT_ENDPOINT = "http://host.docker.internal:4566"
#: Seconds a smoke command gets before it counts as a failure.
SMOKE_TIMEOUT = 180


def build_image(context: Path) -> None:
    """Build the toolchain image if it is not already present."""
    present = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True,
    )
    if present.returncode == 0:
        return
    print(f"building {IMAGE} ...", flush=True)
    subprocess.run(["docker", "build", "-t", IMAGE, str(context)], check=True)


def estate_instance_count(endpoint: str) -> int | None:
    """How many EC2 instances the emulator is serving, or None if unreachable.

    Worth knowing before anything else: an arm whose state was wiped fails its
    smoke commands for a reason that has nothing to do with its tooling, and
    reporting that as broken tooling would send someone chasing the wrong bug.
    """
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"AWS_ENDPOINT_URL={endpoint}",
            "-e",
            "AWS_ACCESS_KEY_ID=test",
            "-e",
            "AWS_SECRET_ACCESS_KEY=test",
            "-e",
            "AWS_DEFAULT_REGION=us-east-1",
            IMAGE,
            "sh",
            "-c",
            "aws ec2 describe-instances --query 'length(Reservations[].Instances[])' --output text",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def container_script(arm: Arm) -> str:
    """The smoke script run inside the arm's prepared image.

    The workspace is already in place and writable, which is the point: on the
    read-only mount the scored runs used, Terraform's .terraform/, CDK's
    cdk.out/ and npm's node_modules all had nowhere to write, and all three
    failed.

    When the exported workspace is mounted, it replaces the image's copy first.
    The image is built before the estate is deployed, so its workspace has no
    deployed state in it: no terraform.tfstate, no Pulumi checkpoint, and — for
    chant — no `chant/lifecycle` orphan branch holding the recorded snapshot.
    Trials mount the export, so a gate reading the image was vetting something
    no trial ever receives, and could pass while what shipped was broken.
    """
    lines = [
        # The export is host-owned and this container is root, so git calls it
        # dubiously owned and reports that as "not in a git directory".
        'git config --global --add safe.directory "*" >/dev/null 2>&1 || true',
        f"if [ -d /opt/awsbench-arm ]; then rm -rf {shlex.quote(arm.workdir)} && "
        f"cp -a /opt/awsbench-arm {shlex.quote(arm.workdir)}; fi",
        f"cd {shlex.quote(arm.workdir)}",
    ]
    for i, smoke in enumerate(arm.smoke):
        lines.append(f"echo '::smoke-begin::{i}'")
        lines.append(f"{smoke.cmd} 2>&1 || echo '::exit-nonzero::'")
        lines.append(f"echo '::smoke-end::{i}'")
    return "\n".join(lines)


def run_arm(arm: Arm, endpoint: str, keep_going: bool) -> tuple[bool, list[str]]:
    """Run the arm's smoke commands in its prepared image. (ok, report lines)."""
    image = image_for(arm)
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        return False, [
            f"  NOT PREPARED  {image} does not exist",
            f"                run: python3 benchmarks/agent-env/prepare.py {arm.name}",
        ]

    env = {
        "AWS_ENDPOINT_URL": endpoint,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
        **arm.env,
    }
    cmd = ["docker", "run", "--rm"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    # Vet exactly what a trial gets, when there is one to vet.
    export = Path.home() / ".aws-bench" / "agent-env" / "workspaces" / arm.name
    if export.is_dir():
        cmd += ["-v", f"{export}:/opt/awsbench-arm:ro"]
    cmd += [image, "sh", "-c", container_script(arm)]

    # A tool that never returns is a failed preflight, not a reason to wait
    # forever. alchemy v2's `state tree` loads its entrypoint, which builds an
    # Effect runtime that never shuts down, so the container sat up for 45
    # minutes with the whole queue behind it and nothing on stdout to say why.
    # Whatever the next tool does, it gets three minutes.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SMOKE_TIMEOUT)
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"") + (exc.stderr or b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return False, [
            f"  TIMED OUT after {SMOKE_TIMEOUT}s — the arm's tooling did not return",
            _indent(partial.strip()[-600:] or "(no output)"),
        ]

    ok = True
    report: list[str] = []
    for i, smoke in enumerate(arm.smoke):
        section = _section(out, i)
        if section is None:
            report.append(f"  NO OUTPUT  {smoke.cmd}")
            report.append(_indent(out.strip()[-600:]))
            ok = False
            continue
        nonzero = "::exit-nonzero::" in section
        matched = re.search(smoke.must_match, section) is not None
        if matched and not nonzero:
            report.append(f"  ok    {smoke.cmd}")
            continue
        ok = False
        reason = "exited nonzero" if nonzero else f"no match for /{smoke.must_match}/"
        report.append(f"  FAIL  {smoke.cmd}")
        report.append(f"        {reason} — expected {smoke.why}")
        report.append(_indent(section.strip()[:800]))
        if not keep_going:
            break
    return ok, report


def _section(out: str, i: int) -> str | None:
    """Pull one smoke command's output out of the container log."""
    begin, end = f"::smoke-begin::{i}\n", f"::smoke-end::{i}"
    if begin not in out or end not in out:
        return None
    return out.split(begin, 1)[1].split(end, 1)[0]


def _indent(text: str) -> str:
    return "\n".join(f"        | {line}" for line in text.splitlines())


def main() -> int:
    """Check the requested arms and report which cannot answer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arms", nargs="*", default=None, help="arms to check (default: all)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="emulator endpoint")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="report every failing command instead of stopping at the first",
    )
    parser.add_argument("--jobs", type=int, default=4, help="arms to check at once")
    args = parser.parse_args()

    names = args.arms or list(ARMS)
    unknown = [n for n in names if n not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {', '.join(unknown)}. known: {', '.join(ARMS)}")

    build_image(Path(__file__).parent)

    instances = estate_instance_count(args.endpoint)
    if instances is None:
        print(f"note: no emulator answering at {args.endpoint}")
        print("      arms that read the live estate will fail for that reason alone.")
    elif instances == 0:
        print(f"note: the emulator at {args.endpoint} has no EC2 instances.")
        print("      Deploy an arm's estate first, or a failure below is a wiped")
        print("      estate rather than broken tooling.")

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(
            pool.map(lambda n: (n, *run_arm(ARMS[n], args.endpoint, args.keep_going)), names)
        )

    failed = []
    for name, ok, report in results:
        print(f"\n{name}")
        print("\n".join(report))
        if not ok:
            failed.append(name)

    print()
    if failed:
        print(f"BROKEN TOOLING: {', '.join(failed)} — do not score these arms")
        return 1
    print(f"all {len(names)} arm(s) can answer with their own tooling")
    return 0


if __name__ == "__main__":
    sys.exit(main())
