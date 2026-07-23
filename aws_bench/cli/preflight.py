"""Pre-execution checks that fail fast with clear messages.

Run before any non-trivial work (Docker build, AWS API calls). Each
check is a single round-trip (<1s) so operator sees configuration
errors immediately instead of after a 30s docker build.
"""

from __future__ import annotations

import shutil
import subprocess

from aws_bench.exceptions import AWSBenchError
from aws_bench.logging.logger import get_logger
from aws_bench.utils.credentials_provider import CredentialProvider

logger = get_logger(__name__)


class PreflightError(AWSBenchError):
    """A preflight check failed; the CLI should exit immediately."""


def preflight_docker_cli() -> None:
    """Verify the docker binary is on $PATH."""
    if shutil.which("docker") is None:
        raise PreflightError(
            "docker binary not found on PATH. Install Docker Desktop "
            "(macOS) or docker-engine (Linux) and ensure 'docker' is in "
            "your shell's PATH."
        )


def preflight_docker_daemon() -> None:
    """Verify the docker daemon is reachable.

    ``docker info`` exits 0 only when the daemon responds. On macOS
    with Docker Desktop stopped this exits 1 with 'Cannot connect to
    the Docker daemon'.
    """
    proc = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise PreflightError(
            f"docker daemon not reachable. {stderr}\n"
            "Ensure Docker Desktop is running (macOS) or the docker "
            "service is started (Linux)."
        )


def preflight_aws_credentials(
    cred_provider: CredentialProvider, *, ou_name: str | None = None
) -> str:
    """Verify AWS credentials and log the caller identity the command will use.

    Logs the account and caller ARN (and OU when given) so the operator sees,
    before any work, which account the command is acting on — the common cause
    of running ``init`` and ``setup`` against different accounts unawares.
    Returns the caller account id.
    """
    try:
        identity = cred_provider.session.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001 — boto3 surfaces a wide tree
        raise PreflightError(
            f"AWS credentials are missing or expired: {exc}\nRefresh credentials and re-run."
        ) from exc

    account_id = identity["Account"]
    suffix = f", OU '{ou_name}'" if ou_name else ""
    logger.info("Using AWS account %s (%s)%s", account_id, identity["Arn"], suffix)
    return account_id
