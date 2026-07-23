"""Secret-safe capture for the command ledger.

- ``record_env`` keeps only an exact allowlist of non-secret AWS resolution
  keys; every other environment variable (including any credential) is dropped.
- ``redact_argv`` / ``redact_config`` are name-driven: they redact the value of
  any flag or config field whose name matches a secret keyword. There is no
  value inspection, so a secret under a benign key (or a positional) is missed.

Best-effort, and covers only ``command.json`` — the sibling ``run.log`` (verbatim
DEBUG) and ``job-snapshot.tar.gz`` (raw job dir) are not redacted. The tree is
owner-only (``0o700``/``0o600``); treat ``~/.aws-bench/logs/`` as sensitive.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

REDACTED = "***"

# The exact env keys recorded in command.json. Everything else — ambient shell
# vars and any AWS credentials — is dropped; only these non-secret AWS
# resolution keys are kept verbatim.
_ENV_ALLOWLIST = frozenset(
    {
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    }
)

# Key-name fragments that mark a value as secret (matched case-insensitively).
_SECRET_KEY_PARTS = ("SECRET", "TOKEN", "PASSWORD", "KEY", "CREDENTIAL")


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _SECRET_KEY_PARTS)


def redact_argv(argv: list[str]) -> list[str]:
    """Redact the value of any secret-named flag; pass every other token through."""
    out: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        if token.startswith("--") and "=" in token:
            flag, _, _ = token.partition("=")
            if _is_secret_key(flag.lstrip("-").replace("-", "_")):
                out.append(f"{flag}={REDACTED}")
                continue
        if token.startswith("-") and _is_secret_key(token.lstrip("-").replace("-", "_")):
            out.append(token)
            redact_next = True
            continue
        out.append(token)
    return out


def record_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Record only the exact-allowlisted env keys; drop everything else."""
    return {key: environ[key] for key in _ENV_ALLOWLIST if key in environ}


def redact_config(config: dict | None) -> dict | None:
    """Recursively redact the value of any dict key matching the secret keywords."""
    if config is None:
        return None

    def scrub(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: (REDACTED if _is_secret_key(k) else scrub(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    return cast("dict", scrub(config))
