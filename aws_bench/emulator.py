"""Floci emulator mode: run aws-bench against a local AWS emulator.

Activated with ``AWS_BENCH_EMULATOR=floci`` (or the ``--emulator floci`` CLI
option, which sets that variable). In this mode no AWS Organizations accounts
are provisioned and no STS calls are made — every account tag maps to the
emulator's single account and all AWS traffic (scenario deploy, agent, and
verifier) is pointed at the Floci endpoint.

Seams, matched to the live-AWS flow:

* ``assume_role_for_script`` returns :func:`static_credentials` instead of a
  chained STS assume-role.
* ``AwsBenchSingleStepTrial._staged_credentials`` adds
  :func:`container_endpoint_env` so the agent's and verifier's SDK/CLI calls
  resolve to Floci instead of real AWS.
* ``env``-level provisioning boots the pinned Floci image instead of creating
  member accounts.
* Introspection verifiers get their ``judge.toml`` model rewritten from
  ``bedrock/<model>`` to ``anthropic/<model>`` (see :func:`rewrite_judge_model`)
  because there is no real Bedrock behind the emulator; the same Claude model
  judges over the Anthropic API.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

#: The emulator's single AWS account. Floci reports 000000000000 everywhere.
ACCOUNT_ID = "000000000000"

#: Placeholder credentials — Floci does not verify request signatures.
ACCESS_KEY_ID = "test"
SECRET_ACCESS_KEY = "test"
SESSION_TOKEN = "test"

#: Floci endpoint as seen from the host (the aws-bench process itself).
DEFAULT_HOST_ENDPOINT = "http://localhost:4566"

#: Floci endpoint as seen from inside agent/verifier/deploy containers.
#: host.docker.internal resolves on Docker Desktop and on Linux when the
#: container is run with ``--add-host=host.docker.internal:host-gateway``.
DEFAULT_CONTAINER_ENDPOINT = "http://host.docker.internal:4566"

#: The published integration image (fork main + the CFN/EC2 parity series).
DEFAULT_IMAGE = "ghcr.io/lex00/floci:awsbench"

#: File holding the Claude Code OAuth token (``claude setup-token`` output).
DEFAULT_CLAUDE_TOKEN_FILE = "~/.anthropic"

_JUDGE_BEDROCK_RE = re.compile(r'^(\s*judge\s*=\s*")bedrock/(?:us\.)?([^"]+)(")', re.MULTILINE)


def is_active() -> bool:
    """True when Floci emulator mode is enabled for this process."""
    return os.environ.get("AWS_BENCH_EMULATOR", "").lower() == "floci"


def host_endpoint() -> str:
    """The Floci endpoint for the aws-bench process itself."""
    return os.environ.get("AWS_BENCH_EMULATOR_ENDPOINT", DEFAULT_HOST_ENDPOINT)


def container_endpoint() -> str:
    """The Floci endpoint for workloads inside containers aws-bench launches."""
    return os.environ.get(
        "AWS_BENCH_EMULATOR_CONTAINER_ENDPOINT", DEFAULT_CONTAINER_ENDPOINT
    )


def static_credentials() -> dict[str, str]:
    """Placeholder credentials in the ``chain_assume_role`` return shape.

    ``assume_role_for_script`` / ``chain_assume_role`` return env-style keys
    (``AWS_ACCESS_KEY_ID`` etc.) that ``build_aws_credentials_file`` and
    ``env_credentials_dict_to_session`` consume directly.
    """
    return {
        "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
        "AWS_SESSION_TOKEN": SESSION_TOKEN,
    }


def host_env() -> dict[str, str]:
    """Env for host-side boto3/CLI use (scenario management, placeholders)."""
    return {
        "AWS_ENDPOINT_URL": host_endpoint(),
        "AWS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY,
        "AWS_SESSION_TOKEN": SESSION_TOKEN,
    }


def container_endpoint_env() -> dict[str, str]:
    """Env appended to a launched container so its AWS traffic hits Floci.

    Credentials still come from the staged ``~/.aws/credentials`` profiles;
    only the endpoint needs overriding here.
    """
    return {"AWS_ENDPOINT_URL": container_endpoint()}


def account_mapping(tags: list[str]) -> dict[str, str]:
    """Map every scenario account tag to the emulator's single account."""
    return dict.fromkeys(tags, ACCOUNT_ID)


def claude_oauth_token() -> str | None:
    """The Claude Code OAuth token for the agent under test, if available.

    Prefers an existing ``CLAUDE_CODE_OAUTH_TOKEN``; otherwise reads the token
    file (``AWS_BENCH_CLAUDE_TOKEN_FILE``, default ``~/.anthropic``). Harbor's
    claude-code agent forwards the variable into the agent container, where it
    authenticates Claude Code against the user's subscription — no API key.
    """
    ambient = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if ambient:
        return ambient
    path = os.path.expanduser(
        os.environ.get("AWS_BENCH_CLAUDE_TOKEN_FILE", DEFAULT_CLAUDE_TOKEN_FILE)
    )
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        return None
    return token or None


def prime_process_env() -> None:
    """Export emulator-mode env into this process before agents/SDKs read it.

    Idempotent. Sets ``CLAUDE_CODE_OAUTH_TOKEN`` from the token file so
    Harbor's claude-code agent can forward it into the agent container, and
    forces the host AWS env (endpoint + placeholder credentials) so every
    boto3 session in this process resolves to Floci — deliberately clobbering
    any ambient real-AWS profile, so emulator mode can never touch a real
    account.
    """
    if not is_active():
        return
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        token = claude_oauth_token()
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    os.environ.update(host_env())
    os.environ.pop("AWS_PROFILE", None)
    os.environ.pop("AWS_DEFAULT_PROFILE", None)


#: Replacement verifier entrypoint for introspection tasks in emulator mode.
JUDGE_TEST_SH = """#!/bin/bash
# Floci emulator mode: judge with Claude Code (subscription OAuth token)
# instead of rewardkit -> Bedrock. Same rubric, same reward contract
# (/logs/verifier/reward.txt).
set -ex

uv run --no-project /tests/resolve_placeholders.py
uv run --no-project /tests/claude_judge.py /tests
"""

#: Self-contained (stdlib-only) Claude Code judge staged next to test.sh.
CLAUDE_JUDGE_PY = '''#!/usr/bin/env python3
"""Judge an introspection task with Claude Code instead of rewardkit/Bedrock.

Reads the task's judge.toml (criteria, files, prompt template), asks Claude
for a per-criterion boolean verdict via ``claude -p``, and writes the Harbor
reward contract: /logs/verifier/reward.txt with 1.0 (all criteria pass) or
0.0. The raw judge response is kept at /logs/verifier/claude-judge.json.

Exit codes: 0 = judged (either reward); nonzero = judging itself failed.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

VERIFIER_DIR = Path(os.environ.get("AWS_BENCH_VERIFIER_DIR", "/logs/verifier"))


def find_claude() -> str:
    """Locate the claude binary regardless of which user installed it.

    Harbor installs Claude Code as the agent user (install.sh puts it in that
    user's ``~/.local/bin``); the verifier may run as a different user whose
    PATH does not include it.
    """
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    candidates = [
        *glob.glob("/home/*/.local/bin/claude"),
        "/root/.local/bin/claude",
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ]
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(f"claude binary not found on PATH or in {candidates}")


def build_prompt(tests: Path, judge: dict, criteria: list[dict]) -> str:
    template = (tests / judge["prompt_template"]).read_text()
    crit_lines = "\\n".join(
        f"- {c['name']} ({c.get('type', 'binary')}): {c['description']}" for c in criteria
    )
    prompt = template.replace("{criteria}", "## Criteria\\n\\n" + crit_lines)
    parts = [prompt, "\\n\\n# Files\\n"]
    for name in judge.get("files", []):
        path = Path(name)
        parts.append(f"\\n## {path.name}\\n```\\n{path.read_text()}\\n```\\n")
    names = ", ".join(f'"{c["name"]}": true|false' for c in criteria)
    parts.append(
        "\\nRespond with ONLY a JSON object mapping each criterion name to a "
        f"boolean, e.g. {{{names}}}. No prose outside the JSON."
    )
    return "".join(parts)


def extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        matches = re.findall(r"\\{[^{}]*\\}", text, re.DOTALL)
        for candidate in reversed(matches):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON verdict found in judge output: {text[:500]!r}")


def main() -> int:
    tests = Path(sys.argv[1] if len(sys.argv) > 1 else "/tests")
    config = tomllib.loads((tests / "judge.toml").read_text())
    judge = config["judge"]
    criteria = config.get("criterion", [])
    prompt = build_prompt(tests, judge, criteria)

    model = os.environ.get("AWS_BENCH_JUDGE_MODEL", "claude-sonnet-4-5")
    proc = subprocess.run(
        [find_claude(), "-p", "--model", model, "--output-format", "json"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=540,
    )
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    (VERIFIER_DIR / "claude-judge.json").write_text(
        json.dumps({"stdout": proc.stdout, "stderr": proc.stderr, "rc": proc.returncode})
    )
    if proc.returncode != 0:
        print(f"claude -p failed (rc={proc.returncode}): {proc.stderr[:500]}", file=sys.stderr)
        return 1

    envelope = json.loads(proc.stdout)
    verdict = extract_json(envelope.get("result", ""))
    passed = bool(criteria) and all(verdict.get(c["name"]) is True for c in criteria)
    (VERIFIER_DIR / "reward.txt").write_text("1.0" if passed else "0.0")
    print(f"claude judge verdict: {verdict} -> reward {'1.0' if passed else '0.0'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def stage_task_for_emulator(task_dir: Path, staging_root: Path) -> Path:
    """Return an emulator-adapted copy of ``task_dir`` (or ``task_dir`` itself).

    Mutation tasks (no ``tests/ground_truth.json``) pass through untouched —
    their boto3 verifiers work against Floci as-is. Introspection tasks are
    copied under ``staging_root`` with the rewardkit ``test.sh`` swapped for the
    Claude Code judge, so the original dataset checkout is never modified.
    """
    if not (task_dir / "tests" / "ground_truth.json").is_file():
        return task_dir
    staged = staging_root / "emulator-staged" / task_dir.parent.name / task_dir.name
    staged.parent.mkdir(parents=True, exist_ok=True)
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(task_dir, staged)
    tests = staged / "tests"
    (tests / "test.sh").write_text(JUDGE_TEST_SH)
    (tests / "claude_judge.py").write_text(CLAUDE_JUDGE_PY)
    judge_toml = tests / "judge.toml"
    if judge_toml.is_file():
        judge_toml.write_text(rewrite_judge_model(judge_toml.read_text()))
    return staged


def rewrite_judge_model(judge_toml_body: str) -> str:
    """Rewrite a ``judge.toml`` Bedrock model id to its Anthropic API twin.

    ``bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0`` becomes
    ``anthropic/claude-sonnet-4-5-20250929`` — the same model judged over the
    Anthropic API (LiteLLM's ``anthropic/`` provider), since the emulator has
    no real Bedrock. Non-Bedrock judge strings pass through unchanged.
    """

    def _sub(match: re.Match[str]) -> str:
        model = match.group(2)
        model = model.removeprefix("anthropic.")
        model = re.sub(r"-v\d+:\d+$", "", model)
        return f"{match.group(1)}anthropic/{model}{match.group(3)}"

    return _JUDGE_BEDROCK_RE.sub(_sub, judge_toml_body)
