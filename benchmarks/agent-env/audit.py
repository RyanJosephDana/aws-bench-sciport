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

from arms import ARMS  # noqa: E402

# The shell's own report that the tool is not installed. This is the signature
# the scenario-1 runs were full of: 22 `npx: command not found` in the CDK arm,
# 30+ `alchemy: command not found` in the Alchemy arm.
MISSING = re.compile(r"command not found|No such file or directory|executable file not found")


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
                if source == arm.source:
                    return name
    return next((name for name in ARMS if job.name.startswith(name)), None)


def audit_trial(trial: Path, pattern: re.Pattern[str]) -> TrialAudit | None:
    """Count one trial's tool calls, or None if it has no agent log."""
    log = trial / "agent" / "claude-code.txt"
    if not log.exists():
        return None
    reward_file = trial / "verifier" / "reward.txt"
    reward = reward_file.read_text().strip() if reward_file.exists() else "-"

    ok = missing = failed = 0
    for command, output, is_error in bash_calls(log):
        if not pattern.search(command):
            continue
        head = output[:2000]
        if MISSING.search(head):
            missing += 1
        elif is_error or re.match(r"\s*Exit code [1-9]", head):
            failed += 1
        else:
            ok += 1
    return TrialAudit(trial.name, reward, ok, missing, failed)


def audit_job(job: Path) -> tuple[bool, list[str]]:
    """Audit every trial in a job. (all trials used the tool, report lines)."""
    name = arm_of(job)
    if name is None:
        return True, [f"{job.name}: no arm identified, skipped"]

    pattern = re.compile(ARMS[name].tool_pattern)
    audits = [a for t in sorted(job.iterdir()) if t.is_dir() and (a := audit_trial(t, pattern))]
    if not audits:
        return True, [f"{job.name} [{name}]: no trial logs, skipped"]

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

    if unused:
        scored = [a for a in unused if a.reward not in {"-", "0.0"}]
        lines.append(
            f"    {len(unused)} of {len(audits)} trials answered without the tool"
            + (f", {len(scored)} of them scored" if scored else "")
        )
    return not unused, lines


def main() -> int:
    """Audit the given jobs and report trials that never used the tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="+", type=Path, help="job directories to audit")
    args = parser.parse_args()

    failed = []
    for job in args.jobs:
        if not job.is_dir():
            print(f"{job}: not a directory")
            failed.append(job.name)
            continue
        ok, lines = audit_job(job)
        print("\n".join(lines))
        print()
        if not ok:
            failed.append(job.name)

    if failed:
        print(f"TOOLING NOT EXERCISED: {', '.join(failed)} — these are not tool comparisons")
        return 1
    print("every trial used its arm's tooling")
    return 0


if __name__ == "__main__":
    sys.exit(main())
