#!/usr/bin/env python3
"""Turn a scored job into one portable result set, with its provenance attached.

A score is only meaningful alongside the conditions that produced it: which
harness, which briefing, whether the gates passed, and whether the arm needed to
read the cloud to answer. Those live in four different places right now — the
job's result.json, the audit's output, the git worktree, the briefing file — so
quoting a number means re-deriving them, and re-deriving them is how "24/24"
ends up travelling without the fact that it was the top of a 20-24 band.

This writes one JSON file per (bench, scenario, arm, run) holding all of it, in
a shape that has nothing aws-bench-specific in it. A different provider's
benchmark emitting the same shape can be presented the same way without the
presentation layer knowing anything about either.

    python3 benchmarks/agent-env/emit-result.py chant-s16-gated
    python3 benchmarks/agent-env/emit-result.py chant-s16-gated --out results/

Fields that exist because of specific ways this went wrong:

  gates       A run whose tooling broke is not a low score, it is not a
              measurement. Four runs carried "command not found" as a note and
              still printed a clean rate.
  trials      Harbor records an exception and carries on with a smaller
              denominator, so a run reported "19/23" with nothing saying the 23
              should have been 24. `expected` is what was asked for.
  independence  Whether the arm answered from state it already held. This is
              the axis the comparison is actually about, and the one that
              carries over to any other provider's benchmark unchanged.
  briefing_sha  The instruction is part of the experiment. Two runs with the
              same code and different briefings are different experiments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics as st
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from arms import ARMS  # noqa: E402

BENCH = "aws-bench"
SCENARIO = "ec2-multiregion"
#: Commands that read the provider as they answer, rather than state already held.
LIVE_READ = re.compile(r"\baws\s+(?:ec2|iam|cloudformation|s3api|sts)\b|--live\b")


def bash_commands(log: Path):
    """Every Bash command in a trial's agent log."""
    if not log.exists():
        return
    for line in log.open():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if item.get("type") == "tool_use" and item.get("name") == "Bash":
                yield " ".join(str(item.get("input", {}).get("command", "")).split())


def result_event(log: Path) -> tuple[int | None, int | None]:
    """The agent's own turn count and duration, from its final result event."""
    turns = duration = None
    if not log.exists():
        return turns, duration
    for line in log.open():
        if '"type":"result"' in line:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            turns = event.get("num_turns", turns)
            duration = event.get("duration_ms", duration)
    return turns, duration


def reward_of(trial: Path) -> float | None:
    """The trial's reward, wherever in its result.json it sits."""
    result = trial / "result.json"
    if not result.exists():
        return None

    def find(node):
        if isinstance(node, dict):
            if isinstance(node.get("reward"), (int, float)):
                return node["reward"]
            for value in node.values():
                found = find(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find(value)
                if found is not None:
                    return found
        return None

    try:
        return find(json.load(result.open()))
    except (OSError, ValueError):
        return None


def git_commit(path: Path) -> str | None:
    """Short HEAD of the worktree at `path`, or None if it is not one."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def emit(job_name: str) -> dict:
    """Build the result set for one scored job."""
    job = REPO / "jobs" / job_name
    if not job.is_dir():
        raise SystemExit(f"no such job: {job}")

    arm = next((name for name in ARMS if job_name.startswith(name)), None)
    if arm is None:
        raise SystemExit(f"cannot tell which arm {job_name} is; name it <arm>-<run>")

    summary = json.loads((job / "result.json").read_text())
    stats = summary.get("stats", {})
    exceptions = stats.get("exception_stats") or {}

    by_task: dict[str, list[int]] = defaultdict(list)
    live_reads = 0
    tool_calls: list[int] = []
    turns: list[int] = []
    wall: list[float] = []
    passed = trials = 0

    for trial in sorted(job.iterdir()):
        if not trial.is_dir():
            continue
        reward = reward_of(trial)
        if reward is None:
            continue
        trials += 1
        passed += int(reward == 1)
        by_task[re.sub(r"__\w+$", "", trial.name)].append(int(reward == 1))
        commands = list(bash_commands(trial / "agent" / "claude-code.txt"))
        tool_calls.append(len(commands))
        live_reads += sum(1 for c in commands if LIVE_READ.search(c))
        turn_count, duration = result_event(trial / "agent" / "claude-code.txt")
        if turn_count is not None:
            turns.append(turn_count)
        if duration is not None:
            wall.append(duration / 1000)

    # The gate is the audit's own verdict, not a re-implementation of it.
    audit = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "audit.py"), str(job)],
        capture_output=True, text=True,
    )
    audit_text = audit.stdout + audit.stderr
    tool_missing = "could not find" in audit_text

    briefing = REPO / "benchmarks" / "arms" / ARMS[arm].briefing
    mean = lambda xs: round(st.mean(xs), 2) if xs else None  # noqa: E731

    return {
        "schema": 1,
        "bench": BENCH,
        "scenario": SCENARIO,
        "arm": arm,
        "run": {
            "id": job_name,
            "finished_at": summary.get("finished_at"),
            "harness_commit": git_commit(REPO),
        },
        "agent": {
            "name": "claude-code",
            "model": "claude-haiku-4-5-20251001",
            "k": 3,
        },
        "score": {
            "trials": trials,
            # What was asked for. A crashed trial shrinks `trials` silently, and
            # the printed rate is then over the survivors.
            "expected_trials": summary.get("n_total_trials"),
            "passed": passed,
            "pass_rate": round(passed / trials, 4) if trials else None,
            "by_task": dict(sorted(by_task.items())),
        },
        "gates": {
            # An invalid run is not a low score. Anything false here means the
            # numbers above describe something other than this arm.
            "audit": audit.returncode == 0,
            "tool_missing": tool_missing,
            "exceptions": {k: len(v) for k, v in exceptions.items()},
            "complete": trials == summary.get("n_total_trials"),
        },
        "independence": {
            "account_reads": live_reads,
            "answered_from_own_state": live_reads == 0,
        },
        "effort": {
            "tool_calls": mean(tool_calls),
            "turns": mean(turns),
            "wall_seconds": mean(wall),
        },
        "briefing": {
            "path": f"benchmarks/arms/{ARMS[arm].briefing}",
            "sha256": hashlib.sha256(briefing.read_bytes()).hexdigest()[:12]
            if briefing.exists() else None,
        },
        "reproduce": f"benchmarks/arms/{ARMS[arm].source}/REPRODUCE.md",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="+", help="job name(s) under jobs/")
    parser.add_argument("--out", type=Path, help="directory to write <job>.json into")
    args = parser.parse_args()

    for job_name in args.jobs:
        record = emit(job_name)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            path = args.out / f"{job_name}.json"
            path.write_text(json.dumps(record, indent=2) + "\n")
            valid = all(
                v for k, v in record["gates"].items() if isinstance(v, bool) and k != "tool_missing"
            ) and not record["gates"]["tool_missing"]
            print(f"  {'ok  ' if valid else 'INVALID'}  {path}")
        else:
            print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
