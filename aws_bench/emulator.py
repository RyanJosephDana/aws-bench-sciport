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
    """STS-shaped placeholder credentials for :func:`build_aws_credentials_file`.

    Matches the ``sts.assume_role`` response keys that
    ``assumed_credentials_dict_to_credentials_env`` consumes.
    """
    return {
        "AccessKeyId": ACCESS_KEY_ID,
        "SecretAccessKey": SECRET_ACCESS_KEY,
        "SessionToken": SESSION_TOKEN,
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
