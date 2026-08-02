#!/usr/bin/env python3
"""Extract what each arm actually ran, per question, for publication.

The scores say which tools answered. They do not say *how*, and the how is the
more interesting half: one arm answers "which security groups are unused" with a
single query, another walks ids between state records by hand, a third joins a
synthesized template to a CloudFormation call because it keeps no state of its
own. Those are the differences the comparison exists to show, and they are
sitting unread in every trial transcript.

Writes one file per run holding, for each question, the commands one trial ran
and what it concluded — chosen from a passing trial where there is one, since
the point is to show how the tool answers rather than how an agent flails.

    python3 benchmarks/agent-env/emit-transcript.py chant-m1 --out ../chant-bench/transcripts

Kept separate from the result set so the contract stays small: a result is what
happened, a transcript is how.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from arms import arm_of  # noqa: E402

#: Noise every trial emits that says nothing about how the tool was used.
BORING = re.compile(r"^\s*(cd\s+\S+\s*&&\s*)?(ls|pwd|cat\s+/etc|echo)\b")


def commands(log: Path) -> list[str]:
    """Every Bash command a trial ran, in order, deduped and trimmed."""
    out: list[str] = []
    if not log.exists():
        return out
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
                cmd = " ".join(str(item.get("input", {}).get("command", "")).split())
                if cmd and not BORING.match(cmd) and cmd not in out:
                    out.append(cmd)
    return out


def answer(trial: Path) -> str:
    """The trial's final answer, trimmed to something quotable."""
    path = trial / "agent" / "agent-output.txt"
    if not path.exists():
        return ""
    text = path.read_text(errors="replace").strip()
    # The tail is the conclusion; the head is usually restated question.
    return text[-700:].strip()


def reward(trial: Path) -> float | None:
    path = trial / "result.json"
    if not path.exists():
        return None

    def find(node):
        if isinstance(node, dict):
            if isinstance(node.get("reward"), (int, float)):
                return node["reward"]
            for v in node.values():
                got = find(v)
                if got is not None:
                    return got
        elif isinstance(node, list):
            for v in node:
                got = find(v)
                if got is not None:
                    return got
        return None

    try:
        return find(json.load(path.open()))
    except (OSError, ValueError):
        return None


def emit(job_name: str) -> dict:
    job = REPO / "jobs" / job_name
    if not job.is_dir():
        raise SystemExit(f"no such job: {job}")

    arm = arm_of(job_name)
    by_task: dict[str, dict] = {}

    for trial in sorted(job.iterdir()):
        if not trial.is_dir() or "__" not in trial.name:
            continue
        task = re.sub(r"__\w+$", "", trial.name)
        got = reward(trial)
        if got is None:
            continue
        entry = {
            "trial": trial.name,
            "passed": got == 1,
            "commands": commands(trial / "agent" / "claude-code.txt"),
            "answer": answer(trial),
        }
        # Prefer a passing trial: the page is about how the tool answers, not
        # about how an agent recovers when it does not.
        current = by_task.get(task)
        if current is None or (entry["passed"] and not current["passed"]):
            by_task[task] = entry

    return {"schema": 1, "run": job_name, "arm": arm, "by_task": by_task}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for job_name in args.jobs:
        record = emit(job_name)
        path = args.out / f"{job_name}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        n = sum(len(v["commands"]) for v in record["by_task"].values())
        print(f"  ok    {path.name}  ({len(record['by_task'])} question(s), {n} commands)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
