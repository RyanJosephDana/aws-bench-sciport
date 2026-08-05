#!/usr/bin/env python3
"""Check, after a job, that every trial actually used the arm's tooling.

Preflight proves the tools work before a run. This proves they were used during
it. A trial whose tool is missing does not error — the agent shrugs, greps the
state files with jq, answers anyway, and the verifier hands out a reward. The
job's result.json looks identical either way, which is how the scenario-1 CDK and
Alchemy arms came to be reported as tool comparisons when no `cdk` or `alchemy`
command ever ran in them.

    python3 benchmarks/agent-env/audit.py jobs/cdk-s1-rerun
    python3 benchmarks/agent-env/audit.py jobs/*            # every job

Exit status is nonzero if any trial never got a successful command out of its
arm's tool, so it can gate publishing a number.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from arms import ARMS, arm_of as arm_by_name  # noqa: E402

# The shell's own report that the tool is not installed. This is the signature
# the scenario-1 runs were full of: 22 `npx: command not found` in the CDK arm,
# 30+ `alchemy: command not found` in the Alchemy arm.
MISSING = re.compile(r"command not found|No such file or directory|executable file not found")

# Which command a shell says is absent. `bash: line 7: column: command not
# found` names it, and the name is what decides whether the ARM's tooling was
# missing or some unrelated utility the agent reached for. Without this, a
# trial that piped a working `chant` into an absent `column` was recorded as
# chant not being installed — and it read as the arm being broken for four runs.
MISSING_NAME = re.compile(r"(?:^|:\s*)([\w./-]+): (?:command not found|not found)", re.M)

# The tool was killed rather than having failed on its own terms. 137 is
# SIGKILL, which on a shared Docker VM means the kernel reclaimed memory: `cdk
# ls` runs a synth that peaks near 1.4GB and six concurrent trials overflowed an
# 8GB VM. Worth separating from an ordinary nonzero exit, because the tool was
# working and the box was too small — and because the agent then answered from
# files the deploy had left behind and still scored, so the run looked clean.
KILLED = re.compile(r"Exit code 137|\bKilled\b|OOMKilled|out of memory", re.I)

# A command that reads the AWS account as it answers, rather than reading state
# the arm already holds. Every briefing permits this as a last resort for
# runtime values, so it is not a failure — but it is the thing the comparison is
# about, and it should be counted rather than assumed. An arm that answers
# without it answered from its own state; one that leans on it is being carried
# by the AWS CLI.
LIVE_READ = re.compile(r"\baws\s+(?:ec2|iam|cloudformation|s3api|sts)\b|--live\b")


@dataclass
class TrialAudit:
    """What one trial got out of its arm's tool."""

    trial: str
    reward: str
    tool_ok: int
    """Commands using the arm's tool that succeeded."""
    tool_missing: int
    """Commands that failed because the tool is not installed."""
    tool_failed: int
    """Commands using the tool that ran but exited nonzero."""
    live_reads: int
    """Commands that read the AWS account directly rather than the arm's state."""
    tool_killed: int
    """Tool calls the kernel killed — the box was too small, not the tool."""

    @property
    def used_tool(self) -> bool:
        """Whether the arm's tool answered at least once."""
        return self.tool_ok > 0


def bash_calls(path: Path):
    """Yield (command, output, is_error) for each Bash call in a trial log."""
    pending: dict[str, str] = {}
    with path.open() as handle:
        for line in handle:
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
                    pending[item["id"]] = item.get("input", {}).get("command", "")
                elif item.get("type") == "tool_result":
                    command = pending.pop(item.get("tool_use_id"), None)
                    if command is None:
                        continue
                    raw = item.get("content")
                    if isinstance(raw, list):
                        text = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
                    else:
                        text = str(raw)
                    yield command, text, bool(item.get("is_error"))


def arm_of(job: Path) -> str | None:
    """Identify the arm from the job's mount, falling back to its name.

    The mount is what actually decides which tool the trial had, so it beats
    guessing from a job name someone typed.
    """
    config = job / "config.json"
    if config.exists():
        try:
            mounts = json.loads(config.read_text())["environment"]["mounts"]
        except (KeyError, json.JSONDecodeError):
            mounts = []
        for mount in mounts:
            source = Path(str(mount.get("source", ""))).name
            for name, arm in ARMS.items():
                # The arm's source directory, or the exported workspace, which
                # is named for the arm rather than its source. Only the second
                # is ever mounted, so comparing against `arm.source` alone made
                # this whole branch dead code — every job fell through to the
                # name guess below, including the one case the guess gets wrong.
                if source in (arm.source, name):
                    return name
    # Longest match, via the shared helper. `next()` returns the first, and
    # `alchemy` precedes `alchemy-effect` in ARMS, so every alchemy-effect run
    # was audited as v1. Harmless so far only because the two arms happen to
    # share a tool_pattern; the day they do not, the audit would be checking
    # for the wrong binary and reporting a clean run.
    try:
        return arm_by_name(job.name)
    except SystemExit:
        return None


def audit_trial(trial: Path, pattern: re.Pattern[str], tool_names: set[str]) -> TrialAudit | None:
    """Count one trial's tool calls, or None if it has no agent log."""
    log = trial / "agent" / "claude-code.txt"
    if not log.exists():
        return None
    reward_file = trial / "verifier" / "reward.txt"
    reward = reward_file.read_text().strip() if reward_file.exists() else "-"

    ok = missing = failed = killed = 0
    live = 0
    for command, output, is_error in bash_calls(log):
        if LIVE_READ.search(command):
            live += 1
        if not pattern.search(command):
            continue
        head = output[:2000]
        named = {m.group(1).rsplit("/", 1)[-1] for m in MISSING_NAME.finditer(head)}
        # Only the arm's own binary counts as missing tooling. Anything else the
        # shell could not find is the agent reaching for a utility, which is a
        # different fact and belongs in the failed bucket.
        if named and not (named & tool_names):
            failed += 1
        elif MISSING.search(head) and any(n in head for n in tool_names):
            missing += 1
        elif MISSING.search(head):
            # The shell could not find something, but not this arm's tooling.
            # `cat: '': No such file or directory` is an agent expanding an empty
            # variable, and it invalidated a whole bare run as "tooling not
            # found" — bare has no binary to be missing, its tool is the CLI.
            # A fumbled command is a failed call, which is a different fact.
            failed += 1
        elif KILLED.search(head):
            killed += 1
            failed += 1
        elif is_error or re.match(r"\s*Exit code [1-9]", head):
            failed += 1
        else:
            ok += 1
    return TrialAudit(trial.name, reward, ok, missing, failed, live, killed)


def audit_job(job: Path) -> tuple[bool, list[str], list[str]]:
    """Audit every trial in a job. (run is a measurement, report lines, why not).

    The third value is why the run was refused, in the refusal's own words.
    Four different faults reach this gate and only one of them is "the tool
    never ran"; reporting all four under that heading sent a reader hunting
    for a missing CLI when a trial had timed out with 311 successful
    invocations behind it. Empty when the run stands.
    """
    name = arm_of(job)
    if name is None:
        return True, [f"{job.name}: no arm identified, skipped"], []

    pattern = re.compile(ARMS[name].tool_pattern)
    # The binaries that ARE this arm's tooling, taken from the pattern itself so
    # the two cannot drift apart.
    # The names of the binaries this arm is supposed to use, read out of its
    # tool_pattern. Escapes have to go first: `\w+` over `\baws\s+(ec2|iam)\b`
    # returns {'b', 'baws', 's', ...} — the tool name glued to the `\b` in front
    # of it, plus single letters from the escapes. So `aws` was never in the set,
    # and `b` and `s` are substrings of nearly any text. Both missing-tool checks
    # were reading that set: one could never match the real binary, and the other
    # matched everything.
    literal = re.sub(r"\\[a-zA-Z]", " ", ARMS[name].tool_pattern)
    tool_names = {w for w in re.findall(r"[A-Za-z][\w.-]+", literal)} | {name}
    audits = [
        a for t in sorted(job.iterdir()) if t.is_dir() and (a := audit_trial(t, pattern, tool_names))
    ]
    if not audits:
        return True, [f"{job.name} [{name}]: no trial logs, skipped"], []

    unused = [a for a in audits if not a.used_tool]
    lines = [
        f"{job.name} [{name}]  {len(audits)} trials, "
        f"{len(audits) - len(unused)} used {name}'s tooling"
    ]
    for audit in audits:
        if audit.used_tool and not audit.tool_missing:
            continue
        note = []
        if audit.tool_missing:
            note.append(f"{audit.tool_missing} call(s) found no such tool")
        if audit.tool_failed:
            note.append(f"{audit.tool_failed} errored")
        if not audit.used_tool:
            note.append("never ran successfully")
        lines.append(f"    reward={audit.reward:<4} {audit.trial}: {', '.join(note)}")

    # A trial or two out of 24 answering some other way does not stop the run
    # describing the tool; it is one agent taking one detour. Voiding on any
    # fallback at all threw away cdk-m2 — twenty-four completed trials — for a
    # single one, and it would have made alchemy-effect permanently unmeasurable:
    # its CLI deadlocks on one entrypoint, agents occasionally give up on it, and
    # a tool broken enough to be abandoned should score badly rather than vanish.
    #
    # So the threshold is a share, not a count. Above it the run genuinely is not
    # a comparison; below it the fallbacks are noted and the run stands.
    FALLBACK_LIMIT = 0.10
    mostly_untooled = bool(unused) and len(unused) / len(audits) > FALLBACK_LIMIT
    if unused:
        scored = [a for a in unused if a.reward not in {"-", "0.0"}]
        lines.append(
            f"    {len(unused)} of {len(audits)} trials answered without the tool"
            + (f", {len(scored)} of them scored" if scored else "")
            + (
                " — too many for this to be a measurement of the tool"
                if mostly_untooled
                else f" ({len(unused) / len(audits):.0%}, within the {FALLBACK_LIMIT:.0%} the gate allows)"
            )
        )

    # A `command not found` for the arm's own CLI fails the job, even when the
    # trial went on to score. It means that trial answered some other way, so it
    # is not a measurement of this arm — and a run containing one is not a
    # comparison. This was a note for four consecutive runs while every one of
    # them was reported as clean, which is the same "tooling broke and nobody
    # noticed" this whole gate exists to catch.
    broken = [a for a in audits if a.tool_missing]
    if broken:
        lines.append(
            f"    {len(broken)} of {len(audits)} trials could not find {name} on PATH"
            f" — the run is not a measurement of this arm"
        )

    # How healthy the arm's own tooling was across the whole run, not just
    # whether each trial got one call through. A trial that ran the tool five
    # times, had three die, and answered anyway passed every check above — so
    # did four CDK trials whose only `cdk ls` was OOM-killed and which then
    # answered from files the deploy had left behind. A tool failing a third of
    # the time is not a tool being measured.
    calls_ok = sum(a.tool_ok for a in audits)
    calls_failed = sum(a.tool_failed for a in audits)
    calls_killed = sum(a.tool_killed for a in audits)
    total_calls = calls_ok + calls_failed + sum(a.tool_missing for a in audits)
    if total_calls:
        rate = calls_failed / total_calls
        lines.append(
            f"    {name} invocations: {calls_ok} ok, {calls_failed} failed"
            + (f" ({calls_killed} killed)" if calls_killed else "")
            + f" — {rate:.0%} failure rate"
        )

    # Killed at all is a hard stop: the tool was working and the machine was
    # not, so nothing in the run describes the tool.
    unhealthy = calls_killed > 0
    if unhealthy:
        lines.append(
            f"    {calls_killed} {name} call(s) were killed by the kernel — the environment"
            f" starved the tool, so this run does not measure it"
        )
    # A high failure rate is the same conclusion reached more slowly.
    elif total_calls >= 10 and calls_failed / total_calls > 0.25:
        unhealthy = True
        lines.append(
            f"    {name} failed {calls_failed} of {total_calls} invocations —"
            f" too unhealthy for the run to describe the tool"
        )

    # Not a gate: every briefing allows a raw account read for runtime values.
    # Reported because it is the whole point of the comparison — an arm that
    # never needs it answered from its own state.
    leaned = [a for a in audits if a.live_reads]
    total_live = sum(a.live_reads for a in audits)
    lines.append(
        f"    account reads: {total_live} call(s) across {len(leaned)}/{len(audits)} trials"
        + (" — answered entirely from its own state" if not leaned else "")
    )

    # An agent that died did not answer the question. Harbor records the
    # exception and carries on with a smaller denominator, so the job still
    # prints a rate; that rate is over the trials that survived.
    # A share, not a count — the same call the fallback gate already makes, and
    # for the same reason. One timed-out trial voided alchemy-effect-h1 at
    # 17/24, its best result ever, with 311 successful invocations behind it.
    # Discarding 23 good trials to avoid mis-stating one is the wrong trade, and
    # the record already carries `errored_trials` so nothing is hidden by it.
    #
    # Above the limit the run genuinely cannot be read: at 1 of 6 the denominator
    # moves 17% and a rate over the survivors is not a rate over the run.
    CRASH_LIMIT = 0.10
    crashed = crashed_trials(job)
    too_crashed = bool(crashed) and len(crashed) / len(audits) > CRASH_LIMIT
    if crashed:
        lines.append(
            f"    {len(crashed)} trial(s) ended in an agent exception: {', '.join(crashed[:3])}"
            + (
                " — too many for the rate to describe the run"
                if too_crashed
                else f" ({len(crashed) / len(audits):.0%}, within the {CRASH_LIMIT:.0%} the gate allows;"
                " they count as trials the arm did not answer)"
            )
        )

    why: list[str] = []
    if mostly_untooled:
        why.append(f"{len(unused)} of {len(audits)} trials answered without {name}")
    if broken:
        why.append(f"{name} was not on PATH in {len(broken)} trial(s)")
    if unhealthy:
        why.append(
            f"{calls_killed} {name} call(s) killed by the kernel"
            if calls_killed
            else f"{name} failed {calls_failed} of {total_calls} invocations"
        )
    if too_crashed:
        why.append(
            f"{len(crashed)} of {len(audits)} trials ended in an agent exception"
        )

    return not why, lines, why


def crashed_trials(job: Path) -> list[str]:
    """Trials whose agent exited nonzero or raised, by name.

    Harbor writes the exception into the trial's result.json and keeps going.
    Nothing downstream re-reads it, so a crashed trial silently shrinks the
    denominator and the printed pass rate is over the survivors.
    """
    # The job's own result.json is the authority: Harbor tallies every raised
    # exception under `exception_stats`, keyed by type, with the trial names.
    out: list[str] = []
    result = job / "result.json"
    if result.exists():
        try:
            stats = json.loads(result.read_text()).get("stats", {})
            # Nested under a per-eval key, not directly on `stats`.
            for eval_stats in (stats.get("evals") or {}).values():
                for trials in (eval_stats.get("exception_stats") or {}).values():
                    out.extend(trials if isinstance(trials, list) else [str(trials)])
        except (OSError, ValueError):
            pass
    if out:
        return sorted(set(out))
    # Fall back to the per-trial record, which carries `exception_info` when the
    # job summary is missing or was written by an older harness.
    for trial in sorted(job.iterdir()):
        if not trial.is_dir() or not (trial / "result.json").exists():
            continue
        try:
            info = json.loads((trial / "result.json").read_text()).get("exception_info")
        except (OSError, ValueError):
            continue
        if info:
            out.append(trial.name)
    return sorted(set(out))


def main() -> int:
    """Audit the given jobs and report trials that never used the tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="+", type=Path, help="job directories to audit")
    args = parser.parse_args()

    failed = []
    for job in args.jobs:
        if not job.is_dir():
            print(f"{job}: not a directory")
            failed.append((job.name, ["not a directory"]))
            continue
        ok, lines, why = audit_job(job)
        print("\n".join(lines))
        print()
        if not ok:
            failed.append((job.name, why))

    if failed:
        print("REFUSED — these runs do not measure their arm:")
        for job_name, why in failed:
            print(f"  {job_name}: {'; '.join(why) or 'refused'}")
        return 1
    # Not "every trial used its arm's tooling" — that stopped being what a pass
    # means once the gate started tolerating a few fallbacks, and a summary that
    # overstates is exactly what this gate exists to prevent. The per-run lines
    # above carry the actual counts.
    print("every run is a measurement of its arm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
