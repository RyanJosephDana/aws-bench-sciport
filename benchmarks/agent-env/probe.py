#!/usr/bin/env python3
"""Ask the agent one estate question and read what it did — in seconds, not minutes.

A full aws-bench run is 24 trials and roughly ten minutes. Every theory about why
a task fails so far — chant not on PATH, a warning breaking JSON parsing, a
grammar term the agent guessed wrong, provenance leaking into the deliverable —
was visible in a single trajectory. Waiting for a scored run to learn that is the
slow way round.

    python3 benchmarks/agent-env/probe.py                 # every question
    python3 benchmarks/agent-env/probe.py ssh unused      # by name
    python3 benchmarks/agent-env/probe.py --briefing b.md ssh
    python3 benchmarks/agent-env/probe.py --show-commands ssh

This is NOT the benchmark. There is no verifier and no LLM judge, so nothing here
is a score, and a green result does not mean a trial would pass — aws-bench grades
against a reference answer whose phrasing matters. What this checks is narrower
and deterministic: did the agent arrive at the fact, and what did it run to get
there. That is the loop worth tightening.

The expectations below were read off the deployed estate, not taken from the
dataset's ground truth, so a check failing means the agent was wrong about the
world rather than about a reference sentence.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

IMAGE = "awsbench-probe:latest"
HERE = Path(__file__).resolve().parent
ARMS = HERE.parent / "arms"
EXPORTS = Path.home() / ".aws-bench" / "agent-env"


@dataclass(frozen=True)
class Question:
    """One estate question, and what a correct answer must contain."""

    name: str
    prompt: str
    #: Every regex must match the answer. Facts, not phrasing.
    expect: list[str] = field(default_factory=list)
    #: None of these may match — the specific wrong answers seen in real trials.
    reject: list[str] = field(default_factory=list)
    why: str = ""


# Read off the live estate: 6 instances (4/1/1 by region), 4 VPCs, 5 in public
# subnets, 2 SSH-reachable, 4 unattached security groups.
QUESTIONS: list[Question] = [
    Question(
        "ssh",
        "Which EC2 instances in my account are reachable via SSH from the internet? "
        "My account only operates in us-east-1, us-west-1 and us-west-2.",
        expect=[r"i-e392157f576caf855", r"i-ee2744528df10ea0d"],
        reject=[r"\b(1|one) instance is reachable", r"only 1 instance"],
        why="the launch-template hop — the one a flat sweep misses, and chant's whole discriminator",
    ),
    Question(
        "public",
        "Find my ec2 instances that are in a public subnet in us-east-1, us-west-1, us-west-2.",
        expect=[r"\b(5|five)\b"],
        why="5 including defaultVpcServer, which only resolves through the account's default VPC routing",
    ),
    Question(
        "vpc",
        "Which EC2 instances are in which VPCs across all regions? "
        "My account only operates in us-east-1, us-west-1 and us-west-2.",
        expect=[r"\b(6|six)\b", r"vpc-"],
        why="6 instances across 4 VPCs",
    ),
    Question(
        "unused",
        "Provide me a list of unused Security Groups by all regions",
        expect=[r"\b(4|four)\b", r"sg-724404b9b8838e80d"],
        why="4 unattached groups; the defaults are all in use on this estate",
    ),
    Question(
        "regions",
        "Could you please list my account's EC2 instance ids in all regions.",
        expect=[r"\b(6|six)\b"],
        why="6 instances total",
    ),
    Question(
        "crossregion",
        "Describe my EC2 instances across us-east-1, us-west-1, and us-west-2. "
        "How many do I have in each region, and which instances in us-east-1 share networking?",
        expect=[r"\b(4|four)\b", r"us-west-1", r"us-west-2"],
        why="4 in us-east-1, 1 in each west region",
    ),
    Question(
        "nodefaultvpc",
        "Which of my EC2 instances don't have a default VPC (all regions)?",
        expect=[r"\b(5|five)\b"],
        # Only the wrong CONCLUSION. `all 6` alone also matched answers that
        # said five and then mentioned the six in passing — the check was
        # failing correct work.
        reject=[r"all (6|six) (of my |your )?(ec2 )?instances?[^.]{0,40}\b(do not|don't|are not|aren't)\b"],
        why="5 — defaultVpcServer IS in the default VPC, and the graph does not label it as such",
    ),
    Question(
        "privateips",
        "List all of my ec2 and their private ip in all regions in a table format.",
        expect=[r"\b(6|six)\b"],
        why="6 instances with private IPs",
    ),
]

BY_NAME = {q.name: q for q in QUESTIONS}


def run_question(q: Question, briefing: str, show_commands: bool) -> tuple[str, bool, list[str]]:
    """Ask one question in a trial-shaped container. Returns (name, ok, report)."""
    prompt = (
        f"{briefing}\n\n---\n\n{q.prompt}\n\n"
        "Write your final answer as plain text. Answer about the AWS estate itself."
    )
    # Copy into the probe user's home: the image's /workspace/chant is
    # root-owned, and a tool that writes needs somewhere it can.
    # The prompt goes through the environment, never into the command string.
    # A briefing is full of backticks, and inside `sh -c "..."` those are command
    # substitution — the shell ate the briefing and ran pieces of it.
    # Mirror what the agent adapter does for a real trial: the arm's own
    # launchers go on PATH. Without it the probe diagnoses an environment the
    # benchmark does not have, and every trace opens with the agent hunting for
    # a binary that is on PATH in the thing being measured.
    script = (
        "cp -a /opt/awsbench-arm $HOME/chant && cd $HOME/chant && "
        "mkdir -p $HOME/bin && for e in $HOME/chant/bin/*; do [ -x \"$e\" ] && "
        "printf '#!/bin/sh\\nexec \"%s\" \"$@\"\\n' \"$e\" > $HOME/bin/$(basename $e) && "
        "chmod +x $HOME/bin/$(basename $e); done; export PATH=$HOME/bin:$PATH; "
        "claude --print --permission-mode bypassPermissions "
        "--output-format stream-json --include-partial-messages --verbose "
        "--model claude-haiku-4-5-20251001 \"$PROMPT\""
    )
    # The same credential aws-bench uses (`claude setup-token` writes
    # ~/.anthropic), passed the documented way rather than by mounting a secret
    # into the container filesystem.
    token = (Path.home() / ".anthropic").read_text().strip()
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "-e", f"CLAUDE_CODE_OAUTH_TOKEN={token}",
            "-e", f"PROMPT={prompt}",
            "-v", f"{EXPORTS}/workspaces/chant:/opt/awsbench-arm:ro",
            IMAGE, "sh", "-c", script,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )

    if proc.returncode != 0 and not proc.stdout.strip():
        return q.name, False, [f"MISS  {q.name:<12} container failed", "        " + " ".join(proc.stderr.split())[:300]]

    # stream-json is one event per line: the final result, plus every tool call
    # on the way. The commands are the diagnosis — an answer alone cannot say
    # whether the agent reached for the fold or hand-rolled it from templates.
    answer = ""
    commands: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            answer = event.get("result") or answer
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list):
            for item in content:
                if item.get("type") == "tool_use" and item.get("name") == "Bash":
                    commands.append(" ".join(str(item.get("input", {}).get("command", "")).split()))
    answer = answer or proc.stdout

    report: list[str] = []
    missing = [p for p in q.expect if not re.search(p, answer, re.I)]
    wrong = [p for p in q.reject if re.search(p, answer, re.I)]
    ok = not missing and not wrong
    report.append(f"{'ok  ' if ok else 'MISS'}  {q.name:<12} {q.why}")
    for p in missing:
        report.append(f"        expected /{p}/ — absent")
    for p in wrong:
        report.append(f"        matched a known-wrong answer /{p}/")
    if not ok:
        report.append("        " + " ".join(answer.split())[:300])
    if show_commands or not ok:
        for c in commands:
            report.append(f"        $ {c[:150]}")
    return q.name, ok, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("questions", nargs="*", help="question names (default: all)")
    parser.add_argument(
        "--briefing",
        default=str(ARMS / "briefing-chant-snapshot.md"),
        help="briefing to put in front of the question",
    )
    parser.add_argument("--show-commands", action="store_true", help="echo the commands the agent ran")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="attempts per question — the agent is stochastic, so one sample says little",
    )
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()

    unknown = [n for n in args.questions if n not in BY_NAME]
    if unknown:
        parser.error(f"unknown question(s): {', '.join(unknown)}. known: {', '.join(BY_NAME)}")
    chosen = [BY_NAME[n] for n in args.questions] if args.questions else QUESTIONS

    briefing_path = Path(args.briefing)
    if not briefing_path.is_file():
        print(f"no such briefing: {briefing_path}", file=sys.stderr)
        return 2
    briefing = briefing_path.read_text()

    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        print(f"building {IMAGE} ...", flush=True)
        subprocess.run(
            ["docker", "build", "-t", IMAGE, "-f", str(HERE / "Dockerfile.probe"), str(HERE)],
            check=True,
        )

    work = [q for q in chosen for _ in range(args.repeat)]
    print(
        f"briefing: {briefing_path.name}   questions: {len(chosen)}"
        f"   attempts each: {args.repeat}   total: {len(work)}\n",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda q: run_question(q, briefing, args.show_commands), work))

    # Group by question: a rate is the point, and one failure among three is a
    # different signal from three.
    tally: dict[str, list[bool]] = {}
    reports: dict[str, list[list[str]]] = {}
    for name, ok, report in results:
        tally.setdefault(name, []).append(ok)
        reports.setdefault(name, []).append(report)

    total_ok = 0
    for q in chosen:
        runs = tally.get(q.name, [])
        hits = sum(runs)
        total_ok += hits
        mark = "ok  " if hits == len(runs) else ("MISS" if hits == 0 else "part")
        print(f"{mark}  {q.name:<12} {hits}/{len(runs)}   {q.why}")
        # Detail from the first failing attempt only — the rest repeats it.
        for ok, report in zip(runs, reports[q.name]):
            if not ok:
                print("\n".join(report[1:]))
                break

    print(f"\n{total_ok}/{len(work)} attempts reached the right fact")
    return 0 if total_ok == len(work) else 1


if __name__ == "__main__":
    sys.exit(main())
