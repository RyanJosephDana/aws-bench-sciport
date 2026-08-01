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

#: Text that is being written, not run. An agent's answer file explains what it
#: did, so it quotes the commands it ran — and matching the raw command string
#: counted `cat > answer.txt <<EOF … aws ec2 …` as another call to AWS. That was
#: 6% of every read on the board, and it landed hardest on the arms with the
#: fewest, where one phantom read is the difference between "answered from its
#: own state" and "did not".
HEREDOC = re.compile(r"<<-?\s*['\"]?\w+['\"]?.*", re.S)
ANSWER_FILE = re.compile(r">\s*\S*/logs/\S*")


def reads_the_account(command: str) -> bool:
    """Whether this command actually calls AWS, rather than describing one."""
    if ANSWER_FILE.search(command):
        return False
    return bool(LIVE_READ.search(HEREDOC.sub("", command)))


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


def result_event(log: Path) -> dict[str, float | int | None]:
    """Turns, duration, tokens and cost, from the agent's own final result event.

    Tokens are the thing that actually costs money, so they belong beside the
    score: an arm that answers in a third of the tokens is a third of the bill
    every time the question is asked. Input includes cache reads and creation,
    because those are billed too — quoting only fresh input would flatter
    whichever arm re-reads the most context.

    The dollar figure is the agent's own `total_cost_usd`, not tokens multiplied
    by a rate card. Cache reads bill at a tenth of fresh input and cache writes
    at more than it, so any per-token arithmetic over the sum above would be
    wrong in whichever direction the arm's caching happened to lean.

    A dict rather than a tuple: this returned four values and its missing-log
    early return still handed back two, so a trial that died before writing a
    log crashed the emitter — the one case that most needs a result.
    """
    out: dict[str, float | int | None] = {
        "turns": None, "duration_ms": None, "tokens_in": None,
        "tokens_out": None, "cost_usd": None,
    }
    if not log.exists():
        return out
    for line in log.open():
        if '"type":"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        out["turns"] = event.get("num_turns", out["turns"])
        out["duration_ms"] = event.get("duration_ms", out["duration_ms"])
        out["cost_usd"] = event.get("total_cost_usd", out["cost_usd"])
        usage = event.get("usage") or {}
        if usage:
            out["tokens_in"] = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            ) or out["tokens_in"]
            out["tokens_out"] = usage.get("output_tokens", out["tokens_out"])
    return out


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

    # Longest prefix wins. `alchemy-effect-m1` starts with `alchemy` too, and
    # `alchemy` comes first in ARMS, so every v2 run was published as a v1 one:
    # Alchemy showed 7/24 at $27.53 — v2's numbers under v1's name — while v2
    # itself still read "not yet run".
    arm = max(
        (name for name in ARMS if job_name.startswith(name)), key=len, default=None
    )
    if arm is None:
        raise SystemExit(f"cannot tell which arm {job_name} is; name it <arm>-<run>")

    summary = json.loads((job / "result.json").read_text())
    stats = summary.get("stats", {})
    # `exception_stats` sits under a per-eval key, not directly on `stats`, so
    # reading the obvious path reported {} for a run that had crashed — the
    # emitter exists to make provenance trustworthy, so it has to find it.
    # `n_errored_trials` is the authoritative count either way.
    exceptions: dict[str, list[str]] = {}
    for eval_stats in (stats.get("evals") or {}).values():
        for kind, trials in (eval_stats.get("exception_stats") or {}).items():
            exceptions.setdefault(kind, []).extend(trials if isinstance(trials, list) else [str(trials)])
    n_errored = stats.get("n_errored_trials") or 0

    by_task: dict[str, list[int]] = defaultdict(list)
    live_reads = 0
    tool_calls: list[int] = []
    turns: list[int] = []
    wall: list[float] = []
    tokens_in: list[int] = []
    tokens_out: list[int] = []
    cost: list[float] = []
    passed = trials = errored = 0

    for trial in sorted(job.iterdir()):
        # Trial directories are `<task>__<id>`. Now that a directory without a
        # reward counts as a failed trial rather than being skipped, anything
        # else the harness drops in here would inflate the denominator instead
        # of being ignored.
        if not trial.is_dir() or "__" not in trial.name:
            continue
        trials += 1
        reward = reward_of(trial)
        if reward is None:
            # A trial that ran and produced no reward: the harness crashed, the
            # container was killed, the emulator was gone. It is a trial that
            # did not pass, and it stays in the denominator.
            #
            # It used to be dropped from both sides of the ratio, which is how a
            # catastrophe read as a triumph: terraform-m1 lost 22 of 24 trials
            # to Docker exhausting its address pool and published 2 passed / 2
            # trials = 1.000. The gates recorded `complete: false` and
            # `errored_trials: 22` correctly, and the number next to them still
            # said the run was perfect. A consistency check cannot catch that —
            # 2/2 is exactly 1.000 — because the denominator was already wrong
            # when it was written down.
            errored += 1
            continue
        passed += int(reward == 1)
        by_task[re.sub(r"__\w+$", "", trial.name)].append(int(reward == 1))
        commands = list(bash_commands(trial / "agent" / "claude-code.txt"))
        tool_calls.append(len(commands))
        live_reads += sum(1 for c in commands if reads_the_account(c))
        ev = result_event(trial / "agent" / "claude-code.txt")
        if ev["turns"] is not None:
            turns.append(ev["turns"])
        if ev["duration_ms"] is not None:
            wall.append(ev["duration_ms"] / 1000)
        if ev["tokens_in"] is not None:
            tokens_in.append(ev["tokens_in"])
        if ev["tokens_out"] is not None:
            tokens_out.append(ev["tokens_out"])
        if ev["cost_usd"] is not None:
            cost.append(ev["cost_usd"])

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
            # Every trial that ran, whether or not it produced a reward. A
            # crashed trial is a trial the arm did not pass, so it belongs here
            # and in the denominator below.
            "trials": trials,
            "expected_trials": summary.get("n_total_trials"),
            # Of those, the ones that reached a verdict. `trials - completed` is
            # the damage, and it is why a rate can be low without the arm being
            # bad — read `complete` in the gates before reading the rate.
            "completed": trials - errored,
            "errored": errored,
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
            "errored_trials": n_errored,
            "complete": trials == summary.get("n_total_trials") and n_errored == 0,
        },
        "independence": {
            "account_reads": live_reads,
            "answered_from_own_state": live_reads == 0,
        },
        # What a question costs to answer with this tool, per trial. The
        # comparison is about whether a tool makes the work cheaper, so these
        # are the headline rather than a footnote.
        "effort": {
            "tool_calls": mean(tool_calls),
            "turns": mean(turns),
            "wall_seconds": mean(wall),
            "tokens_in": mean(tokens_in),
            "tokens_out": mean(tokens_out),
            # What one question cost, in dollars, as the agent itself billed it.
            # This is the number the whole comparison is for: an estate question
            # asked by a person or by another agent has a price, and the tool
            # decides most of it. Four decimals because the interesting arms
            # differ in the third.
            "cost_usd": round(st.mean(cost), 4) if cost else None,
            # What the whole run cost, so the per-question figure cannot be read
            # as the total. They are two orders of magnitude apart: a question is
            # cents, a run is most of a dollar.
            "cost_usd_run": round(sum(cost), 4) if cost else None,
        },
        # Where the evidence for this run lives, so a published number can link
        # to what produced it rather than asking to be believed.
        "logs": {
            "run": f"jobs/{job_name}/run-arm.log" if (job / "run-arm.log").exists() else None,
            "job": f"jobs/{job_name}/job.log" if (job / "job.log").exists() else None,
            "trials": f"jobs/{job_name}/<task>__<id>/agent/",
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

    refused = 0
    for job_name in args.jobs:
        record = emit(job_name)
        g = record["gates"]
        valid = bool(g.get("audit")) and bool(g.get("complete")) and not g.get("tool_missing")

        if not args.out:
            print(json.dumps(record, indent=2))
            continue

        # An invalid run is not a data point. It is a run that has to happen
        # again, and writing a file for it only creates something downstream has
        # to remember to discount. terraform-m1 is the whole argument: it was
        # written, badged invalid, and still ended up quoted as a score.
        #
        # So nothing is written and the exit is non-zero. The job directory is
        # untouched, so the evidence is still on disk for whoever wants to know
        # what went wrong.
        if not valid:
            why = []
            if not g.get("complete"):
                why.append(
                    f"{record['score']['errored']} of {record['score']['trials']} trials errored"
                )
            if not g.get("audit"):
                why.append("postflight audit failed")
            if g.get("tool_missing"):
                why.append("the arm's tooling was not found")
            print(f"  REFUSED  {job_name}: {'; '.join(why)} — re-run it", file=sys.stderr)
            refused += 1
            continue

        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"{job_name}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        print(f"  ok    {path}")

    if refused:
        print(f"\n{refused} run(s) refused; nothing was published for them", file=sys.stderr)
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
