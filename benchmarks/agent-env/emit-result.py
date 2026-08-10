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

from arms import ARMS, arm_of  # noqa: E402

BENCH = "aws-bench"
#: The scenario a job scored, when the job did not say. Every job recorded
#: before `run-arm.sh` started stamping one was the eight-question set, so the
#: default keeps those emitting exactly as they did.
DEFAULT_SCENARIO = "ec2-multiregion"
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


def workspace_fingerprint(arm: str) -> str | None:
    """The digest prepare.py stamped when it exported this arm's workspace."""
    f = Path.home() / ".aws-bench" / "agent-env" / "workspaces" / f"{arm}.fingerprint"
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


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
    """Short HEAD of the worktree at `path`, suffixed `-dirty` when it is.

    HEAD alone is not the harness that produced a result, and every record
    written before this said so. All 45 published results carry `8d3bcb2` and
    all 45 carry the `tooling` and `workspace` blocks, which only exist as of
    `67282e8` — a later commit. They were emitted from a tree holding
    uncommitted work, and the field recorded the last commit instead, so it
    named a harness that could not have written them.

    That matters because the comparability rule is stated in terms of this
    field: two runs are the same experiment when they share a harness commit
    and a briefing SHA. A commit that silently omits the working tree cannot
    carry that claim. `-dirty` does not say what changed, but it does say the
    number is not "8d3bcb2" and stops it being read as one.
    """
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not head:
            return None
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return f"{head}-dirty" if status else head
    except (OSError, subprocess.SubprocessError):
        return None


def job_workspace(job: Path) -> str | None:
    """The workspace fingerprint this job was handed, as the run recorded it.

    Read from the job rather than from `<arm>.fingerprint`, which the next
    export overwrites: a run emitted after a later rebuild was stamped with a
    workspace it never ran against, and nothing about the record showed it.
    """
    stamped = job / "workspace"
    if stamped.is_file():
        value = stamped.read_text().strip()
        if value:
            return value
    return None


def job_harness_commit(job: Path) -> str | None:
    """The harness this job ran under, as the run recorded it.

    Read from the job rather than computed now: a record emitted after any later
    commit or edit named a harness that could not have produced it, which is the
    fault INTENTIUS/chant-bench#26 is about and the one `workspace` was already
    fixed for. Absent on jobs that predate the stamp; those fall back.
    """
    stamped = job / "harness_commit"
    if stamped.is_file():
        value = stamped.read_text().strip()
        if value:
            return value
    return None


def job_emulator(job: Path) -> str | None:
    """The emulator image that served this job's estate, as the run recorded it.

    An `image@digest`, stamped by the run script while the emulator was up.
    The compose file names a floating tag, so the tag alone cannot say which
    emulator a result was earned against once the tag moves. No fallback: a
    job without the stamp predates it, and computing one now would name
    whatever image is current rather than the one the run saw.
    """
    stamped = job / "emulator"
    if stamped.is_file():
        value = stamped.read_text().strip()
        if value:
            return value
    return None


def job_tool(job: Path) -> dict | None:
    """The tool under test and its version, as the run recorded them.

    `run-arm.sh` asks the arm before the run and stamps the answer, because by
    emit time the exported workspace may have been rebuilt on a different
    version — the same reason `workspace` is stamped rather than derived.

    Absent on every run recorded before this existed, and absent rather than
    guessed when a tool would not name itself. A missing field reads as unknown;
    a wrong one reads as fact.
    """
    stamped = job / "tool_version"
    if not stamped.is_file():
        return None
    value = stamped.read_text().strip()
    if not value:
        return None
    name, _, version = value.rpartition(" ")
    if not name or not version:
        return None
    return {"name": name, "version": version}


def scenario_of(job: Path) -> str:
    """Which question set this job scored.

    Written by the script that ran it, because the job itself cannot be asked:
    `result.json` names the tasks but not the set they came from, and deriving a
    set from its members would make a partial run look like a different
    scenario.

    This matters more than it looks. The eight-question board is over 24 trials
    and the negative set is over 6; a record that mislabels which one it is
    lands a 6-trial run beside 24-trial runs, and `validate_results.py` rejects
    the whole group rather than the one bad record.
    """
    stamped = job / "scenario"
    if stamped.is_file():
        name = stamped.read_text().strip()
        if name:
            return name
    return DEFAULT_SCENARIO


def emit(job_name: str) -> dict:
    """Build the result set for one scored job."""
    job = REPO / "jobs" / job_name
    if not job.is_dir():
        raise SystemExit(f"no such job: {job}")

    arm = arm_of(job_name)
    scenario = scenario_of(job)
    tool = job_tool(job)

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

    # How often the arm's own tooling failed it. Recorded because it is the
    # signal that would have caught the alchemy v2 mismeasurement three runs
    # earlier: v2 ran at 15-24% while no other arm exceeded 8%, and the harness
    # printed that per run without ever comparing arms. An arm fighting its
    # tooling is not a tool scoring badly, and the two look identical in a rate.
    health = re.search(r"invocations: (\d+) ok, (\d+) failed", audit_text)
    tool_ok, tool_failed = (int(health[1]), int(health[2])) if health else (None, None)

    briefing = REPO / "benchmarks" / "arms" / ARMS[arm].briefing
    mean = lambda xs: round(st.mean(xs), 2) if xs else None  # noqa: E731

    return {
        "schema": 1,
        "bench": BENCH,
        "scenario": scenario,
        "arm": arm,
        # What was actually measured. The arm names the entry on the board; this
        # names the artifact behind it, which moves independently — the chant
        # arm's published figures crossed four releases without the arm's name
        # changing once. Omitted rather than guessed when the run did not record
        # it, which is every run predating the stamp.
        **({"tool": tool} if tool else {}),
        "run": {
            "id": job_name,
            # Which workspace the trials were handed. The briefing hash and the
            # harness commit already travel with a result; this is the third
            # thing that decides what an arm could answer, and it was the one
            # nobody could name. A run whose dependencies were rebuilt is a
            # different experiment even when the code and the prompt match.
            # What the run recorded, not what the export says today — the
            # export moves and the run does not.
            "workspace": job_workspace(job) or workspace_fingerprint(arm),
            "finished_at": summary.get("finished_at"),
            # What the run recorded, not what the tree says now.
            "harness_commit": job_harness_commit(job) or git_commit(REPO),
            # Which emulator served the estate, when the run stamped it.
            # Absent on jobs that predate the stamp — never computed later.
            **({"emulator": emulator} if (emulator := job_emulator(job)) else {}),
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
        # Whether the arm could use its own tooling, as opposed to whether it
        # answered. A run can score while the tool is failing a fifth of its
        # calls, and that run is measuring the agent's patience.
        "tooling": {
            "invocations_ok": tool_ok,
            "invocations_failed": tool_failed,
            "failure_rate": (
                round(tool_failed / (tool_ok + tool_failed), 4)
                if tool_ok is not None and (tool_ok + tool_failed)
                else None
            ),
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
        # `complete` stays factual — it means every trial completed, and a
        # record saying otherwise would be a lie about the run. Validity is the
        # separate question, and it tolerates the same 10% of crashed trials the
        # audit does: a trial the agent never returned from is a trial the arm
        # did not answer, which is a score rather than missing data, and
        # `pass_rate` is already computed over every trial including those.
        CRASH_LIMIT = 0.10
        trials = record["score"]["trials"] or 0
        errored = record["score"]["errored"] or 0
        too_crashed = bool(errored) and trials and errored / trials > CRASH_LIMIT
        valid = bool(g.get("audit")) and not g.get("tool_missing") and not too_crashed

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
